"""Tests for creating item-level catalog engagement events."""

from __future__ import annotations

import argparse
import unittest
from unittest.mock import Mock, call, patch

import requests

import create_events


def product(
    item_id: str = "product-1", price: float = 19.99
) -> create_events.CatalogProduct:
    """Build a concise catalog product fixture."""
    return create_events.CatalogProduct(item_id=item_id, price=price)


def assignment(
    profile_id: str = "profile-1",
    metric_name: str = "Viewed Product",
    item_id: str = "product-1",
    unique_id: str = "event-1",
) -> create_events.EventAssignment:
    """Build a concise event assignment fixture."""
    return create_events.EventAssignment(
        profile_id=profile_id,
        metric_name=metric_name,
        product=product(item_id),
        unique_id=unique_id,
    )


class ParserTests(unittest.TestCase):
    """Verify metric/count parsing and CLI defaults."""

    def test_parses_supported_metric_count(self) -> None:
        self.assertEqual(
            create_events.parse_metric_count("viewed-product=500"),
            create_events.MetricCount("Viewed Product", 500),
        )

    def test_rejects_unknown_metric_and_non_positive_count(self) -> None:
        for value in ("unknown=1", "viewed-product=zero", "viewed-product=0"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    create_events.parse_metric_count(value)

    def test_parser_accepts_repeated_metrics_and_overlap(self) -> None:
        arguments = create_events.build_parser().parse_args(
            [
                "--metric",
                "viewed-product=20",
                "--metric",
                "ordered-product=5",
                "--overlap-count",
                "5",
            ]
        )

        self.assertEqual(len(arguments.metrics), 2)
        self.assertEqual(arguments.overlap_count, 5)

    def test_help_explains_variant_level_limitation(self) -> None:
        help_text = create_events.build_parser().format_help()

        self.assertIn("does not create variant-level events", help_text)
        self.assertIn("Price Drop", help_text)
        self.assertIn("Low Inventory", help_text)
        self.assertIn("share an external ID", help_text)
        self.assertIn("another company's catalog", help_text)


class DiscoveryTests(unittest.TestCase):
    """Verify paginated profile and catalog selection."""

    @patch("create_events.get_json")
    def test_fetches_only_marked_profiles_until_enough_are_found(
        self, get_json: Mock
    ) -> None:
        get_json.side_effect = [
            {
                "data": [
                    {
                        "id": "not-load-test",
                        "attributes": {"properties": {"load_test": False}},
                    },
                    {
                        "id": "load-test-1",
                        "attributes": {"properties": {"load_test": True}},
                    },
                ],
                "links": {"next": "https://next-page"},
            },
            {
                "data": [
                    {
                        "id": "load-test-2",
                        "attributes": {"properties": {"load_test": True}},
                    }
                ],
                "links": {"next": None},
            },
        ]

        profiles = create_events.fetch_load_test_profiles(
            Mock(spec=requests.Session),
            api_key="key",
            needed=2,
        )

        self.assertEqual(profiles, ["load-test-1", "load-test-2"])
        self.assertEqual(get_json.call_count, 2)
        self.assertEqual(get_json.call_args_list[1].args[1], "https://next-page")
        self.assertIsNone(get_json.call_args_list[1].kwargs["params"])

    @patch("create_events.get_json")
    def test_stops_profile_pagination_when_requested_count_is_met(
        self, get_json: Mock
    ) -> None:
        get_json.return_value = {
            "data": [
                {
                    "id": "load-test-1",
                    "attributes": {"properties": {"load_test": True}},
                },
                {
                    "id": "load-test-2",
                    "attributes": {"properties": {"load_test": True}},
                },
            ],
            "links": {"next": "https://unused-page"},
        }

        profiles = create_events.fetch_load_test_profiles(
            Mock(spec=requests.Session),
            api_key="key",
            needed=1,
        )

        self.assertEqual(profiles, ["load-test-1"])
        get_json.assert_called_once()

    @patch("create_events.get_json")
    def test_fetches_priced_variants_and_deduplicates_external_ids(
        self, get_json: Mock
    ) -> None:
        get_json.return_value = {
            "data": [
                {
                    "attributes": {
                        "external_id": "product-1",
                        "price": 12.5,
                    },
                },
                {
                    "attributes": {
                        "external_id": "product-1",
                        "price": 13,
                    },
                },
                {
                    "attributes": {
                        "external_id": "product-2",
                        "price": None,
                    },
                },
            ],
            "links": {"next": None},
        }

        products = create_events.fetch_catalog_products(
            Mock(spec=requests.Session),
            api_key="key",
            needed=3,
        )

        self.assertEqual(products, [product("product-1", 12.5)])
        self.assertEqual(
            get_json.call_args.args[1],
            f"{create_events.API_BASE_URL}/catalog-variants",
        )
        request_params = get_json.call_args.kwargs["params"]
        self.assertNotIn("filter", request_params)


class ApiRequestTests(unittest.TestCase):
    """Verify API retry and error behavior."""

    def test_get_json_retries_rate_limit(self) -> None:
        session = Mock(spec=requests.Session)
        limited = Mock(status_code=429, headers={"Retry-After": "2"})
        success = Mock(status_code=200)
        success.json.return_value = {"data": []}
        session.get.side_effect = [limited, success]
        sleep = Mock()

        document = create_events.get_json(
            session,
            "https://example.test",
            api_key="key",
            sleep=sleep,
        )

        self.assertEqual(document, {"data": []})
        sleep.assert_called_once_with(2.0)

    def test_get_json_retries_timeout_with_backoff(self) -> None:
        session = Mock(spec=requests.Session)
        success = Mock(status_code=200)
        success.json.return_value = {"data": []}
        session.get.side_effect = [requests.ReadTimeout("slow"), success]
        sleep = Mock()

        create_events.get_json(
            session,
            "https://example.test",
            api_key="key",
            sleep=sleep,
        )

        sleep.assert_called_once_with(create_events.RETRY_BASE_SECONDS)


class AllocationTests(unittest.TestCase):
    """Verify exact counts, overlap, and resource reuse."""

    def test_combines_repeated_metric_counts_in_first_seen_order(self) -> None:
        combined = create_events.combine_metric_counts(
            [
                create_events.MetricCount("Viewed Product", 2),
                create_events.MetricCount("Started Checkout", 3),
                create_events.MetricCount("Viewed Product", 4),
            ]
        )

        self.assertEqual(
            combined,
            [
                create_events.MetricCount("Viewed Product", 6),
                create_events.MetricCount("Started Checkout", 3),
            ],
        )

    def test_rejects_overlap_larger_than_inclusion_or_ordered_counts(self) -> None:
        with self.assertRaisesRegex(ValueError, "inclusion"):
            create_events.validate_request(
                [
                    create_events.MetricCount("Viewed Product", 1),
                    create_events.MetricCount("Ordered Product", 2),
                ],
                2,
            )
        with self.assertRaisesRegex(ValueError, "Ordered Product"):
            create_events.validate_request(
                [
                    create_events.MetricCount("Viewed Product", 2),
                    create_events.MetricCount("Ordered Product", 1),
                ],
                2,
            )

    def test_allocates_overlap_to_same_profile_and_product(self) -> None:
        unique_ids = iter(["view-1", "view-2", "order-1", "order-2"])

        assignments = create_events.allocate_events(
            [
                create_events.MetricCount("Viewed Product", 2),
                create_events.MetricCount("Ordered Product", 2),
            ],
            overlap_count=1,
            profile_ids=["profile-1", "profile-2", "profile-3"],
            products=[product("one"), product("two"), product("three")],
            unique_id=lambda: next(unique_ids),
        )

        self.assertEqual(len(assignments), 4)
        overlapped_view = assignments[0]
        overlapped_order = assignments[2]
        self.assertEqual(overlapped_order.profile_id, overlapped_view.profile_id)
        self.assertEqual(overlapped_order.product, overlapped_view.product)
        self.assertEqual(assignments[3].profile_id, "profile-3")

    def test_reuses_profiles_and_products_only_after_pool_is_exhausted(self) -> None:
        assignments = create_events.allocate_events(
            [create_events.MetricCount("Viewed Product", 3)],
            overlap_count=0,
            profile_ids=["profile-1", "profile-2"],
            products=[product("one"), product("two")],
            unique_id=Mock(side_effect=["1", "2", "3"]),
        )

        self.assertEqual(
            [event.profile_id for event in assignments],
            ["profile-1", "profile-2", "profile-1"],
        )
        self.assertEqual(
            [event.product.item_id for event in assignments],
            ["one", "two", "one"],
        )

    def test_rejects_small_pool_when_overlap_must_remain_exact(self) -> None:
        with self.assertRaisesRegex(ValueError, "keep overlap count exact"):
            create_events.allocate_events(
                [
                    create_events.MetricCount("Viewed Product", 3),
                    create_events.MetricCount("Ordered Product", 2),
                ],
                overlap_count=1,
                profile_ids=["profile-1", "profile-2", "profile-3"],
                products=[product("one"), product("two"), product("three")],
            )

    def test_requires_profiles_and_products(self) -> None:
        metric_counts = [create_events.MetricCount("Viewed Product", 1)]
        with self.assertRaisesRegex(ValueError, "no profiles"):
            create_events.allocate_events(
                metric_counts,
                overlap_count=0,
                profile_ids=[],
                products=[product()],
            )
        with self.assertRaisesRegex(ValueError, "no catalog products"):
            create_events.allocate_events(
                metric_counts,
                overlap_count=0,
                profile_ids=["profile-1"],
                products=[],
            )


class PayloadTests(unittest.TestCase):
    """Verify item-level JSON:API payloads and job splitting."""

    def test_builds_item_level_payload_with_current_price(self) -> None:
        payload = create_events.build_bulk_payload(
            [
                create_events.EventAssignment(
                    profile_id="profile-1",
                    metric_name="Started Checkout",
                    product=product("product-1", 42.75),
                    unique_id="event-1",
                )
            ]
        )

        bulk_entry = payload["data"]["attributes"]["events-bulk-create"]["data"][0]
        profile_data = bulk_entry["attributes"]["profile"]["data"]
        event = bulk_entry["attributes"]["events"]["data"][0]["attributes"]
        self.assertEqual(profile_data["id"], "profile-1")
        self.assertEqual(
            event["properties"],
            {
                "Items": [
                    {
                        "ProductID": "product-1",
                        "ItemPrice": 42.75,
                    }
                ]
            },
        )
        self.assertEqual(event["value"], 42.75)
        self.assertEqual(event["value_currency"], "USD")
        self.assertNotIn("VariantID", event["properties"]["Items"][0])
        self.assertEqual(
            event["metric"]["data"]["attributes"]["name"],
            "Started Checkout",
        )

    def test_builds_jomashop_property_paths_for_each_metric(self) -> None:
        expected_properties = {
            "Viewed Product": {"ProductID": "product-1", "Price": 19.99},
            "Started Checkout": {
                "Items": [{"ProductID": "product-1", "ItemPrice": 19.99}]
            },
            "Ordered Product": {"ProductID": "product-1"},
            "Added to Cart": {"ProductID": "product-1", "Price": 19.99},
        }

        for metric_name, expected in expected_properties.items():
            with self.subTest(metric_name=metric_name):
                properties = create_events.build_event_properties(
                    assignment(metric_name=metric_name)
                )

                self.assertEqual(properties, expected)

    def test_groups_multiple_events_for_the_same_profile(self) -> None:
        payload = create_events.build_bulk_payload(
            [
                assignment(unique_id="one"),
                assignment(metric_name="Ordered Product", unique_id="two"),
            ]
        )

        bulk_entries = payload["data"]["attributes"]["events-bulk-create"]["data"]
        self.assertEqual(len(bulk_entries), 1)
        self.assertEqual(
            len(bulk_entries[0]["attributes"]["events"]["data"]),
            2,
        )

    def test_splits_jobs_at_one_thousand_events(self) -> None:
        assignments = [assignment(unique_id=f"event-{index}") for index in range(1_001)]

        batches = list(create_events.iter_job_batches(assignments))

        self.assertEqual([len(batch) for batch in batches], [1_000, 1])

    def test_splits_jobs_when_payload_exceeds_size_limit(self) -> None:
        assignments = [
            assignment(unique_id="x" * 200),
            assignment(unique_id="y" * 200),
        ]
        one_event_size = len(
            create_events.json.dumps(
                create_events.build_bulk_payload(assignments[:1]),
            ).encode()
        )

        with patch.object(create_events, "MAX_PAYLOAD_BYTES", one_event_size):
            batches = list(create_events.iter_job_batches(assignments))

        self.assertEqual([len(batch) for batch in batches], [1, 1])


class SubmissionTests(unittest.TestCase):
    """Verify job retries, pacing, and partial progress."""

    def test_submit_bulk_job_retries_rate_limit(self) -> None:
        session = Mock(spec=requests.Session)
        limited = Mock(status_code=429, headers={"Retry-After": "2"})
        accepted = Mock(status_code=202)
        session.post.side_effect = [limited, accepted]
        sleep = Mock()

        create_events.submit_bulk_job(
            session,
            api_key="key",
            payload={"data": {}},
            sleep=sleep,
        )

        sleep.assert_called_once_with(2.0)

    def test_timeout_followed_by_conflict_confirms_prior_acceptance(self) -> None:
        session = Mock(spec=requests.Session)
        session.post.side_effect = [
            requests.ReadTimeout("slow"),
            Mock(status_code=409),
        ]
        sleep = Mock()

        create_events.submit_bulk_job(
            session,
            api_key="key",
            payload={"data": {}},
            sleep=sleep,
        )

        sleep.assert_called_once_with(create_events.RETRY_BASE_SECONDS)

    @patch("create_events.submit_bulk_job")
    def test_submit_events_paces_jobs_and_reports_progress(
        self, submit_bulk_job: Mock
    ) -> None:
        assignments = [assignment(unique_id=f"event-{index}") for index in range(1_001)]
        sleep = Mock()
        progress = Mock()

        accepted = create_events.submit_events(
            assignments,
            api_key="key",
            session=Mock(spec=requests.Session),
            sleep=sleep,
            progress=progress,
        )

        self.assertEqual(accepted, 1_001)
        self.assertEqual(submit_bulk_job.call_count, 2)
        sleep.assert_called_once_with(create_events.SUBMISSION_INTERVAL_SECONDS)
        self.assertEqual(
            progress.call_args_list,
            [call(1_000, 1_001, 1), call(1_001, 1_001, 2)],
        )

    @patch("create_events.submit_bulk_job")
    def test_submit_events_reports_partial_progress(
        self, submit_bulk_job: Mock
    ) -> None:
        assignments = [assignment(unique_id=f"event-{index}") for index in range(1_001)]
        submit_bulk_job.side_effect = [None, requests.HTTPError("failed")]

        with self.assertRaises(create_events.EventBatchError) as raised:
            create_events.submit_events(
                assignments,
                api_key="key",
                session=Mock(spec=requests.Session),
                sleep=Mock(),
            )

        self.assertEqual(raised.exception.accepted_events, 1_000)
        self.assertEqual(raised.exception.accepted_jobs, 1)
        self.assertEqual(raised.exception.total_events, 1_001)


class CreateEventsTests(unittest.TestCase):
    """Verify discovery sizing and end-to-end orchestration."""

    @patch("create_events.submit_events", return_value=8)
    @patch("create_events.allocate_events", return_value=[assignment()])
    @patch("create_events.fetch_catalog_products", return_value=[product()])
    @patch("create_events.fetch_load_test_profiles", return_value=["profile-1"])
    def test_discovers_only_distinct_resources_needed(
        self,
        fetch_profiles: Mock,
        fetch_products: Mock,
        _allocate_events: Mock,
        _submit_events: Mock,
    ) -> None:
        session = Mock(spec=requests.Session)

        accepted = create_events.create_events(
            [
                create_events.MetricCount("Viewed Product", 5),
                create_events.MetricCount("Ordered Product", 3),
            ],
            overlap_count=2,
            api_key="key",
            session=session,
        )

        self.assertEqual(accepted, 8)
        self.assertEqual(fetch_profiles.call_args.kwargs["needed"], 6)
        self.assertEqual(fetch_products.call_args.kwargs["needed"], 6)


if __name__ == "__main__":
    unittest.main()
