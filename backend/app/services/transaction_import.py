from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.sync_context import get_connector_sync_context
from app.models import (
    Account,
    Institution,
    Transaction,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
)
from app.services.asset_groups import ASSET_GROUP_PROVIDERS
from app.services.manual_accounts import MANUAL_PROVIDER
from app.services.manual_institutions import MANUAL_INSTITUTION_PROVIDER
from app.services.event_bus import (
    EVENT_TRANSACTION_IMPORT_STATUS,
    mark_dirty_after_commit,
)
from app.services.time_utils import aware_utc as _aware_utc
from app.services.time_utils import utc_now as _utcnow
from app.services.user_utils import get_user_local_today_from_db

DEFAULT_BACKFILL_DAYS = 365 * 2
DEFAULT_BACKFILL_CHUNK_DAYS = 365
DEFAULT_INCREMENTAL_DAYS = 60
DEFAULT_INCREMENTAL_OVERLAP_DAYS = 14

BACKFILL_STATUS_NOT_STARTED = "not_started"
BACKFILL_STATUS_QUEUED = "queued"
BACKFILL_STATUS_RUNNING = "running"
BACKFILL_STATUS_PARTIAL = "partial"
BACKFILL_STATUS_COMPLETE = "complete"
BACKFILL_STATUS_ERROR = "error"

WINDOW_STATUS_RUNNING = "running"
WINDOW_STATUS_COMPLETE = "complete"
WINDOW_STATUS_ERROR = "error"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETE = "complete"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_AUTH_REQUIRED = "auth_required"
JOB_STATUS_SURFACE_TERMINAL_FOR_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class TransactionImportPlan:
    mode: str
    start_date: date | None
    end_date: date
    latest_persisted_date: date | None
    last_successful_fetch_date: date | None
    overlap_days: int
    target_start_date: date
    target_end_date: date
    pending_backfill_windows: tuple[tuple[date, date], ...]
    completed_backfill_windows: tuple[tuple[date, date], ...]
    total_backfill_windows: int

    def as_sync_state(self) -> dict[str, Any]:
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
            "backfill_start_date": self.target_start_date.isoformat(),
            "backfill_end_date": self.target_end_date.isoformat(),
            "pending_backfill_windows": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in self.pending_backfill_windows
            ],
            "completed_backfill_windows": [
                {"start": start.isoformat(), "end": end.isoformat()}
                for start, end in self.completed_backfill_windows
            ],
            "total_backfill_windows": self.total_backfill_windows,
        }


def _window_progress_label(
    action: str,
    *,
    mode: str,
    account_name: str,
    window_start_date: date,
    window_end_date: date,
) -> str:
    name = str(account_name or "account").strip() or "account"
    return (
        f"{action} {mode} window "
        f"{window_start_date.isoformat()} - {window_end_date.isoformat()} for {name}"
    )[:255]


async def _record_current_transaction_job_progress(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    label: str,
    sync_id: str | None = None,
) -> None:
    context = get_connector_sync_context()
    if (
        context is None
        or context.sync_scope != "transactions"
        or not context.transaction_job_id
    ):
        return
    row = (
        await db.execute(
            select(TransactionImportJob).where(
                TransactionImportJob.user_id == user_id,
                TransactionImportJob.job_id == context.transaction_job_id,
                TransactionImportJob.status == "running",
                TransactionImportJob.lease_token == context.transaction_job_lease_token,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    now = _utcnow()
    row.last_progress_at = now
    row.last_progress_label = label
    row.updated_at = now
    row.lease_expires_at = now + timedelta(minutes=5)
    if sync_id:
        row.sync_id = sync_id


def _date_to_key(value: date | datetime | str | None) -> str | None:
    parsed = _value_to_date(value)
    return parsed.isoformat() if parsed else None


def _value_to_date(value: date | datetime | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _max_date(values) -> date | None:
    dates = [parsed for parsed in (_value_to_date(value) for value in values) if parsed]
    return max(dates) if dates else None


def incremental_window_start_date(
    *,
    latest_persisted_date: date | datetime | str | None,
    last_successful_fetch_date: date | datetime | str | None,
    overlap_days: int,
    today: date,
) -> date:
    latest_covered_date = _max_date((latest_persisted_date, last_successful_fetch_date))
    if latest_covered_date and latest_covered_date > today:
        latest_covered_date = today
    start = (latest_covered_date or today) - timedelta(
        days=max(int(overlap_days or 0), 0)
    )
    return min(start, today)


def _split_windows(start_date: date, end_date: date, chunk_days: int) -> tuple[tuple[date, date], ...]:
    if start_date > end_date:
        start_date = end_date
    chunk_days = max(int(chunk_days or DEFAULT_BACKFILL_CHUNK_DAYS), 1)
    windows: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(start_date, current_end - timedelta(days=chunk_days - 1))
        windows.append((current_start, current_end))
        if current_start <= start_date:
            break
        current_end = current_start - timedelta(days=1)
    return tuple(windows)


def _window_ranges(
    rows: list[TransactionImportWindow],
    *,
    status: str | None = None,
    statuses: set[str] | tuple[str, ...] | None = None,
) -> tuple[tuple[date, date], ...]:
    allowed_statuses = set(statuses or ())
    if status:
        allowed_statuses.add(status)
    ranges = [
        (start, end)
        for start, end in (
            (_value_to_date(row.window_start_date), _value_to_date(row.window_end_date))
            for row in rows
            if not allowed_statuses or row.status in allowed_statuses
        )
        if start is not None and end is not None
    ]
    return tuple(sorted(ranges, key=lambda item: (item[0], item[1])))


def _latest_completed_window_range(
    windows: list[TransactionImportWindow],
) -> tuple[str | None, str | None]:
    completed_by_sync_id: dict[str, list[TransactionImportWindow]] = {}
    for window in windows:
        if not window.sync_id or window.status != WINDOW_STATUS_COMPLETE:
            continue
        completed_by_sync_id.setdefault(window.sync_id, []).append(window)

    if completed_by_sync_id:
        latest_group = max(
            completed_by_sync_id.values(),
            key=lambda rows: max(
                (row.completed_at or row.updated_at or datetime.min for row in rows),
                default=datetime.min,
            ),
        )
        starts = [
            parsed
            for parsed in (_value_to_date(window.window_start_date) for window in latest_group)
            if parsed
        ]
        ends = [
            parsed
            for parsed in (_value_to_date(window.window_end_date) for window in latest_group)
            if parsed
        ]
        if starts and ends:
            return min(starts).isoformat(), max(ends).isoformat()

    latest_window = max(
        (window for window in windows if window.status == WINDOW_STATUS_COMPLETE),
        key=lambda window: window.completed_at or window.updated_at or datetime.min,
        default=None,
    )
    if latest_window is None:
        return None, None
    return (
        _date_to_key(latest_window.window_start_date),
        _date_to_key(latest_window.window_end_date),
    )


def _deduplicate_ranges(
    ranges: tuple[tuple[date, date], ...],
) -> tuple[tuple[date, date], ...]:
    seen: set[tuple[date, date]] = set()
    deduplicated: list[tuple[date, date]] = []
    for start, end in sorted(ranges, key=lambda item: (item[0], item[1])):
        if (start, end) in seen:
            continue
        seen.add((start, end))
        deduplicated.append((start, end))
    return tuple(deduplicated)


def _window_date_range(row: TransactionImportWindow) -> tuple[date, date] | None:
    start = _value_to_date(row.window_start_date)
    end = _value_to_date(row.window_end_date)
    if start is None or end is None:
        return None
    if start > end:
        return end, start
    return start, end


def _strictly_contains(
    outer: tuple[date, date],
    inner: tuple[date, date],
) -> bool:
    return outer[0] <= inner[0] and outer[1] >= inner[1] and outer != inner


def _effective_backfill_window_rows(
    rows: list[TransactionImportWindow],
) -> list[TransactionImportWindow]:
    parsed = [
        (row, row_range)
        for row in rows
        for row_range in [_window_date_range(row)]
        if row_range is not None
    ]
    effective_rows: list[TransactionImportWindow] = []
    for row, row_range in parsed:
        if any(
            other_row is not row
            and other_row.status == WINDOW_STATUS_COMPLETE
            and _strictly_contains(other_range, row_range)
            for other_row, other_range in parsed
        ):
            continue
        if row.status != WINDOW_STATUS_COMPLETE and any(
            other_row is not row and _strictly_contains(row_range, other_range)
            for other_row, other_range in parsed
        ):
            continue
        effective_rows.append(row)
    return sorted(
        effective_rows,
        key=lambda row: (
            _value_to_date(row.window_start_date) or date.min,
            _value_to_date(row.window_end_date) or date.min,
        ),
    )


def _uncovered_failed_window_rows(
    rows: list[TransactionImportWindow],
) -> list[TransactionImportWindow]:
    completed_ranges = _deduplicate_ranges(
        tuple(
            row_range
            for row in rows
            for row_range in [_window_date_range(row)]
            if row.status == WINDOW_STATUS_COMPLETE and row_range is not None
        )
    )
    failed_rows: list[TransactionImportWindow] = []
    for row in rows:
        if row.status != WINDOW_STATUS_ERROR:
            continue
        row_range = _window_date_range(row)
        if row_range is None:
            failed_rows.append(row)
            continue
        if _subtract_ranges(row_range, completed_ranges):
            failed_rows.append(row)
    return failed_rows


def _subtract_ranges(
    window: tuple[date, date],
    covered_ranges: tuple[tuple[date, date], ...],
) -> tuple[tuple[date, date], ...]:
    pending = [window]
    for covered_start, covered_end in covered_ranges:
        next_pending: list[tuple[date, date]] = []
        for pending_start, pending_end in pending:
            if covered_end < pending_start or covered_start > pending_end:
                next_pending.append((pending_start, pending_end))
                continue
            if covered_start > pending_start:
                next_pending.append((pending_start, covered_start - timedelta(days=1)))
            if covered_end < pending_end:
                next_pending.append((covered_end + timedelta(days=1), pending_end))
        pending = next_pending
        if not pending:
            break
    return tuple(pending)


def _untracked_window_ranges(
    target_windows: tuple[tuple[date, date], ...],
    known_ranges: tuple[tuple[date, date], ...],
) -> tuple[tuple[date, date], ...]:
    return _deduplicate_ranges(
        tuple(
            pending_window
            for target_window in target_windows
            for pending_window in _subtract_ranges(target_window, known_ranges)
        )
    )


def _clip_ranges_to_target(
    ranges: tuple[tuple[date, date], ...],
    target_start: date,
    target_end: date,
) -> tuple[tuple[date, date], ...]:
    clipped: list[tuple[date, date]] = []
    for start, end in ranges:
        clipped_start = max(start, target_start)
        clipped_end = min(end, target_end)
        if clipped_start <= clipped_end:
            clipped.append((clipped_start, clipped_end))
    return _deduplicate_ranges(tuple(clipped))


def _account_identity(account: Account | None) -> tuple[int | None, int | None]:
    if not account:
        return None, None
    return account.institution_id, account.id


def _context_institution_id(user_id: int, provider: str) -> int:
    context = get_connector_sync_context()
    if (
        context is None
        or int(context.user_id) != int(user_id)
        or context.institution_id is None
    ):
        raise RuntimeError(
            f"Connection identity is required for {provider} transaction import"
        )
    return int(context.institution_id)


async def _find_account(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account_external_id: str,
    institution_id: int | None = None,
) -> Account | None:
    resolved_institution_id = int(institution_id or _context_institution_id(user_id, provider))
    return (
        await db.execute(
            select(Account)
            .join(Institution, Account.institution_id == Institution.id)
            .where(
                Account.user_id == user_id,
                Institution.user_id == user_id,
                Institution.id == resolved_institution_id,
                Account.external_id == account_external_id,
            )
        )
    ).scalars().first()


async def ensure_account_state(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account_external_id: str,
    account_name: str,
    account_type: str | None = None,
    is_liability: bool = False,
    target_start_date: date | None = None,
    target_end_date: date | None = None,
    account: Account | None = None,
    replace_target_range: bool = False,
) -> TransactionImportAccountState:
    account_external_id = str(account_external_id or "").strip()
    if not account_external_id:
        raise ValueError("account_external_id is required")

    if account is None:
        account = await _find_account(
            db,
            user_id=user_id,
            provider=provider,
            account_external_id=account_external_id,
        )

    institution_id, account_id = _account_identity(account)
    if institution_id is None or account_id is None:
        raise RuntimeError(
            f"Account {account_external_id} is not attached to a concrete {provider} connection"
        )
    now = _utcnow()
    state = (
        await db.execute(
            select(TransactionImportAccountState).where(
                TransactionImportAccountState.user_id == user_id,
                TransactionImportAccountState.institution_id == institution_id,
                TransactionImportAccountState.account_id == account_id,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        state = TransactionImportAccountState(
            user_id=user_id,
            institution_id=institution_id,
            account_id=account_id,
            provider=provider,
            account_external_id=account_external_id,
            account_name=account_name or (account.name if account else "Account"),
            account_type=account_type or (account.account_type if account else None),
            is_liability=bool(is_liability if is_liability is not None else account.is_liability if account else False),
            backfill_status=BACKFILL_STATUS_NOT_STARTED,
            created_at=now,
            updated_at=now,
        )
        db.add(state)
        await db.flush()
    else:
        state.institution_id = institution_id or state.institution_id
        state.account_id = account_id or state.account_id
        state.account_name = account_name or state.account_name
        state.account_type = account_type or state.account_type
        state.is_liability = bool(is_liability)
        state.updated_at = now

    incoming_start = _value_to_date(target_start_date)
    incoming_end = _value_to_date(target_end_date)
    existing_start = _value_to_date(state.target_start_date)
    existing_end = _value_to_date(state.target_end_date)
    new_start = incoming_start
    new_end = incoming_end
    if replace_target_range and new_start and new_end:
        state.target_start_date = new_start
        state.target_end_date = new_end
        existing_start = new_start
        existing_end = new_end
    expands_existing_target_earlier = bool(
        new_start and existing_start and new_start < existing_start
    )
    preserve_existing_target = (
        existing_start is not None
        and existing_end is not None
        and not expands_existing_target_earlier
    )
    if not preserve_existing_target:
        if new_start and (existing_start is None or new_start < existing_start):
            state.target_start_date = new_start
        elif incoming_start and not state.target_start_date:
            state.target_start_date = incoming_start
        if new_end and (existing_end is None or new_end > existing_end):
            state.target_end_date = new_end
        elif incoming_end and not state.target_end_date:
            state.target_end_date = incoming_end

    return state


async def _query_account_windows(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account_external_id: str,
    mode: str | None = None,
    institution_id: int | None = None,
) -> list[TransactionImportWindow]:
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    if account is None:
        return []
    filters = [
        TransactionImportWindow.user_id == user_id,
        TransactionImportWindow.institution_id == account.institution_id,
        TransactionImportWindow.account_id == account.id,
    ]
    if mode:
        filters.append(TransactionImportWindow.mode == mode)
    return (
        await db.execute(select(TransactionImportWindow).where(*filters))
    ).scalars().all()


async def _latest_persisted_transaction_date(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account_external_id: str,
    institution_id: int | None = None,
) -> date | None:
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    if account is None:
        return None
    value = (
        await db.execute(
            select(func.max(Transaction.date))
            .join(Account, Transaction.account_id == Account.id)
            .where(
                Transaction.user_id == user_id,
                Transaction.account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    return _value_to_date(value)


async def plan_account_transaction_import(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
    account_external_id: str,
    account_name: str,
    account_type: str | None = None,
    is_liability: bool = False,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    backfill_chunk_days: int = DEFAULT_BACKFILL_CHUNK_DAYS,
    incremental_days: int = DEFAULT_INCREMENTAL_DAYS,
    overlap_days: int = DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    backfill_start_date: date | None = None,
    today: date | None = None,
) -> TransactionImportPlan:
    today = today or await get_user_local_today_from_db(db, user_id)
    overlap_days = max(int(overlap_days or 0), 0)
    target_start = today - timedelta(days=max(int(backfill_days or DEFAULT_BACKFILL_DAYS), 1))
    if backfill_start_date and backfill_start_date > target_start:
        target_start = min(backfill_start_date, today)
    target_end = today
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    state = await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        account_name=account_name,
        account_type=account_type,
        is_liability=is_liability,
        target_start_date=target_start,
        target_end_date=target_end,
        account=account,
    )
    persisted_target_start = _value_to_date(state.target_start_date) or target_start
    persisted_target_end = _value_to_date(state.target_end_date) or target_end
    if backfill_start_date and persisted_target_start < target_start:
        persisted_target_start = target_start
    if backfill_start_date and persisted_target_end < target_end:
        persisted_target_end = target_end
    if persisted_target_start > persisted_target_end:
        persisted_target_start = persisted_target_end
    if backfill_start_date:
        state.target_start_date = persisted_target_start
        state.target_end_date = persisted_target_end
    latest_persisted = await _latest_persisted_transaction_date(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    last_successful_fetch_date = _max_date(
        (
            state.last_successful_window_end_date,
            state.incremental_end_date,
            state.backfill_completed_at,
        )
    )
    target_windows = _split_windows(
        persisted_target_start,
        persisted_target_end,
        backfill_chunk_days,
    )
    persisted_windows = await _query_account_windows(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        mode="backfill",
        institution_id=institution_id,
    )
    effective_windows = _effective_backfill_window_rows(persisted_windows)
    completed_ranges = _clip_ranges_to_target(
        _window_ranges(effective_windows, status=WINDOW_STATUS_COMPLETE),
        persisted_target_start,
        persisted_target_end,
    )
    retry_ranges = _clip_ranges_to_target(
        _window_ranges(
            effective_windows,
            statuses=(WINDOW_STATUS_RUNNING, WINDOW_STATUS_ERROR),
        ),
        persisted_target_start,
        persisted_target_end,
    )
    known_ranges = _clip_ranges_to_target(
        _window_ranges(effective_windows),
        persisted_target_start,
        persisted_target_end,
    )
    untracked_ranges = _untracked_window_ranges(target_windows, known_ranges)
    planned_total_windows = len(_deduplicate_ranges((*known_ranges, *untracked_ranges)))
    if not planned_total_windows:
        planned_total_windows = len(target_windows)
    completed_windows = completed_ranges
    pending_windows = _deduplicate_ranges((*retry_ranges, *untracked_ranges))
    running_count = sum(1 for row in effective_windows if row.status == WINDOW_STATUS_RUNNING)
    error_count = sum(1 for row in effective_windows if row.status == WINDOW_STATUS_ERROR)
    now = _utcnow()

    if not pending_windows:
        if state.backfill_status != BACKFILL_STATUS_COMPLETE:
            state.backfill_status = BACKFILL_STATUS_COMPLETE
            state.backfill_completed_at = state.backfill_completed_at or now
            state.updated_at = now
    elif running_count:
        state.backfill_status = BACKFILL_STATUS_RUNNING
        state.updated_at = now
    elif completed_windows:
        state.backfill_status = BACKFILL_STATUS_PARTIAL
        state.updated_at = now
    elif error_count:
        state.backfill_status = BACKFILL_STATUS_ERROR
        state.updated_at = now
    else:
        state.backfill_status = BACKFILL_STATUS_QUEUED
        state.updated_at = now

    if state.backfill_status == BACKFILL_STATUS_COMPLETE:
        start = incremental_window_start_date(
            latest_persisted_date=latest_persisted,
            last_successful_fetch_date=last_successful_fetch_date,
            overlap_days=overlap_days or incremental_days,
            today=today,
        )
        return TransactionImportPlan(
            mode="incremental",
            start_date=start,
            end_date=today,
            latest_persisted_date=latest_persisted,
            last_successful_fetch_date=last_successful_fetch_date,
            overlap_days=overlap_days,
            target_start_date=persisted_target_start,
            target_end_date=persisted_target_end,
            pending_backfill_windows=(),
            completed_backfill_windows=completed_windows,
            total_backfill_windows=planned_total_windows,
        )

    return TransactionImportPlan(
        mode="backfill",
        start_date=None,
        end_date=today,
        latest_persisted_date=latest_persisted,
        last_successful_fetch_date=last_successful_fetch_date,
        overlap_days=overlap_days,
        target_start_date=persisted_target_start,
        target_end_date=persisted_target_end,
        pending_backfill_windows=pending_windows,
        completed_backfill_windows=completed_windows,
        total_backfill_windows=planned_total_windows,
    )


async def _upsert_window(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account_external_id: str,
    mode: str,
    window_start_date: date,
    window_end_date: date,
    account: Account | None = None,
) -> TransactionImportWindow:
    if account is None:
        account = await _find_account(
            db,
            user_id=user_id,
            provider=provider,
            account_external_id=account_external_id,
        )
    institution_id, account_id = _account_identity(account)
    if institution_id is None or account_id is None:
        raise RuntimeError(
            f"Account {account_external_id} is not attached to a concrete {provider} connection"
        )
    row = (
        await db.execute(
            select(TransactionImportWindow).where(
                TransactionImportWindow.user_id == user_id,
                TransactionImportWindow.institution_id == institution_id,
                TransactionImportWindow.account_id == account_id,
                TransactionImportWindow.mode == mode,
                TransactionImportWindow.window_start_date == window_start_date,
                TransactionImportWindow.window_end_date == window_end_date,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = TransactionImportWindow(
            user_id=user_id,
            institution_id=institution_id,
            account_id=account_id,
            provider=provider,
            account_external_id=account_external_id,
            mode=mode,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
            status="pending",
            transaction_count=0,
            attempt_count=0,
            updated_at=_utcnow(),
        )
        db.add(row)
        await db.flush()
    else:
        row.institution_id = institution_id or row.institution_id
        row.account_id = account_id or row.account_id
    return row


async def record_transaction_window_started(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
    account_external_id: str,
    account_name: str,
    account_type: str | None,
    is_liability: bool,
    mode: str,
    window_start_date: date,
    window_end_date: date,
    target_start_date: date | None = None,
    target_end_date: date | None = None,
    sync_id: str | None = None,
) -> None:
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        account_name=account_name,
        account_type=account_type,
        is_liability=is_liability,
        target_start_date=(target_start_date or window_start_date) if mode == "backfill" else None,
        target_end_date=(target_end_date or window_end_date) if mode == "backfill" else None,
        account=account,
        replace_target_range=bool(mode == "backfill" and target_start_date and target_end_date),
    )
    now = _utcnow()
    row = await _upsert_window(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        mode=mode,
        window_start_date=window_start_date,
        window_end_date=window_end_date,
        account=account,
    )
    row.status = WINDOW_STATUS_RUNNING
    row.started_at = now
    row.updated_at = now
    row.sync_id = sync_id or row.sync_id
    row.attempt_count = int(row.attempt_count or 0) + 1
    row.last_error = None
    state = await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        account_name=account_name,
        account_type=account_type,
        is_liability=is_liability,
        account=account,
    )
    if mode == "backfill":
        state.backfill_status = BACKFILL_STATUS_RUNNING
    state.updated_at = now
    await _record_current_transaction_job_progress(
        db,
        user_id=user_id,
        provider=provider,
        label=_window_progress_label(
            "Started",
            mode=mode,
            account_name=account_name,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
        ),
        sync_id=sync_id,
    )
    mark_dirty_after_commit(db, user_id, EVENT_TRANSACTION_IMPORT_STATUS)


async def _refresh_backfill_completion(
    db: AsyncSession,
    *,
    state: TransactionImportAccountState,
) -> None:
    target_start = _value_to_date(state.target_start_date)
    target_end = _value_to_date(state.target_end_date)
    if not target_start or not target_end:
        return
    rows = await _query_account_windows(
        db,
        user_id=state.user_id,
        provider=state.provider,
        account_external_id=state.account_external_id,
        mode="backfill",
        institution_id=state.institution_id,
    )
    effective_rows = _effective_backfill_window_rows(rows)
    effective_rows = [
        row
        for row in effective_rows
        for window_range in [_window_date_range(row)]
        if window_range is not None
        and max(window_range[0], target_start) <= min(window_range[1], target_end)
    ]
    completed_ranges = sorted(
        (
            (_value_to_date(row.window_start_date), _value_to_date(row.window_end_date))
            for row in effective_rows
            if row.status == WINDOW_STATUS_COMPLETE
        ),
        key=lambda item: item[0] or date.max,
    )
    completed_ranges = [
        (start, end)
        for start, end in completed_ranges
        if start is not None and end is not None
    ]
    error_count = sum(1 for row in effective_rows if row.status == WINDOW_STATUS_ERROR)
    coverage_cursor = target_start
    for start, end in completed_ranges:
        if start > coverage_cursor:
            break
        if end >= coverage_cursor:
            coverage_cursor = end + timedelta(days=1)
        if coverage_cursor > target_end:
            break
    target_covered = coverage_cursor > target_end
    now = _utcnow()
    if target_covered:
        state.backfill_status = BACKFILL_STATUS_COMPLETE
        state.backfill_completed_at = state.backfill_completed_at or now
    elif completed_ranges:
        state.backfill_status = BACKFILL_STATUS_PARTIAL
    elif error_count:
        state.backfill_status = BACKFILL_STATUS_ERROR
    else:
        state.backfill_status = BACKFILL_STATUS_QUEUED
    state.updated_at = now


async def record_transaction_window_completed(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
    account_external_id: str,
    account_name: str,
    account_type: str | None,
    is_liability: bool,
    mode: str,
    window_start_date: date,
    window_end_date: date,
    transaction_count: int,
    target_start_date: date | None = None,
    target_end_date: date | None = None,
    sync_id: str | None = None,
) -> None:
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    state = await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        account_name=account_name,
        account_type=account_type,
        is_liability=is_liability,
        target_start_date=(target_start_date or window_start_date) if mode == "backfill" else None,
        target_end_date=(target_end_date or window_end_date) if mode == "backfill" else None,
        account=account,
        replace_target_range=bool(mode == "backfill" and target_start_date and target_end_date),
    )
    row = await _upsert_window(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        mode=mode,
        window_start_date=window_start_date,
        window_end_date=window_end_date,
        account=account,
    )
    now = _utcnow()
    row.status = WINDOW_STATUS_COMPLETE
    row.transaction_count = max(int(transaction_count or 0), 0)
    row.completed_at = now
    row.updated_at = now
    row.sync_id = sync_id or row.sync_id
    row.last_error = None
    state.last_successful_window_start_date = window_start_date
    state.last_successful_window_end_date = window_end_date
    state.last_successful_fetch_at = now
    state.last_error = None
    if mode == "incremental":
        state.incremental_start_date = window_start_date
        state.incremental_end_date = window_end_date
    await _refresh_backfill_completion(db, state=state)
    await _record_current_transaction_job_progress(
        db,
        user_id=user_id,
        provider=provider,
        label=_window_progress_label(
            "Completed",
            mode=mode,
            account_name=account_name,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
        ),
        sync_id=sync_id,
    )
    mark_dirty_after_commit(db, user_id, EVENT_TRANSACTION_IMPORT_STATUS)


async def record_transaction_window_failed(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
    account_external_id: str,
    account_name: str,
    account_type: str | None,
    is_liability: bool,
    mode: str,
    window_start_date: date,
    window_end_date: date,
    error: str,
    target_start_date: date | None = None,
    target_end_date: date | None = None,
    sync_id: str | None = None,
) -> None:
    account = await _find_account(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        institution_id=institution_id,
    )
    state = await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        account_name=account_name,
        account_type=account_type,
        is_liability=is_liability,
        target_start_date=(target_start_date or window_start_date) if mode == "backfill" else None,
        target_end_date=(target_end_date or window_end_date) if mode == "backfill" else None,
        account=account,
        replace_target_range=bool(mode == "backfill" and target_start_date and target_end_date),
    )
    row = await _upsert_window(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account_external_id,
        mode=mode,
        window_start_date=window_start_date,
        window_end_date=window_end_date,
        account=account,
    )
    now = _utcnow()
    row.status = WINDOW_STATUS_ERROR
    row.last_error = str(error or "")[:1000]
    row.updated_at = now
    row.sync_id = sync_id or row.sync_id
    state.backfill_status = BACKFILL_STATUS_ERROR if mode == "backfill" else state.backfill_status
    state.last_error = row.last_error
    state.updated_at = now
    await _record_current_transaction_job_progress(
        db,
        user_id=user_id,
        provider=provider,
        label=_window_progress_label(
            "Failed",
            mode=mode,
            account_name=account_name,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
        ),
        sync_id=sync_id,
    )
    mark_dirty_after_commit(db, user_id, EVENT_TRANSACTION_IMPORT_STATUS)


async def record_successful_account_transaction_sync(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    account: Account,
    transaction_count: int,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    backfill_chunk_days: int = DEFAULT_BACKFILL_DAYS,
    overlap_days: int = DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    today: date | None = None,
) -> None:
    if not account.external_id:
        return
    today = today or await get_user_local_today_from_db(db, user_id)
    target_start = today - timedelta(days=max(int(backfill_days or DEFAULT_BACKFILL_DAYS), 1))
    state = await ensure_account_state(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account.external_id,
        account_name=account.name,
        account_type=account.account_type,
        is_liability=bool(account.is_liability),
        target_start_date=target_start,
        target_end_date=today,
        account=account,
    )
    latest_persisted = await _latest_persisted_transaction_date(
        db,
        user_id=user_id,
        provider=provider,
        account_external_id=account.external_id,
        institution_id=account.institution_id,
    )
    if state.backfill_status == BACKFILL_STATUS_COMPLETE:
        end_date = today
        start_date = (latest_persisted or today) - timedelta(days=overlap_days)
        if start_date > end_date:
            start_date = end_date
        await record_transaction_window_completed(
            db,
            user_id=user_id,
            provider=provider,
            institution_id=account.institution_id,
            account_external_id=account.external_id,
            account_name=account.name,
            account_type=account.account_type,
            is_liability=bool(account.is_liability),
            mode="incremental",
            window_start_date=start_date,
            window_end_date=end_date,
            transaction_count=transaction_count,
        )
        return

    for start, end in _split_windows(target_start, today, backfill_chunk_days):
        await record_transaction_window_completed(
            db,
            user_id=user_id,
            provider=provider,
            institution_id=account.institution_id,
            account_external_id=account.external_id,
            account_name=account.name,
            account_type=account.account_type,
            is_liability=bool(account.is_liability),
            mode="backfill",
            window_start_date=start,
            window_end_date=end,
            transaction_count=transaction_count,
        )


def _account_status_from_windows(
    *,
    state: TransactionImportAccountState | None,
    total_windows: int,
    completed_windows: int,
    failed_windows: int,
    running_windows: int,
    has_active_job: bool,
) -> str:
    if state and state.backfill_status == BACKFILL_STATUS_COMPLETE:
        return "incremental"
    if running_windows:
        return "backfill_running"
    if failed_windows:
        return "needs_retry"
    if total_windows and completed_windows >= total_windows:
        return "incremental"
    if completed_windows:
        return "backfill_queued" if has_active_job else "needs_retry"
    if state:
        if has_active_job:
            return "backfill_queued"
        return "needs_retry" if total_windows else "not_started"
    return "backfill_queued" if has_active_job else "not_started"


def _status_label(status: str) -> str:
    labels = {
        "incremental": "Backfill complete",
        "backfill_running": "Importing transactions",
        "needs_retry": "Retry needed",
        "backfill_partial": "Retry needed",
        "backfill_queued": "Queued",
        "not_started": "Not started",
    }
    return labels.get(status, status.replace("_", " ").title())


def _history_status(status: str) -> str:
    if status == "incremental":
        return "complete"
    if status == "backfill_running":
        return "running"
    if status == "backfill_queued":
        return "queued"
    if status in {"needs_retry", "backfill_partial"}:
        return "retry_needed"
    return "not_started"


def _history_status_label(status: str) -> str:
    labels = {
        "complete": "Backfill complete",
        "running": "Importing history",
        "retry_needed": "Retry needed",
        "queued": "Queued",
        "not_started": "Not started",
    }
    return labels.get(status, status.replace("_", " ").title())


def _current_fetch_label(status: str, mode: str | None) -> str | None:
    if status == "running":
        return "Fetching transactions"
    if status == "queued":
        return "Queued"
    if status == "retry_needed":
        return "Recent transactions retry needed" if mode == "incremental" else "Retry needed"
    return None


def _current_fetch_status(
    *,
    active_job: TransactionImportJob | None,
    history_status: str,
    running_incremental: int,
    failed_incremental: int,
    account_has_running_backfill_window: bool,
    account_last_successful_fetch_at: datetime | None = None,
    account_backfill_complete: bool = False,
) -> tuple[str, str | None, str | None]:
    if running_incremental and active_job:
        return "running", "incremental", _current_fetch_label("running", "incremental")
    if account_has_running_backfill_window and active_job:
        return "running", "backfill", _current_fetch_label("running", "backfill")
    if history_status == "running":
        return "running", "backfill", _current_fetch_label("running", "backfill")
    if active_job and history_status == "queued" and active_job.status in {"queued", "running"}:
        return "queued", "backfill", _current_fetch_label("queued", "backfill")
    if active_job and active_job.status in {"queued", "running"} and history_status != "retry_needed":
        job_started_at = _aware_utc(active_job.started_at) if active_job.started_at else None
        last_fetched_at = _aware_utc(account_last_successful_fetch_at) if account_last_successful_fetch_at else None
        not_touched_yet = (
            last_fetched_at is None
            or (job_started_at is not None and last_fetched_at < job_started_at)
        )
        if not_touched_yet:
            mode = "incremental" if account_backfill_complete else "backfill"
            return "queued", mode, _current_fetch_label("queued", mode)
    if failed_incremental:
        return "retry_needed", "incremental", _current_fetch_label("retry_needed", "incremental")
    return "idle", None, None


def _job_is_active(job: TransactionImportJob | None) -> bool:
    return bool(job and job.status in {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING})


def _job_is_surfaceable_terminal(job: TransactionImportJob | None) -> bool:
    return bool(job and job.status in {JOB_STATUS_FAILED, JOB_STATUS_AUTH_REQUIRED})


def _account_status_label(status: str) -> str:
    return _status_label(status)


def _institution_status_from_account_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "not_started"
    if all(status == "not_started" for status in statuses):
        return "not_started"
    if any(status == "needs_retry" for status in statuses):
        return "needs_retry"
    if any(status == "backfill_running" for status in statuses):
        return "backfill_running"
    if all(status == "backfill_queued" for status in statuses):
        return "backfill_queued"
    if any(status in {"backfill_partial", "backfill_queued", "not_started"} for status in statuses):
        return "needs_retry"
    return "incremental"


def _institution_history_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_started"
    if all(status == "not_started" for status in statuses):
        return "not_started"
    if any(status == "retry_needed" for status in statuses):
        return "retry_needed"
    if any(status == "running" for status in statuses):
        return "running"
    if all(status == "queued" for status in statuses):
        return "queued"
    if any(status in {"queued", "not_started"} for status in statuses):
        return "retry_needed"
    return "complete"


def _institution_current_fetch_status(
    statuses: list[str],
    modes: list[str | None],
    *,
    active_job: TransactionImportJob | None = None,
) -> tuple[str, str | None, str | None]:
    if not statuses:
        return "idle", None, None
    if "running" in statuses:
        mode = "backfill" if "backfill" in modes else "incremental"
        return "running", mode, _current_fetch_label("running", mode)
    if active_job and active_job.status == "running" and "queued" in statuses:
        mode = "backfill" if "backfill" in modes else "incremental"
        return "running", mode, _current_fetch_label("running", mode)
    if "queued" in statuses:
        mode = "backfill" if "backfill" in modes else "incremental"
        return "queued", mode, _current_fetch_label("queued", mode)
    if "retry_needed" in statuses:
        mode = "backfill" if "backfill" in modes else "incremental"
        return "retry_needed", mode, _current_fetch_label("retry_needed", mode)
    return "idle", None, None


def _window_matches_active_job(
    window: TransactionImportWindow,
    active_job: TransactionImportJob | None,
) -> bool:
    if not active_job:
        return False
    window_sync_id = str(window.sync_id or "").strip()
    job_sync_ids = {
        str(value or "").strip()
        for value in (active_job.sync_id, active_job.source_sync_id)
        if str(value or "").strip()
    }
    return not window_sync_id or not job_sync_ids or window_sync_id in job_sync_ids


def _job_rank_timestamp(job: TransactionImportJob) -> float:
    timestamp = (
        job.updated_at
        or job.finished_at
        or job.started_at
        or job.created_at
    )
    if not timestamp:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


def _select_surfaceable_import_job(
    jobs: list[TransactionImportJob],
) -> TransactionImportJob | None:
    active_jobs = [
        job for job in jobs if job.status in {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}
    ]
    if active_jobs:
        return max(active_jobs, key=_job_rank_timestamp)

    terminal_jobs = [
        job for job in jobs if job.status in {JOB_STATUS_FAILED, JOB_STATUS_AUTH_REQUIRED}
    ]
    if not terminal_jobs:
        return None

    latest_terminal = max(terminal_jobs, key=_job_rank_timestamp)
    latest_complete_timestamp = max(
        (
            _job_rank_timestamp(job)
            for job in jobs
            if job.status == JOB_STATUS_COMPLETE
        ),
        default=0.0,
    )
    if latest_complete_timestamp > _job_rank_timestamp(latest_terminal):
        return None
    return latest_terminal


async def get_transaction_import_status_payload(db: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await db.execute(
        select(Account, Institution)
        .join(Institution, Account.institution_id == Institution.id)
        .where(
            Account.user_id == user_id,
            Institution.user_id == user_id,
            Institution.hidden.is_(False),
            Institution.enabled.is_(True),
            Institution.provider.notin_(
                (MANUAL_PROVIDER, MANUAL_INSTITUTION_PROVIDER, *ASSET_GROUP_PROVIDERS)
            ),  # connector-less (cash, user manual, net-worth asset/debt buckets) never sync
            Account.hidden.is_(False),
            # Statement-imported (balance-only) accounts live under a real provider but are
            # never synced, so they have no transaction import to track or report.
            Account.is_imported.is_(False),
        )
        .order_by(Institution.name, Account.name)
    )
    account_rows = result.all()
    if not account_rows:
        return {"institutions": [], "generated_at": _utcnow().isoformat()}

    account_ids = [account.id for account, _institution in account_rows if account.id is not None]
    state_rows = []
    window_rows = []
    if account_ids:
        state_rows = (
            await db.execute(
                select(TransactionImportAccountState).where(
                    TransactionImportAccountState.user_id == user_id,
                    TransactionImportAccountState.account_id.in_(account_ids),
                )
            )
        ).scalars().all()
        window_rows = (
            await db.execute(
                select(TransactionImportWindow).where(
                    TransactionImportWindow.user_id == user_id,
                    TransactionImportWindow.account_id.in_(account_ids),
                )
            )
        ).scalars().all()

    state_by_key = {
        (int(state.institution_id), int(state.account_id)): state
        for state in state_rows
        if state.institution_id is not None and state.account_id is not None
    }
    recent_terminal_job_cutoff = _utcnow() - timedelta(seconds=JOB_STATUS_SURFACE_TERMINAL_FOR_SECONDS)
    surfaceable_job_rows = (
        await db.execute(
            select(TransactionImportJob).where(
                TransactionImportJob.user_id == user_id,
                or_(
                    TransactionImportJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
                    and_(
                        TransactionImportJob.status.in_(
                            (
                                JOB_STATUS_COMPLETE,
                                JOB_STATUS_FAILED,
                                JOB_STATUS_AUTH_REQUIRED,
                            )
                        ),
                        TransactionImportJob.updated_at >= recent_terminal_job_cutoff,
                    ),
                ),
            )
        )
    ).scalars().all()
    jobs_by_connection: dict[int, list[TransactionImportJob]] = {}
    for job in surfaceable_job_rows:
        if job.institution_id is not None:
            jobs_by_connection.setdefault(int(job.institution_id), []).append(job)
    active_job_by_connection = {}
    for institution_id, jobs in jobs_by_connection.items():
        selected_job = _select_surfaceable_import_job(jobs)
        if selected_job is not None:
            active_job_by_connection[institution_id] = selected_job
    windows_by_key: dict[tuple[int, int], list[TransactionImportWindow]] = {}
    for window in window_rows:
        if window.institution_id is not None and window.account_id is not None:
            windows_by_key.setdefault((int(window.institution_id), int(window.account_id)), []).append(window)

    tx_range_by_account_id: dict[int, tuple[Any, Any]] = {}
    if account_ids:
        tx_range_rows = (
            await db.execute(
                select(
                    Transaction.account_id,
                    func.min(Transaction.date).label("first_date"),
                    func.max(Transaction.date).label("last_date"),
                )
                .where(
                    Transaction.user_id == user_id,
                    Transaction.account_id.in_(account_ids),
                )
                .group_by(Transaction.account_id)
            )
        ).all()
        tx_range_by_account_id = {
            int(row.account_id): (row.first_date, row.last_date) for row in tx_range_rows
        }

    institutions: dict[int, dict[str, Any]] = {}
    for account, institution in account_rows:
        external_id = account.external_id or ""
        key = (int(institution.id), int(account.id))
        state = state_by_key.get(key)
        account_windows = windows_by_key.get(key, [])
        backfill_windows = [window for window in account_windows if window.mode == "backfill"]
        incremental_windows = [window for window in account_windows if window.mode == "incremental"]
        effective_backfill_windows = _effective_backfill_window_rows(backfill_windows)
        target_start = _value_to_date(state.target_start_date if state else None)
        target_end = _value_to_date(state.target_end_date if state else None)
        if target_start and target_end:
            effective_backfill_windows = [
                window
                for window in effective_backfill_windows
                for window_range in [_window_date_range(window)]
                if window_range is not None
                and max(window_range[0], target_start) <= min(window_range[1], target_end)
            ]
        completed_backfill = [window for window in effective_backfill_windows if window.status == WINDOW_STATUS_COMPLETE]
        failed_backfill = [window for window in effective_backfill_windows if window.status == WINDOW_STATUS_ERROR]
        active_job = active_job_by_connection.get(int(institution.id))
        running_backfill = [
            window
            for window in effective_backfill_windows
            if window.status == WINDOW_STATUS_RUNNING
            and _window_matches_active_job(window, active_job)
        ]
        raw_running_backfill = [
            window
            for window in backfill_windows
            for window_range in [_window_date_range(window)]
            if window.status == WINDOW_STATUS_RUNNING
            and (
                target_start is None
                or target_end is None
                or (
                    window_range is not None
                    and max(window_range[0], target_start) <= min(window_range[1], target_end)
                )
            )
            and _window_matches_active_job(window, active_job)
        ]
        failed_incremental = _uncovered_failed_window_rows(incremental_windows)
        running_incremental = [
            window
            for window in incremental_windows
            if window.status == WINDOW_STATUS_RUNNING
            and _window_matches_active_job(window, active_job)
        ]
        known_ranges = _window_ranges(effective_backfill_windows)
        if target_start and target_end:
            known_ranges = _clip_ranges_to_target(known_ranges, target_start, target_end)
        untracked_ranges = ()
        if target_start and target_end:
            target_windows = _split_windows(target_start, target_end, DEFAULT_BACKFILL_CHUNK_DAYS)
            untracked_ranges = _untracked_window_ranges(target_windows, known_ranges)
        total_windows = len(_deduplicate_ranges((*known_ranges, *untracked_ranges)))
        if target_start and target_end and not total_windows:
            total_windows = len(_split_windows(target_start, target_end, DEFAULT_BACKFILL_CHUNK_DAYS))
        completed_dates = [
            (_value_to_date(window.window_start_date), _value_to_date(window.window_end_date))
            for window in completed_backfill
        ]
        completed_dates = [
            (start, end) for start, end in completed_dates if start and end
        ]
        latest_incremental_start_date, latest_incremental_end_date = (
            _latest_completed_window_range(
                [*effective_backfill_windows, *incremental_windows]
            )
        )
        current_window = sorted(
            running_backfill,
            key=lambda window: (window.started_at or window.updated_at or datetime.min),
            reverse=True,
        )[0] if running_backfill else (
            sorted(
                raw_running_backfill,
                key=lambda window: (window.started_at or window.updated_at or datetime.min),
                reverse=True,
            )[0] if raw_running_backfill else None
        )
        status = _account_status_from_windows(
            state=state,
            total_windows=total_windows,
            completed_windows=len(completed_backfill),
            failed_windows=len(failed_backfill),
            running_windows=len(running_backfill) or len(raw_running_backfill),
            has_active_job=_job_is_active(active_job),
        )
        if (
            status == "not_started"
            and not state
            and not total_windows
            and _job_is_surfaceable_terminal(active_job)
        ):
            status = "needs_retry"
        history_status = _history_status(status)
        current_fetch_status, current_fetch_mode, current_fetch_label = _current_fetch_status(
            active_job=active_job,
            history_status=history_status,
            running_incremental=len(running_incremental),
            failed_incremental=len(failed_incremental),
            account_has_running_backfill_window=bool(raw_running_backfill),
            account_last_successful_fetch_at=(state.last_successful_fetch_at if state else None),
            account_backfill_complete=bool(state and state.backfill_status == BACKFILL_STATUS_COMPLETE),
        )
        account_payload = {
            "account_id": account.id,
            "account_name": account.name,
            "account_type": account.account_type,
            "is_liability": bool(account.is_liability),
            "status": status,
            "status_label": _account_status_label(status),
            "history_status": history_status,
            "history_status_label": _history_status_label(history_status),
            "current_fetch_status": current_fetch_status,
            "current_fetch_label": current_fetch_label,
            "current_fetch_mode": current_fetch_mode,
            "provider": institution.provider,
            "account_external_id": external_id,
            "backfill_start_date": target_start.isoformat() if target_start else None,
            "backfill_end_date": target_end.isoformat() if target_end else None,
            "completed_start_date": min((start for start, _end in completed_dates), default=None).isoformat()
            if completed_dates
            else None,
            "completed_end_date": max((end for _start, end in completed_dates), default=None).isoformat()
            if completed_dates
            else None,
            "data_start_date": (
                tx_range_by_account_id.get(int(account.id), (None, None))[0].isoformat()
                if account.id is not None
                and tx_range_by_account_id.get(int(account.id), (None, None))[0] is not None
                else None
            ),
            "data_end_date": (
                tx_range_by_account_id.get(int(account.id), (None, None))[1].isoformat()
                if account.id is not None
                and tx_range_by_account_id.get(int(account.id), (None, None))[1] is not None
                else None
            ),
            "completed_windows": len(completed_backfill),
            "total_windows": total_windows,
            "pending_windows": max(total_windows - len(completed_backfill), 0),
            "failed_windows": len(failed_backfill),
            "current_window_start_date": (
                current_window.window_start_date
                if current_fetch_status == "running" and current_window
                else None
            ),
            "current_window_end_date": (
                current_window.window_end_date
                if current_fetch_status == "running" and current_window
                else None
            ),
            "latest_incremental_start_date": latest_incremental_start_date or (
                state.incremental_start_date if state else None
            ),
            "latest_incremental_end_date": latest_incremental_end_date or (
                state.incremental_end_date if state else None
            ),
            "last_successful_fetch_at": (
                state.last_successful_fetch_at.isoformat()
                if state and state.last_successful_fetch_at
                else None
            ),
            "last_error": state.last_error if state else (active_job.last_error if active_job else None),
            "transaction_count": sum(
                int(window.transaction_count or 0)
                for window in (*effective_backfill_windows, *incremental_windows)
            ),
        }
        institution_payload = institutions.setdefault(
            institution.id,
            {
                "institution_id": institution.id,
                "institution": institution.name,
                "provider": institution.provider,
                "status": "not_started",
                "status_label": _status_label("not_started"),
                "transaction_import_job_status": (
                    active_job.status if active_job else None
                ),
                "transaction_import_job_id": active_job.job_id if active_job else None,
                "transaction_import_job_last_progress_at": (
                    active_job.last_progress_at.isoformat()
                    if active_job and active_job.last_progress_at
                    else None
                ),
                "transaction_import_job_last_progress_label": (
                    active_job.last_progress_label if active_job else None
                ),
                "transaction_import_job_stale_recovery_count": (
                    int(active_job.stale_recovery_count or 0) if active_job else 0
                ),
                "accounts": [],
            },
        )
        institution_payload["accounts"].append(account_payload)

    for institution_payload in institutions.values():
        active_job = active_job_by_connection.get(int(institution_payload["institution_id"]))
        if active_job and active_job.status == "running":
            accounts = institution_payload["accounts"]
            has_visible_running = any(
                account["current_fetch_status"] == "running"
                for account in accounts
            )
            queued_accounts = [
                account
                for account in accounts
                if account["current_fetch_status"] == "queued"
            ]
            if not has_visible_running and len(queued_accounts) == 1:
                queued_account = queued_accounts[0]
                queued_account["current_fetch_status"] = "running"
                queued_account["current_fetch_mode"] = queued_account["current_fetch_mode"] or "backfill"
                queued_account["current_fetch_label"] = _current_fetch_label(
                    "running",
                    queued_account["current_fetch_mode"],
                )

        statuses = [account["status"] for account in institution_payload["accounts"]]
        status = _institution_status_from_account_statuses(statuses)
        institution_payload["status"] = status
        institution_payload["status_label"] = _status_label(status)
        history_statuses = [account["history_status"] for account in institution_payload["accounts"]]
        history_status = _institution_history_status(history_statuses)
        institution_payload["history_status"] = history_status
        institution_payload["history_status_label"] = _history_status_label(history_status)
        current_fetch_status, current_fetch_mode, current_fetch_label = _institution_current_fetch_status(
            [account["current_fetch_status"] for account in institution_payload["accounts"]],
            [account["current_fetch_mode"] for account in institution_payload["accounts"]],
            active_job=active_job,
        )
        institution_payload["current_fetch_status"] = current_fetch_status
        institution_payload["current_fetch_label"] = current_fetch_label
        institution_payload["current_fetch_mode"] = current_fetch_mode

    return {
        "institutions": list(institutions.values()),
        "generated_at": _utcnow().isoformat(),
    }
