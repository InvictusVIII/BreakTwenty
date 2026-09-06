from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.routes import CurrentUser, get_current_user
from app.database import async_session
from app.services.event_bus import (
    EVENT_SYNC_ACTIVITY,
    EVENT_SYNC_BATCH,
    EVENT_TRANSACTION_IMPORT_STATUS,
    Subscription,
    SubscriptionLimitError,
    consume_pending_dirty_topic,
    subscribe,
    unsubscribe,
)
from app.services.sync_activity import get_sync_activity_payload
from app.services.sync_batch import get_user_active_sync_batches
from app.services.transaction_import import get_transaction_import_status_payload

logger = logging.getLogger("breaktwenty.api.events")

router = APIRouter()

HEARTBEAT_SECONDS = 25.0


def _sse_event(record: dict[str, Any]) -> str:
    data = json.dumps(record, default=str)
    return f"data: {data}\n\n"


async def _build_sync_activity(user_id: int) -> dict[str, Any]:
    async with async_session() as db:
        return await get_sync_activity_payload(db, user_id)


async def _build_transaction_import_status(user_id: int) -> dict[str, Any]:
    async with async_session() as db:
        return await get_transaction_import_status_payload(db, user_id)


async def _emit_topic(user_id: int, topic: str) -> dict[str, Any] | None:
    if topic == EVENT_SYNC_ACTIVITY:
        return await _build_sync_activity(user_id)
    if topic == EVENT_TRANSACTION_IMPORT_STATUS:
        return await _build_transaction_import_status(user_id)
    return None


async def _stream_events(request: Request, sub: Subscription) -> AsyncIterator[str]:
    for topic in (EVENT_SYNC_ACTIVITY, EVENT_TRANSACTION_IMPORT_STATUS):
        try:
            payload = await _emit_topic(sub.user_id, topic)
        except Exception as exc:
            logger.warning("event stream initial snapshot failed topic=%s user_id=%s: %s", topic, sub.user_id, exc)
            continue
        if payload is None:
            continue
        yield _sse_event({"type": topic, "payload": payload})

    try:
        active_batches = await get_user_active_sync_batches(sub.user_id)
    except Exception as exc:
        logger.warning("event stream initial sync_batch snapshot failed user_id=%s: %s", sub.user_id, exc)
        active_batches = []
    for batch in active_batches:
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id:
            continue
        yield _sse_event({"type": EVENT_SYNC_BATCH, "payload": batch, "batch_id": batch_id})

    while True:
        if sub.closed or await request.is_disconnected():
            return
        try:
            item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            if sub.closed:
                return
            yield ": keepalive\n\n"
            continue

        kind = str(item.get("_kind") or "")
        if kind == "closed":
            return
        if kind == "dirty":
            topic = str(item.get("topic") or "")
            consume_pending_dirty_topic(sub, topic)
            try:
                payload = await _emit_topic(sub.user_id, topic)
            except Exception as exc:
                logger.warning("event stream rebuild failed topic=%s user_id=%s: %s", topic, sub.user_id, exc)
                continue
            if payload is None:
                continue
            yield _sse_event({"type": topic, "payload": payload})
        elif kind == "event":
            record = {k: v for k, v in item.items() if k != "_kind"}
            yield _sse_event(record)


@router.get("/events/stream")
async def events_stream(
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        sub = await subscribe(current_user.id)
    except SubscriptionLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc

    async def stream() -> AsyncIterator[str]:
        try:
            async for line in _stream_events(request, sub):
                yield line
        except asyncio.CancelledError:
            return
        finally:
            await unsubscribe(sub)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
