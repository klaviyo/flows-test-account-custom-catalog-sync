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
MAX_TIMEOUT_RETRIES = 3
DEFAULT_RETRY_AFTER_SECONDS = 1.0
TIMEOUT_RETRY_BASE_SECONDS = 1.0
PROGRESS_INTERVAL = 25

# Use roughly half the 750/min steady limit to leave room for other API clients.
REQUEST_INTERVAL_SECONDS = 60 / 370

FAKE = Faker()


class ProfileBatchError(RuntimeError):
    """Report confirmed progress when a profile batch stops early."""

    def __init__(self, confirmed_count: int, requested_count: int, cause: Exception):
        self.confirmed_count = confirmed_count
        self.requested_count = requested_count
        self.cause = cause
        super().__init__(
            f"stopped after {confirmed_count} of {requested_count} profiles "
            f"were confirmed: {cause}"
        )


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
    """Create one profile, retrying rate limits and transient timeouts."""
    headers = {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
        "revision": API_REVISION,
    }

    rate_limit_retries = 0
    timeout_retries = 0
    request_timed_out = False
    while True:
        try:
            response = session.post(
                API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout:
            request_timed_out = True
            if timeout_retries == MAX_TIMEOUT_RETRIES:
                raise
            # Backoff gives a slow API time to recover without increasing request load.
            sleep(TIMEOUT_RETRY_BASE_SECONDS * (2**timeout_retries))
            timeout_retries += 1
            continue

        if response.status_code == 409 and request_timed_out:
            # The UUID identifiers only exist in this payload, so a conflict confirms
            # that an earlier timed-out attempt reached the API.
            return str(payload["data"]["attributes"]["external_id"])
        if response.status_code != 429:
            response.raise_for_status()
            return str(response.json()["data"]["id"])
        if rate_limit_retries == MAX_RATE_LIMIT_RETRIES:
            raise RuntimeError("Profiles API rate limit retries exhausted")

        retry_after = response.headers.get("Retry-After")
        try:
            retry_delay = float(retry_after or DEFAULT_RETRY_AFTER_SECONDS)
        except ValueError:
            retry_delay = DEFAULT_RETRY_AFTER_SECONDS
        sleep(max(0, retry_delay))
        rate_limit_retries += 1


def create_profiles(
    count: int,
    *,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Create profiles at a safe pace and report confirmed batch progress."""
    if count < 0:
        raise ValueError("count must be non-negative")

    owned_session = session is None
    request_session = session or requests.Session()
    try:
        for index in range(count):
            payload = build_profile_payload(uuid.uuid4().hex)
            try:
                create_profile(
                    request_session,
                    api_key=api_key,
                    payload=payload,
                    sleep=sleep,
                )
            except (requests.RequestException, RuntimeError, KeyError) as error:
                raise ProfileBatchError(index, count, error) from error

            confirmed_count = index + 1
            if progress and (
                confirmed_count % PROGRESS_INTERVAL == 0 or confirmed_count == count
            ):
                progress(confirmed_count, count)
            if confirmed_count < count:
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
        created_count = create_profiles(
            arguments.count,
            api_key=api_key,
            progress=lambda confirmed, requested: print(
                f"Confirmed {confirmed}/{requested} profiles."
            ),
        )
    except ProfileBatchError as error:
        print(f"Profile creation failed: {error}")
        if isinstance(error.cause, requests.Timeout):
            print("The final timed-out request may still have created a profile.")
        return 1
    except ValueError as error:
        print(f"Profile creation failed: {error}")
        return 1

    print(f"Created {created_count} profiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
