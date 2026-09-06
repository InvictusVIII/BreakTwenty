from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes import events as events_route
from app.auth import CurrentUser
from app.services.event_bus import Subscription, SubscriptionLimitError


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class EventStreamLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_returns_429_with_retry_after_when_subscription_is_capped(self) -> None:
        with patch.object(
            events_route,
            "subscribe",
            new=AsyncMock(
                side_effect=SubscriptionLimitError("Too many event streams for this user")
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await events_route.events_stream(_Request(), CurrentUser(id=41))

        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.headers, {"Retry-After": "5"})
        self.assertEqual(caught.exception.detail, "Too many event streams for this user")

    async def test_stream_completion_always_unsubscribes(self) -> None:
        subscription = Subscription(user_id=41)

        async def finite_stream(request, sub):
            self.assertIs(sub, subscription)
            yield "data: {}\n\n"

        with (
            patch.object(events_route, "subscribe", new=AsyncMock(return_value=subscription)),
            patch.object(events_route, "unsubscribe", new=AsyncMock()) as unsubscribe,
            patch.object(events_route, "_stream_events", new=finite_stream),
        ):
            response = await events_route.events_stream(_Request(), CurrentUser(id=41))
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(chunks, ["data: {}\n\n"])
        unsubscribe.assert_awaited_once_with(subscription)


if __name__ == "__main__":
    unittest.main()
