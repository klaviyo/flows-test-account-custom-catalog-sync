#!/usr/bin/env python3
"""Download a custom catalog feed and report invalid product rows."""

from __future__ import annotations

import getpass
from typing import Any
from urllib.parse import urlparse

import requests

# Runbook usage:
# 1. Open Terminal (CMD+Space, then search for "Terminal").
# 2. Run `cd ~/Desktop`, or change to the directory containing this script.
# 3. Install dependencies with `python3 -m pip install -r requirements.txt`.
# 4. Have the feed URL and its username/password ready, if authentication applies.
# 5. Run `python3 custom_catalog_checker.py`.
# 6. Paste the feed URL and answer the authentication prompts.
# 7. Review the good, bad, and total product counts printed when the check finishes.

REQUIRED_FIELDS = frozenset({"id", "title", "link", "image_link", "description"})
REQUEST_TIMEOUT_SECONDS = 30


def fetch_feed(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> Any:
    """Fetch and decode a JSON feed, using basic authentication when supplied."""
    auth = (
        (username, password) if username is not None and password is not None else None
    )
    if auth is not None and urlparse(url).scheme.casefold() != "https":
        raise ValueError("authenticated feed URLs must use HTTPS")
    response = requests.get(url, auth=auth, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def validate_feed(feed: Any) -> tuple[int, int, list[str]]:
    """Count valid rows and describe rows missing required catalog fields."""
    if not isinstance(feed, list):
        raise ValueError("feed must be a JSON array of product objects")

    good_count = 0
    errors: list[str] = []
    for row_number, row in enumerate(feed, start=1):
        if not isinstance(row, dict):
            errors.append(f"Row {row_number}: expected a product object")
            continue

        missing_fields = sorted(REQUIRED_FIELDS - row.keys())
        if missing_fields:
            errors.append(
                f"Row {row_number}: missing required fields "
                f"{', '.join(missing_fields)}"
            )
            continue
        good_count += 1

    return good_count, len(errors), errors


def _authentication_required() -> bool:
    """Prompt until the user answers the authentication question with y or n."""
    while True:
        answer = input("Does this require authentication? y/n: ").strip().casefold()
        if answer in {"y", "n"}:
            return answer == "y"
        print("Please enter y or n.")


def main() -> int:
    """Run the interactive checker and return a shell-friendly status code."""
    url = input("URL: ").strip()
    username = None
    password = None
    if _authentication_required():
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")

    try:
        feed = fetch_feed(url, username=username, password=password)
        good_count, bad_count, errors = validate_feed(feed)
    except (requests.RequestException, ValueError) as error:
        print(f"Feed check failed: {error}")
        return 1

    for error in errors:
        print(error)
    print(f"Good rows: {good_count}")
    print(f"Bad rows: {bad_count}")
    print(f"Total products: {good_count + bad_count}")
    return 1 if bad_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
