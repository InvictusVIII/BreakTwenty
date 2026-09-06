from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import app.connectors.questrade as questrade_module
from app.connectors.questrade import QuestradeConnector
from app.connectors.sync_context import connector_sync_context
from app.connectors.sync_modes import AccountSyncWindow
from app.connectors.types import SyncStatus
from app.services.questrade import QuestradeTransportError


class QuestradeConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_qt_api_get = questrade_module.qt_api_get
        self._original_questrade_now = questrade_module._questrade_now
        self._original_get_authenticated_client = questrade_module.get_authenticated_client

    async def asyncTearDown(self) -> None:
        questrade_module.qt_api_get = self._original_qt_api_get
        questrade_module._questrade_now = self._original_questrade_now
        questrade_module.get_authenticated_client = self._original_get_authenticated_client

    async def test_incremental_activities_use_short_questrade_chunks(self) -> None:
        requested_endpoints: list[str] = []
        current = datetime(2026, 5, 14, 10, 16, 44, tzinfo=questrade_module.QUESTRADE_TIMEZONE)

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            requested_endpoints.append(endpoint)
            return {"activities": []}

        questrade_module.qt_api_get = fake_qt_api_get
        questrade_module._questrade_now = lambda: current

        transactions, succeeded = await QuestradeConnector()._fetch_activities(
            "token",
            "https://api03.iq.questrade.com/",
            "12345678",
            sync_window=AccountSyncWindow(
                mode="incremental",
                start_date=date(2026, 2, 28),
                end_date=date(2026, 5, 14),
                latest_persisted_date=date(2026, 3, 14),
                last_successful_fetch_date=None,
                overlap_days=14,
            ),
            user_id=1,
        )

        self.assertTrue(succeeded)
        self.assertEqual(transactions, [])
        self.assertGreaterEqual(len(requested_endpoints), 3)

        for endpoint in requested_endpoints:
            query = parse_qs(urlparse(endpoint).query)
            start_time = datetime.fromisoformat(query["startTime"][0])
            end_time = datetime.fromisoformat(query["endTime"][0])

            self.assertLessEqual(
                (end_time - start_time).total_seconds(),
                questrade_module.QUESTRADE_INCREMENTAL_ACTIVITY_CHUNK_DAYS * 24 * 60 * 60,
            )
            self.assertLessEqual(end_time, current)

    async def test_transaction_window_uses_questrade_incremental_overlap(self) -> None:
        captured_kwargs: dict = {}

        async def fake_plan_account_sync_window(*args, **kwargs) -> AccountSyncWindow:
            captured_kwargs.update(kwargs)
            return AccountSyncWindow(
                mode="incremental",
                start_date=date(2026, 5, 7),
                end_date=date(2026, 5, 14),
                latest_persisted_date=date(2026, 5, 13),
                last_successful_fetch_date=date(2026, 5, 13),
                overlap_days=kwargs["overlap_days"],
            )

        with patch.object(
            questrade_module,
            "plan_account_sync_window",
            new=fake_plan_account_sync_window,
        ):
            sync_window = await QuestradeConnector()._transaction_sync_window(
                1,
                "account:12345678",
            )

        self.assertEqual(
            questrade_module.QUESTRADE_INCREMENTAL_OVERLAP_DAYS,
            captured_kwargs["overlap_days"],
        )
        self.assertEqual(
            questrade_module.QUESTRADE_INCREMENTAL_OVERLAP_DAYS,
            sync_window.overlap_days,
        )

    async def test_backfill_activity_times_use_questrade_local_time(self) -> None:
        requested_endpoints: list[str] = []
        current = datetime(2026, 5, 14, 14, 16, 44, tzinfo=timezone.utc)

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            requested_endpoints.append(endpoint)
            if len(requested_endpoints) == 1:
                return {
                    "activities": [
                        {
                            "type": "Deposits",
                            "action": "CON",
                            "tradeDate": "2026-05-06T00:00:00.000000-04:00",
                            "settlementDate": "2026-05-06T00:00:00.000000-04:00",
                            "transactionDate": "2026-05-06T00:00:00.000000-04:00",
                            "netAmount": 5,
                            "currency": "CAD",
                            "symbol": "",
                            "quantity": 0,
                            "price": 0,
                            "commission": 0,
                            "description": "FHSA CONTRIBUTION",
                        }
                    ]
                }
            return {"activities": []}

        questrade_module.qt_api_get = fake_qt_api_get
        questrade_module._questrade_now = lambda: current

        _, succeeded = await QuestradeConnector()._fetch_activities(
            "token",
            "https://api03.iq.questrade.com/",
            "12345678",
            sync_window=AccountSyncWindow(
                mode="backfill",
                start_date=None,
                end_date=date(2026, 5, 14),
                latest_persisted_date=None,
                last_successful_fetch_date=None,
                overlap_days=14,
            ),
            user_id=1,
        )

        self.assertTrue(succeeded)
        self.assertGreaterEqual(len(requested_endpoints), 3)
        first_query = parse_qs(urlparse(requested_endpoints[0]).query)
        self.assertEqual(first_query["endTime"][0], "2026-05-14T10:16:44-04:00")

    async def test_activity_timeout_retries_with_fresh_api_server(self) -> None:
        requested_endpoints: list[str] = []
        refreshed = False

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            requested_endpoints.append(endpoint)
            if len(requested_endpoints) == 1:
                raise QuestradeTransportError("Connection failed - unable to reach Questrade API (ReadTimeout)")
            return {"activities": []}

        async def fake_get_authenticated_client(user_id: int) -> tuple[str, str]:
            nonlocal refreshed
            refreshed = True
            return "fresh-token", "https://api07.iq.questrade.com/"

        questrade_module.qt_api_get = fake_qt_api_get
        questrade_module.get_authenticated_client = fake_get_authenticated_client
        questrade_module._questrade_now = lambda: datetime(2026, 5, 14, 10, 16, 44, tzinfo=questrade_module.QUESTRADE_TIMEZONE)

        transactions, succeeded = await QuestradeConnector()._fetch_activities(
            "token",
            "https://api02.iq.questrade.com/",
            "12345678",
            sync_window=AccountSyncWindow(
                mode="incremental",
                start_date=date(2026, 5, 13),
                end_date=date(2026, 5, 14),
                latest_persisted_date=date(2026, 5, 13),
                last_successful_fetch_date=None,
                overlap_days=14,
            ),
            user_id=1,
        )

        self.assertTrue(refreshed)
        self.assertTrue(succeeded)
        self.assertEqual(transactions, [])
        self.assertEqual(2, len(requested_endpoints))

    async def test_incremental_activity_failure_stops_after_first_chunk(self) -> None:
        requested_endpoints: list[str] = []

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            requested_endpoints.append(endpoint)
            raise QuestradeTransportError("Connection failed - unable to reach Questrade API (ReadTimeout)")

        questrade_module.qt_api_get = fake_qt_api_get
        questrade_module._questrade_now = lambda: datetime(
            2026,
            5,
            14,
            10,
            16,
            44,
            tzinfo=questrade_module.QUESTRADE_TIMEZONE,
        )

        transactions, succeeded = await QuestradeConnector()._fetch_activities(
            "token",
            "https://api03.iq.questrade.com/",
            "12345678",
            sync_window=AccountSyncWindow(
                mode="incremental",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 14),
                latest_persisted_date=date(2026, 5, 13),
                last_successful_fetch_date=None,
                overlap_days=7,
            ),
        )

        self.assertFalse(succeeded)
        self.assertEqual(transactions, [])
        self.assertEqual(1, len(requested_endpoints))

    async def test_transaction_scope_uses_broad_durable_backfill_window(self) -> None:
        captured_kwargs: dict = {}
        requested_endpoints: list[str] = []

        async def fake_get_authenticated_client(user_id: int) -> tuple[str, str]:
            return "token", "https://api03.iq.questrade.com/"

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            requested_endpoints.append(endpoint)
            if endpoint == "accounts":
                return {
                    "accounts": [
                        {
                            "number": "12345678",
                            "status": "Active",
                            "type": "FHSA",
                        }
                    ]
                }
            if endpoint.endswith("/balances"):
                return {"combinedBalances": [{"currency": "CAD", "totalEquity": 100}], "perCurrencyBalances": []}
            if endpoint.endswith("/positions"):
                return {"positions": []}
            return {"activities": []}

        async def fake_fetch_and_persist_transaction_import_windows(**kwargs):
            captured_kwargs.update(kwargs)
            return [], True, {"mode": "backfill"}

        questrade_module.get_authenticated_client = fake_get_authenticated_client
        questrade_module.qt_api_get = fake_qt_api_get
        connector = QuestradeConnector()
        connector.fetch_and_persist_transaction_import_windows = fake_fetch_and_persist_transaction_import_windows

        with connector_sync_context(user_id=1, provider="questrade", sync_scope="transactions"):
            result = await connector._sync_impl(1)

        self.assertEqual(SyncStatus.OK, result.status)
        self.assertEqual(
            questrade_module.QUESTRADE_DURABLE_BACKFILL_CHUNK_DAYS,
            captured_kwargs["backfill_chunk_days"],
        )
        self.assertGreater(
            captured_kwargs["backfill_chunk_days"],
            questrade_module.QUESTRADE_ACTIVITY_CHUNK_DAYS,
        )
        self.assertEqual(
            questrade_module.QUESTRADE_INCREMENTAL_OVERLAP_DAYS,
            captured_kwargs["overlap_days"],
        )
        self.assertFalse(any(endpoint.endswith("/balances") for endpoint in requested_endpoints))
        self.assertFalse(any(endpoint.endswith("/positions") for endpoint in requested_endpoints))
        self.assertFalse(result.accounts[0].balance_authoritative)
        self.assertFalse(result.accounts[0].holdings_authoritative)

    async def test_balance_failure_is_not_treated_as_authoritative_zero(self) -> None:
        async def fake_get_authenticated_client(user_id: int) -> tuple[str, str]:
            return "token", "https://api03.iq.questrade.com/"

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            if endpoint == "accounts":
                return {
                    "accounts": [
                        {
                            "number": "12345678",
                            "status": "Active",
                            "type": "FHSA",
                        }
                    ]
                }
            if endpoint.endswith("/balances"):
                raise TimeoutError("balance request timed out")
            if endpoint.endswith("/positions"):
                return {"positions": []}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        questrade_module.get_authenticated_client = fake_get_authenticated_client
        questrade_module.qt_api_get = fake_qt_api_get

        with connector_sync_context(
            user_id=1,
            provider="questrade",
            sync_scope="accounts",
        ):
            result = await QuestradeConnector()._sync_impl(1)

        self.assertEqual(SyncStatus.NETWORK_ERROR, result.status)
        self.assertEqual([], result.accounts)

    async def test_incomplete_transaction_backfill_returns_network_error(self) -> None:
        async def fake_get_authenticated_client(user_id: int) -> tuple[str, str]:
            return "token", "https://api03.iq.questrade.com/"

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            if endpoint == "accounts":
                return {
                    "accounts": [
                        {
                            "number": "12345678",
                            "status": "Active",
                            "type": "FHSA",
                        }
                    ]
                }
            if endpoint.endswith("/balances"):
                return {"combinedBalances": [{"currency": "CAD", "totalEquity": 100}], "perCurrencyBalances": []}
            if endpoint.endswith("/positions"):
                return {"positions": []}
            return {"activities": []}

        async def fake_transaction_sync_window(user_id: int, account_external_id: str | None) -> AccountSyncWindow:
            return AccountSyncWindow(
                mode="backfill",
                start_date=None,
                end_date=date(2026, 5, 14),
                latest_persisted_date=None,
                last_successful_fetch_date=None,
                overlap_days=14,
            )

        async def fake_fetch_activities(*args, **kwargs):
            return [], False

        questrade_module.get_authenticated_client = fake_get_authenticated_client
        questrade_module.qt_api_get = fake_qt_api_get
        connector = QuestradeConnector()
        connector._transaction_sync_window = fake_transaction_sync_window
        connector._fetch_activities = fake_fetch_activities

        result = await connector._sync_impl(1)

        self.assertEqual(SyncStatus.NETWORK_ERROR, result.status)
        self.assertEqual([], result.transactions)

    async def test_bounded_backfill_window_does_not_early_stop_on_activity_gap(self) -> None:
        # A bounded durable yearly window must fetch every 30-day chunk even when a
        # >60-day activity gap sits between two active periods, so older transactions
        # in that year are never skipped. Regression guard: the in-window empty stop
        # must NOT apply when window_start is set (durable path).
        calls = {"n": 0}

        def deposit(date_iso: str, amount: float) -> dict:
            stamp = f"{date_iso}T00:00:00.000000-05:00"
            return {
                "type": "Deposits",
                "action": "CON",
                "tradeDate": stamp,
                "settlementDate": stamp,
                "transactionDate": stamp,
                "netAmount": amount,
                "currency": "CAD",
                "symbol": "",
                "quantity": 0,
                "price": 0,
                "commission": 0,
                "description": "FHSA CONTRIBUTION",
            }

        async def fake_qt_api_get(access_token: str, api_server: str, endpoint: str, **kwargs) -> dict:
            calls["n"] += 1
            # Activity in the newest chunk and again four chunks back (a ~120-day gap
            # the old 2-empty-chunk stop would have cut at); empty everywhere else.
            if calls["n"] == 1:
                return {"activities": [deposit("2025-12-15", 5)]}
            if calls["n"] == 5:
                return {"activities": [deposit("2025-08-15", 7)]}
            return {"activities": []}

        questrade_module.qt_api_get = fake_qt_api_get
        questrade_module._questrade_now = lambda: datetime(2026, 6, 18, 12, 0, 0, tzinfo=questrade_module.QUESTRADE_TIMEZONE)

        transactions, succeeded = await QuestradeConnector()._fetch_activities(
            "token",
            "https://api03.iq.questrade.com/",
            "12345678",
            sync_window=AccountSyncWindow(
                mode="backfill",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                latest_persisted_date=None,
                last_successful_fetch_date=None,
                overlap_days=14,
            ),
            user_id=1,
        )

        self.assertTrue(succeeded)
        # The older (5th-chunk) deposit must be captured — the walk did not stop at
        # the 2-empty-chunk gap — and the full bounded year was walked.
        self.assertEqual(2, len(transactions))
        self.assertGreaterEqual(calls["n"], 12)


if __name__ == "__main__":
    unittest.main()
