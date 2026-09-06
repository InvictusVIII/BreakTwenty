from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.services.portfolio_performance import (
    _get_benchmark_prices,
    _benchmark_lookup_start_date,
    _benchmark_period_return,
    _cache_covers_range,
    _local_date,
    _merge_benchmark_returns,
)


class PortfolioPerformanceBenchmarkTests(unittest.TestCase):
    def test_local_date_accepts_calendar_date_rows(self) -> None:
        self.assertEqual(_local_date(date(2026, 8, 6), ZoneInfo("America/Toronto")), date(2026, 8, 6))

    def test_local_date_keeps_datetime_timezone_conversion(self) -> None:
        self.assertEqual(_local_date(datetime(2026, 8, 6, 1, 30), ZoneInfo("America/Toronto")), date(2026, 8, 5))

    def test_cache_covers_range_accepts_calendar_date_rows(self) -> None:
        prices = [
            {"date": date(2026, 8, 3), "close": 100.0},
            {"date": date(2026, 8, 6), "close": 102.0},
        ]

        self.assertTrue(_cache_covers_range(prices, date(2026, 8, 3), date(2026, 8, 6)))

    def test_benchmark_series_uses_prior_close_when_portfolio_starts_on_weekend(self) -> None:
        series = [
            {"date": "2026-05-16", "portfolio_return_pct": 0.0},
            {"date": "2026-05-17", "portfolio_return_pct": 0.4},
            {"date": "2026-05-18", "portfolio_return_pct": 0.5},
        ]
        benchmark_prices = {
            "sp500": [
                {"date": "2026-05-15", "close": 100.0},
                {"date": "2026-05-18", "close": 102.0},
            ],
        }

        merged = _merge_benchmark_returns(series, benchmark_prices)

        self.assertEqual(merged[0]["sp500_return_pct"], 0.0)
        self.assertEqual(merged[1]["sp500_return_pct"], 0.0)
        self.assertAlmostEqual(merged[2]["sp500_return_pct"], 2.0)
        self.assertAlmostEqual(_benchmark_period_return(benchmark_prices["sp500"], date(2026, 5, 16)), 2.0)

    def test_benchmark_series_prefers_exact_start_date_when_available(self) -> None:
        series = [
            {"date": "2026-05-18", "portfolio_return_pct": 0.0},
            {"date": "2026-05-19", "portfolio_return_pct": 0.5},
        ]
        benchmark_prices = {
            "sp500": [
                {"date": "2026-05-15", "close": 100.0},
                {"date": "2026-05-18", "close": 105.0},
                {"date": "2026-05-19", "close": 110.0},
            ],
        }

        merged = _merge_benchmark_returns(series, benchmark_prices)

        self.assertEqual(merged[0]["sp500_return_pct"], 0.0)
        self.assertAlmostEqual(merged[1]["sp500_return_pct"], (110.0 / 105.0 - 1.0) * 100.0)
        self.assertAlmostEqual(
            _benchmark_period_return(benchmark_prices["sp500"], date(2026, 5, 18)),
            (110.0 / 105.0 - 1.0) * 100.0,
        )

    def test_benchmark_lookup_start_pads_range_for_prior_market_close(self) -> None:
        self.assertEqual(_benchmark_lookup_start_date(date(2026, 5, 16)), date(2026, 5, 9))



class PortfolioPerformanceFetchTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.portfolio_performance._fetch_current_benchmark_price", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._fetch_fmp_benchmark_prices", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._upsert_benchmark_prices", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._read_cached_benchmark_prices", new_callable=AsyncMock)
    async def test_historical_backfill_is_persisted_before_current_quote_failure(
        self,
        read_prices,
        upsert_prices,
        fetch_history,
        fetch_current,
    ) -> None:
        db = object()
        stale_prices = [
            {"date": "2026-08-05", "close": 100.0},
            {"date": "2026-08-13", "close": 101.0},
        ]
        refreshed_prices = [
            {"date": "2026-08-05", "close": 100.0},
            {"date": "2026-09-03", "close": 105.0},
        ]
        read_prices.side_effect = [stale_prices, refreshed_prices]
        fetch_history.return_value = refreshed_prices
        fetch_current.side_effect = RuntimeError("current quote unavailable")

        prices, status = await _get_benchmark_prices(
            db,
            "fmp",
            "^GSPC",
            date(2026, 8, 5),
            date(2026, 9, 4),
            "key",
            date(2026, 9, 4),
        )

        self.assertEqual(prices, refreshed_prices)
        self.assertEqual(status, "current_quote_unavailable")
        fetch_history.assert_awaited_once()
        self.assertEqual(fetch_history.await_args.args[2:4], (date(2026, 8, 5), date(2026, 9, 3)))
        upsert_prices.assert_awaited_once_with(db, "fmp", "^GSPC", refreshed_prices)

    @patch("app.services.portfolio_performance._fetch_current_benchmark_price", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._fetch_fmp_benchmark_prices", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._upsert_benchmark_prices", new_callable=AsyncMock)
    @patch("app.services.portfolio_performance._read_cached_benchmark_prices", new_callable=AsyncMock)
    async def test_historical_backfill_expands_backward_to_earlier_portfolio_history(
        self,
        read_prices,
        upsert_prices,
        fetch_history,
        fetch_current,
    ) -> None:
        db = object()
        existing_prices = [
            {"date": "2024-01-02", "close": 150.0},
            {"date": "2026-09-03", "close": 190.0},
        ]
        expanded_prices = [
            {"date": "2020-10-23", "close": 100.0},
            {"date": "2026-09-03", "close": 190.0},
        ]
        final_prices = [*expanded_prices, {"date": "2026-09-04", "close": 191.0}]
        read_prices.side_effect = [existing_prices, expanded_prices, final_prices]
        fetch_history.return_value = expanded_prices
        fetch_current.return_value = 191.0

        prices, status = await _get_benchmark_prices(
            db,
            "fmp",
            "^GSPC",
            date(2020, 10, 23),
            date(2026, 9, 4),
            "key",
            date(2026, 9, 4),
        )

        self.assertEqual(prices, final_prices)
        self.assertEqual(status, "ok")
        self.assertEqual(fetch_history.await_args.args[2:4], (date(2020, 10, 23), date(2026, 9, 3)))
        self.assertEqual(upsert_prices.await_count, 2)


if __name__ == "__main__":
    unittest.main()
