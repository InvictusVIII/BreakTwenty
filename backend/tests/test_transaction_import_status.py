import unittest
from datetime import date, datetime
from types import SimpleNamespace

from app.services.transaction_import import _latest_completed_window_range


class TransactionImportStatusTests(unittest.TestCase):
    def test_latest_completed_range_includes_backfill_windows(self):
        earlier_incremental = SimpleNamespace(
            sync_id="incremental-sync",
            status="complete",
            window_start_date=date(2026, 5, 23),
            window_end_date=date(2026, 6, 6),
            completed_at=datetime(2026, 6, 7),
            updated_at=datetime(2026, 6, 7),
        )
        latest_backfill = SimpleNamespace(
            sync_id="backfill-sync",
            status="complete",
            window_start_date=date(2026, 7, 10),
            window_end_date=date(2026, 7, 10),
            completed_at=datetime(2026, 7, 10),
            updated_at=datetime(2026, 7, 10),
        )

        self.assertEqual(
            ("2026-07-10", "2026-07-10"),
            _latest_completed_window_range([earlier_incremental, latest_backfill]),
        )
