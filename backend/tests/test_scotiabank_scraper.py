import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.scrapers import scotiabank


class _FakeReplaySession:
    def __init__(self, *args, **kwargs):
        self.storage_state = {
            "cookies": [
                {"name": "session-id", "value": "2", "domain": ".scotiabank.com", "path": "/"},
                {"name": "_csrf", "value": "3", "domain": ".scotiabank.com", "path": "/"},
            ],
            "origins": [],
        }
        self.session_artifact = {
            "captured_at": "2026-01-01T00:00:00+00:00",
            "last_reused_at": "2026-01-01T00:00:00+00:00",
            "user_agent": "ua",
        }
        self.refreshed = False

    async def load_async(self):
        return self

    def missing_artifact_result(self):
        raise AssertionError("storage_state should be present")

    async def refresh_runtime_artifacts_async(self, *args, **kwargs):
        self.refreshed = True
        return True

    def normalize_result(self, payload):
        return payload


class ScotiabankDirectSyncTests(unittest.IsolatedAsyncioTestCase):
    def _summary_payload(self):
        return {
            "data": {
                "products": [
                    {
                        "key": "acct-1",
                        "displayId": "1234",
                        "description": "Chequing",
                        "productCategory": "BANKING",
                        "assetLiabilityType": "ASSET",
                        "primaryBalances": [{"amount": 100.0, "currencyCode": "CAD"}],
                    }
                ]
            }
        }

    async def test_direct_sync_attempts_replay_when_required_cookies_exist(self):
        payload = {
            "status": "ok",
            "accounts": [{"name": "Chequing", "external_id": "account:1"}],
            "transactions": {},
            "transaction_fetch_succeeded_accounts": [],
        }

        with (
            patch.object(scotiabank, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(
                scotiabank,
                "_sync_scotia_with_saved_artifacts",
                new=AsyncMock(return_value=payload),
            ) as sync_saved,
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            result = await scotiabank.try_headless_sync(1)

        self.assertEqual("ok", result["status"])
        sync_saved.assert_awaited_once()

    async def test_direct_network_exception_returns_network_error(self):
        with (
            patch.object(scotiabank, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(
                scotiabank,
                "_sync_scotia_with_saved_artifacts",
                new=AsyncMock(side_effect=socket.gaierror(-3, "Temporary failure in name resolution")),
            ),
            patch.object(scotiabank, "_scotia_log_event") as log_event,
        ):
            result = await scotiabank.try_headless_sync(1)

        self.assertEqual("network_error", result["status"])
        self.assertEqual("Connection failed", result["message"])
        log_event.assert_any_call(
            "headless sync network_error",
            user_id=1,
            engine="direct",
            level="warning",
            exception_type="gaierror",
            message="[Errno -3] Temporary failure in name resolution",
        )

    async def test_accounts_scope_skips_transaction_fetch(self):
        with (
            patch.object(
                scotiabank,
                "_fetch_scotia_summary_direct",
                new=AsyncMock(return_value=self._summary_payload()),
            ),
            patch.object(
                scotiabank,
                "fetch_transactions_direct",
                new=AsyncMock(),
            ) as fetch_transactions,
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            result = await scotiabank._sync_scotia_with_saved_artifacts(
                {"cookies": []},
                {"user_agent": "ua"},
                user_id=1,
                sync_scope="accounts",
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual({}, result["transactions"])
        fetch_transactions.assert_not_awaited()

    async def test_direct_get_retries_transient_connect_timeout(self):
        expected = {
            "status": 200,
            "url": scotiabank.SCOTIA_SUMMARY_URL,
            "payload": self._summary_payload(),
        }

        with (
            patch.object(
                scotiabank.SavedArtifactHttpClient,
                "request_once",
                new=AsyncMock(side_effect=[httpx.ConnectTimeout("timed out"), expected]),
            ) as request_once,
            patch.object(scotiabank.asyncio, "sleep", new=AsyncMock()) as sleep,
            patch.object(scotiabank, "_scotia_log_event") as log_event,
        ):
            result = await scotiabank._scotia_direct_get(
                {"cookies": []},
                scotiabank.SCOTIA_SUMMARY_URL,
                user_agent="ua",
                timeout_seconds=scotiabank.SCOTIA_SUMMARY_TIMEOUT_SECONDS,
            )

        self.assertEqual(expected, result)
        self.assertEqual(2, request_once.await_count)
        sleep.assert_awaited_once()
        log_event.assert_any_call(
            "direct get retry",
            level="warning",
            endpoint="secure.scotiabank.com/api/accounts/summary",
            attempt=1,
            max_attempts=scotiabank.SCOTIA_DIRECT_RETRY_ATTEMPTS,
            exception_type="ConnectTimeout",
            message="timed out",
        )

    async def test_summary_fetch_uses_fast_single_session_probe(self):
        with (
            patch.object(
                scotiabank,
                "_scotia_direct_get",
                new=AsyncMock(return_value={"status": 200, "payload": self._summary_payload()}),
            ) as direct_get,
        ):
            payload = await scotiabank._fetch_scotia_summary_direct(
                {"cookies": []},
                user_agent="ua",
                user_id=1,
            )

        self.assertEqual(self._summary_payload(), payload)
        direct_get.assert_awaited_once_with(
            {"cookies": []},
            scotiabank.SCOTIA_SUMMARY_URL,
            user_agent="ua",
            timeout_seconds=scotiabank.SCOTIA_SUMMARY_TIMEOUT_SECONDS,
            retry_attempts=scotiabank.SCOTIA_SUMMARY_RETRY_ATTEMPTS,
        )

    async def test_summary_auth_redirect_logs_structured_cause(self):
        response = {
            "status": 200,
            "url": "https://auth.scotiaonline.scotiabank.com/login?opaque=secret",
            "payload": None,
        }
        with (
            patch.object(scotiabank, "_scotia_direct_get", new=AsyncMock(return_value=response)),
            patch.object(scotiabank, "_scotia_log_event") as log_event,
        ):
            with self.assertRaises(scotiabank.ScotiaAuthRequired):
                await scotiabank._fetch_scotia_summary_direct(
                    {"cookies": []},
                    user_agent="ua",
                    user_id=1,
                )

        log_event.assert_called_once_with(
            "accounts api auth_required",
            user_id=1,
            level="warning",
            auth_signal="authentication_host_redirect",
            status_code=200,
            response_host="auth.scotiaonline.scotiabank.com",
            response_path="/login",
            payload_type="NoneType",
        )

    async def test_transaction_fetch_records_completed_window(self):
        events = []

        async def recorder(event):
            events.append(event)

        transaction = {
            "transactionKey": "txn-1",
            "transactionDate": "2026-01-05",
            "transactionAmount": {"amount": 12.34, "currencyCode": "CAD"},
            "transactionType": "DEBIT",
            "description": "Coffee",
        }

        with (
            patch.object(
                scotiabank,
                "_scotia_direct_get",
                new=AsyncMock(
                    return_value={
                        "status": 200,
                        "url": "https://secure.scotiabank.com/api/transactions/transaction-history/acct-1",
                        "payload": {"data": {"settled": [transaction]}},
                    }
                ),
            ),
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            transactions, succeeded, failed, network_failed = await scotiabank.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "name": "Chequing",
                        "external_id": "scotiabank:acct-1",
                        "encrypted_id": "acct-1",
                    }
                ],
                user_agent="ua",
                sync_window_per_account={
                    "scotiabank:acct-1": {
                        "mode": "backfill",
                        "pending_backfill_windows": [{"start": "2026-01-01", "end": "2026-01-31"}],
                    }
                },
                user_id=1,
                transaction_window_recorder=recorder,
            )

        self.assertEqual(["started", "completed"], [event["event"] for event in events])
        self.assertEqual([transaction], events[1]["transactions"])
        self.assertEqual(["txn-1"], [txn["transactionKey"] for txn in transactions["scotiabank:acct-1"]])
        self.assertEqual({"scotiabank:acct-1"}, succeeded)
        self.assertEqual(set(), failed)
        self.assertEqual(set(), network_failed)

    async def test_transaction_fetch_rejects_unexpected_payload_shape(self):
        events = []

        async def recorder(event):
            events.append(event)

        with (
            patch.object(
                scotiabank,
                "_scotia_direct_get",
                new=AsyncMock(
                    return_value={
                        "status": 200,
                        "url": "https://secure.scotiabank.com/api/transactions/transaction-history/acct-1",
                        "payload": {"data": {"notSettled": []}},
                    }
                ),
            ),
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            transactions, succeeded, failed, network_failed = await scotiabank.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "name": "Chequing",
                        "external_id": "scotiabank:acct-1",
                        "encrypted_id": "acct-1",
                    }
                ],
                user_agent="ua",
                sync_window_per_account={
                    "scotiabank:acct-1": {
                        "mode": "backfill",
                        "pending_backfill_windows": [{"start": "2026-01-01", "end": "2026-01-31"}],
                    }
                },
                user_id=1,
                transaction_window_recorder=recorder,
            )

        self.assertEqual(["started", "failed"], [event["event"] for event in events])
        self.assertEqual({}, transactions)
        self.assertEqual(set(), succeeded)
        self.assertEqual({"scotiabank:acct-1"}, failed)
        self.assertEqual(set(), network_failed)

    async def test_transaction_fetch_accepts_data_list_as_valid_empty_window(self):
        events = []

        async def recorder(event):
            events.append(event)

        with (
            patch.object(
                scotiabank,
                "_scotia_direct_get",
                new=AsyncMock(
                    return_value={
                        "status": 200,
                        "url": "https://secure.scotiabank.com/api/transactions/transaction-history/acct-1",
                        "payload": {"data": [], "notifications": []},
                    }
                ),
            ),
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            transactions, succeeded, failed, network_failed = await scotiabank.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "name": "ScotiaLine",
                        "external_id": "scotiabank:acct-1",
                        "encrypted_id": "acct-1",
                    }
                ],
                user_agent="ua",
                sync_window_per_account={
                    "scotiabank:acct-1": {
                        "mode": "backfill",
                        "pending_backfill_windows": [{"start": "2026-01-01", "end": "2026-01-31"}],
                    }
                },
                user_id=1,
                transaction_window_recorder=recorder,
            )

        self.assertEqual(["started", "completed"], [event["event"] for event in events])
        self.assertEqual([], events[1]["transactions"])
        self.assertEqual({}, transactions)
        self.assertEqual({"scotiabank:acct-1"}, succeeded)
        self.assertEqual(set(), failed)
        self.assertEqual(set(), network_failed)

    async def test_summary_read_timeout_with_reachable_auth_entry_returns_auth_required(self):
        with (
            patch.object(scotiabank, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(
                scotiabank,
                "_sync_scotia_with_saved_artifacts",
                new=AsyncMock(side_effect=httpx.ReadTimeout("timed out")),
            ),
            patch.object(
                scotiabank,
                "_scotia_auth_entry_reachable_after_timeout",
                new=AsyncMock(return_value=True),
            ) as auth_probe,
            patch.object(scotiabank, "_scotia_log_event") as log_event,
        ):
            result = await scotiabank.try_headless_sync(1)

        self.assertEqual("auth_required", result["status"])
        auth_probe.assert_awaited_once_with(user_agent="ua", user_id=1)
        log_event.assert_any_call(
            "headless sync auth_required",
            user_id=1,
            engine="direct",
            message="saved-session direct reuse timed out while Scotiabank auth entry was reachable",
        )

    async def test_summary_read_timeout_without_reachable_auth_entry_returns_network_error(self):
        with (
            patch.object(scotiabank, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(
                scotiabank,
                "_sync_scotia_with_saved_artifacts",
                new=AsyncMock(side_effect=httpx.ReadTimeout("timed out")),
            ),
            patch.object(
                scotiabank,
                "_scotia_auth_entry_reachable_after_timeout",
                new=AsyncMock(return_value=False),
            ),
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            result = await scotiabank.try_headless_sync(1)

        self.assertEqual("network_error", result["status"])
        self.assertEqual("Connection failed", result["message"])

    async def test_summary_connect_timeout_with_reachable_auth_entry_returns_auth_required(self):
        with (
            patch.object(scotiabank, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(
                scotiabank,
                "_sync_scotia_with_saved_artifacts",
                new=AsyncMock(side_effect=httpx.ConnectTimeout("timed out")),
            ),
            patch.object(
                scotiabank,
                "_scotia_auth_entry_reachable_after_timeout",
                new=AsyncMock(return_value=True),
            ) as auth_probe,
            patch.object(scotiabank, "_scotia_log_event"),
        ):
            result = await scotiabank.try_headless_sync(1)

        self.assertEqual("auth_required", result["status"])
        auth_probe.assert_awaited_once_with(user_agent="ua", user_id=1)


if __name__ == "__main__":
    unittest.main()
