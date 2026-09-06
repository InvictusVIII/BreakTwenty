import asyncio
import httpx
import logging
import secrets
import socket
import ssl
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Institution, PendingSyncStatus, ProviderSyncLease, Setting

logger = logging.getLogger("breaktwenty.sync_utils")

_sync_locks: dict[tuple[int, str, int | None], str] = {}
_sync_lock_renewal_tasks: dict[tuple[int, str, int | None], asyncio.Task[Any]] = {}

SYNC_LEASE_DURATION = timedelta(minutes=30)
SYNC_LEASE_RENEW_INTERVAL_SECONDS = 60
SYNC_LEASE_RENEW_ERROR_RETRY_SECONDS = 5
SYNC_LEASE_WATCHDOG_INTERVAL_SECONDS = 60

_STATUS_SEVERITY_RANK: dict[str, int] = {
    "ok": 0,
    "skipped": 0,
    "already_syncing": 0,
    "syncing": 0,
    "network_error": 2,
    "different_profile_detected": 3,
    "auth_required": 3,
    "error": 4,
    "error_scraper": 4,
    "error_flex": 4,
}


def _status_severity(status: str | None) -> int:
    return _STATUS_SEVERITY_RANK.get(str(status or "").strip().lower(), 1)

NETWORK_KEYWORDS = (
    "timeout", "connect", "dns", "network", "unreachable",
    "resolve", "name resolution", "name or service not known",
    "max retries exceeded", "connectionpool", "sslerror", "connectionrefused",
    "ns_error_unknown_host", "err_name_not_resolved", "host not found",
)

AUTH_REQUIRED_KEYWORDS = (
    "session expired",
    "no pending auth session",
    "auth required",
    "invalid token",
    "refresh token",
    "unauthorized",
    "cookies expired",
    "login again",
)

INVALID_CREDENTIAL_KEYWORDS = (
    "invalid credentials",
    "invalid username",
    "invalid password",
    "invalid username password combination",
    "wrong password",
    "doesn't seem right",
    "different answer",
    "please check your card",
    "please check your card number",
    "please check your card number / username or password",
    "not recognized",
    "incorrect",
)

INVALID_SCRAPER_CREDENTIAL_MARKER_PREFIX = "invalid_scraper_credentials:"
IBKR_SCRAPER_SUCCESS_SETTING_PREFIX = "ibkr_scraper_last_success_at:"


def is_network_error(e: Exception) -> bool:
    """Check if an exception is a network/connection/timeout error vs credential/auth."""
    network_types = [
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.TransportError,
        ConnectionError,
        TimeoutError,
        socket.gaierror,
        ssl.SSLError,
    ]
    try:
        import requests.exceptions

        network_types.extend([requests.exceptions.ConnectionError, requests.exceptions.Timeout])
    except ImportError:
        pass
    try:
        import urllib3.exceptions

        network_types.extend([urllib3.exceptions.MaxRetryError, urllib3.exceptions.NewConnectionError])
    except ImportError:
        pass
    if isinstance(e, tuple(network_types)):
        return True
    msg = str(e).lower()
    return any(kw in msg for kw in NETWORK_KEYWORDS)


def check_network_error_result(result: dict) -> bool:
    """Check if a service result dict contains a swallowed network error."""
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    msg = (result.get("message") or "").lower()
    return any(kw in msg for kw in NETWORK_KEYWORDS)


def check_auth_required_result(result: dict) -> bool:
    """Check if a result should be treated as auth/session renewal required."""
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    if status in ("auth_required", "2fa_required", "waiting", "different_profile_detected"):
        return True
    if status != "error":
        return False
    msg = (result.get("message") or "").lower()
    return any(kw in msg for kw in AUTH_REQUIRED_KEYWORDS)


def check_invalid_credentials_result(result: dict) -> bool:
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    msg = (result.get("message") or "").lower()
    return any(kw in msg for kw in INVALID_CREDENTIAL_KEYWORDS)


def get_persisted_sync_status(result: dict, *, error_status: str = "error") -> str:
    """Map a sync result dict onto the persisted institution sync_status contract."""
    if not isinstance(result, dict):
        return error_status
    status = result.get("status")
    if status in ("ok", "network_error", "auth_required"):
        return status
    if status == "different_profile_detected":
        return "auth_required"
    if status == "skipped":
        return "ok"
    if status in ("2fa_required", "waiting"):
        return "auth_required"
    if check_network_error_result(result):
        return "network_error"
    if check_auth_required_result(result):
        return "auth_required"
    return error_status


def _sync_lease_institution_key(institution_id: int | None) -> str:
    return str(int(institution_id)) if institution_id is not None else "new"


async def _renew_sync_lease(
    lock_key: tuple[int, str, int | None],
    owner_token: str,
    owner_task: asyncio.Task[Any],
    lease_expires_at: datetime,
) -> None:
    from app.services.sqlite_write_gate import sqlite_write_gate

    loop = asyncio.get_running_loop()
    lease_duration_seconds = max(SYNC_LEASE_DURATION.total_seconds(), 0.001)
    normal_delay = max(float(SYNC_LEASE_RENEW_INTERVAL_SECONDS), 0.001)
    error_retry_delay = min(
        normal_delay,
        max(float(SYNC_LEASE_RENEW_ERROR_RETRY_SECONDS), 0.001),
    )
    fence_margin = min(error_retry_delay, lease_duration_seconds / 2)
    initial_remaining = (lease_expires_at - datetime.now(timezone.utc)).total_seconds()
    confirmed_deadline = loop.time() + max(initial_remaining - fence_margin, 0)
    next_delay = normal_delay

    while True:
        remaining = confirmed_deadline - loop.time()
        if remaining <= 0:
            _cancel_lost_sync_lease_owner(
                lock_key,
                owner_token,
                owner_task,
                reason="renewal deadline elapsed",
            )
            return
        await asyncio.sleep(min(next_delay, remaining))
        remaining = confirmed_deadline - loop.time()
        if remaining <= 0:
            _cancel_lost_sync_lease_owner(
                lock_key,
                owner_token,
                owner_task,
                reason="renewal deadline elapsed",
            )
            return
        now = datetime.now(timezone.utc)
        renewed_expires_at = now + SYNC_LEASE_DURATION
        try:
            async with asyncio.timeout(remaining):
                async with async_session() as db, sqlite_write_gate():
                    async with db.begin():
                        result = await db.execute(
                            update(ProviderSyncLease)
                            .where(
                                ProviderSyncLease.user_id == lock_key[0],
                                ProviderSyncLease.provider == lock_key[1],
                                ProviderSyncLease.institution_key
                                == _sync_lease_institution_key(lock_key[2]),
                                ProviderSyncLease.owner_token == owner_token,
                            )
                            .values(expires_at=renewed_expires_at)
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "sync lease renewal failed user_id=%s provider=%s "
                "institution_id=%s error_type=%s",
                lock_key[0],
                lock_key[1],
                lock_key[2],
                type(exc).__name__,
            )
            next_delay = error_retry_delay
            continue
        if result.rowcount != 1:
            _cancel_lost_sync_lease_owner(
                lock_key,
                owner_token,
                owner_task,
                reason="durable ownership lost",
            )
            return
        renewed_remaining = (
            renewed_expires_at - datetime.now(timezone.utc)
        ).total_seconds()
        confirmed_deadline = loop.time() + max(renewed_remaining - fence_margin, 0)
        next_delay = normal_delay


def _cancel_lost_sync_lease_owner(
    lock_key: tuple[int, str, int | None],
    owner_token: str,
    owner_task: asyncio.Task[Any],
    *,
    reason: str,
) -> None:
    if _sync_locks.get(lock_key) == owner_token:
        _sync_locks.pop(lock_key, None)
    renewal_task = _sync_lock_renewal_tasks.get(lock_key)
    if renewal_task is asyncio.current_task():
        _sync_lock_renewal_tasks.pop(lock_key, None)
    logger.error(
        "sync lease ownership lost user_id=%s provider=%s institution_id=%s reason=%s",
        lock_key[0],
        lock_key[1],
        lock_key[2],
        reason,
    )
    if not owner_task.done():
        # This fences later async persistence, but Python cannot preempt an SDK
        # call already running in a thread; hard preemption requires a worker process.
        owner_task.cancel()


def _start_sync_lease_renewal(
    lock_key: tuple[int, str, int | None],
    owner_token: str,
    owner_task: asyncio.Task[Any],
    lease_expires_at: datetime,
) -> bool:
    try:
        from app.services.task_supervisor import create_tracked_task

        task = create_tracked_task(
            _renew_sync_lease(lock_key, owner_token, owner_task, lease_expires_at),
            name=f"sync-lease:{lock_key[0]}:{lock_key[1]}:{lock_key[2] or 'new'}",
        )
    except RuntimeError:
        return False
    _sync_lock_renewal_tasks[lock_key] = task
    return True


def _prune_inactive_local_sync_lock(lock_key: tuple[int, str, int | None]) -> bool:
    if lock_key not in _sync_locks:
        return False
    renewal_task = _sync_lock_renewal_tasks.get(lock_key)
    if renewal_task is not None and not renewal_task.done():
        return False
    _sync_locks.pop(lock_key, None)
    _sync_lock_renewal_tasks.pop(lock_key, None)
    return True


async def acquire_sync_lock(
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
    owner_token: str | None = None,
) -> bool:
    from app.services.sqlite_write_gate import sqlite_write_gate

    owner_task = asyncio.current_task()
    if owner_task is None:
        return False
    lock_key = (int(user_id), str(provider), institution_id)
    if _sync_locks.get(lock_key) and not _prune_inactive_local_sync_lock(lock_key):
        return False
    token = str(owner_token or secrets.token_hex(16))
    now = datetime.now(timezone.utc)
    lease_expires_at = now + SYNC_LEASE_DURATION
    try:
        async with async_session() as db, sqlite_write_gate():
            async with db.begin():
                replaced = await db.execute(
                    update(ProviderSyncLease)
                    .where(
                        ProviderSyncLease.user_id == lock_key[0],
                        ProviderSyncLease.provider == lock_key[1],
                        ProviderSyncLease.institution_key == _sync_lease_institution_key(lock_key[2]),
                        ProviderSyncLease.expires_at <= now,
                    )
                    .values(
                        owner_token=token,
                        acquired_at=now,
                        expires_at=lease_expires_at,
                    )
                )
                if not replaced.rowcount:
                    db.add(
                        ProviderSyncLease(
                            user_id=lock_key[0],
                            provider=lock_key[1],
                            institution_key=_sync_lease_institution_key(lock_key[2]),
                            owner_token=token,
                            acquired_at=now,
                            expires_at=lease_expires_at,
                        )
                    )
                    await db.flush()
    except IntegrityError:
        return False
    _sync_locks[lock_key] = token
    if not _start_sync_lease_renewal(
        lock_key,
        token,
        owner_task,
        lease_expires_at,
    ):
        await release_sync_lock(
            lock_key[0],
            lock_key[1],
            institution_id=lock_key[2],
            owner_token=token,
        )
        return False
    return True


def sync_lock_state_snapshot() -> dict[str, dict[str, str]]:
    return {
        f"{user_id}:{provider}:{institution_id or 'new'}": {
            "user_id": str(user_id),
            "provider": str(provider),
            "institution_id": str(institution_id or ""),
            "owner_token_set": "yes" if token else "no",
        }
        for (user_id, provider, institution_id), token in _sync_locks.items()
    }


async def release_sync_lock(
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
    owner_token: str | None = None,
) -> bool:
    from app.services.sqlite_write_gate import sqlite_write_gate

    lock_key = (int(user_id), str(provider), institution_id)
    local_token = _sync_locks.get(lock_key)
    token = str(owner_token) if owner_token is not None else local_token
    if not token:
        return False
    if owner_token is not None and local_token is not None and local_token != token:
        return False
    async with async_session() as db, sqlite_write_gate():
        async with db.begin():
            result = await db.execute(
                delete(ProviderSyncLease).where(
                    ProviderSyncLease.user_id == lock_key[0],
                    ProviderSyncLease.provider == lock_key[1],
                    ProviderSyncLease.institution_key == _sync_lease_institution_key(lock_key[2]),
                    ProviderSyncLease.owner_token == token,
                )
            )
    renewal_task = _sync_lock_renewal_tasks.pop(lock_key, None)
    if renewal_task and not renewal_task.done():
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task
    if local_token == token:
        _sync_locks.pop(lock_key, None)
    released = bool(result.rowcount)
    if released:
        try:
            from app.provider_catalog import get_status_provider_for_result
            status_provider = get_status_provider_for_result(provider)
        except Exception:
            status_provider = provider
        _schedule_drain_pending_status(int(user_id), str(status_provider), institution_id)
    return released


def _institution_id_from_lease_key(value: str) -> int | None:
    if str(value) == "new":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def recover_expired_sync_leases(*, force: bool = False) -> int:
    from app.services.sqlite_write_gate import sqlite_write_gate

    now = datetime.now(timezone.utc)
    async with async_session() as db, sqlite_write_gate(), db.begin():
        statement = select(ProviderSyncLease)
        if not force:
            statement = statement.where(ProviderSyncLease.expires_at <= now)
        expired = (await db.execute(statement)).scalars().all()
        for lease in expired:
            await db.delete(lease)
        pending = (
            await db.execute(select(PendingSyncStatus))
        ).scalars().all()
        drain_targets = [
            (
                int(row.user_id),
                str(row.status_provider),
                _institution_id_from_lease_key(row.institution_key),
            )
            for row in pending
        ]
    for user_id, status_provider, institution_id in drain_targets:
        await _drain_pending_status(user_id, status_provider, institution_id)
    return len(expired)


async def _sync_lease_watchdog_loop() -> None:
    while True:
        await asyncio.sleep(SYNC_LEASE_WATCHDOG_INTERVAL_SECONDS)
        try:
            await recover_expired_sync_leases()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("sync lease watchdog failed error_type=%s", type(exc).__name__)


def start_sync_lease_watchdog() -> None:
    from app.services.task_supervisor import create_tracked_task

    create_tracked_task(_sync_lease_watchdog_loop(), name="sync-lease-watchdog")


async def get_institution_by_provider(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
):
    if institution_id is None:
        try:
            from app.connectors.sync_context import get_connector_sync_context

            context = get_connector_sync_context()
            if context and context.user_id == int(user_id):
                institution_id = context.institution_id
        except ImportError:
            institution_id = None
    query = select(Institution).where(
        Institution.provider == provider,
        Institution.user_id == user_id,
    )
    if institution_id is not None:
        query = query.where(Institution.id == int(institution_id))
    return (await db.execute(query)).scalar_one_or_none()


def get_invalid_scraper_credentials_setting_key(provider: str, institution_id: int | str) -> str:
    return f"{get_invalid_scraper_credentials_setting_prefix(provider)}{institution_id}"


def get_invalid_scraper_credentials_setting_prefix(provider: str) -> str:
    return f"{INVALID_SCRAPER_CREDENTIAL_MARKER_PREFIX}{provider}:"


def get_ibkr_scraper_success_setting_key(institution_id: int) -> str:
    return f"{IBKR_SCRAPER_SUCCESS_SETTING_PREFIX}{int(institution_id)}"


def get_scraper_credentials_fingerprint(username: str, password: str) -> str:
    return hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()


async def set_invalid_scraper_credentials_marker(
    db: AsyncSession,
    user_id: int,
    provider: str,
    username: str,
    password: str,
    *,
    institution_id: int | None = None,
):
    institution = await get_institution_by_provider(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if institution is None:
        return
    key = get_invalid_scraper_credentials_setting_key(provider, institution.id)
    value = get_scraper_credentials_fingerprint(username, password)
    result = await db.execute(select(Setting).where(Setting.key == key, Setting.user_id == user_id))
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = value
    else:
        db.add(Setting(user_id=user_id, key=key, value=value))


async def clear_invalid_scraper_credentials_marker(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
):
    institution = await get_institution_by_provider(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if institution is None:
        return
    key = get_invalid_scraper_credentials_setting_key(provider, institution.id)
    await db.execute(delete(Setting).where(Setting.key == key, Setting.user_id == user_id))


async def saved_scraper_credentials_are_known_invalid(
    db: AsyncSession,
    user_id: int,
    provider: str,
    username: str,
    password: str,
    *,
    institution_id: int | None = None,
) -> bool:
    institution = await get_institution_by_provider(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if institution is None:
        return False
    key = get_invalid_scraper_credentials_setting_key(provider, institution.id)
    result = await db.execute(select(Setting).where(Setting.key == key, Setting.user_id == user_id))
    existing = result.scalar_one_or_none()
    if not existing:
        return False
    return existing.value == get_scraper_credentials_fingerprint(username, password)


async def set_ibkr_scraper_success_marker(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
    synced_at: datetime | None = None,
):
    synced_at = synced_at or datetime.now(timezone.utc)
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    value = synced_at.astimezone(timezone.utc).isoformat()
    key = get_ibkr_scraper_success_setting_key(institution_id)
    result = await db.execute(
        select(Setting).where(
            Setting.key == key,
            Setting.user_id == user_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = value
    else:
        db.add(Setting(user_id=user_id, key=key, value=value))


async def get_ibkr_scraper_success_marker(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
) -> datetime | None:
    result = await db.execute(
        select(Setting.value).where(
            Setting.key == get_ibkr_scraper_success_setting_key(institution_id),
            Setting.user_id == user_id,
        )
    )
    value = result.scalar_one_or_none()
    if not value:
        return None
    try:
        synced_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    return synced_at.astimezone(timezone.utc)


async def set_institution_sync_status(
    db: AsyncSession,
    user_id: int,
    provider: str,
    status: str,
    *,
    institution_id: int | None = None,
    commit: bool = True,
):
    institution = await get_institution_by_provider(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if institution:
        institution.sync_status = status
    if commit:
        await db.commit()
    return institution


def _related_route_providers_for_status(status_provider: str) -> tuple[str, ...]:
    try:
        from app.provider_catalog import (
            get_status_provider_for_result,
            iter_provider_metadata,
        )
    except Exception:
        return (status_provider,)
    related: list[str] = []
    target = str(status_provider or "").strip().lower()
    if not target:
        return ()
    for provider, _metadata in iter_provider_metadata():
        try:
            resolved = get_status_provider_for_result(provider)
        except KeyError:
            continue
        if str(resolved or "").strip().lower() == target:
            related.append(provider)
    if status_provider not in related:
        related.append(status_provider)
    return tuple(dict.fromkeys(related))


def _related_lock_providers(status_provider: str) -> tuple[str, ...]:
    try:
        from app.provider_catalog import get_lock_provider_for_route
    except Exception:
        return (status_provider,)
    locks: list[str] = []
    for provider in _related_route_providers_for_status(status_provider):
        try:
            lock_p = get_lock_provider_for_route(provider)
        except Exception:
            lock_p = provider
        if lock_p:
            locks.append(lock_p)
    return tuple(dict.fromkeys(locks))


def _any_lock_held(
    user_id: int,
    lock_providers: tuple[str, ...],
    institution_id: int | None = None,
) -> bool:
    for lock_p in lock_providers:
        for (locked_user_id, locked_provider, locked_institution_id), _token in _sync_locks.items():
            if locked_user_id != int(user_id) or locked_provider != lock_p:
                continue
            if institution_id is None or locked_institution_id == institution_id:
                return True
    return False


async def _any_durable_lock_held(
    user_id: int,
    lock_providers: tuple[str, ...],
    institution_id: int | None = None,
) -> bool:
    if not lock_providers:
        return False
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        query = select(ProviderSyncLease.id).where(
            ProviderSyncLease.user_id == int(user_id),
            ProviderSyncLease.provider.in_(lock_providers),
            ProviderSyncLease.expires_at > now,
        )
        if institution_id is not None:
            query = query.where(
                ProviderSyncLease.institution_key == _sync_lease_institution_key(institution_id)
            )
        return (await db.execute(query.limit(1))).first() is not None


async def _has_active_tximport(
    user_id: int,
    family_providers: tuple[str, ...],
    institution_id: int | None = None,
) -> bool:
    if not family_providers:
        return False
    try:
        from app.models import TransactionImportJob
    except Exception:
        return False
    try:
        async with async_session() as db:
            query = select(TransactionImportJob.id).where(
                TransactionImportJob.user_id == int(user_id),
                TransactionImportJob.provider.in_(family_providers),
                TransactionImportJob.status.in_(("queued", "running")),
            )
            if institution_id is not None:
                query = query.where(TransactionImportJob.institution_id == int(institution_id))
            row = (await db.execute(query.limit(1))).first()
        return row is not None
    except Exception as exc:
        logger.warning("pending-status active-tximport check failed user_id=%s: %s", user_id, exc)
        return False


async def _institution_family_actively_syncing(
    user_id: int,
    status_provider: str,
    institution_id: int | None = None,
) -> bool:
    family = _related_route_providers_for_status(status_provider)
    lock_providers = _related_lock_providers(status_provider)
    if _any_lock_held(user_id, lock_providers, institution_id):
        return True
    if await _any_durable_lock_held(user_id, lock_providers, institution_id):
        return True
    if await _has_active_tximport(user_id, family, institution_id):
        return True
    return False


async def _drain_pending_status(
    user_id: int,
    status_provider: str,
    institution_id: int | None = None,
) -> None:
    if await _institution_family_actively_syncing(int(user_id), status_provider, institution_id):
        return
    try:
        from app.services.sqlite_write_gate import sqlite_write_gate

        async with async_session() as db, sqlite_write_gate(), db.begin():
            pending = (
                await db.execute(
                    select(PendingSyncStatus)
                    .where(
                        PendingSyncStatus.user_id == int(user_id),
                        PendingSyncStatus.status_provider == str(status_provider),
                        PendingSyncStatus.institution_key
                        == _sync_lease_institution_key(institution_id),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if pending is None:
                return
            await set_institution_sync_status(
                db,
                int(user_id),
                status_provider,
                pending.final_status,
                institution_id=institution_id,
                commit=False,
            )
            await db.delete(pending)
    except Exception as exc:
        logger.warning(
            "pending-status drain write failed user_id=%s provider=%s: %s",
            user_id,
            status_provider,
            exc,
        )
        return
    # The deferred sync_status only lands here, after the provider family goes
    # idle. The sync-activity settle event already fired while the status was
    # still stale, so emit one more so subscribers refetch the final state and a
    # chip stuck on auth_required flips to its real status.
    try:
        from app.services.event_bus import EVENT_SYNC_ACTIVITY, mark_dirty

        mark_dirty(int(user_id), EVENT_SYNC_ACTIVITY)
    except Exception as exc:
        logger.warning(
            "pending-status drain notify failed user_id=%s provider=%s: %s",
            user_id,
            status_provider,
            exc,
        )


async def clear_pending_status_writes(
    user_id: int,
    status_provider: str,
    *,
    institution_id: int | None = None,
    db: AsyncSession | None = None,
) -> None:
    """Drop durable pending statuses before connection removal."""
    family = _related_route_providers_for_status(status_provider)
    providers = {str(status_provider), *(str(related) for related in family)}
    query = delete(PendingSyncStatus).where(
        PendingSyncStatus.user_id == int(user_id),
        PendingSyncStatus.status_provider.in_(providers),
    )
    if institution_id is not None:
        query = query.where(
            PendingSyncStatus.institution_key
            == _sync_lease_institution_key(institution_id)
        )
    if db is not None:
        await db.execute(query)
        return
    from app.services.sqlite_write_gate import sqlite_write_gate

    async with async_session() as owned_db, sqlite_write_gate(), owned_db.begin():
        await owned_db.execute(query)


def _schedule_drain_pending_status(
    user_id: int,
    status_provider: str,
    institution_id: int | None = None,
) -> None:
    if not status_provider:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from app.services.task_supervisor import create_tracked_task

        create_tracked_task(
            _drain_pending_status(int(user_id), str(status_provider), institution_id),
            name=f"sync-status-drain:{user_id}:{status_provider}",
        )
    except RuntimeError:
        return


async def persist_result_sync_status(
    db: AsyncSession,
    user_id: int,
    provider: str,
    result: dict,
    *,
    error_status: str = "error",
    institution_id: int | None = None,
    commit: bool = True,
):
    final_status = get_persisted_sync_status(result, error_status=error_status)
    new_severity = _status_severity(final_status)
    institution_key = _sync_lease_institution_key(institution_id)
    updated_at = datetime.now(timezone.utc)
    insert_pending = sqlite_insert(PendingSyncStatus).values(
        user_id=int(user_id),
        status_provider=str(provider),
        institution_key=institution_key,
        final_status=final_status,
        severity=new_severity,
        updated_at=updated_at,
    )
    await db.execute(
        insert_pending.on_conflict_do_update(
            index_elements=(
                PendingSyncStatus.user_id,
                PendingSyncStatus.status_provider,
                PendingSyncStatus.institution_key,
            ),
            set_={
                "final_status": insert_pending.excluded.final_status,
                "severity": insert_pending.excluded.severity,
                "updated_at": insert_pending.excluded.updated_at,
            },
            where=insert_pending.excluded.severity >= PendingSyncStatus.severity,
        )
    )
    existing = (
        await db.execute(
            select(PendingSyncStatus)
            .where(
                PendingSyncStatus.user_id == int(user_id),
                PendingSyncStatus.status_provider == str(provider),
                PendingSyncStatus.institution_key == institution_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is None:
        raise RuntimeError("pending sync status upsert did not return a row")
    if await _institution_family_actively_syncing(int(user_id), provider, institution_id):
        # Hold the write; the last sync to finish for this institution flushes.
        if commit:
            await db.commit()
        return None
    flushed_status = existing.final_status
    institution = await set_institution_sync_status(
        db,
        int(user_id),
        provider,
        flushed_status,
        institution_id=institution_id,
        commit=False,
    )
    await db.delete(existing)
    if commit:
        await db.commit()
    return institution
