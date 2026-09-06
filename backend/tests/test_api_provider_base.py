from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.sync_context import connector_sync_context
from app.connectors.types import NormalizedAccount, SyncResult, SyncStatus


class DummyApiConnector(ApiProviderConnectorBase):
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        super().__init__(provider="dummy_api", display_name="Dummy API")
        self.result = result
        self.exc = exc

    async def _sync_impl(self, user_id: int) -> SyncResult:
        if self.exc:
            raise self.exc
        return self.result


class MissingCredentialsConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="missing_api", display_name="Missing API")

    async def _sync_impl(self, user_id: int) -> SyncResult:
        return self.missing_credentials_result(
            user_id=user_id,
            message="Missing API token.",
        )


class ApiProviderConnectorBaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_transaction_sync_window_uses_provider_planner(self) -> None:
        db = object()
        session_context = AsyncMock()
        session_context.__aenter__.return_value = db
        planned_window = object()
        with (
            patch("app.database.async_session", return_value=session_context),
            patch(
                "app.connectors.sync_modes.plan_account_sync_window",
                new=AsyncMock(return_value=planned_window),
            ) as planner,
        ):
            result = await DummyApiConnector()._transaction_sync_window(7, None)

        self.assertIs(result, planned_window)
        planner.assert_awaited_once_with(db, 7, "", provider="dummy_api")

    async def test_missing_credentials_maps_to_auth_required(self) -> None:
        result = await MissingCredentialsConnector().sync(1)

        self.assertEqual(result.status, SyncStatus.AUTH_REQUIRED)
        self.assertEqual(result.message, "Missing API token.")

    async def test_network_exception_maps_to_network_error(self) -> None:
        result = await DummyApiConnector(exc=httpx.ConnectError("dns failed")).sync(1)

        self.assertEqual(result.status, SyncStatus.NETWORK_ERROR)
        self.assertEqual(result.message, "Connection failed")

    async def test_mapping_result_is_normalized(self) -> None:
        account = NormalizedAccount(name="Example", account_type="chequing")

        result = await DummyApiConnector(
            result={
                "status": "ok",
                "accounts": [account],
                "transaction_fetch_succeeded_accounts": ["Example"],
            }
        ).sync(1)

        self.assertEqual(result.status, SyncStatus.OK)
        self.assertEqual(result.accounts, [account])
        self.assertEqual(result.transaction_fetch_succeeded_accounts, {"Example"})

    async def test_replay_diagnostics_receive_visible_auth_attempt_identity(self) -> None:
        connector = DummyApiConnector(result={"status": "ok", "accounts": []})
        with (
            connector_sync_context(
                user_id=1,
                provider="dummy_api",
                sync_id="sync-one",
                attempt_id="attempt-one",
            ),
            patch("app.connectors.api_base.begin_replay_diagnostics", return_value=None) as begin,
        ):
            await connector.sync(1)

        begin.assert_called_once_with(
            "dummy_api",
            1,
            sync_id="sync-one",
            attempt_id="attempt-one",
        )

    async def test_unexpected_result_maps_to_error(self) -> None:
        result = await DummyApiConnector(result=None).sync(1)

        self.assertEqual(result.status, SyncStatus.ERROR)
        self.assertIn("unexpected sync result", result.message or "")

    async def test_exception_result_redacts_secrets_before_returning(self) -> None:
        result = await DummyApiConnector(
            exc=RuntimeError("request failed password=provider-secret"),
        ).sync(1)

        self.assertEqual(result.status, SyncStatus.ERROR)
        self.assertNotIn("provider-secret", result.message or "")
        self.assertIn("password=<redacted>", result.message or "")


if __name__ == "__main__":
    unittest.main()
