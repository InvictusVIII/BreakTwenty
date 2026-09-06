from __future__ import annotations

from datetime import date, timedelta
import unittest
from unittest.mock import AsyncMock, patch

import app.connectors.moomoo as moomoo_module
from app.connectors.moomoo import (
    MOOMOO_BACKFILL_DAYS,
    MOOMOO_CLOUD_HISTORY_CHUNK_DAYS,
    MoomooConnector,
    _account_home_currency,
    _convert_balance_currency,
    _normalize_cash_holdings,
    _normalize_deals,
    _normalize_positions,
)
from app.connectors.sync_modes import AccountSyncWindow
from app.connectors.types import SyncStatus
from app.services.moomoo_cloud import MoomooCloudAuthRequired, MoomooCloudTokens
from app.services.transaction_import import _split_windows


class MoomooConnectorNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_oauth_sync_fetches_multiple_accounts_holdings_and_fills(self) -> None:
        connector = MoomooConnector()
        requested_funds_currencies = []

        async def fake_get(path, _token, *, stage, params=None, client=None):
            if path.endswith("authorized_trd_accs"):
                return ({"accounts": [{
                        "account_id": "1234567890123456",
                        "account_card_number": "9999888877776666",
                        "security_firm": "FUTUCA",
                        "acc_type": "margin",
                        "enable_market": [2],
                    }, {
                        "account_id": "6543210987654321",
                        "account_card_number": "1111222233334444",
                        "security_firm": "FUTUCA",
                        "acc_type": "cash",
                        "enable_market": [12],
                    }]}, 200)
            if path.endswith("/funds"):
                requested_funds_currencies.append(params.get("currency"))
                total_assets = "225.50" if "6543210987654321" in path else "125.50"
                return ({"total_assets": total_assets, "us_cash": "25.50", "currency": "USD"}, 200)
            if path.endswith("/positions"):
                second_account = "6543210987654321" in path
                return ([{
                    "code": "CA.SHOP" if second_account else "US.AAPL",
                    "stock_name": "Shopify" if second_account else "Apple",
                    "qty": "1",
                    "market_val": "100",
                    "nominal_price": "100",
                    "currency": "USD",
                }], 200)
            raise AssertionError(f"unexpected cloud path {path} ({stage}, {params}, {client})")

        async def fake_history(**kwargs):
            second_account = kwargs["account_id"] == "6543210987654321"
            suffix = "2" if second_account else "1"
            if kwargs["endpoint"] == "orders_history":
                currency = "CAD" if second_account else "USD"
                return ([{"order_id": f"order-{suffix}", "currency": currency}], [{"page": 1, "rows": 1}])
            return ([{
                "deal_id": f"fill-{suffix}",
                "order_id": f"order-{suffix}",
                "code": "CA.SHOP" if second_account else "US.AAPL",
                "stock_name": "Shopify" if second_account else "Apple",
                "qty": "1",
                "price": "100",
                "trd_side": "BUY",
                "create_time": 1788282000000000,
                "status": "OK",
            }], [{"page": 1, "rows": 1}])

        async def fake_window(_user_id, _account_external_id):
            return AccountSyncWindow(
                mode="incremental",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 3),
                latest_persisted_date=None,
                last_successful_fetch_date=None,
                overlap_days=14,
            )

        connector._transaction_sync_window = fake_window
        session = {
            "schema": "breaktwenty.moomoo-cloud-oauth.v1",
            "auth_mode": "cloud_oauth",
            "client_id": "public-client",
            "refresh_token": "refresh-token",
            "scope": "trade:read accid:1234567890123456 accid:6543210987654321",
        }
        refreshed = MoomooCloudTokens(
            access_token="access-token",
            refresh_token="refresh-token",
            scope="trade:read accid:1234567890123456 accid:6543210987654321",
            expires_in=7200,
        )
        with (
            patch.object(moomoo_module, "load_connection_artifact_async", AsyncMock(return_value=session)),
            patch.object(moomoo_module, "refresh_moomoo_cloud_access_token", AsyncMock(return_value=refreshed)),
            patch.object(moomoo_module, "moomoo_cloud_get", AsyncMock(side_effect=fake_get)),
            patch.object(moomoo_module, "fetch_moomoo_cloud_history_pages", AsyncMock(side_effect=fake_history)),
            patch.object(moomoo_module, "get_fx_rates", AsyncMock(return_value={"CAD": 1.0, "USD": 0.8})),
        ):
            result = await connector.sync(1)

        self.assertEqual(result.status, SyncStatus.OK)
        self.assertEqual(len(result.accounts), 2)
        self.assertEqual(requested_funds_currencies, ["USD", "USD"])
        self.assertEqual({account.currency for account in result.accounts}, {"CAD"})
        self.assertEqual({account.balance for account in result.accounts}, {156.875, 281.875})
        self.assertEqual(len(result.holdings), 4)
        self.assertEqual(len(result.transactions), 2)
        self.assertEqual(
            {transaction.external_id for transaction in result.transactions},
            {"moomoo_fill-1", "moomoo_fill-2"},
        )
        self.assertEqual({transaction.currency for transaction in result.transactions}, {"USD", "CAD"})

    def test_account_home_currency_uses_security_firm(self) -> None:
        expected_by_firm = {
            "FUTUSECURITIES": "HKD",
            "FUTUINC": "USD",
            "FUTUSG": "SGD",
            "FUTUAU": "AUD",
            "FUTUCA": "CAD",
            "FUTUMY": "MYR",
            "FUTUJP": "JPY",
        }

        for security_firm, expected_currency in expected_by_firm.items():
            with self.subTest(security_firm=security_firm):
                self.assertEqual(
                    _account_home_currency({"security_firm": security_firm}, ["US", "CA"]),
                    expected_currency,
                )

        self.assertEqual(_account_home_currency({}, ["US"]), "USD")
        self.assertEqual(_account_home_currency({}, []), "CAD")

    def test_balance_conversion_uses_cross_rates_through_cad(self) -> None:
        rates = {"CAD": 1.0, "USD": 0.8, "HKD": 5.9}

        self.assertEqual(_convert_balance_currency(80, "USD", "CAD", rates), 100)
        self.assertEqual(_convert_balance_currency(80, "USD", "HKD", rates), 590)
        self.assertIsNone(_convert_balance_currency(80, "USD", "JPY", rates))

    def test_positions_normalize_prefixed_symbols_and_numeric_fields(self) -> None:
        holdings = _normalize_positions(
            [
                {
                    "code": "US.AAPL",
                    "stock_name": "Apple",
                    "qty": "2",
                    "average_cost": "150.25",
                    "market_val": "330.50",
                    "nominal_price": "165.25",
                    "pl_ratio": "0.1",
                    "today_pl_val": "4.25",
                    "currency": "USD",
                },
                {"code": "CA.CASH", "qty": "0"},
            ]
        )

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].symbol, "AAPL")
        self.assertEqual(holdings[0].quantity, 2)
        self.assertEqual(holdings[0].currency, "USD")

    def test_cash_holdings_use_currency_specific_cash_fields(self) -> None:
        holdings = _normalize_cash_holdings(
            {
                "ca_cash": "12.34",
                "us_cash": "-5.50",
                "hk_cash": "",
            }
        )

        self.assertEqual([holding.symbol for holding in holdings], ["CAD", "USD"])
        self.assertEqual([holding.market_value for holding in holdings], [12.34, -5.50])

    def test_deals_become_signed_buy_sell_transactions(self) -> None:
        transactions = _normalize_deals(
            [
                {
                    "deal_id": "d1",
                    "order_id": "o1",
                    "code": "US.MSFT",
                    "stock_name": "Microsoft",
                    "qty": "3",
                    "price": "10.00",
                    "trd_side": "BUY",
                    "create_time": "2026-05-01 14:30:00",
                    "status": "OK",
                },
                {
                    "deal_id": "d2",
                    "order_id": "o2",
                    "code": "CA.XEQT",
                    "stock_name": "XEQT",
                    "qty": "4",
                    "price": "20.00",
                    "trd_side": "SELL",
                    "create_time": "2026-05-02 14:30:00",
                    "status": "OK",
                },
            ],
            account_id=123,
            order_currency_by_id={"o1": "USD"},
            fallback_currency="CAD",
        )

        self.assertEqual([tx.type for tx in transactions], ["buy", "sell"])
        self.assertEqual([tx.amount for tx in transactions], [-30.0, 80.0])
        self.assertEqual([tx.currency for tx in transactions], ["USD", "CAD"])
        self.assertEqual([tx.symbol for tx in transactions], ["MSFT", "XEQT"])

    def test_cloud_history_uses_provider_proven_360_day_windows(self) -> None:
        self.assertEqual(MOOMOO_CLOUD_HISTORY_CHUNK_DAYS, 360)
        end_date = date(2026, 9, 3)
        windows = _split_windows(
            end_date - timedelta(days=MOOMOO_BACKFILL_DAYS),
            end_date,
            MOOMOO_CLOUD_HISTORY_CHUNK_DAYS,
        )
        self.assertEqual(len(windows), 11)

class MoomooConnectorAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_oauth_session_requires_reconnect_without_browser(self) -> None:
        connector = MoomooConnector()
        with patch.object(
            moomoo_module,
            "load_connection_artifact_async",
            AsyncMock(return_value=None),
        ):
            result = await connector.sync(1)

        self.assertEqual(result.status, SyncStatus.AUTH_REQUIRED)
        self.assertIn("Reconnect Moomoo", result.message or "")

    async def test_expired_oauth_session_requires_reconnect(self) -> None:
        connector = MoomooConnector()
        session = {
            "schema": "breaktwenty.moomoo-cloud-oauth.v1",
            "auth_mode": "cloud_oauth",
            "client_id": "public-client",
            "refresh_token": "expired-refresh-token",
            "scope": "trade:read accid:1234567890123456",
        }
        expired = MoomooCloudAuthRequired(
            "refresh rejected",
            stage="token_refresh",
            status_code=401,
            provider_code="invalid_grant",
        )
        with (
            patch.object(
                moomoo_module,
                "load_connection_artifact_async",
                AsyncMock(return_value=session),
            ),
            patch.object(
                moomoo_module,
                "refresh_moomoo_cloud_access_token",
                AsyncMock(side_effect=expired),
            ),
        ):
            result = await connector.sync(1)

        self.assertEqual(result.status, SyncStatus.AUTH_REQUIRED)
        self.assertIn("Reconnect Moomoo", result.message or "")


if __name__ == "__main__":
    unittest.main()
