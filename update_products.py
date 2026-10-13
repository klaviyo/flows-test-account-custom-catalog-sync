#!/usr/bin/env python3
"""Update prices or inventory for products in a custom catalog feed."""

from __future__ import annotations

import argparse
from pathlib import Path

from catalog_tools import load_feed, update_products, write_feed

DEFAULT_FEED = Path("feed.json")


def build_parser() -> argparse.ArgumentParser:
    """Build the product-update command-line interface."""
    parser = argparse.ArgumentParser(
        description="Update the first matching products in feed order."
    )
    parser.add_argument("mode", choices=("price", "inventory"))
    parser.add_argument(
        "--count",
        type=int,
        required=True,
        help="maximum number of products to update",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=DEFAULT_FEED,
        help=f"feed to update (default: {DEFAULT_FEED})",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=(),
        metavar="CATEGORY",
        help="update products matching any listed category, case-insensitively",
    )
    amount_group = parser.add_mutually_exclusive_group(required=True)
    amount_group.add_argument(
        "--drop-amount",
        help="amount to subtract, such as 10, 2.50, or 15%%",
    )
    amount_group.add_argument(
        "--target-amount",
        help="raw value to assign to price or inventory",
    )
    return parser


def main() -> None:
    """Apply the requested updates and persist the feed atomically."""
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        products = load_feed(arguments.feed)
        updated_count = update_products(
            products,
            mode=arguments.mode,
            count=arguments.count,
            categories=arguments.categories,
            drop_amount=arguments.drop_amount,
            target_amount=arguments.target_amount,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    write_feed(arguments.feed, products)
    print(f"Updated {updated_count} products in {arguments.feed}.")


if __name__ == "__main__":
    main()
