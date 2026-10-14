"""Create and update fake products for custom catalog load tests."""

from __future__ import annotations

import json
import random
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Iterator, Sequence

Product = dict[str, Any]

ID_PREFIX = "FEED-"
PRICE_QUANTUM = Decimal("0.01")
# DENY makes zero inventory suppress the feed-created variant from recommendations.
INVENTORY_POLICY_DENY = 1

# These paths mirror the categories in Varun's large custom-catalog test feed.
CATEGORY_PATHS = (
    ("Art Supplies", "Paint", "Brushes"),
    ("Automotive", "Tools", "Accessories"),
    ("Baby", "Safety", "Monitors"),
    ("Electronics", "Accessories", "Peripherals"),
    ("Electronics", "Audio", "Headphones"),
    ("Electronics", "Computers", "Laptops"),
    ("Electronics", "Gaming", "Controllers"),
    ("Electronics", "Wearables", "Watches"),
    ("Furniture", "Living Room", "Seating"),
    ("Furniture", "Office", "Desks"),
    ("Grocery", "Organic", "Snacks"),
    ("Health", "Fitness", "Wearables"),
    ("Home", "Appliances", "Portable"),
    ("Home", "Cleaning", "Vacuums"),
    ("Home", "Lighting", "Smart"),
    ("Kitchen", "Cookware", "Bakeware"),
    ("Kitchen", "Storage", "Containers"),
    ("Outdoor", "Camping", "Gear"),
    ("Sports", "Running", "Shoes"),
    ("Toys", "Educational", "STEM"),
)

BRANDS = (
    "BabyBliss",
    "BoldStrike",
    "FlexCore",
    "GreenChef",
    "HydroSync",
    "IronClad",
    "LumiDesk",
    "PureGlow",
    "SilkThread",
    "SwiftEdge",
    "TerraRoot",
    "TitanGrip",
    "TrailWay",
    "UrbanNest",
    "VoltEdge",
)

PRODUCT_TYPES = (
    "Blender",
    "Cable",
    "Charger",
    "Controller",
    "Dock",
    "Headphones",
    "Hub",
    "Lamp",
    "Laptop",
    "Monitor",
    "Scale",
    "Shoes",
    "Speaker",
    "Tablet",
    "Watch",
)

MODELS = (
    "Classic",
    "Compact",
    "Deluxe",
    "Eco",
    "Flex",
    "Lite",
    "Nano",
    "Premium",
    "Prime",
    "Pro",
    "Ultra",
)

DESCRIPTIONS = (
    "Advanced connectivity options support a reliable everyday experience.",
    "Built with premium materials for durable, long-lasting performance.",
    "Compact design fits seamlessly into a workspace or living area.",
    "Energy-efficient construction helps reduce power consumption.",
    "Ergonomic and lightweight for everyday use at home or in the office.",
    "Feature-packed design suits professionals and enthusiasts alike.",
    "High-performance construction combines modern technology with a sleek design.",
    "Smart controls automatically adjust settings for consistent results.",
    "Versatile design adapts to a wide range of everyday needs.",
    "Whisper-quiet operation supports a peaceful and productive environment.",
)


def load_feed(path: Path) -> list[Product]:
    """Load a feed and verify that its root is a JSON array."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")

    with path.open(encoding="utf-8") as feed_file:
        products = json.load(feed_file)
    if not isinstance(products, list):
        raise ValueError(f"{path} must contain a JSON array")
    return products


def write_feed(path: Path, products: Sequence[Product]) -> None:
    """Atomically replace a feed with consistently formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(products, temporary_file, indent=2)
            temporary_file.write("\n")
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _array_content_end(path: Path) -> tuple[int, bool]:
    """Find the final array value without loading or parsing the full feed."""
    first_non_whitespace: tuple[int, int] | None = None
    previous_non_whitespace: tuple[int, int] | None = None
    last_non_whitespace: tuple[int, int] | None = None
    position = 0

    with path.open("rb") as feed_file:
        while chunk := feed_file.read(64 * 1024):
            for offset, value in enumerate(chunk):
                if value not in b" \t\r\n":
                    entry = (position + offset, value)
                    if first_non_whitespace is None:
                        first_non_whitespace = entry
                    previous_non_whitespace = last_non_whitespace
                    last_non_whitespace = entry
            position += len(chunk)

    if first_non_whitespace is None:
        raise ValueError(f"{path} is empty")
    if first_non_whitespace[1] != ord("[") or last_non_whitespace is None:
        raise ValueError(f"{path} must contain a JSON array")
    if last_non_whitespace[1] != ord("]") or previous_non_whitespace is None:
        raise ValueError(f"{path} must contain a JSON array")

    content_end, content_byte = previous_non_whitespace
    return content_end, content_byte != ord("[")


def _write_indented_product(output_file: Any, product: Product) -> None:
    """Write one product with the feed's two-space array indentation."""
    serialized = json.dumps(product, indent=2)
    indented = "\n".join(f"  {line}" for line in serialized.splitlines())
    output_file.write(indented.encode("utf-8"))


def append_products(path: Path, products: Iterable[Product]) -> int:
    """Atomically append products while keeping memory use independent of feed size."""
    product_iterator = iter(products)
    first_product = next(product_iterator, None)
    if first_product is None:
        if not path.exists():
            write_feed(path, [])
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_feed = path.exists()
    content_end = -1
    has_existing_products = False
    if existing_feed:
        content_end, has_existing_products = _array_content_end(path)

    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            if existing_feed:
                with path.open("rb") as input_file:
                    bytes_remaining = content_end + 1
                    while bytes_remaining:
                        chunk = input_file.read(min(64 * 1024, bytes_remaining))
                        if not chunk:
                            raise ValueError(
                                f"could not read complete feed from {path}"
                            )
                        output_file.write(chunk)
                        bytes_remaining -= len(chunk)
            else:
                output_file.write(b"[")

            output_file.write(b",\n" if has_existing_products else b"\n")
            _write_indented_product(output_file, first_product)
            appended_count = 1
            for product in product_iterator:
                output_file.write(b",\n")
                _write_indented_product(output_file, product)
                appended_count += 1
            output_file.write(b"\n]\n")

        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return appended_count


def generate_products(count: int, *, seed: int = 4236) -> Iterator[Product]:
    """Yield products with UUID-based IDs that need no existing-feed scan."""
    if count < 0:
        raise ValueError("count must be non-negative")

    random_generator = random.Random(seed)

    for index in range(count):
        product_uuid = uuid.uuid4().hex
        product_id = f"{ID_PREFIX}{product_uuid}"
        brand = BRANDS[index % len(BRANDS)]
        product_type = PRODUCT_TYPES[random_generator.randrange(len(PRODUCT_TYPES))]
        model = MODELS[random_generator.randrange(len(MODELS))]
        category_path = CATEGORY_PATHS[index % len(CATEGORY_PATHS)]
        price = Decimal(random_generator.randrange(500, 250_001)) / 100

        yield {
            "id": product_id,
            "title": f"{brand} {model} {product_type}",
            "description": DESCRIPTIONS[random_generator.randrange(len(DESCRIPTIONS))],
            "link": f"https://example.com/products/{product_id}",
            "image_link": f"https://images.example.com/products/{product_id}.jpg",
            "radius": 0,
            "categories": list(category_path),
            "brand": brand,
            "price": f"{price:.2f}",
            "currency": "USD",
            "sku": f"{brand[:3].upper()}-{product_uuid}",
            "inventory_quantity": random_generator.randrange(0, 1_001),
            "inventory_policy": INVENTORY_POLICY_DENY,
        }


def _parse_amount(value: str) -> tuple[Decimal, bool]:
    """Parse a non-negative raw or percentage update amount."""
    percentage = value.endswith("%")
    numeric_value = value[:-1] if percentage else value
    try:
        amount = Decimal(numeric_value)
    except InvalidOperation as error:
        raise ValueError(f"invalid amount: {value}") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("amount must be a non-negative finite number")
    return amount, percentage


def _matches_categories(product: Product, categories: set[str]) -> bool:
    """Match a product when any requested category appears in its path."""
    if not categories:
        return True
    product_categories = {
        str(category).casefold() for category in product.get("categories", [])
    }
    return bool(product_categories & categories)


def _updated_price(current_value: object, amount: Decimal, percentage: bool) -> str:
    """Calculate a non-negative price rounded to catalog cents."""
    current = Decimal(str(current_value))
    reduction = current * amount / 100 if percentage else amount
    return f"{max(Decimal(0), current - reduction).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def _updated_inventory(current_value: object, amount: Decimal, percentage: bool) -> int:
    """Calculate non-negative integer inventory after a reduction."""
    current = Decimal(str(current_value))
    reduction = (
        (current * amount / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if percentage
        else amount
    )
    # Catalog inventory is integral; half-up avoids silently ignoring a 0.5-unit drop.
    return max(0, int(current - reduction))


def update_products(
    products: list[Product],
    *,
    mode: str,
    count: int,
    categories: Sequence[str] = (),
    drop_amount: str | None = None,
    target_amount: str | None = None,
) -> int:
    """Update the first matching products in feed order and return the count."""
    if mode not in {"price", "inventory"}:
        raise ValueError("mode must be 'price' or 'inventory'")
    if count < 0:
        raise ValueError("count must be non-negative")
    if (drop_amount is None) == (target_amount is None):
        raise ValueError("provide exactly one of drop_amount or target_amount")

    normalized_categories = {
        category.strip().casefold() for category in categories if category.strip()
    }
    amount, percentage = _parse_amount(
        drop_amount if drop_amount is not None else target_amount or ""
    )
    if target_amount is not None and percentage:
        raise ValueError("target amount must be a raw number")
    if mode == "inventory" and not percentage and amount != amount.to_integral_value():
        raise ValueError("raw inventory amounts must be whole numbers")

    field = "price" if mode == "price" else "inventory_quantity"
    updated_count = 0
    for product in products:
        if updated_count >= count:
            break
        if not _matches_categories(product, normalized_categories):
            continue
        if field not in product:
            raise ValueError(f"product {product.get('id', '<unknown>')} has no {field}")

        if target_amount is not None:
            product[field] = (
                f"{amount.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP):.2f}"
                if mode == "price"
                else int(amount)
            )
        elif mode == "price":
            product[field] = _updated_price(product[field], amount, percentage)
        else:
            product[field] = _updated_inventory(product[field], amount, percentage)
        updated_count += 1

    return updated_count
