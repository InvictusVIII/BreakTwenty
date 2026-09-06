from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import market_strip
from app.services.market_strip import MARKET_STRIP_TILE_BY_ID, _build_tile_payload


class MarketStripPayloadTests(unittest.TestCase):
    def test_daily_payload_keeps_value_but_not_multiday_graph_points(self) -> None:
        tile = MARKET_STRIP_TILE_BY_ID["nasdaq"]
        payload = _build_tile_payload(
            tile,
            [
                {"date": "2026-06-29", "close": 100.0},
                {"date": "2026-06-30", "close": 102.0},
                {"date": "2026-07-01", "close": 101.0},
                {"date": "2026-07-02", "close": 105.0},
            ],
            source="polygon_massive",
            source_label="Polygon / Massive",
            provider_symbol="I:COMP",
        )

        self.assertEqual(payload["point_mode"], "daily")
        self.assertEqual(payload["points"], [])
        self.assertEqual(payload["reference_value"], 101.0)
        self.assertEqual(payload["change"], 4.0)
        self.assertAlmostEqual(payload["change_pct"], 3.9603960396)

    def test_keyless_daily_payload_models_one_session_from_previous_and_latest_eod(self) -> None:
        tile = MARKET_STRIP_TILE_BY_ID["nasdaq"]
        payload = _build_tile_payload(
            tile,
            [
                {"date": "2026-06-29", "close": 100.0},
                {"date": "2026-06-30", "close": 102.0},
                {"date": "2026-07-01", "close": 101.0},
                {"date": "2026-07-02", "close": 105.0},
            ],
            source="fred",
            source_label="FRED",
            provider_symbol="NASDAQCOM",
            model_daily_session_points=True,
        )

        self.assertEqual(payload["point_mode"], "daily")
        self.assertEqual(payload["points"], [101.0, 105.0])
        self.assertEqual(payload["reference_value"], 101.0)

    def test_snapshot_payload_uses_current_day_samples_only(self) -> None:
        tile = MARKET_STRIP_TILE_BY_ID["nasdaq"]
        payload = _build_tile_payload(
            tile,
            [
                {"date": "2026-07-01", "close": 101.0},
                {"date": "2026-07-02", "close": 105.0},
            ],
            source="polygon_massive",
            source_label="Polygon / Massive",
            provider_symbol="I:COMP",
            snapshot_series=[
                {"date": "2026-07-02T14:00:00+00:00", "close": 104.0},
                {"date": "2026-07-02T14:15:00+00:00", "close": 105.0},
            ],
        )

        self.assertEqual(payload["point_mode"], "snapshot")
        self.assertEqual(payload["points"], [104.0, 105.0])
        self.assertEqual(payload["reference_value"], 101.0)


class MarketStripProviderFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        market_strip._market_strip_intraday_denials.clear()
        market_strip._market_strip_provider_denials.clear()

    def tearDown(self) -> None:
        market_strip._market_strip_intraday_denials.clear()
        market_strip._market_strip_provider_denials.clear()

    async def test_fmp_429_backs_off_without_daily_fallback(self) -> None:
        request = httpx.Request("GET", "https://example.test/fmp")
        rate_limit_error = httpx.HTTPStatusError(
            "HTTP 429",
            request=request,
            response=httpx.Response(429, request=request),
        )
        fred_series = [
            {"date": "2026-08-20", "close": 100.0},
            {"date": "2026-08-21", "close": 101.0},
        ]
        tile = MARKET_STRIP_TILE_BY_ID["sp500"]

        with (
            patch.object(
                market_strip,
                "_fetch_provider_intraday_series",
                new=AsyncMock(side_effect=rate_limit_error),
            ) as fetch_intraday,
            patch.object(
                market_strip,
                "_fetch_provider_series",
                new=AsyncMock(return_value=[{"date": "2026-08-21", "close": 999.0}]),
            ) as fetch_daily,
            patch.object(
                market_strip,
                "_fetch_fred_series",
                new=AsyncMock(return_value=fred_series),
            ) as fetch_fred,
        ):
            payload = await market_strip._fetch_tile(
                None,
                object(),
                1,
                tile,
                "fmp",
                "FMP",
                "test-key",
                date(2026, 7, 8),
                date(2026, 8, 21),
                source="user",
                mode="user_key",
                symbol_key="sp500",
                now=datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc),
            )

        fetch_intraday.assert_awaited_once()
        fetch_daily.assert_not_awaited()
        fetch_fred.assert_awaited_once()
        self.assertEqual(payload["source"], "fred")

        with (
            patch.object(
                market_strip,
                "_fetch_provider_intraday_series",
                new=AsyncMock(return_value=[{"date": "2026-08-21T15:00:00Z", "close": 999.0}]),
            ) as fetch_intraday,
            patch.object(
                market_strip,
                "_fetch_provider_series",
                new=AsyncMock(return_value=[{"date": "2026-08-21", "close": 999.0}]),
            ) as fetch_daily,
            patch.object(
                market_strip,
                "_fetch_fred_series",
                new=AsyncMock(return_value=fred_series),
            ),
        ):
            payload = await market_strip._fetch_tile(
                None,
                object(),
                1,
                tile,
                "fmp",
                "FMP",
                "test-key",
                date(2026, 7, 8),
                date(2026, 8, 21),
                source="user",
                mode="user_key",
                symbol_key="sp500",
                now=datetime(2026, 8, 21, 15, 5, tzinfo=timezone.utc),
            )

        fetch_intraday.assert_not_awaited()
        fetch_daily.assert_not_awaited()
        self.assertEqual(payload["source"], "fred")

    async def test_fmp_provider_fetches_are_skipped_outside_market_hours(self) -> None:
        tile = MARKET_STRIP_TILE_BY_ID["sp500"]
        with (
            patch.object(
                market_strip,
                "_fetch_provider_intraday_series",
                new=AsyncMock(return_value=[{"date": "2026-08-22T02:00:00Z", "close": 999.0}]),
            ) as fetch_intraday,
            patch.object(
                market_strip,
                "_fetch_provider_series",
                new=AsyncMock(return_value=[{"date": "2026-08-21", "close": 999.0}]),
            ) as fetch_daily,
            patch.object(
                market_strip,
                "_fetch_fred_series",
                new=AsyncMock(return_value=[
                    {"date": "2026-08-20", "close": 100.0},
                    {"date": "2026-08-21", "close": 101.0},
                ]),
            ),
        ):
            payload = await market_strip._fetch_tile(
                None,
                object(),
                1,
                tile,
                "fmp",
                "FMP",
                "test-key",
                date(2026, 7, 8),
                date(2026, 8, 22),
                source="user",
                mode="user_key",
                symbol_key="sp500",
                now=datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc),
            )

        fetch_intraday.assert_not_awaited()
        fetch_daily.assert_not_awaited()
        self.assertEqual(payload["source"], "fred")


if __name__ == "__main__":
    unittest.main()
