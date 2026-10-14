#!/usr/bin/env python3
"""Create uniquely identified profiles for a Klaviyo load-test account."""

from __future__ import annotations

import argparse
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Callable
from typing import Any

import requests
from faker import Faker

API_URL = "https://a.klaviyo.com/api/profiles"
API_REVISION = "2026-07-15"
API_KEY_ENVIRONMENT_VARIABLE = "KLAVIYO_PRIVATE_API_KEY"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Use roughly half the 750/min steady limit to leave room for other API clients.
REQUEST_INTERVAL_SECONDS = 60 / 370

FAKE = Faker()


def build_parser() -> argparse.ArgumentParser:
    """Build the profile creation command-line interface."""
    parser = argparse.ArgumentParser(
        description="Create fake profiles in the account associated with the API key."
    )
    parser.add_argument("count", type=int, help="number of new profiles to create")
    return parser


def _email_component(name: str) -> str:
    """Convert a generated name into a readable email-safe component."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.casefold()).strip("_") or "profile"


def build_profile_payload(profile_uuid: str) -> dict[str, Any]:
    """Build one unique JSON:API profile payload."""
    first_name = FAKE.first_name()
    last_name = FAKE.last_name()
    external_id = f"fp-4236-load-test-{profile_uuid}"
    email_name = f"{_email_component(first_name)}_{_email_component(last_name)}"
    return {
        "data": {
            "type": "profile",
            "attributes": {
                "email": f"{email_name}_{profile_uuid}@example.com",
                "external_id": external_id,
                "first_name": first_name,
                "last_name": last_name,
                "properties": {
                    "load_test": True,
                    "load_test_ticket": "FP-4236",
                },
            },
        }
    }


def create_profile(
    session: requests.Session,
    *,
    api_key: str,
    payload: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Create one profile, honoring server-directed retries for rate limits."""
    headers = {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
        "revision": API_REVISION,
    }

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        response = session.post(
            API_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 429:
            response.raise_for_status()
            return str(response.json()["data"]["id"])
        if attempt == MAX_RATE_LIMIT_RETRIES:
            break

        retry_after = response.headers.get("Retry-After")
        try:
            retry_delay = float(retry_after or DEFAULT_RETRY_AFTER_SECONDS)
        except ValueError:
            retry_delay = DEFAULT_RETRY_AFTER_SECONDS
        sleep(max(0, retry_delay))

    raise RuntimeError("Profiles API rate limit retries exhausted")


def create_profiles(
    count: int,
    *,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Create the requested number of unique profiles at a rate-safe pace."""
    if count < 0:
        raise ValueError("count must be non-negative")

    owned_session = session is None
    request_session = session or requests.Session()
    try:
        for index in range(count):
            payload = build_profile_payload(uuid.uuid4().hex)
            create_profile(
                request_session,
                api_key=api_key,
                payload=payload,
                sleep=sleep,
            )
            if index < count - 1:
                sleep(REQUEST_INTERVAL_SECONDS)
    finally:
        if owned_session:
            request_session.close()

    return count


def main() -> int:
    """Read configuration, create profiles, and report the completed count."""
    parser = build_parser()
    arguments = parser.parse_args()
    api_key = os.environ.get(API_KEY_ENVIRONMENT_VARIABLE)
    if not api_key:
        parser.error(f"set {API_KEY_ENVIRONMENT_VARIABLE} to the account's API key")

    try:
        created_count = create_profiles(arguments.count, api_key=api_key)
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as error:
        print(f"Profile creation failed: {error}")
        return 1

    print(f"Created {created_count} profiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
