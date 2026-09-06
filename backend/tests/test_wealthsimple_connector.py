from __future__ import annotations

import unittest
from datetime import date, timedelta

import app.connectors.wealthsimple as wealthsimple_module
from app.connectors.sync_context import connector_sync_context
from app.connectors.sync_modes import AccountSyncWindow
from app.connectors.wealthsimple import WealthsimpleConnector


class FakeWealthsimpleClient:
    def __init__(self) -> None:
        self.activity_calls: list[dict] = []

    def get_accounts(self) -> list[dict]:
        return [
            {
                "id": "ws-account-1",
                "status": "open",
                "nickname": "TFSA",
                "currency": "CAD",
                "unifiedAccountType": "ca_tfsa",
                "financials": {
                    "currentCombined": {
                        "netLiquidationValue": {"amount": 1000}
                    }
                },
            },
            {
                "id": "ws-account-2",
                "status": "open",
                "nickname": "RRSP",
                "currency": "CAD",
                "unifiedAccountType": "ca_rrsp",
                "financials": {
                    "currentCombined": {
                        "netLiquidationValue": {"amount": 2000}
                    }
                },
            },
        ]

    def get_activities(self, **kwargs) -> list[dict]:
        self.activity_calls.append(kwargs)
        return []

    def get_account_historical_financials(self, *args, **kwargs) -> list[dict]:
        return []

    def get_token_info(self) -> dict:
        return {"identity_canonical_id": "identity-1"}

    def do_graphql_query(self, *args, **kwargs) -> list[dict]:
        return []


class InProcessWealthsimpleWorker:
    def __init__(self, client) -> None:
        self.client = client

    async def start(self) -> None:
        pass

    async def call(self, operation: str, payload: dict | None = None, *, timeout: float):
        payload = payload or {}
        if operation == "accounts":
            value = self.client.get_accounts()
        elif operation == "historical_financials":
            value = self.client.get_account_historical_financials(
                payload["account_id"],
                currency=payload["currency"],
                resolution=payload["resolution"],
            )
        elif operation == "activities":
            value = self.client.get_activities(
                account_id=payload["account_id"],
                start_date=payload["start_date"],
                end_date=payload["end_date"],
                load_all=True,
            )
        elif operation == "positions":
            value = self.client.do_graphql_query()
        else:
            raise ValueError(operation)
        return {"value": value, "refreshed_sessions": []}

    async def close(self) -> None:
        pass


class WealthsimpleConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_load_session_async = wealthsimple_module.load_session_async
        self._original_create_worker = WealthsimpleConnector._create_sdk_worker
        self._original_window = WealthsimpleConnector._transaction_sync_window

    async def asyncTearDown(self) -> None:
        wealthsimple_module.load_session_async = self._original_load_session_async
        WealthsimpleConnector._create_sdk_worker = self._original_create_worker
        WealthsimpleConnector._transaction_sync_window = self._original_window

    async def test_activities_are_requested_per_account_with_planned_incremental_window(self) -> None:
        fake_client = FakeWealthsimpleClient()
        today = date.today()

        async def fake_load_session(user_id: int) -> dict:
            return {"client_id": "client"}

        wealthsimple_module.load_session_async = fake_load_session
        WealthsimpleConnector._create_sdk_worker = (
            lambda self, user_id, session_data: InProcessWealthsimpleWorker(fake_client)
        )

        async def fake_window(self, user_id: int, account_external_id: str | None) -> AccountSyncWindow:
            return AccountSyncWindow(
                mode="incremental",
                start_date=today - timedelta(days=30),
                end_date=today,
                latest_persisted_date=today - timedelta(days=16),
                last_successful_fetch_date=None,
                overlap_days=14,
            )

        WealthsimpleConnector._transaction_sync_window = fake_window

        result = await WealthsimpleConnector().sync(1)

        self.assertEqual(result.status.value, "ok")
        self.assertEqual(len(fake_client.activity_calls), 2)
        self.assertEqual(
            {call["account_id"] for call in fake_client.activity_calls},
            {"ws-account-1", "ws-account-2"},
        )
        self.assertTrue(all(call["start_date"] is not None for call in fake_client.activity_calls))
        self.assertTrue(all(call["end_date"] is not None for call in fake_client.activity_calls))
        self.assertEqual(len(result.transaction_fetch_succeeded_accounts), 2)
        self.assertTrue(all(account.balance_authoritative for account in result.accounts))
        self.assertTrue(all(account.holdings_authoritative for account in result.accounts))

    async def test_incomplete_positions_are_not_treated_as_authoritative_snapshot(self) -> None:
        class IncompletePositionsClient(FakeWealthsimpleClient):
            def do_graphql_query(self, *args, **kwargs):
                return [
                    {
                        "node": {
                            "quantity": 1,
                            "accounts": [{"id": "ws-account-1"}],
                            "security": {
                                "currency": "CAD",
                                "stock": {"symbol": "XEQT", "name": "XEQT"},
                            },
                        }
                    }
                ]

        fake_client = IncompletePositionsClient()
        async def fake_load_session(user_id: int) -> dict:
            return {"client_id": "client"}

        wealthsimple_module.load_session_async = fake_load_session
        WealthsimpleConnector._create_sdk_worker = (
            lambda self, user_id, session_data: InProcessWealthsimpleWorker(fake_client)
        )

        with connector_sync_context(
            user_id=1,
            provider="wealthsimple",
            sync_scope="accounts",
        ):
            result = await WealthsimpleConnector().sync(1)

        self.assertEqual(result.status.value, "network_error")
        self.assertEqual(result.accounts, [])

if __name__ == "__main__":
    unittest.main()
