from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from itertools import count
from typing import TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url

import app.database as database

logger = logging.getLogger("breaktwenty.sqlite_write_gate")

_sqlite_write_condition = asyncio.Condition()
_sqlite_write_active = False
_sqlite_waiting_interactive = 0
_sqlite_waiting_background = 0
_sqlite_write_owner: dict[str, object] | None = None
_sqlite_write_waiters: dict[int, dict[str, object]] = {}
_sqlite_waiter_ids = count(1)
_sqlite_last_state_sample_at = 0.0
_sqlite_write_gate_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "sqlite_write_gate_depth",
    default=0,
)
_BACKGROUND_TASK_PREFIXES = (
    "event-stream-lease",
    "market-strip-",
    "sync-batch",
    "sync-lease",
    "sync-status-drain",
    "transaction-import",
)
_SLOW_WAIT_SECONDS = 0.25
_SLOW_HOLD_SECONDS = 0.75
_STATE_SAMPLE_INTERVAL_SECONDS = 1.0
_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}
_SQLITE_BUSY_MESSAGES = (
    "database is locked",
    "database is busy",
    "sqlite_busy",
    "sqlite_locked",
)
_SQLITE_BUSY_RETRY_ATTEMPTS = 4
_SQLITE_BUSY_RETRY_BASE_SECONDS = 0.1

T = TypeVar("T")


def sqlite_write_gate_enabled() -> bool:
    try:
        mode = str(os.getenv("BREAKTWENTY_SQLITE_WRITE_GATE", "on")).strip().lower()
        return (
            mode not in _DISABLED_VALUES
            and make_url(database.DATABASE_URL).get_backend_name() == "sqlite"
        )
    except Exception:
        return False


def is_sqlite_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    text = " ".join(
        str(value).lower()
        for value in (exc, getattr(exc, "orig", None))
        if value is not None
    )
    return any(message in text for message in _SQLITE_BUSY_MESSAGES)


async def retry_sqlite_busy(
    operation: Callable[[], Awaitable[T]],
    *,
    label: str,
    rollback: Callable[[], Awaitable[object]] | None = None,
    attempts: int = _SQLITE_BUSY_RETRY_ATTEMPTS,
    base_delay_seconds: float = _SQLITE_BUSY_RETRY_BASE_SECONDS,
) -> T:
    """Retry a transactional operation only for transient SQLite writer contention."""
    max_attempts = max(int(attempts or 1), 1)
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except OperationalError as exc:
            if not is_sqlite_busy_error(exc) or attempt >= max_attempts:
                raise
            if rollback is not None:
                try:
                    await rollback()
                except Exception:
                    logger.exception("sqlite busy retry rollback failed label=%s attempt=%s", label, attempt)
                    raise
            delay = max(float(base_delay_seconds or 0), 0) * (2 ** (attempt - 1))
            logger.warning(
                "sqlite busy retry label=%s attempt=%s/%s delay=%.3fs",
                label,
                attempt + 1,
                max_attempts,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)
    raise RuntimeError("unreachable sqlite busy retry state")


def _current_task_name() -> str:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return ""
    if task is None:
        return ""
    return task.get_name() or ""


def _default_priority() -> str:
    task_name = _current_task_name()
    if any(task_name.startswith(prefix) for prefix in _BACKGROUND_TASK_PREFIXES):
        return "background"
    return "interactive"


def sqlite_write_gate_state() -> dict[str, object]:
    now = time.perf_counter()
    owner = dict(_sqlite_write_owner) if _sqlite_write_owner is not None else None
    if owner is not None:
        owner["held_seconds"] = max(0.0, now - float(owner.pop("acquired_at", now)))
    return {
        "active": _sqlite_write_active,
        "owner": owner,
        "waiting_interactive": _sqlite_waiting_interactive,
        "waiting_background": _sqlite_waiting_background,
        "waiters": [dict(waiter) for waiter in _sqlite_write_waiters.values()],
    }


def _log_gate_state(event_name: str, *, force: bool = False) -> None:
    global _sqlite_last_state_sample_at

    now = time.perf_counter()
    if not force and now - _sqlite_last_state_sample_at < _STATE_SAMPLE_INTERVAL_SECONDS:
        return
    _sqlite_last_state_sample_at = now
    state = sqlite_write_gate_state()
    owner = state["owner"] or {}
    waiter_labels = [
        str(waiter.get("label") or "")[:160]
        for waiter in state["waiters"][:16]
    ]
    logger.info(
        "sqlite write gate state event=%s active=%s owner_label=%s owner_priority=%s "
        "owner_held=%.6fs waiting_interactive=%s waiting_background=%s "
        "waiter_count=%s waiter_labels=%s",
        event_name,
        state["active"],
        str(owner.get("label") or "")[:160] or None,
        owner.get("priority"),
        float(owner.get("held_seconds", 0.0)),
        state["waiting_interactive"],
        state["waiting_background"],
        len(state["waiters"]),
        waiter_labels,
    )


async def _acquire_sqlite_write_slot(priority: str, label: str, task_name: str) -> float:
    global _sqlite_waiting_background, _sqlite_waiting_interactive
    global _sqlite_write_active, _sqlite_write_owner

    wait_started = time.perf_counter()
    waiter_id = next(_sqlite_waiter_ids)
    async with _sqlite_write_condition:
        _sqlite_write_waiters[waiter_id] = {
            "label": label,
            "priority": priority,
            "task": task_name or None,
            "waiting_since": wait_started,
        }
        if priority == "background":
            _sqlite_waiting_background += 1
        else:
            _sqlite_waiting_interactive += 1
        try:
            observed_wait = False
            while _sqlite_write_active or (
                priority == "background" and _sqlite_waiting_interactive > 0
            ):
                if not observed_wait:
                    _log_gate_state("waiter", force=True)
                    observed_wait = True
                await _sqlite_write_condition.wait()
            _sqlite_write_active = True
            _sqlite_write_owner = {
                "label": label,
                "priority": priority,
                "task": task_name or None,
                "acquired_at": time.perf_counter(),
            }
        finally:
            _sqlite_write_waiters.pop(waiter_id, None)
            if priority == "background":
                _sqlite_waiting_background -= 1
            else:
                _sqlite_waiting_interactive -= 1
    wait_seconds = time.perf_counter() - wait_started
    _log_gate_state("acquired", force=observed_wait)
    return wait_seconds


async def _release_sqlite_write_slot() -> None:
    global _sqlite_write_active, _sqlite_write_owner

    async with _sqlite_write_condition:
        _sqlite_write_active = False
        _sqlite_write_owner = None
        _sqlite_write_condition.notify_all()
    _log_gate_state("released")


@asynccontextmanager
async def sqlite_write_gate(*, priority: str | None = None, label: str | None = None):
    """Optional app-level SQLite write serializer.

    SQLCipher-backed SQLite writers are serialized by default so concurrent worker
    threads cannot block the Python runtime inside native busy waits. Set
    BREAKTWENTY_SQLITE_WRITE_GATE=off only for targeted driver diagnostics.
    """
    if not sqlite_write_gate_enabled():
        yield
        return
    depth = _sqlite_write_gate_depth.get()
    if depth > 0:
        token = _sqlite_write_gate_depth.set(depth + 1)
        try:
            yield
        finally:
            _sqlite_write_gate_depth.reset(token)
        return
    resolved_priority = priority if priority in {"interactive", "background"} else _default_priority()
    task_name = _current_task_name()
    gate_label = label or task_name or "sqlite-write"
    wait_seconds = await _acquire_sqlite_write_slot(resolved_priority, gate_label, task_name)
    if wait_seconds >= _SLOW_WAIT_SECONDS:
        logger.warning(
            "sqlite write gate waited %.3fs priority=%s label=%s waiting_interactive=%s waiting_background=%s",
            wait_seconds,
            resolved_priority,
            gate_label,
            _sqlite_waiting_interactive,
            _sqlite_waiting_background,
        )
    hold_started = time.perf_counter()
    token = _sqlite_write_gate_depth.set(1)
    try:
        yield
    finally:
        _sqlite_write_gate_depth.reset(token)
        hold_seconds = time.perf_counter() - hold_started
        if hold_seconds >= _SLOW_HOLD_SECONDS:
            logger.warning(
                "sqlite write gate held %.3fs priority=%s label=%s",
                hold_seconds,
                resolved_priority,
                gate_label,
            )
        await _release_sqlite_write_slot()
