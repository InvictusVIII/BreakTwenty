from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, TYPE_CHECKING

from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import EventStreamLease
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.task_supervisor import create_tracked_task

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("breaktwenty.event_bus")

SSE_QUEUE_MAXSIZE = 128
SSE_MAX_SUBSCRIPTIONS_PER_USER = 4
SSE_MAX_SUBSCRIPTIONS_GLOBAL = 128
SSE_LEASE_DURATION = timedelta(seconds=90)
SSE_LEASE_RENEW_INTERVAL_SECONDS = 30.0
SSE_LEASE_RENEW_ERROR_RETRY_SECONDS = 2.0
SSE_LEASE_ACQUIRE_ATTEMPTS = 8

EVENT_SYNC_ACTIVITY = "sync_activity"
EVENT_TRANSACTION_IMPORT_STATUS = "transaction_import_status"
EVENT_SYNC_BATCH = "sync_batch"


@dataclass(eq=False)
class Subscription:
    user_id: int
    lease_id: str = ""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE))
    closed: bool = False
    lease_released: bool = False
    renewal_task: asyncio.Task[Any] | None = None
    cleanup_task: asyncio.Task[Any] | None = None
    pending_dirty_topics: set[str] = field(default_factory=set)
    dirty_lock: Lock = field(default_factory=Lock)


_subscriptions: dict[int, set[Subscription]] = {}
_subscriptions_lock = Lock()


class SubscriptionLimitError(RuntimeError):
    pass


async def _acquire_event_stream_lease(user_id: int) -> str:
    normalized_user_id = int(user_id)
    last_limit_message = "Event stream capacity has been reached"
    for _attempt in range(SSE_LEASE_ACQUIRE_ATTEMPTS):
        lease_id = secrets.token_hex(24)
        now = datetime.now(timezone.utc)
        expires_at = now + SSE_LEASE_DURATION
        try:
            async with async_session() as db, sqlite_write_gate(), db.begin():
                await db.execute(
                    delete(EventStreamLease).where(EventStreamLease.expires_at <= now)
                )
                rows = (
                    await db.execute(
                        select(
                            EventStreamLease.user_id,
                            EventStreamLease.user_slot,
                            EventStreamLease.global_slot,
                        )
                    )
                ).all()
                user_slots = {
                    int(user_slot)
                    for lease_user_id, user_slot, _global_slot in rows
                    if int(lease_user_id) == normalized_user_id
                }
                global_slots = {int(global_slot) for _user_id, _user_slot, global_slot in rows}
                user_slot = next(
                    (
                        slot
                        for slot in range(1, SSE_MAX_SUBSCRIPTIONS_PER_USER + 1)
                        if slot not in user_slots
                    ),
                    None,
                )
                if user_slot is None:
                    raise SubscriptionLimitError("Too many event streams for this user")
                global_slot = next(
                    (
                        slot
                        for slot in range(1, SSE_MAX_SUBSCRIPTIONS_GLOBAL + 1)
                        if slot not in global_slots
                    ),
                    None,
                )
                if global_slot is None:
                    raise SubscriptionLimitError(last_limit_message)
                db.add(
                    EventStreamLease(
                        lease_id=lease_id,
                        user_id=normalized_user_id,
                        user_slot=user_slot,
                        global_slot=global_slot,
                        acquired_at=now,
                        expires_at=expires_at,
                    )
                )
                await db.flush()
            return lease_id
        except SubscriptionLimitError:
            raise
        except IntegrityError:
            continue
    raise SubscriptionLimitError(last_limit_message)


async def _release_event_stream_lease(sub: Subscription) -> None:
    if sub.lease_released:
        return
    async with async_session() as db, sqlite_write_gate(), db.begin():
        await db.execute(
            delete(EventStreamLease).where(
                EventStreamLease.user_id == sub.user_id,
                EventStreamLease.lease_id == sub.lease_id,
            )
        )
    sub.lease_released = True


def _schedule_subscription_lease_cleanup(sub: Subscription) -> None:
    if sub.lease_released:
        return
    cleanup_task = sub.cleanup_task
    if cleanup_task is not None and not cleanup_task.done():
        return
    try:
        sub.cleanup_task = create_tracked_task(
            _release_event_stream_lease(sub),
            name=f"event-stream-lease-cleanup:{sub.user_id}:{sub.lease_id[:8]}",
        )
    except RuntimeError:
        logger.warning(
            "event stream lease cleanup admission failed user_id=%s",
            sub.user_id,
        )


def _close_local_subscription(
    sub: Subscription,
    *,
    release_lease: bool = False,
) -> None:
    was_closed = sub.closed
    sub.closed = True
    with _subscriptions_lock:
        bucket = _subscriptions.get(sub.user_id)
        if bucket is not None:
            bucket.discard(sub)
            if not bucket:
                _subscriptions.pop(sub.user_id, None)
    if not was_closed:
        while True:
            try:
                sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            sub.queue.put_nowait({"_kind": "closed"})
        except asyncio.QueueFull:
            pass
    if release_lease:
        _schedule_subscription_lease_cleanup(sub)


async def _renew_event_stream_lease(sub: Subscription) -> None:
    loop = asyncio.get_running_loop()
    duration_seconds = max(SSE_LEASE_DURATION.total_seconds(), 0.001)
    renew_delay = max(float(SSE_LEASE_RENEW_INTERVAL_SECONDS), 0.001)
    retry_delay = min(
        renew_delay,
        max(float(SSE_LEASE_RENEW_ERROR_RETRY_SECONDS), 0.001),
    )
    fence_margin = min(retry_delay, duration_seconds / 2)
    confirmed_deadline = loop.time() + duration_seconds - fence_margin
    next_delay = renew_delay
    try:
        while not sub.closed:
            remaining = confirmed_deadline - loop.time()
            if remaining <= 0:
                _close_local_subscription(sub, release_lease=True)
                return
            await asyncio.sleep(min(next_delay, remaining))
            if sub.closed:
                return
            remaining = confirmed_deadline - loop.time()
            if remaining <= 0:
                _close_local_subscription(sub, release_lease=True)
                return
            now = datetime.now(timezone.utc)
            renewed_expires_at = now + SSE_LEASE_DURATION
            try:
                async with asyncio.timeout(remaining):
                    async with async_session() as db, sqlite_write_gate(), db.begin():
                        result = await db.execute(
                            update(EventStreamLease)
                            .where(
                                EventStreamLease.user_id == sub.user_id,
                                EventStreamLease.lease_id == sub.lease_id,
                                EventStreamLease.expires_at > now,
                            )
                            .values(expires_at=renewed_expires_at)
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "event stream lease renewal failed user_id=%s error_type=%s",
                    sub.user_id,
                    type(exc).__name__,
                )
                next_delay = retry_delay
                continue
            if result.rowcount != 1:
                _close_local_subscription(sub, release_lease=True)
                return
            confirmed_deadline = (
                loop.time()
                + max((renewed_expires_at - datetime.now(timezone.utc)).total_seconds(), 0)
                - fence_margin
            )
            next_delay = renew_delay
    finally:
        if not sub.closed:
            _close_local_subscription(sub, release_lease=True)


async def subscribe(user_id: int) -> Subscription:
    normalized_user_id = int(user_id)
    lease_id = await _acquire_event_stream_lease(normalized_user_id)
    sub = Subscription(user_id=normalized_user_id, lease_id=lease_id)
    with _subscriptions_lock:
        _subscriptions.setdefault(normalized_user_id, set()).add(sub)
    try:
        sub.renewal_task = create_tracked_task(
            _renew_event_stream_lease(sub),
            name=f"event-stream-lease:{normalized_user_id}:{lease_id[:8]}",
        )
    except RuntimeError:
        _close_local_subscription(sub)
        await _release_event_stream_lease(sub)
        raise
    return sub


async def unsubscribe(sub: Subscription) -> None:
    _close_local_subscription(sub)
    renewal_task = sub.renewal_task
    if renewal_task and renewal_task is not asyncio.current_task() and not renewal_task.done():
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task
    cleanup_task = sub.cleanup_task
    if cleanup_task and cleanup_task is not asyncio.current_task():
        try:
            await cleanup_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "event stream lease cleanup failed user_id=%s error_type=%s",
                sub.user_id,
                type(exc).__name__,
            )
    if not sub.lease_released:
        await _release_event_stream_lease(sub)


async def recover_expired_event_stream_leases(*, force: bool = False) -> int:
    now = datetime.now(timezone.utc)
    async with async_session() as db, sqlite_write_gate(), db.begin():
        statement = delete(EventStreamLease)
        if not force:
            statement = statement.where(EventStreamLease.expires_at <= now)
        result = await db.execute(statement)
    return int(result.rowcount or 0)


def _enqueue(sub: Subscription, item: dict[str, Any]) -> bool:
    try:
        sub.queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "event bus dropped slow subscriber user_id=%s qsize=%s",
            sub.user_id,
            sub.queue.qsize(),
        )
        return False


def _user_subscriptions(user_id: int) -> list[Subscription]:
    with _subscriptions_lock:
        return list(_subscriptions.get(int(user_id), set()))


def publish_event(user_id: int, event_type: str, payload: dict[str, Any], **extra: Any) -> None:
    subs = _user_subscriptions(user_id)
    if not subs:
        return
    item: dict[str, Any] = {"_kind": "event", "type": event_type, "payload": payload}
    item.update(extra)
    for sub in subs:
        if not _enqueue(sub, item):
            _close_local_subscription(sub, release_lease=True)


def mark_dirty(user_id: int, topic: str) -> None:
    topic_str = str(topic)
    subs = _user_subscriptions(user_id)
    if not subs:
        return
    for sub in subs:
        with sub.dirty_lock:
            if topic_str in sub.pending_dirty_topics:
                continue
            sub.pending_dirty_topics.add(topic_str)
        if not _enqueue(sub, {"_kind": "dirty", "topic": topic_str}):
            with sub.dirty_lock:
                sub.pending_dirty_topics.discard(topic_str)
            _close_local_subscription(sub, release_lease=True)


def consume_pending_dirty_topic(sub: Subscription, topic: str) -> None:
    with sub.dirty_lock:
        sub.pending_dirty_topics.discard(str(topic))


def mark_dirty_after_commit(db: "AsyncSession", user_id: int, *topics: str) -> None:
    """Schedule mark_dirty calls to fire after the current AsyncSession commits.

    Use this from helpers that mutate ORM state but do not commit themselves,
    so SSE subscribers rebuild against the committed snapshot.
    """
    sync_session = db.sync_session
    captured = (int(user_id), tuple(str(topic) for topic in topics if topic))

    fired = False

    def _fire(session: Any) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        target_user_id, topic_list = captured
        for topic in topic_list:
            mark_dirty(target_user_id, topic)

    def _rollback(session: Any) -> None:
        nonlocal fired
        if fired:
            return
        fired = True

    def _soft_rollback(session: Any, previous_transaction: Any) -> None:
        _rollback(session)

    event.listen(sync_session, "after_commit", _fire, once=True)
    event.listen(sync_session, "after_rollback", _rollback, once=True)
    event.listen(sync_session, "after_soft_rollback", _soft_rollback, once=True)
