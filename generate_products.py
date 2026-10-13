#!/usr/bin/env python3
"""Append generated products to a custom catalog JSON feed."""

from __future__ import annotations

import argparse
from pathlib import Path

from catalog_tools import append_products, generate_products

DEFAULT_FEED = Path("feed.json")


def build_parser() -> argparse.ArgumentParser:
    """Build the product-generation command-line interface."""
    parser = argparse.ArgumentParser(
        description="Append uniquely identified fake products to a catalog feed."
    )
    parser.add_argument(
        "--count",
        type=int,
        required=True,
        help="number of products to append",
    )
    parser.add_argument(
        "--feed",
        type=Path,
        default=DEFAULT_FEED,
        help=f"feed to create or extend (default: {DEFAULT_FEED})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=4236,
        help="random seed used for repeatable product data (default: 4236)",
    )
    return parser


def main() -> None:
    """Append requested products without loading the existing feed."""
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        appended_count = append_products(
            arguments.feed,
            generate_products(arguments.count, seed=arguments.seed),
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Added {appended_count} products to {arguments.feed}.")


if __name__ == "__main__":
    main()
