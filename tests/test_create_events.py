"""Tests for creating item-level catalog engagement events."""

from __future__ import annotations

import argparse
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
                "--days",
                "30",
                "--profiles",
                "500",
                "--products",
                "10000",
                "--resume-at",
                "1000",
            ]
        )

        self.assertEqual(len(arguments.metrics), 2)
        self.assertEqual(arguments.overlap_count, 5)
        self.assertEqual(arguments.days, 30)
        self.assertEqual(arguments.profiles, 500)
        self.assertEqual(arguments.products, 10_000)
        self.assertEqual(arguments.resume_at, 1_000)

    def test_optional_positive_integer_arguments(self) -> None:
        arguments = create_events.build_parser().parse_args(
            ["--metric", "viewed-product=1"]
        )

        self.assertIsNone(arguments.days)
        self.assertIsNone(arguments.profiles)
        self.assertIsNone(arguments.products)
        self.assertEqual(arguments.resume_at, 0)
        for option in ("--days", "--profiles", "--products"):
            for value in ("0", "-1", "nope"):
                with self.subTest(option=option, value=value):
                    with self.assertRaises(SystemExit):
                        create_events.build_parser().parse_args(
                            ["--metric", "viewed-product=1", option, value]
                        )
        for value in ("-1", "nope"):
            with self.subTest(option="--resume-at", value=value):
                with self.assertRaises(SystemExit):
                    create_events.build_parser().parse_args(
                        ["--metric", "viewed-product=1", "--resume-at", value]
                    )

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

    def test_assignment_iterator_does_not_precompute_events(self) -> None:
        unique_id = Mock(return_value="one")
        assignments = create_events.iter_event_assignments(
            [create_events.MetricCount("Viewed Product", 40_500_000)],
            overlap_count=0,
            profile_ids=["profile-1"],
            products=[product()],
            unique_id=unique_id,
        )

        self.assertEqual(unique_id.call_count, 0)
        self.assertEqual(next(assignments).unique_id, "one")
        self.assertEqual(unique_id.call_count, 1)

    def test_small_pool_preserves_counts_and_allows_additional_overlap(self) -> None:
        assignments = create_events.allocate_events(
            [
                create_events.MetricCount("Viewed Product", 3),
                create_events.MetricCount("Ordered Product", 2),
            ],
            overlap_count=1,
            profile_ids=["profile-1", "profile-2", "profile-3"],
            products=[product("one"), product("two"), product("three")],
            unique_id=Mock(side_effect=["1", "2", "3", "4", "5"]),
        )

        self.assertEqual(len(assignments), 5)
        self.assertEqual(
            [event.metric_name for event in assignments],
            [
                "Viewed Product",
                "Viewed Product",
                "Viewed Product",
                "Ordered Product",
                "Ordered Product",
            ],
        )
        self.assertEqual(
            {(event.profile_id, event.product.item_id) for event in assignments[3:]},
            {("profile-1", "one")},
        )
        self.assertEqual(len({event.unique_id for event in assignments}), 5)

    def test_resumes_at_global_assignment_index(self) -> None:
        assignments = list(
            create_events.iter_event_assignments(
                [
                    create_events.MetricCount("Viewed Product", 2),
                    create_events.MetricCount("Ordered Product", 2),
                ],
                overlap_count=1,
                profile_ids=["profile-1", "profile-2", "profile-3"],
                products=[product("one"), product("two"), product("three")],
                start_index=2,
                unique_id=Mock(side_effect=["order-1", "order-2"]),
            )
        )

        self.assertEqual(
            [
                (event.metric_name, event.profile_id, event.product.item_id)
                for event in assignments
            ],
            [
                ("Ordered Product", "profile-1", "one"),
                ("Ordered Product", "profile-3", "three"),
            ],
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

    def test_spreads_event_times_evenly_across_days(self) -> None:
        assignments = [assignment(unique_id=f"event-{index}") for index in range(4)]

        timestamped = create_events.spread_event_times(
            assignments,
            2,
            now=datetime(2026, 1, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [event.event_time for event in timestamped],
            [
                "2026-01-01T06:00:00Z",
                "2026-01-01T18:00:00Z",
                "2026-01-02T06:00:00Z",
                "2026-01-02T18:00:00Z",
            ],
        )
        self.assertTrue(all(original.event_time is None for original in assignments))

    def test_omits_event_times_when_days_is_not_set(self) -> None:
        assignments = [assignment()]

        self.assertEqual(
            create_events.spread_event_times(assignments, None),
            assignments,
        )

    def test_timestamp_iterator_only_consumes_requested_events(self) -> None:
        consumed: list[int] = []

        def assignments():
            for index in range(2):
                consumed.append(index)
                yield assignment(unique_id=f"event-{index}")

        timestamped = create_events.iter_timestamped_assignments(
            assignments(),
            total_events=2,
            days=1,
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(consumed, [])
        self.assertEqual(next(timestamped).event_time, "2026-01-01T06:00:00Z")
        self.assertEqual(consumed, [0])

    def test_resumed_timestamps_keep_global_positions(self) -> None:
        timestamped = list(
            create_events.iter_timestamped_assignments(
                [assignment(unique_id="three"), assignment(unique_id="four")],
                total_events=4,
                days=2,
                start_index=2,
                now=datetime(2026, 1, 3, tzinfo=timezone.utc),
            )
        )

        self.assertEqual(
            [event.event_time for event in timestamped],
            ["2026-01-02T06:00:00Z", "2026-01-02T18:00:00Z"],
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
        self.assertNotIn("time", event)

    def test_includes_explicit_event_time(self) -> None:
        payload = create_events.build_bulk_payload(
            [
                create_events.EventAssignment(
                    profile_id="profile-1",
                    metric_name="Viewed Product",
                    product=product(),
                    unique_id="event-1",
                    event_time="2026-01-01T12:00:00Z",
                )
            ]
        )

        event = payload["data"]["attributes"]["events-bulk-create"]["data"][0][
            "attributes"
        ]["events"]["data"][0]["attributes"]
        self.assertEqual(event["time"], "2026-01-01T12:00:00Z")

    def test_builds_jomashop_property_paths_for_each_metric(self) -> None:
        expected_properties = {
            "Viewed Product": {"ProductID": "product-1", "Price": 19.99},
            "Started Checkout": {
                "Items": [{"ProductID": "product-1", "ItemPrice": 19.99}]
            },
            "Ordered Product": {"ProductID": "product-1", "Price": 19.99},
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

    def test_yields_first_batch_without_consuming_the_full_stream(self) -> None:
        consumed: list[int] = []

        def assignments():
            for index in range(3):
                consumed.append(index)
                yield assignment(unique_id=f"event-{index}")

        with patch.object(create_events, "MAX_EVENTS_PER_JOB", 2):
            batches = create_events.iter_job_batches(assignments())
            first_batch = next(batches)

        self.assertEqual(len(first_batch), 2)
        self.assertEqual(consumed, [0, 1])

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

    def test_rate_limiter_spaces_request_attempts(self) -> None:
        sleep = Mock()
        monotonic = Mock(return_value=100.0)
        limiter = create_events.RequestRateLimiter(
            120,
            sleep=sleep,
            monotonic=monotonic,
        )

        limiter.wait()
        limiter.wait()

        sleep.assert_called_once_with(0.5)

    def test_rate_limiter_rejects_non_positive_rate(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            create_events.RequestRateLimiter(0)

    def test_submit_bulk_job_retries_rate_limit(self) -> None:
        session = Mock(spec=requests.Session)
        limited = Mock(status_code=429, headers={"Retry-After": "2"})
        accepted = Mock(status_code=202)
        session.post.side_effect = [limited, accepted]
        sleep = Mock()
        rate_limiter = Mock(spec=create_events.RequestRateLimiter)

        create_events.submit_bulk_job(
            session,
            api_key="key",
            payload={"data": {}},
            rate_limiter=rate_limiter,
            sleep=sleep,
        )

        sleep.assert_called_once_with(2.0)
        self.assertEqual(rate_limiter.wait.call_count, 2)

    @patch("create_events.MAX_RETRIES", 6)
    def test_connection_retries_use_a_capped_backoff(self) -> None:
        session = Mock(spec=requests.Session)
        session.post.side_effect = requests.ConnectionError("broken")
        sleep = Mock()

        with self.assertRaises(requests.ConnectionError):
            create_events.submit_bulk_job(
                session,
                api_key="key",
                payload={"data": {}},
                sleep=sleep,
            )

        self.assertEqual(session.post.call_count, 7)
        self.assertEqual(
            sleep.call_args_list,
            [call(1.0), call(2.0), call(4.0), call(8.0), call(16.0), call(30.0)],
        )

    def test_timeout_followed_by_conflict_is_not_reported_as_accepted(self) -> None:
        session = Mock(spec=requests.Session)
        conflict = Mock(status_code=409)
        conflict.raise_for_status.side_effect = requests.HTTPError("conflict")
        session.post.side_effect = [
            requests.ReadTimeout("slow"),
            conflict,
        ]
        sleep = Mock()

        with self.assertRaises(requests.HTTPError):
            create_events.submit_bulk_job(
                session,
                api_key="key",
                payload={"data": {}},
                sleep=sleep,
            )

        sleep.assert_called_once_with(create_events.RETRY_BASE_SECONDS)

    @patch("create_events.submit_bulk_job")
    def test_submit_events_reports_concurrent_progress(
        self, submit_bulk_job: Mock
    ) -> None:
        assignments = [assignment(unique_id=f"event-{index}") for index in range(1_001)]
        progress = Mock()

        accepted = create_events.submit_events(
            assignments,
            api_key="key",
            session=Mock(spec=requests.Session),
            progress=progress,
            max_workers=1,
            rate_limiter=Mock(spec=create_events.RequestRateLimiter),
        )

        self.assertEqual(accepted, 1_001)
        self.assertEqual(submit_bulk_job.call_count, 2)
        self.assertEqual(
            progress.call_args_list,
            [call(1_000, 1_001, 1), call(1_001, 1_001, 2)],
        )

    @patch("create_events.submit_bulk_job")
    def test_concurrent_workers_use_isolated_sessions(
        self, submit_bulk_job: Mock
    ) -> None:
        workers_started = threading.Barrier(2)
        submit_bulk_job.side_effect = lambda *_args, **_kwargs: workers_started.wait(
            timeout=2
        )
        first_session = Mock(spec=requests.Session)
        second_session = Mock(spec=requests.Session)
        session_factory = Mock(side_effect=[first_session, second_session])

        accepted = create_events.submit_events(
            [assignment(unique_id=f"event-{index}") for index in range(1_001)],
            api_key="key",
            session=Mock(spec=requests.Session),
            max_workers=2,
            rate_limiter=Mock(spec=create_events.RequestRateLimiter),
            session_factory=session_factory,
        )

        self.assertEqual(accepted, 1_001)
        used_sessions = [job.args[0] for job in submit_bulk_job.call_args_list]
        self.assertTrue(any(value is first_session for value in used_sessions))
        self.assertTrue(any(value is second_session for value in used_sessions))
        first_session.close.assert_called_once()
        second_session.close.assert_called_once()

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
                max_workers=1,
                rate_limiter=Mock(spec=create_events.RequestRateLimiter),
            )

        self.assertEqual(raised.exception.accepted_events, 1_000)
        self.assertEqual(raised.exception.accepted_jobs, 1)
        self.assertEqual(raised.exception.total_events, 1_001)

    @patch("create_events.submit_bulk_job")
    def test_resumed_progress_starts_at_the_accepted_prefix(
        self, _submit_bulk_job: Mock
    ) -> None:
        progress = Mock()

        accepted = create_events.submit_events(
            [assignment()],
            total_events=10_001,
            initial_accepted=10_000,
            api_key="key",
            session=Mock(spec=requests.Session),
            progress=progress,
            max_workers=1,
            rate_limiter=Mock(spec=create_events.RequestRateLimiter),
        )

        self.assertEqual(accepted, 10_001)
        progress.assert_called_once_with(10_001, 10_001, 1)

    @patch("create_events.submit_bulk_job")
    def test_reports_only_a_contiguous_accepted_prefix(
        self, submit_bulk_job: Mock
    ) -> None:
        release_first = threading.Event()
        second_finished = threading.Event()
        third_started = threading.Event()

        def finish_second_job_first(*_args: object, **kwargs: object) -> None:
            entries = kwargs["payload"]["data"]["attributes"]["events-bulk-create"][
                "data"
            ]
            first_unique_id = entries[0]["attributes"]["events"]["data"][0][
                "attributes"
            ]["unique_id"]
            if first_unique_id == "event-0":
                release_first.wait(timeout=2)
            elif first_unique_id == "event-1000":
                second_finished.set()
            else:
                third_started.set()

        submit_bulk_job.side_effect = finish_second_job_first
        progress = Mock()
        with ThreadPoolExecutor(max_workers=1) as runner:
            submission = runner.submit(
                create_events.submit_events,
                [assignment(unique_id=f"event-{index}") for index in range(2_001)],
                total_events=2_001,
                api_key="key",
                session=Mock(spec=requests.Session),
                progress=progress,
                max_workers=2,
                rate_limiter=Mock(spec=create_events.RequestRateLimiter),
            )
            self.assertTrue(second_finished.wait(timeout=2))
            progress.assert_not_called()
            self.assertFalse(third_started.is_set())
            release_first.set()
            self.assertEqual(submission.result(timeout=2), 2_001)

        self.assertTrue(third_started.is_set())
        self.assertEqual(
            progress.call_args_list,
            [
                call(1_000, 2_001, 1),
                call(2_000, 2_001, 2),
                call(2_001, 2_001, 3),
            ],
        )

    def test_streaming_submission_requires_an_explicit_total(self) -> None:
        with self.assertRaisesRegex(ValueError, "total_events"):
            create_events.submit_events(
                iter([assignment()]),
                api_key="key",
                session=Mock(spec=requests.Session),
            )


class CreateEventsTests(unittest.TestCase):
    """Verify discovery sizing and end-to-end orchestration."""

    @patch("create_events.submit_events", return_value=8)
    @patch("create_events.iter_event_assignments")
    @patch("create_events.fetch_catalog_products", return_value=[product()])
    @patch("create_events.fetch_load_test_profiles", return_value=["profile-1"])
    def test_discovers_only_distinct_resources_needed(
        self,
        fetch_profiles: Mock,
        fetch_products: Mock,
        iter_assignments: Mock,
        submit_events: Mock,
    ) -> None:
        session = Mock(spec=requests.Session)
        iter_assignments.return_value = iter([assignment()])

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
        self.assertEqual(submit_events.call_args.kwargs["total_events"], 8)
        self.assertEqual(submit_events.call_args.kwargs["initial_accepted"], 0)
        self.assertIs(submit_events.call_args.kwargs["session"], session)
        self.assertIsNone(submit_events.call_args.kwargs["session_factory"])

    def test_owned_session_enables_isolated_worker_sessions(self) -> None:
        owned_session = Mock(spec=requests.Session)
        with (
            patch(
                "create_events.requests.Session", return_value=owned_session
            ) as factory,
            patch(
                "create_events.fetch_load_test_profiles",
                return_value=["profile-1"],
            ),
            patch(
                "create_events.fetch_catalog_products",
                return_value=[product()],
            ),
            patch(
                "create_events.iter_event_assignments",
                return_value=iter([assignment()]),
            ),
            patch("create_events.submit_events", return_value=1) as submit_events,
        ):
            accepted = create_events.create_events(
                [create_events.MetricCount("Viewed Product", 1)],
                overlap_count=0,
                api_key="key",
            )

        self.assertEqual(accepted, 1)
        self.assertIs(submit_events.call_args.kwargs["session"], owned_session)
        self.assertIs(submit_events.call_args.kwargs["session_factory"], factory)
        owned_session.close.assert_called_once()

    @patch("create_events.submit_events", return_value=600_000)
    @patch("create_events.iter_event_assignments")
    @patch("create_events.fetch_catalog_products", return_value=[product()])
    @patch("create_events.fetch_load_test_profiles", return_value=["profile-1"])
    def test_caps_profile_and_product_discovery_independently(
        self,
        fetch_profiles: Mock,
        fetch_products: Mock,
        iter_assignments: Mock,
        _submit_events: Mock,
    ) -> None:
        iter_assignments.return_value = iter([assignment()])
        accepted = create_events.create_events(
            [create_events.MetricCount("Ordered Product", 600_000)],
            overlap_count=0,
            profile_count=500,
            product_count=10_000,
            api_key="key",
            session=Mock(spec=requests.Session),
        )

        self.assertEqual(accepted, 600_000)
        self.assertEqual(fetch_profiles.call_args.kwargs["needed"], 500)
        self.assertEqual(fetch_products.call_args.kwargs["needed"], 10_000)

    @patch("create_events.submit_events", return_value=10)
    @patch("create_events.iter_event_assignments")
    @patch("create_events.fetch_catalog_products", return_value=[product()])
    @patch("create_events.fetch_load_test_profiles", return_value=["profile-1"])
    def test_resumes_discovery_and_submission_at_an_accepted_prefix(
        self,
        _fetch_profiles: Mock,
        _fetch_products: Mock,
        iter_assignments: Mock,
        submit_events: Mock,
    ) -> None:
        iter_assignments.return_value = iter([assignment()])

        accepted = create_events.create_events(
            [create_events.MetricCount("Viewed Product", 10)],
            overlap_count=0,
            days=2,
            resume_at=6,
            api_key="key",
            session=Mock(spec=requests.Session),
        )

        self.assertEqual(accepted, 10)
        self.assertEqual(iter_assignments.call_args.kwargs["start_index"], 6)
        self.assertEqual(submit_events.call_args.kwargs["initial_accepted"], 6)
        self.assertEqual(submit_events.call_args.kwargs["total_events"], 10)

    def test_rejects_non_positive_resource_caps(self) -> None:
        for argument in ({"profile_count": 0}, {"product_count": -1}):
            with self.subTest(argument=argument):
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    create_events.create_events(
                        [create_events.MetricCount("Viewed Product", 1)],
                        overlap_count=0,
                        api_key="key",
                        session=Mock(spec=requests.Session),
                        **argument,
                    )

    def test_rejects_resume_count_beyond_total(self) -> None:
        with self.assertRaisesRegex(ValueError, "resume count"):
            create_events.create_events(
                [create_events.MetricCount("Viewed Product", 10)],
                overlap_count=0,
                resume_at=11,
                api_key="key",
                session=Mock(spec=requests.Session),
            )

    @patch("create_events.fetch_catalog_products")
    @patch("create_events.fetch_load_test_profiles")
    def test_complete_resume_skips_discovery(
        self, fetch_profiles: Mock, fetch_products: Mock
    ) -> None:
        accepted = create_events.create_events(
            [create_events.MetricCount("Viewed Product", 10)],
            overlap_count=0,
            resume_at=10,
            api_key="key",
            session=Mock(spec=requests.Session),
        )

        self.assertEqual(accepted, 10)
        fetch_profiles.assert_not_called()
        fetch_products.assert_not_called()


if __name__ == "__main__":
    unittest.main()
