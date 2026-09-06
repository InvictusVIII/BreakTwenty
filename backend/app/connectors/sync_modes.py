"""Shared helpers for planning first-time backfill and incremental sync windows.

Connectors that fetch transaction history can call ``plan_account_sync_window``
to decide how far back to fetch on each sync: the first time an account is
seen we want a large historical window (``backfill``) so the user starts with
full history; subsequent syncs fetch from the latest known covered date minus a
small overlap through today (``incremental``).

This module is intentionally connector-agnostic — it only depends on the
shared ``Account`` / ``Institution`` / ``Transaction`` models and the
supplied ``external_id``. Provider-specific window sizes (e.g. 24 months
vs 60 days vs the provider's own retention cap) should be passed in by the
connector / scraper; ``DEFAULT_BACKFILL_MONTHS`` is only a fallback for
callers that do not know a better value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Institution, Transaction, TransactionImportAccountState
from app.connectors.sync_context import get_connector_sync_context
from app.services.user_utils import get_user_local_today_from_db


DEFAULT_BACKFILL_MONTHS = 24
DEFAULT_INCREMENTAL_OVERLAP_DAYS = 14


def sync_window_date(entry: Mapping[str, Any] | None, key: str) -> date | None:
    raw = entry.get(key) if entry else None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def pending_backfill_date_windows(
    sync_window: Mapping[str, Any] | None,
    *,
    min_date: date | None = None,
    max_date: date | None = None,
) -> list[tuple[date, date]]:
    raw_windows = sync_window.get("pending_backfill_windows") if sync_window else None
    if (
        not isinstance(raw_windows, Iterable)
        or isinstance(raw_windows, str | bytes | Mapping)
    ):
        return []

    windows: list[tuple[date, date]] = []
    for raw_window in raw_windows:
        if not isinstance(raw_window, Mapping):
            continue
        start_date = sync_window_date(raw_window, "start")
        end_date = sync_window_date(raw_window, "end")
        if not start_date or not end_date:
            continue
        if start_date > end_date:
            continue
        if min_date is not None:
            start_date = max(start_date, min_date)
        if max_date is not None:
            end_date = min(end_date, max_date)
        if start_date <= end_date:
            windows.append((start_date, end_date))
    return windows


@dataclass(frozen=True)
class AccountSyncWindow:
    mode: str
    start_date: date | None
    end_date: date
    latest_persisted_date: date | None
    last_successful_fetch_date: date | None
    overlap_days: int

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "mode": self.mode,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat(),
            "latest_persisted_date": (
                self.latest_persisted_date.isoformat()
                if self.latest_persisted_date
                else None
            ),
            "last_successful_fetch_date": (
                self.last_successful_fetch_date.isoformat()
                if self.last_successful_fetch_date
                else None
            ),
            "overlap_days": self.overlap_days,
        }


async def walk_transaction_backfill_windows(
    *,
    mode: str,
    window_start: date | None,
    window_end: date,
    chunk_days: int,
    max_history_days: int,
    empty_history_stop_chunks: int,
    fetch_chunk: Callable[[date, date], Awaitable[tuple[list, bool]]],
    append_rows: Callable[[list], bool],
    stop_on_chunk_failure: bool = False,
) -> bool:
    """Walk a provider's transaction window in chunks, returning ``fetch_succeeded``.

    Shared by API connectors whose providers fetch transactions in date-bounded
    chunks (Wise statements, Questrade activities). ``backfill`` mode walks backward
    from ``window_end`` to the floor in ``chunk_days`` steps; an UNBOUNDED backfill
    (``window_start is None`` → the ``max_history_days`` floor) stops after
    ``empty_history_stop_chunks`` consecutive empty chunks once activity has been seen,
    but a BOUNDED durable-planner window is fetched in full (no early stop, so an
    activity gap can't be mis-read as end-of-history). ``incremental`` mode walks
    forward. ``fetch_chunk(start, end)`` returns ``(rows, chunk_succeeded)``;
    ``append_rows(rows)`` appends to the caller's accumulator and returns whether any
    row was appended. Returns False if any chunk fetch failed.
    """
    fetch_succeeded = True
    if mode == "backfill":
        absolute_floor = window_start or (window_end - timedelta(days=max_history_days))
        current_end = window_end
        seen_activity = False
        consecutive_empty_chunks = 0
        bounded_window = window_start is not None
        while current_end >= absolute_floor:
            current_start = max(absolute_floor, current_end - timedelta(days=chunk_days))
            rows, chunk_succeeded = await fetch_chunk(current_start, current_end)
            if not chunk_succeeded:
                fetch_succeeded = False
                if stop_on_chunk_failure:
                    break
            appended_any = append_rows(rows)
            if not bounded_window:
                if appended_any:
                    seen_activity = True
                    consecutive_empty_chunks = 0
                elif seen_activity:
                    consecutive_empty_chunks += 1
                    if consecutive_empty_chunks >= empty_history_stop_chunks:
                        break
            if current_start <= absolute_floor:
                break
            current_end = current_start - timedelta(seconds=1)
    else:
        absolute_floor = window_end - timedelta(days=max_history_days)
        chunk_start = max(window_start or absolute_floor, absolute_floor)
        while chunk_start < window_end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), window_end)
            rows, chunk_succeeded = await fetch_chunk(chunk_start, chunk_end)
            if not chunk_succeeded:
                fetch_succeeded = False
                if stop_on_chunk_failure:
                    break
            append_rows(rows)
            if chunk_end >= window_end:
                break
            chunk_start = chunk_end
    return fetch_succeeded


def sync_window_utc_bounds(
    window: AccountSyncWindow,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    end_at = datetime.combine(window.end_date, time.max, tzinfo=timezone.utc)
    if end_at > current:
        end_at = current

    start_at = (
        datetime.combine(window.start_date, time.min, tzinfo=timezone.utc)
        if window.start_date
        else None
    )
    if start_at and start_at > end_at:
        start_at = end_at

    return start_at, end_at


def _value_to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _max_date(values) -> date | None:
    dates = [parsed for parsed in (_value_to_date(value) for value in values) if parsed]
    return max(dates) if dates else None


async def plan_account_sync_window(
    session: AsyncSession,
    user_id: int,
    account_external_id: str,
    *,
    provider: str | None = None,
    institution_id: int | None = None,
    alternate_account_external_ids: Sequence[str] = (),
    overlap_days: int = DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    today: date | None = None,
) -> AccountSyncWindow:
    today = today or await get_user_local_today_from_db(session, user_id)
    overlap_days = max(int(overlap_days or 0), 0)
    external_ids = tuple(
        dict.fromkeys(
            external_id
            for external_id in (
                account_external_id,
                *alternate_account_external_ids,
            )
            if external_id
        )
    )
    backfill_window = AccountSyncWindow(
        mode="backfill",
        start_date=None,
        end_date=today,
        latest_persisted_date=None,
        last_successful_fetch_date=None,
        overlap_days=overlap_days,
    )
    if not external_ids:
        return backfill_window

    account_stmt = (
        select(
            Account.id,
            func.max(Transaction.date),
        )
        .outerjoin(
            Transaction,
            (Transaction.account_id == Account.id)
            & (Transaction.user_id == user_id),
        )
        .where(
            Account.user_id == user_id,
            Account.external_id.in_(external_ids),
        )
        .group_by(Account.id)
    )
    if provider:
        context = get_connector_sync_context()
        resolved_institution_id = institution_id or (context.institution_id if context else None)
        if resolved_institution_id is None:
            raise RuntimeError(f"Connection identity is required for {provider} sync planning")
        account_stmt = account_stmt.join(
            Institution,
            Account.institution_id == Institution.id,
        ).where(
            Institution.user_id == user_id,
            Institution.id == resolved_institution_id,
        )

    account_rows = (await session.execute(account_stmt)).all()
    if not account_rows:
        return backfill_window

    last_successful_fetch_date = None
    latest_persisted_date = _max_date(row[1] for row in account_rows)
    if provider:
        state_rows = (
            await session.execute(
                select(
                    TransactionImportAccountState.backfill_status,
                    TransactionImportAccountState.last_successful_window_end_date,
                    TransactionImportAccountState.incremental_end_date,
                    TransactionImportAccountState.backfill_completed_at,
                ).where(
                    TransactionImportAccountState.user_id == user_id,
                    TransactionImportAccountState.institution_id == resolved_institution_id,
                    TransactionImportAccountState.account_external_id.in_(external_ids),
                )
            )
        ).all()
        if state_rows:
            if not any(row[0] == "complete" for row in state_rows):
                return backfill_window
            last_successful_fetch_date = _max_date(
                (
                    last_successful_fetch_date,
                    *(row[1] for row in state_rows),
                    *(row[2] for row in state_rows),
                    *(row[3] for row in state_rows),
                )
            )
    if last_successful_fetch_date is None:
        return backfill_window

    from app.services.transaction_import import incremental_window_start_date

    return AccountSyncWindow(
        mode="incremental",
        start_date=incremental_window_start_date(
            latest_persisted_date=latest_persisted_date,
            last_successful_fetch_date=last_successful_fetch_date,
            overlap_days=overlap_days,
            today=today,
        ),
        end_date=today,
        latest_persisted_date=latest_persisted_date,
        last_successful_fetch_date=last_successful_fetch_date,
        overlap_days=overlap_days,
    )
