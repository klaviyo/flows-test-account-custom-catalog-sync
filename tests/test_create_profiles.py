"""Tests for creating load-test profiles through the Profiles API."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import ANY, Mock, patch

import requests

import create_profiles


class BuildProfilePayloadTests(unittest.TestCase):
    """Verify generated profile identifiers and marker properties."""

    def test_builds_readable_unique_profile_payload(self) -> None:
        with (
            patch.object(create_profiles.FAKE, "first_name", return_value="Steve"),
            patch.object(create_profiles.FAKE, "last_name", return_value="Smith"),
        ):
            payload = create_profiles.build_profile_payload("abc123")

        attributes = payload["data"]["attributes"]
        self.assertEqual(payload["data"]["type"], "profile")
        self.assertEqual(attributes["email"], "steve_smith_abc123@example.com")
        self.assertEqual(attributes["external_id"], "fp-4236-load-test-abc123")
        self.assertEqual(attributes["first_name"], "Steve")
        self.assertEqual(attributes["last_name"], "Smith")
        self.assertTrue(attributes["properties"]["load_test"])
        self.assertEqual(attributes["properties"]["load_test_ticket"], "FP-4236")

    def test_normalizes_generated_names_for_email(self) -> None:
        with (
            patch.object(create_profiles.FAKE, "first_name", return_value="Élodie"),
            patch.object(
                create_profiles.FAKE,
                "last_name",
                return_value="O'Neil-Smith",
            ),
        ):
            payload = create_profiles.build_profile_payload("abc123")

        self.assertEqual(
            payload["data"]["attributes"]["email"],
            "elodie_o_neil_smith_abc123@example.com",
        )


class CreateProfileTests(unittest.TestCase):
    """Verify API requests and rate-limit retry handling."""

    def test_creates_profile_with_required_headers(self) -> None:
        session = Mock(spec=requests.Session)
        response = session.post.return_value
        response.status_code = 201
        response.json.return_value = {"data": {"id": "profile-id"}}
        payload = create_profiles.build_profile_payload("abc123")

        profile_id = create_profiles.create_profile(
            session,
            api_key="private-key",
            payload=payload,
        )

        self.assertEqual(profile_id, "profile-id")
        session.post.assert_called_once_with(
            create_profiles.API_URL,
            headers={
                "Authorization": "Klaviyo-API-Key private-key",
                "accept": "application/vnd.api+json",
                "content-type": "application/vnd.api+json",
                "revision": create_profiles.API_REVISION,
            },
            json=payload,
            timeout=create_profiles.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status.assert_called_once_with()

    def test_retries_rate_limit_using_retry_after(self) -> None:
        session = Mock(spec=requests.Session)
        limited_response = Mock(status_code=429, headers={"Retry-After": "2"})
        success_response = Mock(status_code=201)
        success_response.json.return_value = {"data": {"id": "profile-id"}}
        session.post.side_effect = [limited_response, success_response]
        sleep = Mock()

        profile_id = create_profiles.create_profile(
            session,
            api_key="private-key",
            payload={"data": {}},
            sleep=sleep,
        )

        self.assertEqual(profile_id, "profile-id")
        sleep.assert_called_once_with(2.0)
        success_response.raise_for_status.assert_called_once_with()

    def test_rejects_exhausted_rate_limit_retries(self) -> None:
        session = Mock(spec=requests.Session)
        session.post.return_value = Mock(status_code=429, headers={})

        with self.assertRaisesRegex(RuntimeError, "retries exhausted"):
            create_profiles.create_profile(
                session,
                api_key="private-key",
                payload={"data": {}},
                sleep=Mock(),
            )

        self.assertEqual(
            session.post.call_count,
            create_profiles.MAX_RATE_LIMIT_RETRIES + 1,
        )

    def test_retries_timeout_with_exponential_backoff(self) -> None:
        session = Mock(spec=requests.Session)
        success_response = Mock(status_code=201)
        success_response.json.return_value = {"data": {"id": "profile-id"}}
        session.post.side_effect = [requests.ReadTimeout("slow"), success_response]
        sleep = Mock()

        profile_id = create_profiles.create_profile(
            session,
            api_key="private-key",
            payload={"data": {}},
            sleep=sleep,
        )

        self.assertEqual(profile_id, "profile-id")
        sleep.assert_called_once_with(create_profiles.TIMEOUT_RETRY_BASE_SECONDS)

    def test_reraises_timeout_after_retries_are_exhausted(self) -> None:
        session = Mock(spec=requests.Session)
        session.post.side_effect = requests.ReadTimeout("slow")

        with self.assertRaisesRegex(requests.ReadTimeout, "slow"):
            create_profiles.create_profile(
                session,
                api_key="private-key",
                payload={"data": {}},
                sleep=Mock(),
            )

        self.assertEqual(
            session.post.call_count,
            create_profiles.MAX_TIMEOUT_RETRIES + 1,
        )

    def test_treats_conflict_after_timeout_as_confirmed(self) -> None:
        session = Mock(spec=requests.Session)
        session.post.side_effect = [
            requests.ReadTimeout("slow"),
            Mock(status_code=409),
        ]
        payload = {
            "data": {
                "attributes": {
                    "external_id": "fp-4236-load-test-abc123",
                }
            }
        }

        profile_id = create_profiles.create_profile(
            session,
            api_key="private-key",
            payload=payload,
            sleep=Mock(),
        )

        self.assertEqual(profile_id, "fp-4236-load-test-abc123")


class CreateProfilesTests(unittest.TestCase):
    """Verify batch generation, pacing, and input validation."""

    @patch("create_profiles.create_profile")
    @patch("create_profiles.uuid.uuid4")
    def test_creates_requested_profiles_with_unique_payloads(
        self, uuid4: Mock, create_profile: Mock
    ) -> None:
        uuid4.side_effect = [Mock(hex="first"), Mock(hex="second")]
        session = Mock(spec=requests.Session)
        sleep = Mock()
        progress = Mock()

        created_count = create_profiles.create_profiles(
            2,
            api_key="private-key",
            session=session,
            sleep=sleep,
            progress=progress,
        )

        self.assertEqual(created_count, 2)
        self.assertEqual(create_profile.call_count, 2)
        self.assertEqual(
            create_profile.call_args_list[0].kwargs["payload"]["data"]["attributes"][
                "external_id"
            ],
            "fp-4236-load-test-first",
        )
        self.assertEqual(
            create_profile.call_args_list[1].kwargs["payload"]["data"]["attributes"][
                "external_id"
            ],
            "fp-4236-load-test-second",
        )
        sleep.assert_called_once_with(create_profiles.REQUEST_INTERVAL_SECONDS)
        progress.assert_called_once_with(2, 2)
        self.assertEqual(create_profiles.REQUEST_INTERVAL_SECONDS, 60 / 370)

    @patch("create_profiles.create_profile")
    def test_reports_confirmed_count_when_batch_stops(
        self, create_profile: Mock
    ) -> None:
        create_profile.side_effect = ["profile-id", requests.ReadTimeout("slow")]

        with self.assertRaises(create_profiles.ProfileBatchError) as raised:
            create_profiles.create_profiles(
                3,
                api_key="private-key",
                session=Mock(spec=requests.Session),
                sleep=Mock(),
            )

        self.assertEqual(raised.exception.confirmed_count, 1)
        self.assertEqual(raised.exception.requested_count, 3)
        self.assertIsInstance(raised.exception.cause, requests.ReadTimeout)

    def test_rejects_negative_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            create_profiles.create_profiles(-1, api_key="private-key")


class MainTests(unittest.TestCase):
    """Verify CLI configuration and completion behavior."""

    @patch.dict("os.environ", {}, clear=True)
    @patch("sys.argv", ["create_profiles.py", "500"])
    def test_requires_api_key_environment_variable(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                create_profiles.main()

    @patch("create_profiles.create_profiles", return_value=500)
    @patch.dict(
        "os.environ",
        {create_profiles.API_KEY_ENVIRONMENT_VARIABLE: "private-key"},
        clear=True,
    )
    @patch("sys.argv", ["create_profiles.py", "500"])
    def test_main_creates_requested_count(self, create_batch: Mock) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            status = create_profiles.main()

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "Created 500 profiles.\n")
        create_batch.assert_called_once_with(
            500,
            api_key="private-key",
            progress=ANY,
        )

    @patch(
        "create_profiles.create_profiles",
        side_effect=create_profiles.ProfileBatchError(
            237,
            500,
            requests.ReadTimeout("slow"),
        ),
    )
    @patch.dict(
        "os.environ",
        {create_profiles.API_KEY_ENVIRONMENT_VARIABLE: "private-key"},
        clear=True,
    )
    @patch("sys.argv", ["create_profiles.py", "500"])
    def test_main_reports_partial_count_after_timeout(self, create_batch: Mock) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            status = create_profiles.main()

        self.assertEqual(status, 1)
        self.assertIn("stopped after 237 of 500 profiles were confirmed", output.getvalue())
        self.assertIn(
            "The final timed-out request may still have created a profile.",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
