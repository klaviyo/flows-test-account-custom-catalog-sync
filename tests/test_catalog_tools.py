"""Tests for catalog product generation and updates."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from catalog_tools import (
    CATEGORY_PATHS,
    append_products,
    generate_products,
    load_feed,
    update_products,
    write_feed,
)
from update_products import build_parser


class FeedFileTests(unittest.TestCase):
    """Verify feed persistence and root validation."""

    def test_written_feed_round_trips(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "feed.json"

            write_feed(feed_path, [{"id": "one"}])

            self.assertEqual(load_feed(feed_path), [{"id": "one"}])

    def test_missing_feed_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "missing.json"

            with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
                load_feed(feed_path)

    def test_failed_write_removes_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "feed.json"

            with self.assertRaises(TypeError):
                write_feed(feed_path, [{"invalid": object()}])

            self.assertEqual(list(Path(directory).glob(".feed.json.*.tmp")), [])
            self.assertFalse(feed_path.exists())

    def test_rejects_non_array_feed(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "feed.json"
            feed_path.write_text('{"id": "one"}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON array"):
                load_feed(feed_path)

    def test_appends_products_to_existing_feed(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "feed.json"
            write_feed(feed_path, [{"id": "existing"}])

            appended_count = append_products(
                feed_path,
                ({"id": f"new-{index}"} for index in range(2)),
            )

            self.assertEqual(appended_count, 2)
            self.assertEqual(
                load_feed(feed_path),
                [{"id": "existing"}, {"id": "new-0"}, {"id": "new-1"}],
            )

    def test_appends_products_to_new_feed(self) -> None:
        with TemporaryDirectory() as directory:
            feed_path = Path(directory) / "feed.json"

            appended_count = append_products(feed_path, iter([{"id": "new"}]))

            self.assertEqual(appended_count, 1)
            self.assertEqual(load_feed(feed_path), [{"id": "new"}])


class GenerateProductsTests(unittest.TestCase):
    """Verify generated products are valid and safe to append."""

    def test_generates_varun_feed_shape_with_unique_ids(self) -> None:
        products = list(generate_products(25))

        self.assertEqual(len(products), 25)
        self.assertEqual(len({product["id"] for product in products}), 25)
        self.assertRegex(products[0]["id"], r"^FEED-[0-9a-f]{32}$")
        self.assertRegex(products[0]["sku"], r"^[A-Z]{3}-[0-9a-f]{32}$")
        self.assertTrue(products[0]["link"].startswith("https://"))
        self.assertTrue(products[0]["image_link"].startswith("https://"))
        self.assertIn(tuple(products[0]["categories"]), CATEGORY_PATHS)
        self.assertEqual(
            set(products[0]),
            {
                "id",
                "title",
                "description",
                "link",
                "image_link",
                "radius",
                "categories",
                "brand",
                "price",
                "currency",
                "sku",
                "inventory_quantity",
                "inventory_policy",
            },
        )
        self.assertEqual(products[0]["inventory_policy"], 1)

    def test_separate_batches_do_not_reuse_ids(self) -> None:
        first_ids = {product["id"] for product in generate_products(10)}
        second_ids = {product["id"] for product in generate_products(10)}

        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_rejects_negative_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            list(generate_products(-1))


class UpdateProductsTests(unittest.TestCase):
    """Verify feed-order selection and update amount behavior."""

    def setUp(self) -> None:
        self.products = [
            {
                "id": "one",
                "categories": ["Electronics", "Audio", "Headphones"],
                "price": "100.00",
                "inventory_quantity": 5,
                "inventory_policy": 1,
            },
            {
                "id": "two",
                "categories": ["Home", "Lighting", "Smart"],
                "price": "50.00",
                "inventory_quantity": 20,
                "inventory_policy": 1,
            },
            {
                "id": "three",
                "categories": ["Electronics", "Gaming", "Controllers"],
                "price": "25.00",
                "inventory_quantity": 100,
                "inventory_policy": 1,
            },
        ]

    def test_updates_first_products_when_categories_are_omitted(self) -> None:
        updated = update_products(
            self.products,
            mode="price",
            count=2,
            drop_amount="10",
        )

        self.assertEqual(updated, 2)
        self.assertEqual(
            [product["price"] for product in self.products],
            ["90.00", "40.00", "25.00"],
        )

    def test_updates_first_products_matching_any_category(self) -> None:
        updated = update_products(
            self.products,
            mode="inventory",
            count=1,
            categories=["smart", "electronics"],
            target_amount="7",
        )

        self.assertEqual(updated, 1)
        self.assertEqual(
            [product["inventory_quantity"] for product in self.products],
            [7, 20, 100],
        )

    def test_applies_percentage_price_drop_and_clamps_at_zero(self) -> None:
        update_products(
            self.products,
            mode="price",
            count=1,
            drop_amount="12.5%",
        )
        update_products(
            self.products,
            mode="price",
            count=1,
            drop_amount="1000",
        )

        self.assertEqual(self.products[0]["price"], "0.00")

    def test_rounds_percentage_inventory_drop_to_nearest_unit(self) -> None:
        update_products(
            self.products,
            mode="inventory",
            count=1,
            drop_amount="10%",
        )

        self.assertEqual(self.products[0]["inventory_quantity"], 4)

    def test_formats_price_target_and_accepts_inventory_target(self) -> None:
        update_products(
            self.products,
            mode="price",
            count=1,
            target_amount="12.345",
        )
        update_products(
            self.products,
            mode="inventory",
            count=1,
            target_amount="3",
        )

        self.assertEqual(self.products[0]["price"], "12.35")
        self.assertEqual(self.products[0]["inventory_quantity"], 3)

    def test_requires_exactly_one_amount(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            update_products(self.products, mode="price", count=1)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            update_products(
                self.products,
                mode="price",
                count=1,
                drop_amount="1",
                target_amount="2",
            )

    def test_rejects_invalid_inventory_and_target_amounts(self) -> None:
        with self.assertRaisesRegex(ValueError, "whole numbers"):
            update_products(
                self.products,
                mode="inventory",
                count=1,
                drop_amount="1.5",
            )
        with self.assertRaisesRegex(ValueError, "raw number"):
            update_products(
                self.products,
                mode="price",
                count=1,
                target_amount="10%",
            )

    def test_cli_makes_drop_and_target_mutually_exclusive(self) -> None:
        parser = build_parser()

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "price",
                        "--count",
                        "1",
                        "--drop-amount",
                        "5",
                        "--target-amount",
                        "10",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
