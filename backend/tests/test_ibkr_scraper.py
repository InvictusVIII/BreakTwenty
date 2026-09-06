from __future__ import annotations

import unittest

import httpx

from app.connectors.ibkr_scraper import IBKRScraperConnector
from app.connectors.types import SyncStatus
from app.scrapers.browser_session import (
    seed_storage_state_cookies,
    storage_state_with_client_cookies,
)


class IBKRScraperConnectorTests(unittest.TestCase):
    def test_string_numeric_holdings_do_not_break_normalization(self) -> None:
        result = IBKRScraperConnector()._build_success_result(
            [
                {
                    "acct_id": "U123",
                    "name": "IBKR Test",
                    "balance": "1000.50",
                    "currency": "CAD",
                    "positions": [
                        {
                            "contractDesc": "ABC",
                            "position": "4",
                            "mktValue": "80.25",
                            "avgCost": "10.5",
                            "currency": "USD",
                        }
                    ],
                    "ledger_by_currency": {
                        "BASE": {"cashbalance": "0"},
                        "USD": {"cashbalance": "12.34"},
                    },
                }
            ]
        )

        self.assertEqual(result.status, SyncStatus.OK)
        self.assertEqual(result.accounts[0].balance, 1000.50)
        self.assertEqual(result.holdings[0].market_value, 80.25)
        self.assertEqual(result.holdings[0].average_cost, 42.0)
        self.assertEqual(result.holdings[1].symbol, "USD")
        self.assertEqual(result.holdings[1].market_value, 12.34)

    def test_non_finite_position_numbers_are_ignored(self) -> None:
        result = IBKRScraperConnector()._build_success_result(
            [
                {
                    "acct_id": "U123",
                    "name": "IBKR Test",
                    "balance": "NaN",
                    "positions": [
                        {
                            "contractDesc": "ABC",
                            "position": "Infinity",
                            "mktValue": "NaN",
                            "avgCost": "NaN",
                        }
                    ],
                    "ledger_by_currency": {"USD": {"cashbalance": "Infinity"}},
                }
            ]
        )

        self.assertIsNone(result.accounts[0].balance)
        self.assertFalse(result.accounts[0].balance_authoritative)
        self.assertFalse(result.accounts[0].holdings_authoritative)
        self.assertEqual(result.holdings, [])

    def test_standard_auth_required_payload_maps_to_auth_required(self) -> None:
        result = IBKRScraperConnector()._saved_artifact_payload_to_result(
            1,
            {
                "status": "auth_required",
                "accounts": [],
                "holdings": {},
                "transactions": {},
                "message": None,
            },
        )

        self.assertEqual(result.status, SyncStatus.AUTH_REQUIRED)


class IBKRScraperRuntimeStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshed_storage_state_drops_client_removed_cookies(self) -> None:
        storage_state = {
            "cookies": [
                {"name": "fresh", "value": "1", "domain": ".interactivebrokers.com", "path": "/"},
                {"name": "stale", "value": "1", "domain": ".interactivebrokers.com", "path": "/"},
            ],
            "origins": [],
        }

        async with httpx.AsyncClient() as client:
            seed_storage_state_cookies(client, storage_state)
            stale_cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "stale")
            client.cookies.jar.clear(stale_cookie.domain, stale_cookie.path, stale_cookie.name)
            updated = storage_state_with_client_cookies(storage_state, client)

        self.assertEqual(
            {cookie["name"] for cookie in updated["cookies"]},
            {"fresh"},
        )


if __name__ == "__main__":
    unittest.main()
