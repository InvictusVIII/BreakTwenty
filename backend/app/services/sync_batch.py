from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
from contextlib import suppress
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.connectors.connector_logging import safe_connector_public_message
from app.connectors.registry import get_connector_spec
from app.connectors.orchestration import run_connector_sync
from app.database import async_session
from app.models import Account, BalanceHistory, Institution, SyncBatchJob
from app.provider_catalog import (
    get_error_status_for_provider,
    get_lock_provider_for_route,
    get_related_state_providers,
    get_status_provider_for_result,
    provider_uses_background_transaction_import,
    provider_uses_daily_transaction_import_dedupe,
    resolve_sync_route_provider,
)
from app.services.event_bus import EVENT_SYNC_BATCH, publish_event
from app.services.network_preflight import (
    SYNC_NETWORK_BLOCKED_MESSAGE,
    TEMPORARY_DNS_FAILURE_MARKER,
    run_provider_dns_resolution,
    run_sync_network_gate,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.sync_activity import clear_provider_activity, set_provider_activity
from app.services.transaction_import_jobs import enqueue_provider_transaction_import
from app.services.sync_tracking import get_provider_sync_id
from app.services.sync_utils import (
    acquire_sync_lock,
    is_network_error,
    release_sync_lock,
    set_institution_sync_status,
)
from app.services.task_supervisor import create_tracked_task
from app.services.time_utils import utc_now as _utc_now
from app.services.time_utils import utc_now_string as _utc_now_string

logger = logging.getLogger("breaktwenty.sync_batch")

SYNC_BATCH_TOTAL_CONCURRENCY = 6
SYNC_BATCH_API_CONCURRENCY = 6
SYNC_BATCH_SCRAPER_CONCURRENCY = 4
SYNC_BATCH_JOB_TTL = timedelta(minutes=30)
ACTIVE_SYNC_BATCH_STATUSES = frozenset({"queued", "running"})
SYNC_BATCH_LEASE_DURATION = timedelta(minutes=30)
SYNC_BATCH_LEASE_RENEW_INTERVAL_SECONDS = 60
SYNC_BATCH_WATCHDOG_INTERVAL_SECONDS = 60
SYNC_BATCH_WAIT_TIMEOUT_SECONDS = 35 * 60
TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS = 10.0
SYNC_BATCH_NETWORK_RECOVERY_MODES = frozenset({"auto", "manual"})
NETWORK_RECOVERY_PENDING_MARKER = "_network_recovery_pending"

_sync_batch_watchdog_task: asyncio.Task[Any] | None = None
_sync_batch_tasks: dict[tuple[int, str], asyncio.Task[Any]] = {}


def _normalize_mode(mode: str | None) -> str:
    normalized = str(mode or "auto").strip().lower()
    if normalized in {"manual", "sync_all", "user_initiated", "nightly"}:
        return normalized
    return "auto"


def _sync_source_for_batch_mode(mode: str | None) -> str:
    normalized = _normalize_mode(mode)
    if normalized in {"manual", "sync_all", "user_initiated"}:
        return "sync_all"
    if normalized == "nightly":
        return "scheduled_sync"
    return "autosync"


def _normalize_connections(connections: list[Any] | tuple[Any, ...] | None) -> list[dict[str, Any]]:
    seen: set[tuple[str, int]] = set()
    result: list[dict[str, Any]] = []
    for connection in connections or []:
        if not isinstance(connection, dict):
            continue
        provider = str(connection.get("provider") or "").strip().lower()
        try:
            institution_id = int(connection.get("institution_id") or 0)
        except (TypeError, ValueError):
            institution_id = 0
        identity = (provider, institution_id)
        if not provider or institution_id <= 0 or identity in seen:
            continue
        seen.add(identity)
        result.append({"provider": provider, "institution_id": institution_id})
    return result


def _connection_key(institution_id: int) -> str:
    return f"connection:{int(institution_id)}"


def _connection_signature(targets_by_key: dict[str, dict[str, Any]]) -> str:
    signature = sorted(
        (str(target["provider"]), int(target["institution_id"]), target.get("route_provider"))
        for target in targets_by_key.values()
    )
    return json.dumps(signature, separators=(",", ":"), ensure_ascii=True)


def _decode_job_state(row: SyncBatchJob) -> dict[str, Any]:
    try:
        job = json.loads(str(row.state_json or "{}"))
    except (TypeError, ValueError):
        job = {}
    if not isinstance(job, dict):
        job = {}
    job["batch_id"] = row.batch_id
    job["mode"] = row.mode
    job["status"] = row.status
    return job


def _encode_job_state(job: dict[str, Any]) -> str:
    return json.dumps(job, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _targets_from_job(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for key, state in (job.get("results") or {}).items():
        if not isinstance(state, dict):
            continue
        if str(state.get("status") or "").strip().lower() not in {"pending", "syncing"}:
            continue
        provider = str(state.get("provider") or "").strip().lower()
        route_provider = state.get("route_provider")
        try:
            institution_id = int(state.get("institution_id") or 0)
        except (TypeError, ValueError):
            continue
        if not provider or institution_id <= 0:
            continue
        targets[str(key)] = {
            "provider": provider,
            "institution_id": institution_id,
            "route_provider": str(route_provider) if route_provider else None,
        }
    return targets


async def _validate_batch_connections(
    user_id: int,
    connections: list[dict[str, Any]],
) -> None:
    if not connections:
        return
    institution_ids = {int(connection["institution_id"]) for connection in connections}
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Institution).where(
                    Institution.user_id == int(user_id),
                    Institution.id.in_(institution_ids),
                    Institution.enabled.is_(True),
                )
            )
        ).scalars().all()
    institutions_by_id = {int(row.id): row for row in rows}
    if len(institutions_by_id) != len(institution_ids):
        raise ValueError("One or more sync batch connections are unavailable")
    for connection in connections:
        institution_id = int(connection["institution_id"])
        institution = institutions_by_id[institution_id]
        if str(institution.provider or "").strip().lower() != str(
            connection["provider"]
        ).strip().lower():
            raise ValueError("One or more sync batch connections are unavailable")


async def _get_job(user_id: int, batch_id: str) -> dict[str, Any] | None:
    async with async_session() as db:
        row = (
            await db.execute(
                select(SyncBatchJob).where(
                    SyncBatchJob.user_id == int(user_id),
                    SyncBatchJob.batch_id == str(batch_id),
                )
            )
        ).scalar_one_or_none()
        return _decode_job_state(row) if row else None


async def get_sync_batch(user_id: int, batch_id: str) -> dict[str, Any] | None:
    return await _get_job(user_id, batch_id)


async def get_user_active_sync_batches(user_id: int) -> list[dict[str, Any]]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SyncBatchJob)
                .where(
                    SyncBatchJob.user_id == int(user_id),
                    SyncBatchJob.status.in_(ACTIVE_SYNC_BATCH_STATUSES),
                )
                .order_by(SyncBatchJob.updated_at.desc())
                .limit(100)
            )
        ).scalars().all()
        return [_decode_job_state(row) for row in rows]


async def _update_job(
    user_id: int,
    batch_id: str,
    updater,
    *,
    expected_lease_token: str | None = None,
    clear_lease: bool = False,
) -> bool:
    snapshot: dict[str, Any] | None = None
    async with async_session() as db, sqlite_write_gate(), db.begin():
        row = (
            await db.execute(
                select(SyncBatchJob)
                .where(
                    SyncBatchJob.user_id == int(user_id),
                    SyncBatchJob.batch_id == str(batch_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if expected_lease_token is not None and row.lease_token != expected_lease_token:
            return False
        job = _decode_job_state(row)
        updater(job)
        job["updated_at"] = _utc_now_string()
        snapshot = copy.deepcopy(job)
        now = _utc_now()
        row.status = str(job.get("status") or row.status)
        row.state_json = _encode_job_state(job)
        row.updated_at = now
        if job.get("finished_at"):
            row.finished_at = now
        if clear_lease:
            row.lease_token = None
            row.lease_expires_at = None
        elif expected_lease_token is not None:
            row.lease_expires_at = now + SYNC_BATCH_LEASE_DURATION
    if snapshot is not None:
        publish_event(user_id, EVENT_SYNC_BATCH, snapshot, batch_id=str(batch_id))
    return snapshot is not None


async def _cleanup_old_jobs() -> None:
    cutoff = _utc_now() - SYNC_BATCH_JOB_TTL
    async with async_session() as db, sqlite_write_gate(), db.begin():
        await db.execute(
            delete(SyncBatchJob).where(
                SyncBatchJob.status.not_in(ACTIVE_SYNC_BATCH_STATUSES),
                SyncBatchJob.updated_at < cutoff,
            )
        )


def _initial_connection_state(
    provider: str,
    institution_id: int,
    route_provider: str | None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "route_provider": route_provider,
        "status": "pending" if route_provider else "skipped",
        "message": None if route_provider else "No sync endpoint configured",
        "started_at": None,
        "finished_at": None,
        "sync_id": None,
        "institution_id": int(institution_id),
        "accounts": [],
    }


async def start_sync_batch(
    user_id: int,
    connections: list[Any] | tuple[Any, ...],
    *,
    mode: str = "auto",
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    normalized_connections = _normalize_connections(connections)
    await _validate_batch_connections(user_id, normalized_connections)
    await _cleanup_old_jobs()
    batch_id = f"syncbatch-{secrets.token_hex(6)}"
    targets_by_key = {
        _connection_key(connection["institution_id"]): {
            **connection,
            "route_provider": resolve_sync_route_provider(connection["provider"], normalized_mode),
        }
        for connection in normalized_connections
    }
    connection_signature = _connection_signature(targets_by_key)
    now = _utc_now_string()
    job = {
        "batch_id": batch_id,
        "status": "queued",
        "mode": normalized_mode,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "results": {
            key: _initial_connection_state(
                target["provider"],
                target["institution_id"],
                target["route_provider"],
            )
            for key, target in targets_by_key.items()
        },
    }
    try:
        async with async_session() as db, sqlite_write_gate(), db.begin():
            existing = (
                await db.execute(
                    select(SyncBatchJob)
                    .where(
                        SyncBatchJob.user_id == int(user_id),
                        SyncBatchJob.mode == normalized_mode,
                        SyncBatchJob.connection_signature == connection_signature,
                        SyncBatchJob.status.in_(ACTIVE_SYNC_BATCH_STATUSES),
                    )
                    .order_by(SyncBatchJob.updated_at.desc())
                )
            ).scalars().first()
            if existing is not None:
                return _deduped_sync_batch_response(existing)
            now_dt = _utc_now()
            db.add(
                SyncBatchJob(
                    user_id=int(user_id),
                    batch_id=batch_id,
                    mode=normalized_mode,
                    status="queued",
                    connection_signature=connection_signature,
                    state_json=_encode_job_state(job),
                    created_at=now_dt,
                    updated_at=now_dt,
                )
            )
            await db.flush()
    except IntegrityError:
        async with async_session() as db:
            existing = (
                await db.execute(
                    select(SyncBatchJob)
                    .where(
                        SyncBatchJob.user_id == int(user_id),
                        SyncBatchJob.mode == normalized_mode,
                        SyncBatchJob.connection_signature == connection_signature,
                        SyncBatchJob.status.in_(ACTIVE_SYNC_BATCH_STATUSES),
                    )
                    .order_by(SyncBatchJob.updated_at.desc())
                )
            ).scalars().first()
        if existing is None:
            raise
        return _deduped_sync_batch_response(existing)
    publish_event(user_id, EVENT_SYNC_BATCH, copy.deepcopy(job), batch_id=batch_id)
    for key, target in targets_by_key.items():
        provider = target["provider"]
        route_provider = target["route_provider"]
        if route_provider:
            set_provider_activity(
                user_id,
                provider,
                activity_id=f"sync-batch:{batch_id}:{key}",
                status="queued",
                institution_id=target["institution_id"],
            )
    _schedule_sync_batch(user_id, batch_id, targets_by_key)
    return {"status": "started", "batch_id": batch_id}


def _schedule_sync_batch(
    user_id: int,
    batch_id: str,
    targets_by_key: dict[str, dict[str, Any]] | None = None,
) -> None:
    identity = (int(user_id), str(batch_id))
    existing = _sync_batch_tasks.get(identity)
    if existing is not None and not existing.done():
        return
    try:
        task = create_tracked_task(
            _run_sync_batch_job(user_id, batch_id, targets_by_key),
            name=f"sync-batch:{user_id}:{batch_id}",
        )
        _sync_batch_tasks[identity] = task

        def forget(completed: asyncio.Task[Any]) -> None:
            if _sync_batch_tasks.get(identity) is completed:
                _sync_batch_tasks.pop(identity, None)

        task.add_done_callback(forget)
    except RuntimeError:
        # Admission is closed during shutdown. The durable queued job will be
        # picked up on the next application start.
        return


def _deduped_sync_batch_response(existing: SyncBatchJob) -> dict[str, Any]:
    _schedule_sync_batch(int(existing.user_id), str(existing.batch_id))
    return {
        "status": "started",
        "batch_id": existing.batch_id,
        "deduped": True,
    }


def _job_references_connection(
    row: SyncBatchJob,
    *,
    provider_family: set[str],
    institution_id: int,
) -> bool:
    job = _decode_job_state(row)
    for state in (job.get("results") or {}).values():
        if not isinstance(state, dict):
            continue
        try:
            state_institution_id = int(state.get("institution_id") or 0)
        except (TypeError, ValueError):
            state_institution_id = 0
        state_providers = {
            str(state.get("provider") or "").strip().lower(),
            str(state.get("route_provider") or "").strip().lower(),
        }
        if state_institution_id == institution_id or provider_family.intersection(state_providers):
            return True
    try:
        signature = json.loads(str(row.connection_signature or "[]"))
    except (TypeError, ValueError):
        signature = []
    for entry in signature if isinstance(signature, list) else []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        try:
            signature_institution_id = int(entry[1])
        except (TypeError, ValueError):
            signature_institution_id = 0
        signature_providers = {
            str(entry[0] or "").strip().lower(),
            str(entry[2] or "").strip().lower() if len(entry) > 2 else "",
        }
        if (
            signature_institution_id == institution_id
            or provider_family.intersection(signature_providers)
        ):
            return True
    return False


async def purge_provider_sync_batch_history(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    institution_id: int,
) -> int:
    """Delete owner-scoped transient batches that reference a removed connection.

    A batch is an aggregate operational record, so retaining only part of it would
    leave inconsistent status/signature state. Financial data for other providers
    is not stored in this table and is not affected.
    """
    normalized_provider = str(provider or "").strip().lower()
    try:
        related_providers = get_related_state_providers(normalized_provider)
    except KeyError:
        related_providers = ()
    provider_family = {
        str(member or "").strip().lower()
        for member in related_providers
    }
    provider_family.add(normalized_provider)
    rows = (
        await db.execute(select(SyncBatchJob).where(SyncBatchJob.user_id == int(user_id)))
    ).scalars().all()
    removed = 0
    for row in rows:
        if not _job_references_connection(
            row,
            provider_family=provider_family,
            institution_id=int(institution_id),
        ):
            continue
        task = _sync_batch_tasks.get((int(user_id), str(row.batch_id)))
        if task is not None and not task.done():
            task.cancel()
        await db.delete(row)
        removed += 1
    return removed


async def _renew_sync_batch_lease(
    user_id: int,
    batch_id: str,
    lease_token: str,
    owner_task: asyncio.Task[Any],
) -> None:
    renew_delay = max(float(SYNC_BATCH_LEASE_RENEW_INTERVAL_SECONDS), 0.001)
    while True:
        await asyncio.sleep(renew_delay)
        try:
            now = _utc_now()
            async with async_session() as db, sqlite_write_gate(), db.begin():
                renewed = await db.execute(
                    update(SyncBatchJob)
                    .where(
                        SyncBatchJob.user_id == int(user_id),
                        SyncBatchJob.batch_id == str(batch_id),
                        SyncBatchJob.status == "running",
                        SyncBatchJob.lease_token == lease_token,
                    )
                    .values(lease_expires_at=now + SYNC_BATCH_LEASE_DURATION)
                )
            if renewed.rowcount == 1:
                continue
            async with async_session() as db:
                current = (
                    await db.execute(
                        select(SyncBatchJob.status, SyncBatchJob.lease_token).where(
                            SyncBatchJob.user_id == int(user_id),
                            SyncBatchJob.batch_id == str(batch_id),
                        )
                    )
                ).one_or_none()
            if (
                current is not None
                and str(current.status) in ACTIVE_SYNC_BATCH_STATUSES
                and current.lease_token != lease_token
                and not owner_task.done()
            ):
                owner_task.cancel()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sync batch lease renewal failed batch_id=%s error_type=%s",
                batch_id,
                type(exc).__name__,
            )


async def _sync_batch_watchdog_loop() -> None:
    while True:
        await asyncio.sleep(SYNC_BATCH_WATCHDOG_INTERVAL_SECONDS)
        try:
            await resume_sync_batch_jobs()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sync batch watchdog failed error_type=%s",
                type(exc).__name__,
            )


def start_sync_batch_watchdog() -> None:
    global _sync_batch_watchdog_task
    if _sync_batch_watchdog_task and not _sync_batch_watchdog_task.done():
        return
    _sync_batch_watchdog_task = create_tracked_task(
        _sync_batch_watchdog_loop(),
        name="sync-batch-watchdog",
    )


async def stop_sync_batch_watchdog() -> None:
    global _sync_batch_watchdog_task
    task = _sync_batch_watchdog_task
    _sync_batch_watchdog_task = None
    if not task or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def resume_sync_batch_jobs(*, force_running: bool = False) -> None:
    now = _utc_now()
    if force_running:
        async with async_session() as db, sqlite_write_gate(), db.begin():
            rows = (
                await db.execute(
                    select(SyncBatchJob).where(SyncBatchJob.status == "running")
                )
            ).scalars().all()
            for row in rows:
                row.status = "queued"
                row.lease_token = None
                row.lease_expires_at = None
                row.updated_at = now
    async with async_session() as db:
        rows = (
            await db.execute(
                select(SyncBatchJob).where(
                    or_(
                        SyncBatchJob.status == "queued",
                        (
                            (SyncBatchJob.status == "running")
                            & or_(
                                SyncBatchJob.lease_expires_at.is_(None),
                                SyncBatchJob.lease_expires_at <= now,
                            )
                        ),
                    )
                )
            )
        ).scalars().all()
    for row in rows:
        _schedule_sync_batch(int(row.user_id), str(row.batch_id))


async def _claim_sync_batch(
    user_id: int,
    batch_id: str,
    lease_token: str,
) -> dict[str, Any] | None:
    now = _utc_now()
    async with async_session() as db, sqlite_write_gate(), db.begin():
        claimed = await db.execute(
            update(SyncBatchJob)
            .where(
                SyncBatchJob.user_id == int(user_id),
                SyncBatchJob.batch_id == str(batch_id),
                or_(
                    (SyncBatchJob.status == "queued") & SyncBatchJob.lease_token.is_(None),
                    (
                        SyncBatchJob.status == "running"
                    )
                    & or_(
                        SyncBatchJob.lease_expires_at.is_(None),
                        SyncBatchJob.lease_expires_at <= now,
                    ),
                ),
            )
            .values(
                status="running",
                lease_token=lease_token,
                lease_expires_at=now + SYNC_BATCH_LEASE_DURATION,
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            return None
        row = (
            await db.execute(
                select(SyncBatchJob).where(
                    SyncBatchJob.user_id == int(user_id),
                    SyncBatchJob.batch_id == str(batch_id),
                )
            )
        ).scalar_one()
        return _decode_job_state(row)


async def run_sync_batch_now(
    user_id: int,
    connections: list[Any] | tuple[Any, ...],
    *,
    mode: str = "nightly",
) -> dict[str, dict[str, Any]]:
    started = await start_sync_batch(user_id, connections, mode=mode)
    batch_id = str(started["batch_id"])
    try:
        async with asyncio.timeout(SYNC_BATCH_WAIT_TIMEOUT_SECONDS):
            while True:
                job = await get_sync_batch(user_id, batch_id)
                if not job:
                    return {}
                if job.get("status") in {"done", "error", "blocked"}:
                    return dict(job.get("results") or {})
                await asyncio.sleep(0.5)
    except TimeoutError:
        job = await get_sync_batch(user_id, batch_id)
        return dict((job or {}).get("results") or {})


async def _run_sync_batch_job(
    user_id: int,
    batch_id: str,
    targets_by_key: dict[str, dict[str, Any]] | None,
) -> None:
    lease_token = secrets.token_hex(16)
    claimed_job = await _claim_sync_batch(user_id, batch_id, lease_token)
    if claimed_job is None:
        return
    if targets_by_key is None:
        targets_by_key = _targets_from_job(claimed_job)
    if not targets_by_key:
        def mark_empty(job: dict[str, Any]) -> None:
            job["status"] = "done"
            job["started_at"] = job.get("started_at") or _utc_now_string()
            job["finished_at"] = _utc_now_string()

        await _update_job(
            user_id,
            batch_id,
            mark_empty,
            expected_lease_token=lease_token,
            clear_lease=True,
        )
        return
    ordered_targets = await _order_connections_for_batch(user_id, targets_by_key)
    mode = str(claimed_job.get("mode") or "auto")
    owner_task = asyncio.current_task()
    if owner_task is None:
        raise RuntimeError("Sync batch worker has no owning task")
    try:
        heartbeat_task = create_tracked_task(
            _renew_sync_batch_lease(user_id, batch_id, lease_token, owner_task),
            name=f"sync-batch-heartbeat:{user_id}:{batch_id}",
        )
    except RuntimeError:
        def mark_admission_closed(job: dict[str, Any]) -> None:
            job["status"] = "queued"
            job["finished_at"] = None

        await _update_job(
            user_id,
            batch_id,
            mark_admission_closed,
            expected_lease_token=lease_token,
            clear_lease=True,
        )
        return
    try:
        gate = await run_sync_network_gate(
            [
                target["route_provider"]
                for _, target in ordered_targets
                if target["route_provider"]
            ],
            user_id=user_id,
            flow="batch",
            mode=mode,
            batch_id=batch_id,
        )
        if gate.status == "blocked":
            await _block_sync_batch_for_network(
                user_id,
                batch_id,
                ordered_targets,
                gate.blocker_payload(),
                lease_token=lease_token,
            )
            return

        def mark_running(job: dict[str, Any]) -> None:
            job["status"] = "running"
            job["started_at"] = job.get("started_at") or _utc_now_string()

        await _update_job(
            user_id,
            batch_id,
            mark_running,
            expected_lease_token=lease_token,
        )

        total_semaphore = asyncio.Semaphore(SYNC_BATCH_TOTAL_CONCURRENCY)
        api_semaphore = asyncio.Semaphore(SYNC_BATCH_API_CONCURRENCY)
        scraper_semaphore = asyncio.Semaphore(SYNC_BATCH_SCRAPER_CONCURRENCY)

        async def run_connection(
            key: str,
            target: dict[str, Any],
            *,
            allow_network_recovery: bool,
        ) -> dict[str, Any] | None:
            route_provider = target["route_provider"]
            if not route_provider:
                return None
            provider_type = _provider_type(route_provider)
            semaphore = scraper_semaphore if provider_type == "scraper" else api_semaphore
            async with total_semaphore, semaphore:
                logger.info(
                    "sync batch stage batch_id=%s provider=%s stage=connector_start",
                    batch_id,
                    route_provider,
                )
                try:
                    return await _run_single_connection(
                        user_id,
                        batch_id,
                        key,
                        target,
                        lease_token=lease_token,
                        mode=mode,
                        defer_temporary_dns_failure=allow_network_recovery,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    clear_provider_activity(f"sync-batch:{batch_id}:{key}")
                    response = {
                        "status": "error",
                        "message": safe_connector_public_message(exc),
                    }
                    await _finish_connection(
                        user_id,
                        batch_id,
                        key,
                        target,
                        route_provider,
                        response,
                        lease_token=lease_token,
                    )
                    return response
                finally:
                    logger.info(
                        "sync batch stage batch_id=%s provider=%s stage=connector_finished",
                        batch_id,
                        route_provider,
                    )

        tasks = [
            create_tracked_task(
                run_connection(
                    key,
                    target,
                    allow_network_recovery=(
                        _normalize_mode(mode) in SYNC_BATCH_NETWORK_RECOVERY_MODES
                    ),
                ),
                name=f"sync-batch-connection:{user_id}:{batch_id}:{key}",
            )
            for key, target in ordered_targets
        ]
        first_results = await asyncio.gather(*tasks, return_exceptions=True)
        pending_network_failures = [
            (key, target, result)
            for (key, target), result in zip(ordered_targets, first_results, strict=True)
            if isinstance(result, dict)
            and result.get(NETWORK_RECOVERY_PENDING_MARKER) is True
        ]
        batch_dns_outage_confirmed = any(
            response.get(TEMPORARY_DNS_FAILURE_MARKER) is True
            for _, _, response in pending_network_failures
        )
        if pending_network_failures and batch_dns_outage_confirmed:
            recovery_gate = await run_sync_network_gate(
                [target["route_provider"] for _, target, _ in pending_network_failures],
                user_id=user_id,
                flow="batch_recovery",
                mode=mode,
                batch_id=batch_id,
            )
            if recovery_gate.status == "ready":
                logger.info(
                    "sync batch temporary DNS recovery retry batch_id=%s connections=%s",
                    batch_id,
                    len(pending_network_failures),
                )
                retry_tasks = [
                    create_tracked_task(
                        run_connection(
                            key,
                            target,
                            allow_network_recovery=False,
                        ),
                        name=f"sync-batch-dns-retry:{user_id}:{batch_id}:{key}",
                    )
                    for key, target, _ in pending_network_failures
                ]
                await asyncio.gather(*retry_tasks, return_exceptions=True)
            else:
                for key, target, response in pending_network_failures:
                    await _finish_connection(
                        user_id,
                        batch_id,
                        key,
                        target,
                        str(target["route_provider"]),
                        response,
                        lease_token=lease_token,
                    )
        elif pending_network_failures:
            for key, target, response in pending_network_failures:
                await _finish_connection(
                    user_id,
                    batch_id,
                    key,
                    target,
                    str(target["route_provider"]),
                    response,
                    lease_token=lease_token,
                )

        def mark_done(job: dict[str, Any]) -> None:
            job["status"] = "done"
            job["finished_at"] = _utc_now_string()

        await _update_job(
            user_id,
            batch_id,
            mark_done,
            expected_lease_token=lease_token,
            clear_lease=True,
        )
    except asyncio.CancelledError:

        def mark_queued(job: dict[str, Any]) -> None:
            job["status"] = "queued"
            job["finished_at"] = None

        await _update_job(
            user_id,
            batch_id,
            mark_queued,
            expected_lease_token=lease_token,
            clear_lease=True,
        )
        raise
    except Exception:
        logger.exception("sync batch crashed batch_id=%s", batch_id)

        def mark_error(job: dict[str, Any]) -> None:
            job["status"] = "error"
            job["finished_at"] = _utc_now_string()

        await _update_job(
            user_id,
            batch_id,
            mark_error,
            expected_lease_token=lease_token,
            clear_lease=True,
        )
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


async def _block_sync_batch_for_network(
    user_id: int,
    batch_id: str,
    ordered_targets: list[tuple[str, dict[str, Any]]],
    blocker: dict[str, Any],
    *,
    lease_token: str,
) -> None:
    finished_at = _utc_now_string()
    for key, target in ordered_targets:
        if target["route_provider"]:
            clear_provider_activity(f"sync-batch:{batch_id}:{key}")

    def mark_blocked(job: dict[str, Any]) -> None:
        job["status"] = "blocked"
        job["blocker"] = dict(blocker)
        job["finished_at"] = finished_at
        for key, target in ordered_targets:
            if not target["route_provider"]:
                continue
            state = job["results"][key]
            state["status"] = "network_blocked"
            state["message"] = SYNC_NETWORK_BLOCKED_MESSAGE
            state["finished_at"] = finished_at

    await _update_job(
        user_id,
        batch_id,
        mark_blocked,
        expected_lease_token=lease_token,
        clear_lease=True,
    )


async def _order_connections_for_batch(
    user_id: int,
    targets_by_key: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    targets = list(targets_by_key.items())
    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(Institution.id, Institution.sync_status).where(
                        Institution.user_id == user_id,
                    )
                )
            ).all()
    except Exception:
        return targets

    sync_status_by_id = {
        int(institution_id): str(sync_status or "")
        for institution_id, sync_status in rows
    }

    def connection_priority(indexed_target: tuple[int, tuple[str, dict[str, Any]]]) -> tuple[int, int]:
        index, (_, target) = indexed_target
        sync_status = sync_status_by_id.get(int(target["institution_id"]))
        return (0 if sync_status == "auth_required" else 1, index)

    return [
        target
        for _, target in sorted(enumerate(targets), key=connection_priority)
    ]


def _provider_type(route_provider: str) -> str:
    try:
        return get_connector_spec(route_provider).provider_type
    except Exception:
        return "api"


async def _run_single_connection(
    user_id: int,
    batch_id: str,
    key: str,
    target: dict[str, Any],
    *,
    lease_token: str,
    mode: str = "auto",
    defer_temporary_dns_failure: bool = False,
) -> dict[str, Any]:
    provider = str(target["provider"])
    institution_id = int(target["institution_id"])
    route_provider = str(target["route_provider"])
    activity_id = f"sync-batch:{batch_id}:{key}"
    status_provider = get_status_provider_for_result(route_provider)
    error_status = get_error_status_for_provider(route_provider)
    lock_provider = get_lock_provider_for_route(route_provider)
    sync_source = _sync_source_for_batch_mode(mode)

    def mark_syncing(job: dict[str, Any]) -> None:
        state = job["results"][key]
        state["status"] = "syncing"
        state["started_at"] = _utc_now_string()

    await _update_job(
        user_id,
        batch_id,
        mark_syncing,
        expected_lease_token=lease_token,
    )
    try:
        set_provider_activity(
            user_id,
            provider,
            activity_id=activity_id,
            institution_id=institution_id,
        )

        if not await acquire_sync_lock(
            user_id,
            lock_provider,
            institution_id=institution_id,
        ):
            sync_id = get_provider_sync_id(
                user_id,
                route_provider,
                institution_id=institution_id,
            ) or get_provider_sync_id(user_id, lock_provider, institution_id=institution_id)
            response = {
                "status": "already_syncing",
                "sync_id": sync_id,
                "message": "Sync already in progress.",
            }
            try:
                from app.services.support_auto_archive import archive_run_fire_and_forget

                archive_run_fire_and_forget(
                    user_id=user_id,
                    provider=route_provider,
                    sync_id=sync_id,
                    trigger="sync_result_already_syncing",
                    error=response["message"],
                    extra_fields={
                        "sync_scope": "accounts",
                        "result_status": "already_syncing",
                        "mode": "sync_batch",
                        "sync_source": sync_source,
                        "institution_id": institution_id,
                        "lock_provider": lock_provider,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "sync batch already-syncing archive schedule failed provider=%s institution_id=%s error_type=%s",
                    route_provider,
                    institution_id,
                    type(exc).__name__,
                )
            await _finish_connection(
                user_id,
                batch_id,
                key,
                target,
                route_provider,
                response,
                lease_token=lease_token,
            )
            return response

        try:
            async with async_session() as db:
                sync_scope = (
                    "accounts"
                    if provider_uses_background_transaction_import(route_provider)
                    else "full"
                )
                _, response = await run_connector_sync(
                    db,
                    user_id,
                    route_provider,
                    institution_id=institution_id,
                    sync_scope=sync_scope,
                    sync_source=sync_source,
                    include_network_recovery_hint=defer_temporary_dns_failure,
                )
            response_status = str(response.get("status") or "")
            can_enqueue_transactions = not bool(response.get("transaction_import_deferred")) and (
                response_status == "ok" or (
                    response_status == "skipped"
                    and provider_uses_daily_transaction_import_dedupe(route_provider)
                )
            )
            if can_enqueue_transactions:
                try:
                    async with asyncio.timeout(TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS):
                        job = await enqueue_provider_transaction_import(
                            user_id,
                            route_provider,
                            institution_id=institution_id,
                            source_sync_id=str(response.get("sync_id") or "") or None,
                            reason=sync_source,
                        )
                except TimeoutError:
                    logger.warning(
                        "transaction import enqueue timeout provider=%s user_id=%s institution_id=%s sync_id=%s timeout_seconds=%s",
                        route_provider,
                        user_id,
                        institution_id,
                        response.get("sync_id"),
                        TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS,
                    )
                    job = None
                    response = {
                        **response,
                        "transaction_import_enqueue_error": "Transaction import enqueue timed out.",
                    }
                if job:
                    response = {**response, "transaction_import_job": job}
        except Exception as exc:
            network_failure = is_network_error(exc)
            status = "network_error" if network_failure else error_status
            async with async_session() as db, sqlite_write_gate():
                await set_institution_sync_status(
                    db,
                    user_id,
                    status_provider,
                    status,
                    institution_id=institution_id,
                )
            response = {
                "status": "network_error" if network_failure else "error",
                "message": (
                    "Connection failed"
                    if network_failure
                    else safe_connector_public_message(exc)
                ),
            }
        finally:
            await release_sync_lock(
                user_id,
                lock_provider,
                institution_id=institution_id,
            )

        if (
            defer_temporary_dns_failure
            and _normalize_mode(mode) in SYNC_BATCH_NETWORK_RECOVERY_MODES
            and str(response.get("status") or "").lower() == "network_error"
        ):
            dns_result = await run_provider_dns_resolution(route_provider)
            temporary_dns_failure = (
                response.get(TEMPORARY_DNS_FAILURE_MARKER) is True
                or dns_result.status == "temporary_failure"
            )
            if temporary_dns_failure:
                logger.info(
                    "sync batch connection waiting for temporary DNS recovery batch_id=%s provider=%s error_name=%s",
                    batch_id,
                    route_provider,
                    dns_result.error_name,
                )
            return {
                **response,
                NETWORK_RECOVERY_PENDING_MARKER: True,
                TEMPORARY_DNS_FAILURE_MARKER: temporary_dns_failure,
            }

        await _finish_connection(
            user_id,
            batch_id,
            key,
            target,
            route_provider,
            response,
            lease_token=lease_token,
        )
        return response
    finally:
        clear_provider_activity(activity_id)


async def _finish_connection(
    user_id: int,
    batch_id: str,
    key: str,
    target: dict[str, Any],
    route_provider: str,
    response: dict[str, Any],
    *,
    lease_token: str,
) -> None:
    status = str(response.get("status") or "error")
    accounts_payload: list[dict[str, Any]] = []
    institution_id = int(target["institution_id"])
    if status == "ok":
        accounts_payload = await _load_connection_accounts(user_id, institution_id)

    def mark_finished(job: dict[str, Any]) -> None:
        state = job["results"][key]
        state["status"] = status
        state["message"] = response.get("message")
        state["sync_id"] = response.get("sync_id")
        state["finished_at"] = _utc_now_string()
        state["institution_id"] = institution_id
        state["accounts"] = accounts_payload if status == "ok" else []
        state["transaction_import_job"] = response.get("transaction_import_job")

    await _update_job(
        user_id,
        batch_id,
        mark_finished,
        expected_lease_token=lease_token,
    )


async def _load_connection_accounts(user_id: int, institution_id: int) -> list[dict[str, Any]]:
    async with async_session() as db:
        institution = (
            await db.execute(
                select(Institution).where(
                    Institution.user_id == user_id,
                    Institution.id == int(institution_id),
                )
            )
        ).scalar_one_or_none()
        if not institution:
            return []
        if institution.hidden:
            return []
        scoped_account = aliased(Account)
        scoped_account_ids = select(scoped_account.id).where(
            scoped_account.user_id == user_id,
            scoped_account.institution_id == institution.id,
            scoped_account.hidden.is_(False),
        )
        latest_balance = (
            select(
                BalanceHistory.account_id,
                func.max(BalanceHistory.date).label("max_date"),
            )
            .where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id.in_(scoped_account_ids),
            )
            .group_by(BalanceHistory.account_id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(Account, BalanceHistory.balance)
                .where(
                    Account.user_id == user_id,
                    Account.institution_id == institution.id,
                    Account.hidden.is_(False),
                )
                .outerjoin(latest_balance, latest_balance.c.account_id == Account.id)
                .outerjoin(
                    BalanceHistory,
                    (BalanceHistory.account_id == Account.id)
                    & (BalanceHistory.date == latest_balance.c.max_date),
                )
            )
        ).all()
        seen = set()
        accounts = []
        for account, balance in rows:
            if account.id in seen:
                continue
            seen.add(account.id)
            accounts.append(
                {
                    "id": account.id,
                    "institution": institution.name,
                    "institution_id": account.institution_id,
                    "name": account.name,
                    "account_type": account.account_type,
                    "currency": account.currency,
                    "is_liability": account.is_liability,
                    "balance": float(balance) if balance is not None else 0.0,
                    "last_synced": (account.last_synced.isoformat() + "Z") if account.last_synced else None,
                }
            )
        return accounts
