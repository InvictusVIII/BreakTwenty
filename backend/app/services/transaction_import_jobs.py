from __future__ import annotations

import asyncio
import logging
import secrets
import traceback
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.connector_logging import (
    get_connector_logger,
    log_connector_event,
    safe_connector_public_message,
)
from app.connectors.sync_context import normalize_sync_scope
from app.database import async_session
from app.models import (
    Institution,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
)
from app.provider_catalog import (
    get_lock_provider_for_route,
    provider_uses_background_transaction_import,
    provider_uses_daily_transaction_import_dedupe,
)
from app.services.event_bus import (
    EVENT_SYNC_ACTIVITY,
    EVENT_TRANSACTION_IMPORT_STATUS,
    mark_dirty,
)
from app.services.provider_keys import provider_status_key
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.sync_utils import (
    acquire_sync_lock,
    is_network_error,
    release_sync_lock,
)
from app.services.task_supervisor import create_tracked_task
from app.services.time_utils import aware_utc as _aware_utc
from app.services.time_utils import utc_now as _utcnow
from app.services.user_utils import get_user_timezone_info_from_db

logger = logging.getLogger("breaktwenty.transaction_import_jobs")

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETE = "complete"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_AUTH_REQUIRED = "auth_required"
ACTIVE_JOB_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)
SYNC_SCOPE_TRANSACTIONS = "transactions"
TRANSACTION_JOB_WATCHDOG_INTERVAL_SECONDS = 60
TRANSACTION_JOB_STALE_REQUEUE_DELAY_SECONDS = 15
TRANSACTION_JOB_MAX_STALE_RECOVERIES = 1
TRANSACTION_JOB_STALE_ERROR = "Transaction import stalled before the current window completed."
TRANSACTION_JOB_QUEUED_REVIVE_SECONDS = 60
# Reasons that represent a one-shot, user-initiated action rather than resumable
# background work. If such a job is interrupted/orphaned, the watchdog must NOT
# silently re-run it — the action is over, so a stale one is failed and the data is
# picked up by the next scheduled/manual sync (a `sync_batch`/`manual_sync` job).
# This is what keeps "syncs only fire from explicit app jobs, never on their own".
# ``provider_add`` remains recognized for durable jobs created before the
# canonical add-flow source was renamed to ``add_connection``.
NON_REVIVABLE_JOB_REASONS = frozenset({"add_connection", "provider_add"})
TRANSACTION_JOB_CRASH_ERROR_PREFIX = "Transaction import task crashed"
TRANSACTION_JOB_LEASE_DURATION = timedelta(minutes=5)
TRANSACTION_JOB_LEASE_RENEW_INTERVAL_SECONDS = 60

_tasks: dict[str, asyncio.Task] = {}
_watchdog_task: asyncio.Task | None = None


def transaction_import_task_snapshot() -> dict[str, dict[str, bool]]:
    snapshot: dict[str, dict[str, bool]] = {}
    for job_id, task in _tasks.items():
        snapshot[str(job_id)] = {
            "done": bool(task.done()),
            "cancelled": bool(task.cancelled()),
        }
    return snapshot


def _new_job_id(provider: str) -> str:
    return f"{provider}-tximport-{secrets.token_hex(8)}"


def _job_payload(job: TransactionImportJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "provider": job.provider,
        "institution_id": job.institution_id,
        "status": job.status,
        "sync_id": job.sync_id,
        "source_sync_id": job.source_sync_id,
        "attempt_id": job.attempt_id,
        "reason": job.reason,
        "last_error": job.last_error,
        "last_progress_at": job.last_progress_at.isoformat() if job.last_progress_at else None,
        "last_progress_label": job.last_progress_label,
        "stale_recovery_count": int(job.stale_recovery_count or 0),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _schedule_job(job_id: str, *, delay_seconds: float = 0.5) -> None:
    existing = _tasks.get(job_id)
    if existing and not existing.done():
        return
    try:
        task = create_tracked_task(
            _run_transaction_import_job(job_id, delay_seconds=delay_seconds),
            name=f"transaction-import:{job_id}",
        )
    except RuntimeError:
        return
    task.add_done_callback(lambda completed_task: _on_job_task_done(job_id, completed_task))
    _tasks[job_id] = task


def _on_job_task_done(job_id: str, task: asyncio.Task) -> None:
    _tasks.pop(job_id, None)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return
    if exc is None:
        return
    logger.error(
        "transaction import task crashed job_id=%s exc=%r",
        job_id,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    try:
        create_tracked_task(
            _mark_job_failed_after_crash(job_id, exc),
            name=f"transaction-import-crash-cleanup:{job_id}",
        )
    except RuntimeError:
        pass
    _archive_crashed_job(job_id, exc)


def _archive_crashed_job(job_id: str, exc: BaseException) -> None:
    try:
        from app.services.support_auto_archive import archive_run_fire_and_forget
    except Exception:
        return

    async def _resolve_and_archive() -> None:
        try:
            job = await _load_job(job_id)
        except Exception:
            job = None
        archive_run_fire_and_forget(
            user_id=int(job.user_id) if job and job.user_id is not None else None,
            provider=str(job.provider) if job and job.provider else "unknown",
            sync_id=(job.source_sync_id or job.sync_id) if job else None,
            attempt_id=(job.attempt_id if job else None),
            trigger="tximport_task_crashed",
            error=f"{type(exc).__name__}: {exc}",
            extra_fields={
                "job_id": job_id,
                "sync_source": (str(job.reason or "") or None) if job else None,
            },
            traceback_text="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )

    try:
        create_tracked_task(
            _resolve_and_archive(),
            name=f"transaction-import-crash-archive:{job_id}",
        )
    except RuntimeError:
        return


async def _mark_job_failed_after_crash(job_id: str, exc: BaseException) -> None:
    error_text = f"{TRANSACTION_JOB_CRASH_ERROR_PREFIX}: {type(exc).__name__}: {exc}"
    try:
        await _mark_job(
            job_id,
            JOB_STATUS_FAILED,
            error=error_text,
            finished=True,
            progress_label="Transaction import task crashed.",
            clear_lease=True,
        )
    except Exception as cleanup_exc:
        logger.warning(
            "transaction import crash cleanup failed job_id=%s exc=%r",
            job_id,
            cleanup_exc,
        )


async def _schedule_job_later(job_id: str, *, delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)
    _schedule_job(job_id)


def _provider_lock(provider: str) -> str:
    try:
        return get_lock_provider_for_route(provider)
    except Exception:
        return provider


def _log_provider_job_event(
    provider: str,
    stage: str,
    *,
    level: str = "info",
    debug: bool = False,
    **fields: Any,
) -> None:
    log_connector_event(
        get_connector_logger(provider),
        provider=provider,
        stage=stage,
        level=level,
        debug=debug,
        **fields,
    )


def _transaction_state_providers(provider: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((provider, provider_status_key(provider))))


def _background_import_enabled(provider: str) -> bool:
    try:
        return provider_uses_background_transaction_import(provider)
    except Exception:
        return False


def _daily_dedupe_enabled(provider: str) -> bool:
    try:
        return provider_uses_daily_transaction_import_dedupe(provider)
    except Exception:
        return False


async def _latest_completed_daily_job(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int,
    now: datetime,
) -> TransactionImportJob | None:
    if not _daily_dedupe_enabled(provider):
        return None
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    today = now.astimezone(user_tz).date()
    latest = (
        await db.execute(
            select(TransactionImportJob)
            .where(
                TransactionImportJob.user_id == user_id,
                TransactionImportJob.provider == provider,
                TransactionImportJob.institution_id == institution_id,
                TransactionImportJob.status == JOB_STATUS_COMPLETE,
                TransactionImportJob.finished_at.is_not(None),
            )
            .order_by(TransactionImportJob.finished_at.desc())
        )
    ).scalars().first()
    if not latest or not latest.finished_at:
        return None
    finished_at = _aware_utc(latest.finished_at)
    if finished_at and finished_at.astimezone(user_tz).date() == today:
        return latest
    return None


async def _mark_running_windows_retryable(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int,
    error: str,
    now: datetime,
) -> int:
    providers = _transaction_state_providers(provider)
    rows = (
        await db.execute(
            select(TransactionImportWindow).where(
                TransactionImportWindow.user_id == user_id,
                TransactionImportWindow.provider.in_(providers),
                TransactionImportWindow.institution_id == institution_id,
                TransactionImportWindow.status == "running",
            )
        )
    ).scalars().all()
    if not rows:
        return 0

    affected_accounts = set()
    affected_backfill_accounts = set()
    for row in rows:
        row.status = "error"
        row.last_error = error[:1000]
        row.updated_at = now
        affected_accounts.add(row.account_external_id)
        if row.mode == "backfill":
            affected_backfill_accounts.add(row.account_external_id)

    state_rows = (
        await db.execute(
            select(TransactionImportAccountState).where(
                TransactionImportAccountState.user_id == user_id,
                TransactionImportAccountState.provider.in_(providers),
                TransactionImportAccountState.institution_id == institution_id,
                TransactionImportAccountState.account_external_id.in_(affected_accounts),
            )
        )
    ).scalars().all()
    for state in state_rows:
        if state.account_external_id in affected_backfill_accounts:
            state.backfill_status = "error"
        state.last_error = error[:1000]
        state.updated_at = now

    return len(rows)


async def _find_institution_id(
    user_id: int,
    provider: str,
    *,
    candidate_institution_id: int | None = None,
) -> int:
    status_provider = provider_status_key(provider)
    allowed_providers = {
        str(status_provider or "").strip().lower(),
        str(provider or "").strip().lower(),
    }
    async with async_session() as db:
        if candidate_institution_id is None:
            raise ValueError("institution_id is required for transaction import")
        candidate = await db.get(Institution, int(candidate_institution_id))
        if (
            candidate is None
            or int(candidate.user_id or 0) != int(user_id)
            or str(candidate.provider or "").strip().lower() not in allowed_providers
        ):
            raise ValueError("institution_id does not belong to this provider connection")
        return int(candidate.id)


async def enqueue_provider_transaction_import(
    user_id: int,
    provider: str,
    *,
    source_sync_id: str | None = None,
    attempt_id: str | None = None,
    reason: str = "sync",
    institution_id: int | None = None,
) -> dict[str, Any] | None:
    if not _background_import_enabled(provider):
        return None

    normalized_attempt_id = str(attempt_id or "").strip() or None
    institution_id = await _find_institution_id(
        user_id, provider, candidate_institution_id=institution_id
    )
    now = _utcnow()
    payload: dict[str, Any] | None = None
    schedule_job_id: str | None = None
    try:
        async with async_session() as db, sqlite_write_gate(), db.begin():
            existing = (
                await db.execute(
                    select(TransactionImportJob)
                    .where(
                        TransactionImportJob.user_id == user_id,
                        TransactionImportJob.provider == provider,
                        TransactionImportJob.institution_id == institution_id,
                        TransactionImportJob.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .order_by(TransactionImportJob.updated_at.desc())
                )
            ).scalars().first()
            if existing:
                existing.source_sync_id = source_sync_id or existing.source_sync_id
                existing.attempt_id = normalized_attempt_id or existing.attempt_id
                existing.reason = reason or existing.reason
                existing.updated_at = now
                payload = _job_payload(existing)
                schedule_job_id = existing.job_id
                _log_provider_job_event(
                    provider,
                    "transaction import enqueue reused",
                    user_id=user_id,
                    sync_id=existing.source_sync_id or existing.sync_id,
                    job_id=existing.job_id,
                    job_status=existing.status,
                    reason=reason,
                    source_sync_id=source_sync_id,
                    attempt_id=existing.attempt_id,
                    institution_id=institution_id,
                )
            else:
                completed_today = await _latest_completed_daily_job(
                    db,
                    user_id=user_id,
                    provider=provider,
                    institution_id=institution_id,
                    now=now,
                )
                if completed_today:
                    _log_provider_job_event(
                        provider,
                        "transaction import enqueue deduped",
                        user_id=user_id,
                        sync_id=source_sync_id or completed_today.source_sync_id or completed_today.sync_id,
                        job_id=completed_today.job_id,
                        job_status=completed_today.status,
                        reason=reason,
                        source_sync_id=source_sync_id,
                        attempt_id=normalized_attempt_id or completed_today.attempt_id,
                        institution_id=institution_id,
                    )
                    institution = await db.get(Institution, int(institution_id))
                    if (
                        institution is not None
                        and institution.user_id == user_id
                        and institution.sync_status != "ok"
                    ):
                        institution.sync_status = "ok"
                    payload = _job_payload(completed_today)
                else:
                    job = TransactionImportJob(
                        user_id=user_id,
                        institution_id=institution_id,
                        provider=provider,
                        job_id=_new_job_id(provider),
                        status=JOB_STATUS_QUEUED,
                        reason=reason,
                        source_sync_id=source_sync_id,
                        attempt_id=normalized_attempt_id,
                        created_at=now,
                        updated_at=now,
                    )
                    db.add(job)
                    await db.flush()
                    payload = _job_payload(job)
                    schedule_job_id = job.job_id
                    _log_provider_job_event(
                        provider,
                        "transaction import enqueue created",
                        user_id=user_id,
                        sync_id=source_sync_id,
                        job_id=job.job_id,
                        job_status=job.status,
                        reason=reason,
                        source_sync_id=source_sync_id,
                        attempt_id=normalized_attempt_id,
                        institution_id=institution_id,
                    )
    except IntegrityError:
        async with async_session() as db:
            existing = (
                await db.execute(
                    select(TransactionImportJob)
                    .where(
                        TransactionImportJob.user_id == user_id,
                        TransactionImportJob.provider == provider,
                        TransactionImportJob.institution_id == institution_id,
                        TransactionImportJob.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .order_by(TransactionImportJob.updated_at.desc())
                )
            ).scalars().first()
        if existing is None:
            raise
        payload = _job_payload(existing)
        schedule_job_id = existing.job_id
    if schedule_job_id:
        _schedule_job(schedule_job_id)
    return payload


async def resume_transaction_import_jobs(*, force_running: bool = False) -> None:
    affected_user_ids: set[int] = set()
    jobs_to_schedule: list[str] = []
    async with async_session() as db, sqlite_write_gate(), db.begin():
        rows = (
            await db.execute(
                select(TransactionImportJob).where(
                    TransactionImportJob.status.in_(ACTIVE_JOB_STATUSES)
                )
            )
        ).scalars().all()
        now = _utcnow()
        for job in rows:
            if (
                not force_running
                and job.status == JOB_STATUS_RUNNING
                and job.lease_expires_at is not None
                and _aware_utc(job.lease_expires_at) > now
            ):
                continue
            previous_status = str(job.status or "")
            resumable = str(job.reason or "") not in NON_REVIVABLE_JOB_REASONS
            job.status = JOB_STATUS_QUEUED if resumable else JOB_STATUS_FAILED
            job.lease_token = None
            job.lease_expires_at = None
            job.last_error = (
                "Transaction import resumed after backend restart."
                if resumable
                else "Interrupted one-shot add import; retry needed."
            )
            job.last_progress_at = now
            job.last_progress_label = (
                "Queued after backend restart."
                if resumable
                else "Interrupted by backend restart; retry needed."
            )
            job.updated_at = now
            job.finished_at = None if resumable else now
            if job.user_id is not None:
                affected_user_ids.add(int(job.user_id))
            if previous_status == JOB_STATUS_RUNNING or job.started_at is not None:
                await _mark_running_windows_retryable(
                    db,
                    user_id=job.user_id,
                    provider=job.provider,
                    institution_id=job.institution_id,
                    error=TRANSACTION_JOB_STALE_ERROR,
                    now=now,
                )
            if resumable:
                jobs_to_schedule.append(job.job_id)
    for user_id in affected_user_ids:
        mark_dirty(user_id, EVENT_SYNC_ACTIVITY)
        mark_dirty(user_id, EVENT_TRANSACTION_IMPORT_STATUS)
    for job_id in jobs_to_schedule:
        _schedule_job(job_id, delay_seconds=0.1)


async def _load_job(job_id: str) -> TransactionImportJob | None:
    async with async_session() as db:
        return (
            await db.execute(
                select(TransactionImportJob).where(TransactionImportJob.job_id == job_id)
            )
        ).scalar_one_or_none()


async def _claim_queued_job(job_id: str, lease_token: str) -> TransactionImportJob | None:
    now = _utcnow()
    async with async_session() as db, sqlite_write_gate(), db.begin():
        claimed = await db.execute(
            update(TransactionImportJob)
            .where(
                TransactionImportJob.job_id == job_id,
                TransactionImportJob.status == JOB_STATUS_QUEUED,
                TransactionImportJob.lease_token.is_(None),
            )
            .values(
                status=JOB_STATUS_RUNNING,
                lease_token=lease_token,
                lease_expires_at=now + TRANSACTION_JOB_LEASE_DURATION,
                started_at=now,
                finished_at=None,
                last_progress_at=now,
                last_progress_label="Transaction import started.",
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            return None
        return (
            await db.execute(
                select(TransactionImportJob).where(TransactionImportJob.job_id == job_id)
            )
        ).scalar_one()


async def _mark_job(
    job_id: str,
    status: str,
    *,
    sync_id: str | None = None,
    error: str | None = None,
    started: bool = False,
    finished: bool = False,
    progress_label: str | None = None,
    lease_token: str | None = None,
    clear_lease: bool = False,
    expected_lease_token: str | None = None,
) -> bool:
    notify_user_id: int | None = None
    archive_context: dict[str, Any] | None = None
    drain_status_provider: str | None = None
    drain_user_id: int | None = None
    drain_institution_id: int | None = None
    async with async_session() as db, sqlite_write_gate(), db.begin():
        job = (
            await db.execute(
                select(TransactionImportJob)
                .where(TransactionImportJob.job_id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not job:
            return False
        if expected_lease_token is not None and job.lease_token != expected_lease_token:
            return False
        previous_status = str(job.status or "")
        now = _utcnow()
        job.status = status
        job.updated_at = now
        if sync_id:
            job.sync_id = sync_id
        if error is not None:
            job.last_error = str(error or "")[:1000] or None
        if started:
            job.started_at = job.started_at or now
            job.finished_at = None
        if finished:
            job.finished_at = now
        if progress_label:
            job.last_progress_at = now
            job.last_progress_label = progress_label[:255]
        if lease_token is not None:
            job.lease_token = lease_token
            job.lease_expires_at = now + TRANSACTION_JOB_LEASE_DURATION
        if clear_lease:
            job.lease_token = None
            job.lease_expires_at = None
        notify_user_id = int(job.user_id) if job.user_id is not None else None
        if finished and status not in ACTIVE_JOB_STATUSES and job.user_id is not None and job.provider:
            drain_user_id = int(job.user_id)
            drain_institution_id = int(job.institution_id)
            try:
                from app.provider_catalog import get_status_provider_for_result
                drain_status_provider = get_status_provider_for_result(str(job.provider))
            except Exception:
                drain_status_provider = str(job.provider)
        if (
            status in {JOB_STATUS_FAILED, JOB_STATUS_AUTH_REQUIRED}
            and previous_status != status
            and not str(job.last_error or "").startswith(TRANSACTION_JOB_CRASH_ERROR_PREFIX)
        ):
            archive_context = {
                "user_id": int(job.user_id) if job.user_id is not None else None,
                "provider": str(job.provider or "unknown"),
                "sync_id": str(job.source_sync_id or job.sync_id or "") or None,
                "attempt_id": job.attempt_id,
                "trigger": f"tximport_mark_job_{status}",
                "error": job.last_error,
                "extra_fields": {
                    "job_id": job_id,
                    "previous_status": previous_status,
                    "sync_source": str(job.reason or "") or None,
                    "institution_id": int(job.institution_id),
                    "result_status": (
                        "auth_required"
                        if status == JOB_STATUS_AUTH_REQUIRED
                        else "error"
                    ),
                },
            }
    if notify_user_id is not None:
        mark_dirty(notify_user_id, EVENT_SYNC_ACTIVITY)
        mark_dirty(notify_user_id, EVENT_TRANSACTION_IMPORT_STATUS)
    if drain_user_id is not None and drain_status_provider:
        try:
            from app.services.sync_utils import _schedule_drain_pending_status
            _schedule_drain_pending_status(
                drain_user_id,
                drain_status_provider,
                drain_institution_id,
            )
        except Exception as exc:
            logger.warning("pending-status drain schedule failed job_id=%s: %s", job_id, exc)
    if archive_context is not None:
        try:
            from app.services.support_auto_archive import archive_run_fire_and_forget

            archive_run_fire_and_forget(**archive_context)
        except Exception as exc:
            logger.warning("transaction import mark_job auto-archive failed job_id=%s: %s", job_id, exc)
    return True


async def _touch_job_progress(
    job_id: str,
    label: str,
    *,
    sync_id: str | None = None,
    expected_lease_token: str | None = None,
) -> bool:
    async with async_session() as db, sqlite_write_gate(), db.begin():
        job = (
            await db.execute(
                select(TransactionImportJob)
                .where(TransactionImportJob.job_id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not job:
            return False
        if expected_lease_token is not None and job.lease_token != expected_lease_token:
            return False
        now = _utcnow()
        job.last_progress_at = now
        job.last_progress_label = label[:255]
        job.updated_at = now
        job.lease_expires_at = now + TRANSACTION_JOB_LEASE_DURATION
        if sync_id:
            job.sync_id = sync_id
        return True


async def recover_stale_transaction_import_jobs() -> int:
    now = _utcnow()
    queue_revive_before = now - timedelta(seconds=TRANSACTION_JOB_QUEUED_REVIVE_SECONDS)
    recovered: list[tuple[str, int, int, str, str | None, str | None, bool]] = []
    queue_revived: list[tuple[str, int, str, str | None]] = []
    queue_failed: list[tuple[str, int, str, str | None]] = []

    async with async_session() as db, sqlite_write_gate(), db.begin():
        rows = (
            await db.execute(
                select(TransactionImportJob).where(
                    TransactionImportJob.status == JOB_STATUS_RUNNING
                )
            )
        ).scalars().all()
        for job in rows:
            lease_expires_at = _aware_utc(job.lease_expires_at)
            if lease_expires_at is not None and lease_expires_at > now:
                continue

            lease_token = job.lease_token
            recovery_count = int(job.stale_recovery_count or 0) + 1
            should_requeue = (
                recovery_count <= TRANSACTION_JOB_MAX_STALE_RECOVERIES
                and str(job.reason or "") not in NON_REVIVABLE_JOB_REASONS
            )
            job.stale_recovery_count = recovery_count
            job.status = JOB_STATUS_QUEUED if should_requeue else JOB_STATUS_FAILED
            job.last_error = TRANSACTION_JOB_STALE_ERROR
            job.last_progress_at = now
            job.last_progress_label = (
                "Recovered stale transaction import; requeued."
                if should_requeue
                else "Recovered stale transaction import; retry needed."
            )
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = now
            if should_requeue:
                job.finished_at = None
            else:
                job.finished_at = now

            window_count = await _mark_running_windows_retryable(
                db,
                user_id=job.user_id,
                provider=job.provider,
                institution_id=job.institution_id,
                error=TRANSACTION_JOB_STALE_ERROR,
                now=now,
            )
            recovered.append(
                (
                    job.job_id,
                    job.user_id,
                    job.institution_id,
                    job.provider,
                    lease_token,
                    job.attempt_id,
                    should_requeue,
                )
            )
            logger.warning(
                "transaction import job stale provider=%s job_id=%s requeue=%s windows=%s",
                job.provider,
                job.job_id,
                should_requeue,
                window_count,
            )

        queued_rows = (
            await db.execute(
                select(TransactionImportJob).where(
                    TransactionImportJob.status == JOB_STATUS_QUEUED
                )
            )
        ).scalars().all()
        for job in queued_rows:
            last_touch = _aware_utc(
                job.updated_at or job.last_progress_at or job.created_at
            )
            if last_touch and last_touch > queue_revive_before:
                continue
            existing_task = _tasks.get(job.job_id)
            if existing_task and not existing_task.done():
                continue
            if str(job.reason or "") in NON_REVIVABLE_JOB_REASONS:
                # One-shot user-add import that lost its worker (e.g. a backend restart):
                # the add is over, so fail it rather than letting the watchdog re-fire a
                # sync the user never re-requested. The transactions are imported by the
                # next scheduled/manual sync instead.
                job.status = JOB_STATUS_FAILED
                job.last_error = "Orphaned one-shot add import; not auto-revived."
                job.last_progress_at = now
                job.last_progress_label = "Add import interrupted; not auto-revived."
                job.finished_at = now
                job.updated_at = now
                queue_failed.append((
                    job.job_id,
                    int(job.user_id) if job.user_id is not None else 0,
                    job.provider,
                    job.attempt_id,
                ))
                logger.warning(
                    "transaction import job orphan one-shot add failed (not revived) provider=%s job_id=%s reason=%s",
                    job.provider,
                    job.job_id,
                    job.reason,
                )
                continue
            job.last_progress_at = now
            job.last_progress_label = "Watchdog revived orphaned queued job."
            job.updated_at = now
            queue_revived.append((
                job.job_id,
                int(job.user_id) if job.user_id is not None else 0,
                job.provider,
                job.attempt_id,
            ))
            logger.warning(
                "transaction import job orphan queue revive provider=%s job_id=%s",
                job.provider,
                job.job_id,
            )

    try:
        from app.services.support_auto_archive import archive_run_fire_and_forget
    except Exception:
        archive_run_fire_and_forget = None  # type: ignore[assignment]

    notified_user_ids: set[int] = set()
    for (
        job_id,
        user_id,
        institution_id,
        provider,
        lease_token,
        attempt_id,
        should_requeue,
    ) in recovered:
        task = _tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        if lease_token:
            await release_sync_lock(
                user_id,
                _provider_lock(provider),
                institution_id=institution_id,
                owner_token=lease_token,
            )
        if user_id is not None and int(user_id) not in notified_user_ids:
            mark_dirty(int(user_id), EVENT_SYNC_ACTIVITY)
            mark_dirty(int(user_id), EVENT_TRANSACTION_IMPORT_STATUS)
            notified_user_ids.add(int(user_id))
        if archive_run_fire_and_forget is not None:
            archive_run_fire_and_forget(
                user_id=int(user_id) if user_id is not None else None,
                provider=str(provider),
                sync_id=None,
                attempt_id=attempt_id,
                trigger="tximport_stale_running" + ("_requeued" if should_requeue else "_failed"),
                error=TRANSACTION_JOB_STALE_ERROR,
                extra_fields={"job_id": job_id, "should_requeue": should_requeue},
            )
        if should_requeue:
            try:
                create_tracked_task(
                    _schedule_job_later(
                        job_id,
                        delay_seconds=TRANSACTION_JOB_STALE_REQUEUE_DELAY_SECONDS,
                    ),
                    name=f"transaction-import-stale-retry:{job_id}",
                )
            except RuntimeError:
                pass

    for job_id, user_id, provider, attempt_id in queue_revived:
        if user_id and user_id not in notified_user_ids:
            mark_dirty(user_id, EVENT_SYNC_ACTIVITY)
            mark_dirty(user_id, EVENT_TRANSACTION_IMPORT_STATUS)
            notified_user_ids.add(user_id)
        if archive_run_fire_and_forget is not None:
            archive_run_fire_and_forget(
                user_id=int(user_id) if user_id else None,
                provider=str(provider),
                sync_id=None,
                attempt_id=attempt_id,
                trigger="tximport_orphan_queued_revived",
                error="Queued transaction import job had no live task; watchdog re-scheduled it.",
                extra_fields={"job_id": job_id},
            )
        _schedule_job(job_id, delay_seconds=0.1)

    for job_id, user_id, provider, _attempt_id in queue_failed:
        task = _tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        if user_id and user_id not in notified_user_ids:
            mark_dirty(user_id, EVENT_SYNC_ACTIVITY)
            mark_dirty(user_id, EVENT_TRANSACTION_IMPORT_STATUS)
            notified_user_ids.add(user_id)

    return len(recovered) + len(queue_revived) + len(queue_failed)


async def _transaction_import_watchdog_loop() -> None:
    while True:
        await asyncio.sleep(TRANSACTION_JOB_WATCHDOG_INTERVAL_SECONDS)
        try:
            await recover_stale_transaction_import_jobs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("transaction import watchdog failed: %s", exc)


def start_transaction_import_watchdog() -> None:
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return
    _watchdog_task = create_tracked_task(
        _transaction_import_watchdog_loop(),
        name="transaction-import-watchdog",
    )


async def stop_transaction_import_watchdog() -> None:
    global _watchdog_task
    task = _watchdog_task
    _watchdog_task = None
    if not task or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _renew_transaction_import_job_lease(
    job_id: str,
    lease_token: str,
    owner_task: asyncio.Task[Any],
) -> None:
    while True:
        await asyncio.sleep(TRANSACTION_JOB_LEASE_RENEW_INTERVAL_SECONDS)
        now = _utcnow()
        try:
            async with async_session() as db, sqlite_write_gate(), db.begin():
                renewed = await db.execute(
                    update(TransactionImportJob)
                    .where(
                        TransactionImportJob.job_id == job_id,
                        TransactionImportJob.status == JOB_STATUS_RUNNING,
                        TransactionImportJob.lease_token == lease_token,
                    )
                    .values(lease_expires_at=now + TRANSACTION_JOB_LEASE_DURATION)
                )
            if renewed.rowcount == 1:
                continue
            async with async_session() as db:
                current = (
                    await db.execute(
                        select(
                            TransactionImportJob.status,
                            TransactionImportJob.lease_token,
                        ).where(TransactionImportJob.job_id == job_id)
                    )
                ).one_or_none()
            if (
                current is not None
                and str(current.status) in ACTIVE_JOB_STATUSES
                and current.lease_token != lease_token
                and not owner_task.done()
            ):
                owner_task.cancel()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "transaction import lease renewal failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )


async def _run_transaction_import_job(job_id: str, *, delay_seconds: float = 0.0) -> None:
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)

    lease_token = secrets.token_hex(16)
    job = await _claim_queued_job(job_id, lease_token)
    if job is None:
        return
    owner_task = asyncio.current_task()
    if owner_task is None:
        raise RuntimeError("Transaction import worker has no owning task")
    try:
        heartbeat_task = create_tracked_task(
            _renew_transaction_import_job_lease(job_id, lease_token, owner_task),
            name=f"transaction-import-heartbeat:{job_id}",
        )
    except RuntimeError:
        await _mark_job(
            job_id,
            JOB_STATUS_QUEUED,
            error="Transaction import deferred during backend shutdown.",
            progress_label="Queued after backend shutdown.",
            clear_lease=True,
            expected_lease_token=lease_token,
        )
        return
    try:
        await _run_claimed_transaction_import_job(job_id, job, lease_token)
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        try:
            finalized_job = await _load_job(job_id)
        except Exception:
            finalized_job = None
        if finalized_job is not None and str(finalized_job.status or "") not in ACTIVE_JOB_STATUSES:
            final_sync_id = str(finalized_job.source_sync_id or finalized_job.sync_id or "").strip() or None
            _log_provider_job_event(
                finalized_job.provider,
                "transaction import job finalized",
                level="warning" if finalized_job.status != JOB_STATUS_COMPLETE else "info",
                user_id=finalized_job.user_id,
                sync_id=final_sync_id,
                job_id=job_id,
                reason=finalized_job.reason,
                source_sync_id=finalized_job.source_sync_id,
                job_status=finalized_job.status,
            )
            try:
                from app.services.support_auto_archive import archive_run_fire_and_forget

                archive_run_fire_and_forget(
                    user_id=int(finalized_job.user_id) if finalized_job.user_id is not None else None,
                    provider=str(finalized_job.provider or "unknown"),
                    sync_id=final_sync_id,
                    attempt_id=finalized_job.attempt_id,
                    trigger=f"tximport_job_finalized_{finalized_job.status}",
                    error=finalized_job.last_error,
                    extra_fields={
                        "job_id": job_id,
                        "job_status": finalized_job.status,
                        "terminal_worker_snapshot": True,
                        "sync_source": str(finalized_job.reason or "") or None,
                        "institution_id": int(finalized_job.institution_id),
                        "result_status": (
                            "ok"
                            if finalized_job.status == JOB_STATUS_COMPLETE
                            else "auth_required"
                            if finalized_job.status == JOB_STATUS_AUTH_REQUIRED
                            else "error"
                        ),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "transaction import terminal archive schedule failed job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )


async def _run_claimed_transaction_import_job(
    job_id: str,
    job: TransactionImportJob,
    lease_token: str,
) -> None:
    if not _background_import_enabled(job.provider):
        await _mark_job(
            job_id,
            JOB_STATUS_FAILED,
            error="Provider does not support background transaction import.",
            finished=True,
            clear_lease=True,
            expected_lease_token=lease_token,
        )
        return
    _log_provider_job_event(
        job.provider,
        "transaction import job starting",
        user_id=job.user_id,
        sync_id=job.source_sync_id or job.sync_id,
        job_id=job_id,
        reason=job.reason,
        source_sync_id=job.source_sync_id,
        attempt_id=job.attempt_id,
        institution_id=job.institution_id,
    )
    lock_provider = _provider_lock(job.provider)
    acquired_lock = False
    for _attempt in range(60):
        if await acquire_sync_lock(
            job.user_id,
            lock_provider,
            institution_id=job.institution_id,
            owner_token=lease_token,
        ):
            acquired_lock = True
            break
        await asyncio.sleep(1)

    if not acquired_lock:
        await _mark_job(
            job_id,
            JOB_STATUS_QUEUED,
            error="Waiting for provider sync lock.",
            progress_label="Waiting for provider sync lock.",
            clear_lease=True,
            expected_lease_token=lease_token,
        )
        try:
            create_tracked_task(
                _schedule_job_later(job_id, delay_seconds=15.0),
                name=f"transaction-import-retry:{job_id}",
            )
        except RuntimeError:
            pass
        return

    try:
        from app.connectors.orchestration import run_connector_sync

        await _touch_job_progress(
            job_id,
            "Provider sync lock acquired.",
            expected_lease_token=lease_token,
        )
        _log_provider_job_event(
            job.provider,
            "transaction import lock acquired",
            user_id=job.user_id,
            sync_id=job.source_sync_id or job.sync_id,
            job_id=job_id,
            reason=job.reason,
            source_sync_id=job.source_sync_id,
            attempt_id=job.attempt_id,
            lock_provider=lock_provider,
        )
        async with async_session() as db:
            source_sync_id = str(job.source_sync_id or job.sync_id or "").strip() or None
            _result, response = await run_connector_sync(
                db,
                job.user_id,
                job.provider,
                institution_id=job.institution_id,
                sync_id=source_sync_id,
                sync_scope=normalize_sync_scope(SYNC_SCOPE_TRANSACTIONS),
                transaction_job_id=job_id,
                transaction_job_lease_token=lease_token,
                sync_source=str(job.reason or "").strip() or "unspecified",
                attempt_id=job.attempt_id,
            )
        response_status = str(response.get("status") or "")
        sync_id = str(response.get("sync_id") or "") or None
        _log_provider_job_event(
            job.provider,
            "transaction import sync finished",
            level="warning" if response_status not in {"ok", ""} else "info",
            user_id=job.user_id,
            job_id=job_id,
            reason=job.reason,
            source_sync_id=job.source_sync_id,
            sync_id=sync_id or job.source_sync_id or job.sync_id,
            route_status=response_status,
            attempt_id=job.attempt_id,
        )
        if response_status == "ok":
            await _mark_job(
                job_id,
                JOB_STATUS_COMPLETE,
                sync_id=sync_id,
                finished=True,
                progress_label="Transaction import complete.",
                clear_lease=True,
                expected_lease_token=lease_token,
            )
            # Newly imported/recategorized rows can create, retire, or re-shape
            # recurring series — refresh detection once per completed import so the
            # Recurring panel is current without waiting for the read-path TTL.
            # Best-effort in its own session; a hiccup must not fail the sync.
            try:
                from app.services.recurrence import run_detection_for_user

                async with async_session() as detect_db:
                    await run_detection_for_user(detect_db, job.user_id)
            except Exception:
                pass
        elif response_status == "auth_required":
            await _mark_job(
                job_id,
                JOB_STATUS_AUTH_REQUIRED,
                sync_id=sync_id,
                error=response.get("message") or "Provider authentication is required.",
                finished=True,
                progress_label="Transaction import needs authentication.",
                clear_lease=True,
                expected_lease_token=lease_token,
            )
        else:
            await _mark_job(
                job_id,
                JOB_STATUS_FAILED,
                sync_id=sync_id,
                error=response.get("message") or response_status or "Transaction import failed.",
                finished=True,
                progress_label="Transaction import failed.",
                clear_lease=True,
                expected_lease_token=lease_token,
            )
    except asyncio.CancelledError:
        resumable = str(job.reason or "") not in NON_REVIVABLE_JOB_REASONS
        await _mark_job(
            job_id,
            JOB_STATUS_QUEUED if resumable else JOB_STATUS_FAILED,
            error="Transaction import interrupted during backend shutdown.",
            finished=not resumable,
            progress_label=(
                "Queued after backend shutdown."
                if resumable
                else "Interrupted one-shot add import; retry needed."
            ),
            clear_lease=True,
            expected_lease_token=lease_token,
        )
        raise
    except Exception as exc:
        await _mark_job(
            job_id,
            JOB_STATUS_FAILED,
            error=(
                "Connection failed"
                if is_network_error(exc)
                else safe_connector_public_message(exc, fallback="Transaction import failed")
            ),
            finished=True,
            progress_label="Transaction import failed.",
            clear_lease=True,
            expected_lease_token=lease_token,
        )
        logger.warning("transaction import job failed provider=%s job_id=%s: %s", job.provider, job_id, exc)
    finally:
        if acquired_lock:
            await release_sync_lock(
                job.user_id,
                lock_provider,
                institution_id=job.institution_id,
                owner_token=lease_token,
            )
