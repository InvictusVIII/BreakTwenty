from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import AsyncMock, patch

from app.services.moomoo_cloud import (
    MoomooCloudHistoryRateLimiter,
    MoomooCloudAuthRequired,
    _request_json,
    date_range_microseconds,
    fetch_history_pages,
    granted_account_ids,
    markets_for_account,
    refresh_access_token,
    sanitize_scope,
    validate_read_scope,
)


class MoomooCloudTests(unittest.IsolatedAsyncioTestCase):
    def test_scope_and_market_normalization_never_exposes_account_grants(self) -> None:
        scope = "quote:read trade:read accid:123456 accid:789012"
        self.assertEqual(
            sanitize_scope(scope),
            [
                "quote:read",
                "trade:read",
                "accid:[authorized]",
                "accid:[authorized]",
            ],
        )
        self.assertEqual(granted_account_ids(scope), {"123456", "789012"})
        self.assertEqual(markets_for_account({"enable_market": [12, 2, 12, 999]}), ["CA", "US"])
        validate_read_scope(scope)
        with self.assertRaises(MoomooCloudAuthRequired):
            validate_read_scope("quote:read accid:123456")
        with self.assertRaises(MoomooCloudAuthRequired) as rejected_write:
            validate_read_scope("trade:read trade:write accid:123456")
        self.assertEqual(rejected_write.exception.provider_code, "trading_write_scope")

    def test_date_windows_use_documented_microsecond_timestamps(self) -> None:
        start, end = date_range_microseconds(date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(start, 1788220800000000)
        self.assertGreater(end, start)
        self.assertLess(end - start, 86_400_000_000)

    async def test_history_pagination_stops_only_on_completed_page(self) -> None:
        responses = [
            ({"order_fills": [{"deal_id": "one"}], "page_flag": "next", "completed": False}, 200),
            ({"order_fills": [{"deal_id": "two"}], "page_flag": "", "completed": True}, 200),
        ]
        limiter = MoomooCloudHistoryRateLimiter(interval_seconds=0)
        limiter.wait = AsyncMock()
        with patch(
            "app.services.moomoo_cloud.api_get",
            AsyncMock(side_effect=responses),
        ) as request:
            rows, pages = await fetch_history_pages(
                account_id="account/id",
                access_token="memory-only-token",
                endpoint="fills_history",
                rows_key="order_fills",
                market="US",
                start=date(2026, 9, 1),
                end=date(2026, 9, 3),
                page_size=50,
                client=object(),
                rate_limiter=limiter,
            )
        self.assertEqual([row["deal_id"] for row in rows], ["one", "two"])
        self.assertEqual(len(pages), 2)
        self.assertEqual(request.await_args_list[1].kwargs["params"]["page_flag"], "next")
        self.assertEqual(limiter.wait.await_count, 2)

    async def test_http_439_is_retried_as_rate_limiting(self) -> None:
        class Response:
            def __init__(self, status_code: int, payload: dict, headers: dict | None = None) -> None:
                self.status_code = status_code
                self._payload = payload
                self.headers = headers or {}

            def json(self):
                return self._payload

        client = AsyncMock()
        client.request.side_effect = [
            Response(439, {"error": {"message": "rate limited"}}),
            Response(200, {"s": "ok", "d": {"rows": []}}),
        ]
        with patch("app.services.moomoo_cloud.asyncio.sleep", AsyncMock()) as sleep:
            payload, status = await _request_json(
                "GET",
                "/api/v1.0/accounts/test/orders_history",
                stage="orders_history_US",
                access_token="memory-only-token",
                client=client,
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["s"], "ok")
        self.assertEqual(client.request.await_count, 2)
        sleep.assert_awaited_once_with(31.0)

    async def test_refresh_keeps_original_scope_when_provider_omits_unchanged_scope(self) -> None:
        with patch(
            "app.services.moomoo_cloud._request_json",
            AsyncMock(return_value=({
                "access_token": "new-access",
                "refresh_token": "same-refresh",
                "expires_in": 7200,
            }, 200)),
        ):
            tokens = await refresh_access_token(
                client_id="public-client",
                refresh_token="same-refresh",
                granted_scope="trade:read accid:123456",
            )
        self.assertEqual(tokens.scope, "trade:read accid:123456")


if __name__ == "__main__":
    unittest.main()
