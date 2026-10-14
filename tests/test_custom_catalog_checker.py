"""Tests for the interactive custom catalog feed checker."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests

import custom_catalog_checker
from custom_catalog_checker import fetch_feed, validate_feed


class FetchFeedTests(unittest.TestCase):
    """Verify feed requests and HTTP failures."""

    @patch("custom_catalog_checker.requests.get")
    def test_fetches_json_with_optional_basic_authentication(self, get: Mock) -> None:
        response = get.return_value
        response.json.return_value = [{"id": "one"}]

        feed = fetch_feed(
            "https://example.com/feed.json",
            username="catalog-user",
            password="catalog-password",
        )

        self.assertEqual(feed, [{"id": "one"}])
        get.assert_called_once_with(
            "https://example.com/feed.json",
            auth=("catalog-user", "catalog-password"),
            timeout=custom_catalog_checker.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("custom_catalog_checker.requests.get")
    def test_propagates_http_failures(self, get: Mock) -> None:
        get.return_value.raise_for_status.side_effect = requests.HTTPError("denied")

        with self.assertRaisesRegex(requests.HTTPError, "denied"):
            fetch_feed("https://example.com/feed.json")

    @patch("custom_catalog_checker.requests.get")
    def test_rejects_authentication_over_http(self, get: Mock) -> None:
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            fetch_feed(
                "http://example.com/feed.json",
                username="catalog-user",
                password="catalog-password",
            )

        get.assert_not_called()


class ValidateFeedTests(unittest.TestCase):
    """Verify required plain-name fields and row diagnostics."""

    def test_counts_valid_rows_without_dollar_prefixed_fields(self) -> None:
        feed = [
            {
                "id": "one",
                "title": "One",
                "link": "https://example.com/one",
                "image_link": "https://example.com/one.jpg",
                "description": "Product one",
            }
        ]

        good_count, bad_count, errors = validate_feed(feed)

        self.assertEqual((good_count, bad_count), (1, 0))
        self.assertEqual(errors, [])

    def test_reports_missing_fields_and_non_object_rows(self) -> None:
        good_count, bad_count, errors = validate_feed(
            [{"id": "incomplete"}, "not-an-object"]
        )

        self.assertEqual((good_count, bad_count), (0, 2))
        self.assertIn("description, image_link, link, title", errors[0])
        self.assertEqual(errors[1], "Row 2: expected a product object")

    def test_rejects_non_array_feed(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON array"):
            validate_feed({"id": "one"})


class InteractiveCheckerTests(unittest.TestCase):
    """Verify prompts, summaries, and process status."""

    @patch("custom_catalog_checker.fetch_feed")
    @patch("builtins.input", side_effect=["https://example.com/feed.json", "n"])
    def test_main_prints_successful_summary(self, _input: Mock, fetch: Mock) -> None:
        fetch.return_value = [
            {
                "id": "one",
                "title": "One",
                "link": "https://example.com/one",
                "image_link": "https://example.com/one.jpg",
                "description": "Product one",
            }
        ]

        output = io.StringIO()
        with redirect_stdout(output):
            status = custom_catalog_checker.main()

        self.assertEqual(status, 0)
        self.assertIn("Good rows: 1", output.getvalue())
        self.assertIn("Bad rows: 0", output.getvalue())
        self.assertIn("Total products: 1", output.getvalue())
        fetch.assert_called_once_with(
            "https://example.com/feed.json",
            username=None,
            password=None,
        )

    @patch("builtins.input", side_effect=["maybe", "y"])
    def test_authentication_prompt_retries_invalid_answer(self, _input: Mock) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            authentication_required = custom_catalog_checker._authentication_required()

        self.assertTrue(authentication_required)
        self.assertIn("Please enter y or n.", output.getvalue())

    @patch("custom_catalog_checker.fetch_feed")
    @patch("custom_catalog_checker.getpass.getpass", return_value="secret")
    @patch(
        "builtins.input",
        side_effect=["https://example.com/feed.json", "y", "catalog-user"],
    )
    def test_main_passes_prompted_authentication(
        self, _input: Mock, _getpass: Mock, fetch: Mock
    ) -> None:
        fetch.return_value = []

        with redirect_stdout(io.StringIO()):
            status = custom_catalog_checker.main()

        self.assertEqual(status, 0)
        fetch.assert_called_once_with(
            "https://example.com/feed.json",
            username="catalog-user",
            password="secret",
        )

    @patch("custom_catalog_checker.fetch_feed", return_value=[{"id": "incomplete"}])
    @patch("builtins.input", side_effect=["https://example.com/feed.json", "n"])
    def test_main_returns_failure_for_bad_rows(
        self, _input: Mock, _fetch: Mock
    ) -> None:
        with redirect_stdout(io.StringIO()):
            status = custom_catalog_checker.main()

        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
