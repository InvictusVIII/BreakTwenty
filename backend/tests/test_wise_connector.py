from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from app.connectors.sync_modes import AccountSyncWindow
from app.connectors.wise import WiseConnector


class FakeWiseResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"transactions": []}


class FakeWiseClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(kwargs.get("params", {}))
        return FakeWiseResponse()


class _TxResponse:
    def __init__(self, transactions: list[dict]) -> None:
        self._transactions = transactions

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"transactions": self._transactions}


def _wise_deposit(ref: str, date_iso: str) -> dict:
    return {
        "referenceNumber": ref,
        "type": "CREDIT",
        "details": {"type": "TRANSFER", "description": "test"},
        "amount": {"value": 10},
        "date": date_iso,
    }


class GapWiseClient:
    """Returns activity in the 1st and 4th chunk, empty in between."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(kwargs.get("params", {}))
        n = len(self.calls)
        if n == 1:
            return _TxResponse([_wise_deposit("REF1", "2025-06-15")])
        if n == 4:
            return _TxResponse([_wise_deposit("REF4", "2023-06-15")])
        return _TxResponse([])


class WiseConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_statement_window_is_split_into_provider_safe_chunks(self) -> None:
        client = FakeWiseClient()
        today = date.today()

        transactions, succeeded = await WiseConnector()._fetch_balance_transactions(
            client,
            {"Authorization": "Bearer token"},
            123,
            {"id": "balance-1", "currency": "CAD"},
            sync_window=AccountSyncWindow(
                mode="incremental",
                start_date=today - timedelta(days=920),
                end_date=today,
                latest_persisted_date=today - timedelta(days=900),
                last_successful_fetch_date=None,
                overlap_days=20,
            ),
            user_id=1,
        )

        self.assertTrue(succeeded)
        self.assertEqual(transactions, [])
        self.assertGreaterEqual(len(client.calls), 2)

        for params in client.calls:
            interval_start = datetime.fromisoformat(
                params["intervalStart"].replace("Z", "+00:00")
            )
            interval_end = datetime.fromisoformat(
                params["intervalEnd"].replace("Z", "+00:00")
            )
            self.assertLessEqual(
                (interval_end - interval_start).total_seconds(),
                460 * 24 * 60 * 60,
            )

    async def test_bounded_backfill_window_does_not_early_stop_on_activity_gap(self) -> None:
        # A bounded durable backfill window must walk every chunk even across a
        # multi-chunk activity gap, so older statements are never skipped. Regression
        # guard: the in-window empty stop must NOT apply when window_start is set.
        client = GapWiseClient()
        today = date.today()

        transactions, succeeded = await WiseConnector()._fetch_balance_transactions(
            client,
            {"Authorization": "Bearer token"},
            123,
            {"id": "balance-1", "currency": "CAD"},
            sync_window=AccountSyncWindow(
                mode="backfill",
                start_date=today - timedelta(days=2000),
                end_date=today,
                latest_persisted_date=None,
                last_successful_fetch_date=None,
                overlap_days=20,
            ),
            user_id=1,
        )

        self.assertTrue(succeeded)
        # Activity in chunk 1 and chunk 4 (two empty chunks between) — the old
        # 2-empty-chunk stop would have cut at chunk 3 and missed REF4.
        self.assertEqual(2, len(transactions))
        self.assertGreaterEqual(len(client.calls), 4)


if __name__ == "__main__":
    unittest.main()
