from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date


class SyncWindowHelperTests(unittest.TestCase):
    def test_sync_window_date_parses_supported_values(self) -> None:
        timestamp = datetime(2026, 5, 17, 12, 30, tzinfo=timezone.utc)

        self.assertEqual(date(2026, 5, 17), sync_window_date({"value": timestamp}, "value"))
        self.assertEqual(
            date(2026, 5, 18),
            sync_window_date({"value": date(2026, 5, 18)}, "value"),
        )
        self.assertEqual(
            date(2026, 5, 19),
            sync_window_date({"value": "2026-05-19"}, "value"),
        )
        self.assertIsNone(sync_window_date({"value": ""}, "value"))
        self.assertIsNone(sync_window_date({"value": "2026-05-19T00:00:00Z"}, "value"))
        self.assertIsNone(sync_window_date({}, "missing"))

    def test_pending_backfill_date_windows_keeps_only_canonical_ordered_ranges(self) -> None:
        sync_window = {
            "pending_backfill_windows": [
                {"start": "2026-01-01", "end": "2026-01-31"},
                {"start_date": "2026-03-31", "end_date": "2026-03-01"},
                {"start": "bad", "end": "2026-04-01"},
                "not-a-window",
            ],
        }

        self.assertEqual(
            [
                (date(2026, 1, 1), date(2026, 1, 31)),
            ],
            pending_backfill_date_windows(sync_window),
        )

    def test_pending_backfill_date_windows_applies_optional_bounds(self) -> None:
        sync_window = {
            "pending_backfill_windows": [
                {"start": "2026-01-01", "end": "2026-06-30"},
                {"start": "2025-01-01", "end": "2025-02-01"},
            ],
        }

        self.assertEqual(
            [(date(2026, 2, 1), date(2026, 5, 31))],
            pending_backfill_date_windows(
                sync_window,
                min_date=date(2026, 2, 1),
                max_date=date(2026, 5, 31),
            ),
        )

    def test_pending_backfill_date_windows_ignores_non_window_payloads(self) -> None:
        self.assertEqual([], pending_backfill_date_windows(None))
        self.assertEqual([], pending_backfill_date_windows({"pending_backfill_windows": "bad"}))
        self.assertEqual([], pending_backfill_date_windows({"pending_backfill_windows": 1}))
