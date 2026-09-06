import socket
import unittest
from unittest.mock import AsyncMock, patch

from app.scrapers import tangerine


class _FakeReplaySession:
    def __init__(self, *args, **kwargs):
        self.storage_state = {"cookies": [], "origins": []}
        self.session_artifact = {"user_agent": "ua"}

    def load(self):
        return self

    def missing_artifact_result(self):
        raise AssertionError("storage_state should be present")

    async def refresh_runtime_artifacts_async(self, *args, **kwargs):
        raise AssertionError("network failures must not refresh artifacts")

    def normalize_result(self, payload):
        return payload


class TangerineDirectSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_auth_failure_logs_structured_cause(self):
        response = {
            "status": 200,
            "payload": {
                "response_status": {
                    "status_code": "NO_SESSION",
                    "message": "Session is no longer active",
                }
            },
        }
        with (
            patch.object(tangerine, "_tangerine_direct_get", new=AsyncMock(return_value=response)),
            patch.object(tangerine, "_tangerine_log_event") as log_event,
        ):
            with self.assertRaises(tangerine.TangerineAuthRequired):
                await tangerine._fetch_tangerine_json_direct(
                    {"cookies": []},
                    {},
                    "https://secure.tangerine.ca/api/accounts?token=secret",
                    user_agent="ua",
                    user_id=1,
                )

        log_event.assert_called_once_with(
            "json request auth_required",
            user_id=1,
            level="warning",
            auth_signal="payload_no_session",
            status_code=200,
            semantic_status_code=None,
            url="https://secure.tangerine.ca/api/accounts",
            payload_type="dict",
            payload_keys=["response_status"],
        )

    async def test_direct_network_exception_returns_network_error(self):
        with (
            patch.object(
                tangerine,
                "load_saved_artifact_replay_session_async",
                new=AsyncMock(return_value=_FakeReplaySession()),
            ),
            patch.object(
                tangerine,
                "_sync_tangerine_with_saved_artifacts",
                new=AsyncMock(side_effect=socket.gaierror(-3, "Temporary failure in name resolution")),
            ),
            patch.object(tangerine, "_tangerine_log_event") as log_event,
        ):
            result = await tangerine.try_headless_sync(1)

        self.assertEqual("network_error", result["status"])
        self.assertEqual("Connection failed", result["message"])
        self.assertEqual([], result["accounts"])
        log_event.assert_any_call(
            "headless sync network_error",
            user_id=1,
            engine="direct",
            level="warning",
            exception_type="gaierror",
            message="[Errno -3] Temporary failure in name resolution",
        )


if __name__ == "__main__":
    unittest.main()
