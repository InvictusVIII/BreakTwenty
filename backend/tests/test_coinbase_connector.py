from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import app.connectors.coinbase as coinbase_module
from app.connectors.coinbase import (
    CoinbaseConnector,
    CoinbaseSdkProcessHandler,
    _get_coinbase_asset_name,
    _get_spot_price_cad,
    _get_usd_cad_rate,
    _parse_single_transaction,
    _parse_transactions,
)
from app.connectors.sync_context import connector_sync_context
from app.connectors.sync_modes import AccountSyncWindow


class FakeCoinbaseClient:
    def __init__(
        self,
        responses: dict[tuple[str, tuple[tuple[str, str], ...]], dict | Exception],
    ):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, path: str, params: dict[str, str] | None = None):
        resolved_params = params or {}
        self.calls.append((path, resolved_params))
        key = (path, tuple(sorted(resolved_params.items())))
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response


class InProcessCoinbaseWorker:
    def __init__(self, client) -> None:
        self._handler = CoinbaseSdkProcessHandler.__new__(CoinbaseSdkProcessHandler)
        self._handler._user_id = 1
        self._handler._client = client
        self.closed = False

    async def start(self) -> None:
        pass

    async def call(self, operation: str, payload: dict | None = None, *, timeout: float):
        return self._handler.handle(operation, payload or {})

    async def close(self) -> None:
        self.closed = True


class CoinbaseConnectorParsingTests(unittest.TestCase):
    def test_trade_with_negative_native_amount_maps_to_sell(self) -> None:
        parsed = _parse_single_transaction(
            {
                "id": "trade-1",
                "status": "completed",
                "type": "trade",
                "native_amount": {"amount": "-42.10", "currency": "CAD"},
                "amount": {"amount": "-0.5", "currency": "ETH"},
                "created_at": "2026-04-01T12:00:00Z",
            },
            "ETH",
            "ETH",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.type, "sell")
        self.assertEqual(parsed.amount, 42.10)
        self.assertEqual(parsed.quantity, 0.5)
        self.assertEqual(parsed.description, "Sold 0.5 ETH")

    def test_malformed_amount_is_skipped_without_raising(self) -> None:
        parsed = _parse_single_transaction(
            {
                "id": "bad-amount",
                "status": "completed",
                "type": "send",
                "native_amount": {"amount": "not-a-number", "currency": "CAD"},
                "amount": {"amount": "still-bad", "currency": "BTC"},
                "created_at": "2026-04-01T12:00:00Z",
            },
            "BTC",
            "BTC",
        )

        self.assertIsNone(parsed)

    def test_send_uses_crypto_amount_sign_when_native_amount_missing(self) -> None:
        parsed = _parse_single_transaction(
            {
                "id": "send-without-native",
                "status": "completed",
                "type": "send",
                "amount": {"amount": "-0.01", "currency": "BTC"},
                "created_at": "2026-04-01T12:00:00Z",
            },
            "BTC",
            "BTC",
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.type, "withdrawal")
        self.assertEqual(parsed.amount, -0.01)

    def test_fiat_buy_sell_leg_is_skipped(self) -> None:
        parsed = _parse_single_transaction(
            {
                "id": "fiat-leg",
                "status": "completed",
                "type": "buy",
                "native_amount": {"amount": "-25.00", "currency": "CAD"},
                "amount": {"amount": "-25.00", "currency": "USD"},
                "created_at": "2026-04-01T12:00:00Z",
            },
            "USD",
            "USD",
        )

        self.assertIsNone(parsed)

    def test_parse_transactions_paginates_accounts(self) -> None:
        responses = {
            (
                "/v2/accounts",
                (("limit", "100"),),
            ): {
                "data": [{"id": "btc-wallet", "currency": {"code": "BTC"}}],
                "pagination": {"next_uri": "/v2/accounts?starting_after=btc-wallet"},
            },
            (
                "/v2/accounts",
                (("starting_after", "btc-wallet"),),
            ): {
                "data": [{"id": "eth-wallet", "currency": {"code": "ETH"}}],
                "pagination": {},
            },
            (
                "/v2/accounts/btc-wallet/transactions",
                (("limit", "100"), ("order", "desc")),
            ): {
                "data": [
                    {
                        "id": "btc-receive",
                        "status": "completed",
                        "type": "receive",
                        "native_amount": {"amount": "100.00", "currency": "CAD"},
                        "amount": {"amount": "0.001", "currency": "BTC"},
                        "created_at": "2026-04-01T12:00:00Z",
                    }
                ],
                "pagination": {},
            },
            (
                "/v2/accounts/eth-wallet/transactions",
                (("limit", "100"), ("order", "desc")),
            ): {
                "data": [
                    {
                        "id": "eth-receive",
                        "status": "completed",
                        "type": "receive",
                        "native_amount": {"amount": "75.00", "currency": "CAD"},
                        "amount": {"amount": "0.02", "currency": "ETH"},
                        "created_at": "2026-04-02T12:00:00Z",
                    }
                ],
                "pagination": {},
            },
        }
        client = FakeCoinbaseClient(responses)

        transactions, succeeded = _parse_transactions(client)

        self.assertTrue(succeeded)
        self.assertEqual(
            [tx.external_id for tx in transactions],
            ["coinbase_btc-receive", "coinbase_eth-receive"],
        )
        self.assertIn(
            ("/v2/accounts", {"starting_after": "btc-wallet"}),
            client.calls,
        )

    def test_parse_transactions_reports_partial_wallet_failure(self) -> None:
        responses = {
            (
                "/v2/accounts",
                (("limit", "100"),),
            ): {
                "data": [{"id": "btc-wallet", "currency": {"code": "BTC"}}],
                "pagination": {},
            },
            (
                "/v2/accounts/btc-wallet/transactions",
                (("limit", "100"), ("order", "desc")),
            ): RuntimeError("coinbase v2 timeout"),
        }
        client = FakeCoinbaseClient(responses)

        transactions, succeeded = _parse_transactions(client)

        self.assertFalse(succeeded)
        self.assertEqual(transactions, [])

    def test_parse_transactions_filters_incremental_window_and_stops_old_pages(self) -> None:
        today = date.today()
        new_date = datetime.combine(
            today - timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        old_date = datetime.combine(
            today - timedelta(days=20),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        responses = {
            (
                "/v2/accounts",
                (("limit", "100"),),
            ): {
                "data": [{"id": "btc-wallet", "currency": {"code": "BTC"}}],
                "pagination": {},
            },
            (
                "/v2/accounts/btc-wallet/transactions",
                (("limit", "100"), ("order", "desc")),
            ): {
                "data": [
                    {
                        "id": "new-receive",
                        "status": "completed",
                        "type": "receive",
                        "native_amount": {"amount": "10.00", "currency": "CAD"},
                        "amount": {"amount": "0.001", "currency": "BTC"},
                        "created_at": new_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    {
                        "id": "old-receive",
                        "status": "completed",
                        "type": "receive",
                        "native_amount": {"amount": "5.00", "currency": "CAD"},
                        "amount": {"amount": "0.0005", "currency": "BTC"},
                        "created_at": old_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                ],
                "pagination": {
                    "next_uri": "/v2/accounts/btc-wallet/transactions?limit=100&order=desc&starting_after=old-receive"
                },
            },
            (
                "/v2/accounts/btc-wallet/transactions",
                (("limit", "100"), ("order", "desc"), ("starting_after", "old-receive")),
            ): {
                "data": [
                    {
                        "id": "older-receive",
                        "status": "completed",
                        "type": "receive",
                        "native_amount": {"amount": "2.00", "currency": "CAD"},
                        "amount": {"amount": "0.0002", "currency": "BTC"},
                        "created_at": (old_date - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                ],
                "pagination": {},
            },
        }
        client = FakeCoinbaseClient(responses)

        transactions, succeeded = _parse_transactions(
            client,
            sync_window=AccountSyncWindow(
                mode="incremental",
                start_date=today - timedelta(days=14),
                end_date=today,
                latest_persisted_date=today - timedelta(days=1),
                last_successful_fetch_date=None,
                overlap_days=14,
            ),
        )

        self.assertTrue(succeeded)
        self.assertEqual([tx.external_id for tx in transactions], ["coinbase_new-receive"])
        self.assertNotIn(
            (
                "/v2/accounts/btc-wallet/transactions",
                {"limit": "100", "order": "desc", "starting_after": "old-receive"},
            ),
            client.calls,
        )


class CoinbaseConnectorRuntimeTests(unittest.IsolatedAsyncioTestCase):
    class _FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    def _credential_patches(self, client):
        return (
            patch.object(
                coinbase_module,
                "async_session",
                return_value=self._FakeSessionContext(),
            ),
            patch.object(
                coinbase_module,
                "get_api_credentials",
                new=AsyncMock(
                    return_value={
                        "coinbase_api_key": "key",
                        "coinbase_api_secret": "secret",
                    }
                ),
            ),
            patch.object(
                CoinbaseConnector,
                "_create_sdk_worker",
                return_value=InProcessCoinbaseWorker(client),
            ),
        )

    async def test_fx_failure_has_no_fabricated_rate(self) -> None:
        with patch(
            "app.services.currency.get_fx_rates",
            new=AsyncMock(side_effect=RuntimeError("fx unavailable")),
        ):
            rate = await _get_usd_cad_rate(user_id=1)

        self.assertIsNone(rate)

    def test_spot_price_failure_is_not_normalized_to_zero(self) -> None:
        class FailedPriceClient:
            def get_product(self, product_id: str):
                raise RuntimeError("price unavailable")

        self.assertIsNone(
            _get_spot_price_cad(
                FailedPriceClient(),
                "BTC",
                1.4,
                user_id=1,
            )
        )

    def test_usdc_name_and_value_do_not_query_coinbase_products(self) -> None:
        class NoProductClient:
            def get_product(self, product_id: str):
                raise AssertionError(f"unexpected Coinbase product lookup: {product_id}")

        client = NoProductClient()

        self.assertEqual(_get_coinbase_asset_name(client, "USDC"), "USD Coin")
        self.assertEqual(_get_spot_price_cad(client, "USDC", 1.36), 1.36)

    async def test_usdc_holding_uses_usd_cad_conversion(self) -> None:
        class UsdcPortfolioClient:
            def __init__(self) -> None:
                self.product_calls: list[str] = []

            def get_accounts(self, *, limit: int):
                return {
                    "accounts": [
                        {
                            "name": "USDC Wallet",
                            "currency": "USDC",
                            "available_balance": {"value": "10"},
                        }
                    ]
                }

            def get_product(self, product_id: str):
                self.product_calls.append(product_id)
                raise AssertionError(f"unexpected Coinbase product lookup: {product_id}")

            def get_portfolios(self):
                return {"portfolios": []}

        client = UsdcPortfolioClient()
        session_patch, credentials_patch, client_patch = self._credential_patches(client)
        with (
            session_patch,
            credentials_patch,
            client_patch,
            patch.object(
                coinbase_module,
                "_get_usd_cad_rate",
                new=AsyncMock(return_value=1.25),
            ),
            patch(
                "app.services.symbol_names.get_cached_symbol_names",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.symbol_names.upsert_symbol_names",
                new=AsyncMock(),
            ),
            connector_sync_context(
                user_id=1,
                provider="coinbase",
                sync_scope="accounts",
            ),
        ):
            result = await CoinbaseConnector()._sync_impl(1)

        self.assertEqual(result.status.value, "ok")
        self.assertEqual(result.accounts[0].balance, 12.5)
        self.assertTrue(result.accounts[0].balance_authoritative)
        self.assertTrue(result.accounts[0].holdings_authoritative)
        self.assertEqual(len(result.holdings), 1)
        self.assertEqual(result.holdings[0].symbol, "USDC")
        self.assertEqual(result.holdings[0].name, "USD Coin")
        self.assertEqual(result.holdings[0].market_value, 12.5)
        self.assertEqual(client.product_calls, [])

    async def test_empty_coinbase_portfolio_is_authoritative_zero(self) -> None:
        class EmptyPortfolioClient:
            def get_accounts(self, *, limit: int):
                return {
                    "accounts": [
                        {
                            "name": "Bitcoin",
                            "currency": "BTC",
                            "available_balance": {"value": "0"},
                        }
                    ]
                }

        session_patch, credentials_patch, client_patch = self._credential_patches(
            EmptyPortfolioClient()
        )
        with (
            session_patch,
            credentials_patch,
            client_patch,
            connector_sync_context(
                user_id=1,
                provider="coinbase",
                sync_scope="accounts",
            ),
        ):
            result = await CoinbaseConnector()._sync_impl(1)

        self.assertEqual(result.status.value, "ok")
        self.assertEqual(len(result.accounts), 1)
        self.assertEqual(result.accounts[0].balance, 0.0)
        self.assertTrue(result.accounts[0].balance_authoritative)
        self.assertTrue(result.accounts[0].holdings_authoritative)
        self.assertEqual(result.holdings, [])

    async def test_fx_failure_does_not_replace_snapshot_with_zero(self) -> None:
        class UsdPortfolioClient:
            def get_accounts(self, *, limit: int):
                return {
                    "accounts": [
                        {
                            "name": "US Dollar",
                            "currency": "USD",
                            "available_balance": {"value": "10"},
                        }
                    ]
                }

        session_patch, credentials_patch, client_patch = self._credential_patches(
            UsdPortfolioClient()
        )
        with (
            session_patch,
            credentials_patch,
            client_patch,
            patch.object(
                coinbase_module,
                "_get_usd_cad_rate",
                new=AsyncMock(return_value=None),
            ),
            connector_sync_context(
                user_id=1,
                provider="coinbase",
                sync_scope="accounts",
            ),
        ):
            result = await CoinbaseConnector()._sync_impl(1)

        self.assertEqual(result.status.value, "network_error")
        self.assertEqual(result.accounts, [])


if __name__ == "__main__":
    unittest.main()
