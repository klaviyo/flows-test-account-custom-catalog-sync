#!/usr/bin/env python3
"""Create item-level catalog engagement events for load-test profiles.

This script does not create variant-level events. That is sufficient for Price
Drop flow load tests because Price Drop supports only item-level products.
Variant-level Low Inventory testing will require additional implementation.

Catalog lookup assumes each item and its single generated variant have the same
external ID. Verify that assumption before using another company's catalog;
feeds with independently identified variants need explicit parent-item lookup.

Requested event counts take priority over resource uniqueness. Profiles and
products cycle when needed, so actual overlap can exceed `--overlap-count`.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

API_BASE_URL = "https://a.klaviyo.com/api"
API_REVISION = "2026-10-15"
API_KEY_ENVIRONMENT_VARIABLE = "KLAVIYO_PRIVATE_API_KEY"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 1.0
RETRY_BASE_SECONDS = 1.0

PROFILE_PAGE_SIZE = 100
CATALOG_PAGE_SIZE = 100
MAX_EVENTS_PER_JOB = 1_000
MAX_PAYLOAD_BYTES = 5_000_000

# Use half the Events API's 150/min steady limit so load tests leave headroom.
SUBMISSION_INTERVAL_SECONDS = 60 / 75

METRIC_NAMES = {
    "viewed-product": "Viewed Product",
    "started-checkout": "Started Checkout",
    "added-to-cart": "Added to Cart",
    "ordered-product": "Ordered Product",
}
ORDERED_PRODUCT = "Ordered Product"


@dataclass(frozen=True)
class MetricCount:
    """Pair a supported Klaviyo metric with its requested event count."""

    name: str
    count: int


@dataclass(frozen=True)
class CatalogProduct:
    """Represent an item-level catalog product and its current price."""

    item_id: str
    price: float


@dataclass(frozen=True)
class EventAssignment:
    """Bind one metric and product to an existing load-test profile."""

    profile_id: str
    metric_name: str
    product: CatalogProduct
    unique_id: str
    event_time: str | None = None


class EventBatchError(RuntimeError):
    """Report accepted progress when submission stops before all events."""

    def __init__(
        self,
        accepted_events: int,
        total_events: int,
        accepted_jobs: int,
        cause: Exception,
    ):
        self.accepted_events = accepted_events
        self.total_events = total_events
        self.accepted_jobs = accepted_jobs
        self.cause = cause
        super().__init__(
            f"stopped after {accepted_events}/{total_events} events in "
            f"{accepted_jobs} jobs were accepted: {cause}"
        )


def parse_metric_count(value: str) -> MetricCount:
    """Parse a CLI metric slug and positive count from `metric=count`."""
    try:
        slug, raw_count = value.rsplit("=", 1)
        metric_name = METRIC_NAMES[slug.casefold()]
        count = int(raw_count)
    except (KeyError, ValueError) as error:
        supported = ", ".join(METRIC_NAMES)
        raise argparse.ArgumentTypeError(
            f"use METRIC=COUNT with one of: {supported}"
        ) from error

    if count <= 0:
        raise argparse.ArgumentTypeError("metric count must be positive")
    return MetricCount(metric_name, count)


def positive_int(value: str) -> int:
    """Parse a strictly positive integer CLI value."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the bulk event generator command-line interface."""
    parser = argparse.ArgumentParser(
        description="Create item-level catalog events for load-test profiles.",
        epilog=(
            "This script does not create variant-level events. Price Drop flows "
            "only support item-level products; variant-level Low Inventory load "
            "testing requires additional work. Catalog lookup assumes each item "
            "and its generated variant share an external ID; verify this before "
            "using another company's catalog."
        ),
    )
    parser.add_argument(
        "--metric",
        action="append",
        required=True,
        type=parse_metric_count,
        dest="metrics",
        metavar="METRIC=COUNT",
        help=(
            "event count for viewed-product, started-checkout, added-to-cart, "
            "or ordered-product; repeat for multiple metrics"
        ),
    )
    parser.add_argument(
        "--overlap-count",
        type=int,
        default=0,
        help=(
            "number of Ordered Product events that intentionally reuse an "
            "inclusion profile and product; resource cycling can create more"
        ),
    )
    parser.add_argument(
        "--days",
        type=positive_int,
        help=(
            "spread event timestamps evenly across the preceding number of days; "
            "omit to let Klaviyo assign ingestion time"
        ),
    )
    parser.add_argument(
        "--profiles",
        type=positive_int,
        help=(
            "use at most the first number of eligible load-test profiles, then cycle"
        ),
    )
    parser.add_argument(
        "--products",
        type=positive_int,
        help="use at most the first number of priced catalog products, then cycle",
    )
    return parser


def api_headers(api_key: str) -> dict[str, str]:
    """Build the common Klaviyo API request headers."""
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/vnd.api+json",
        "content-type": "application/vnd.api+json",
        "revision": API_REVISION,
    }


def _retry_delay(response: requests.Response, retry_index: int) -> float:
    """Choose a server-directed delay or exponential fallback."""
    retry_after = response.headers.get("Retry-After")
    try:
        return max(0, float(retry_after or ""))
    except ValueError:
        return RETRY_BASE_SECONDS * (2**retry_index)


def get_json(
    session: requests.Session,
    url: str,
    *,
    api_key: str,
    params: dict[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch one API page, retrying rate limits and transient failures."""
    for retry_index in range(MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                headers=api_headers(api_key),
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError):
            if retry_index == MAX_RETRIES:
                raise
            sleep(RETRY_BASE_SECONDS * (2**retry_index))
            continue

        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
            return dict(response.json())
        if retry_index == MAX_RETRIES:
            response.raise_for_status()
        sleep(_retry_delay(response, retry_index))

    raise RuntimeError("unreachable API retry state")


def fetch_load_test_profiles(
    session: requests.Session,
    *,
    api_key: str,
    needed: int,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Read profiles until enough `load_test=true` profile IDs are found."""
    if needed <= 0:
        return []

    profiles: list[str] = []
    url: str | None = f"{API_BASE_URL}/profiles"
    params: dict[str, Any] | None = {
        "fields[profile]": "properties",
        "page[size]": PROFILE_PAGE_SIZE,
    }
    while url and len(profiles) < needed:
        document = get_json(
            session,
            url,
            api_key=api_key,
            params=params,
            sleep=sleep,
        )
        for resource in document.get("data", []):
            properties = resource.get("attributes", {}).get("properties") or {}
            if properties.get("load_test") is True:
                profiles.append(str(resource["id"]))
                if len(profiles) == needed:
                    break
        url = document.get("links", {}).get("next")
        params = None
    return profiles


def fetch_catalog_products(
    session: requests.Session,
    *,
    api_key: str,
    needed: int,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CatalogProduct]:
    """Read variants as item-level products under the shared-external-ID contract."""
    if needed <= 0:
        return []

    products: list[CatalogProduct] = []
    seen_item_ids: set[str] = set()
    url: str | None = f"{API_BASE_URL}/catalog-variants"
    params: dict[str, Any] | None = {
        "page[size]": CATALOG_PAGE_SIZE,
    }
    while url and len(products) < needed:
        document = get_json(
            session,
            url,
            api_key=api_key,
            params=params,
            sleep=sleep,
        )
        for resource in document.get("data", []):
            attributes = resource.get("attributes", {})
            item_id = attributes.get("external_id")
            price = attributes.get("price")
            if not item_id or price is None:
                continue
            item_id = str(item_id)
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            products.append(CatalogProduct(item_id=item_id, price=float(price)))
            if len(products) == needed:
                break
        url = document.get("links", {}).get("next")
        params = None
    return products


def combine_metric_counts(metric_counts: Sequence[MetricCount]) -> list[MetricCount]:
    """Combine repeated metric arguments while preserving their first order."""
    combined: OrderedDict[str, int] = OrderedDict()
    for metric_count in metric_counts:
        combined[metric_count.name] = (
            combined.get(metric_count.name, 0) + metric_count.count
        )
    return [MetricCount(name, count) for name, count in combined.items()]


def validate_request(metric_counts: Sequence[MetricCount], overlap_count: int) -> None:
    """Reject overlap guidance that cannot fit within exact metric counts."""
    if overlap_count < 0:
        raise ValueError("overlap count must be non-negative")

    inclusion_count = sum(
        metric.count for metric in metric_counts if metric.name != ORDERED_PRODUCT
    )
    ordered_count = sum(
        metric.count for metric in metric_counts if metric.name == ORDERED_PRODUCT
    )
    if overlap_count > inclusion_count:
        raise ValueError("overlap count exceeds the number of inclusion events")
    if overlap_count > ordered_count:
        raise ValueError("overlap count exceeds the number of Ordered Product events")


def allocate_events(
    metric_counts: Sequence[MetricCount],
    *,
    overlap_count: int,
    profile_ids: Sequence[str],
    products: Sequence[CatalogProduct],
    unique_id: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> list[EventAssignment]:
    """Prioritize exact counts while cycling resources and guiding overlap."""
    validate_request(metric_counts, overlap_count)
    if not profile_ids:
        raise ValueError("no profiles with load_test=true were found")
    if not products:
        raise ValueError("no catalog products with prices were found")

    assignments: list[EventAssignment] = []
    inclusions: list[EventAssignment] = []
    resource_index = 0

    def new_assignment(metric_name: str) -> EventAssignment:
        nonlocal resource_index
        assignment = EventAssignment(
            profile_id=profile_ids[resource_index % len(profile_ids)],
            metric_name=metric_name,
            product=products[resource_index % len(products)],
            unique_id=unique_id(),
        )
        resource_index += 1
        return assignment

    for metric_count in metric_counts:
        if metric_count.name == ORDERED_PRODUCT:
            continue
        for _ in range(metric_count.count):
            assignment = new_assignment(metric_count.name)
            assignments.append(assignment)
            inclusions.append(assignment)

    ordered_count = sum(
        metric.count for metric in metric_counts if metric.name == ORDERED_PRODUCT
    )
    for inclusion in inclusions[:overlap_count]:
        assignments.append(
            EventAssignment(
                profile_id=inclusion.profile_id,
                metric_name=ORDERED_PRODUCT,
                product=inclusion.product,
                unique_id=unique_id(),
            )
        )
    for _ in range(ordered_count - overlap_count):
        assignments.append(new_assignment(ORDERED_PRODUCT))

    return assignments


def spread_event_times(
    assignments: Sequence[EventAssignment],
    days: int | None,
    *,
    now: datetime | None = None,
) -> list[EventAssignment]:
    """Optionally distribute event timestamps uniformly over a historical window."""
    if days is None:
        return list(assignments)
    if days <= 0:
        raise ValueError("days must be positive")
    if not assignments:
        return []

    window_end = now or datetime.now(timezone.utc)
    if window_end.tzinfo is None:
        raise ValueError("now must include a timezone")
    window_start = window_end - timedelta(days=days)
    interval = timedelta(days=days) / len(assignments)

    return [
        replace(
            assignment,
            event_time=(window_start + interval * (index + 0.5))
            .isoformat()
            .replace("+00:00", "Z"),
        )
        for index, assignment in enumerate(assignments)
    ]


def build_event_properties(assignment: EventAssignment) -> dict[str, Any]:
    """Build metric-specific item paths matching Jomashop's CCP extractors."""
    product = assignment.product
    if assignment.metric_name == "Started Checkout":
        return {
            "Items": [
                {
                    "ProductID": product.item_id,
                    "ItemPrice": product.price,
                }
            ]
        }
    return {
        "ProductID": product.item_id,
        "Price": product.price,
    }


def build_bulk_payload(assignments: Sequence[EventAssignment]) -> dict[str, Any]:
    """Group item-level event resources by profile for one bulk API job."""
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for assignment in assignments:
        event = {
            "type": "event",
            "attributes": {
                "properties": build_event_properties(assignment),
                "value": assignment.product.price,
                "value_currency": "USD",
                "metric": {
                    "data": {
                        "type": "metric",
                        "attributes": {"name": assignment.metric_name},
                    }
                },
                "unique_id": assignment.unique_id,
            },
        }
        if assignment.event_time is not None:
            event["attributes"]["time"] = assignment.event_time
        grouped.setdefault(assignment.profile_id, []).append(event)

    bulk_entries = [
        {
            "type": "event-bulk-create",
            "attributes": {
                "profile": {
                    "data": {
                        "type": "profile",
                        "id": profile_id,
                        "attributes": {},
                    }
                },
                "events": {"data": events},
            },
        }
        for profile_id, events in grouped.items()
    ]
    return {
        "data": {
            "type": "event-bulk-create-job",
            "attributes": {
                "events-bulk-create": {
                    "data": bulk_entries,
                }
            },
        }
    }


def iter_job_batches(
    assignments: Sequence[EventAssignment],
) -> Iterator[list[EventAssignment]]:
    """Split assignments under both API event-count and payload-size limits."""

    def split_for_size(batch: list[EventAssignment]) -> Iterator[list[EventAssignment]]:
        payload_size = len(json.dumps(build_bulk_payload(batch)).encode())
        if payload_size <= MAX_PAYLOAD_BYTES:
            yield batch
            return
        if len(batch) == 1:
            raise ValueError("one event exceeds the Bulk Events API 5 MB limit")
        midpoint = len(batch) // 2
        yield from split_for_size(batch[:midpoint])
        yield from split_for_size(batch[midpoint:])

    for start in range(0, len(assignments), MAX_EVENTS_PER_JOB):
        batch = list(assignments[start : start + MAX_EVENTS_PER_JOB])
        yield from split_for_size(batch)


def submit_bulk_job(
    session: requests.Session,
    *,
    api_key: str,
    payload: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Submit one bulk job, retrying transient failures without duplicating events."""
    url = f"{API_BASE_URL}/event-bulk-create-jobs"
    for retry_index in range(MAX_RETRIES + 1):
        try:
            response = session.post(
                url,
                headers=api_headers(api_key),
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except (requests.Timeout, requests.ConnectionError):
            if retry_index == MAX_RETRIES:
                raise
            sleep(RETRY_BASE_SECONDS * (2**retry_index))
            continue

        if response.status_code == 202:
            return
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
        elif retry_index == MAX_RETRIES:
            response.raise_for_status()
        else:
            sleep(_retry_delay(response, retry_index))
            continue

    raise RuntimeError("unreachable bulk event retry state")


def submit_events(
    assignments: Sequence[EventAssignment],
    *,
    api_key: str,
    session: requests.Session,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int, int], None] | None = None,
) -> int:
    """Submit all event assignments and report accepted job progress."""
    batches = list(iter_job_batches(assignments))
    accepted_events = 0
    for job_index, batch in enumerate(batches, start=1):
        try:
            submit_bulk_job(
                session,
                api_key=api_key,
                payload=build_bulk_payload(batch),
                sleep=sleep,
            )
        except (requests.RequestException, RuntimeError, ValueError) as error:
            raise EventBatchError(
                accepted_events,
                len(assignments),
                job_index - 1,
                error,
            ) from error

        accepted_events += len(batch)
        if progress:
            progress(accepted_events, len(assignments), job_index)
        if job_index < len(batches):
            sleep(SUBMISSION_INTERVAL_SECONDS)
    return accepted_events


def create_events(
    metric_counts: Sequence[MetricCount],
    *,
    overlap_count: int,
    days: int | None = None,
    profile_count: int | None = None,
    product_count: int | None = None,
    api_key: str,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int, int], None] | None = None,
    now: datetime | None = None,
) -> int:
    """Discover resources, allocate events, and submit all bulk jobs."""
    combined_counts = combine_metric_counts(metric_counts)
    validate_request(combined_counts, overlap_count)
    for resource_name, resource_count in (
        ("profile count", profile_count),
        ("product count", product_count),
    ):
        if resource_count is not None and resource_count <= 0:
            raise ValueError(f"{resource_name} must be positive")
    total_events = sum(metric.count for metric in combined_counts)
    distinct_resources_needed = total_events - overlap_count
    profiles_needed = min(
        distinct_resources_needed,
        profile_count if profile_count is not None else distinct_resources_needed,
    )
    products_needed = min(
        distinct_resources_needed,
        product_count if product_count is not None else distinct_resources_needed,
    )

    owned_session = session is None
    request_session = session or requests.Session()
    try:
        profile_ids = fetch_load_test_profiles(
            request_session,
            api_key=api_key,
            needed=profiles_needed,
            sleep=sleep,
        )
        products = fetch_catalog_products(
            request_session,
            api_key=api_key,
            needed=products_needed,
            sleep=sleep,
        )
        assignments = allocate_events(
            combined_counts,
            overlap_count=overlap_count,
            profile_ids=profile_ids,
            products=products,
        )
        assignments = spread_event_times(assignments, days, now=now)
        return submit_events(
            assignments,
            api_key=api_key,
            session=request_session,
            sleep=sleep,
            progress=progress,
        )
    finally:
        if owned_session:
            request_session.close()


def main() -> int:
    """Parse configuration, create events, and report accepted progress."""
    parser = build_parser()
    arguments = parser.parse_args()
    api_key = os.environ.get(API_KEY_ENVIRONMENT_VARIABLE)
    if not api_key:
        parser.error(f"set {API_KEY_ENVIRONMENT_VARIABLE} to the account's API key")

    try:
        accepted_count = create_events(
            arguments.metrics,
            overlap_count=arguments.overlap_count,
            days=arguments.days,
            profile_count=arguments.profiles,
            product_count=arguments.products,
            api_key=api_key,
            progress=lambda accepted, total, jobs: print(
                f"Accepted {accepted}/{total} events across {jobs} jobs."
            ),
        )
    except (
        EventBatchError,
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
    ) as error:
        print(f"Event creation failed: {error}")
        return 1

    print(f"Accepted all {accepted_count} events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
