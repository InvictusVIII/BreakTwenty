from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import RuntimeServiceLease
from app.services.sqlite_write_gate import sqlite_write_gate


@dataclass(frozen=True)
class RuntimeLeaseOwnership:
    service_name: str
    owner_token: str
    expires_at: datetime


async def acquire_runtime_service_lease(
    service_name: str,
    *,
    duration: timedelta,
) -> RuntimeLeaseOwnership | None:
    normalized_name = str(service_name or "").strip()
    if not normalized_name:
        raise ValueError("Runtime service name is required")
    now = datetime.now(timezone.utc)
    expires_at = now + duration
    owner_token = secrets.token_hex(24)
    try:
        async with async_session() as db, sqlite_write_gate(), db.begin():
            replaced = await db.execute(
                update(RuntimeServiceLease)
                .where(
                    RuntimeServiceLease.service_name == normalized_name,
                    RuntimeServiceLease.expires_at <= now,
                )
                .values(
                    owner_token=owner_token,
                    acquired_at=now,
                    expires_at=expires_at,
                )
            )
            if not replaced.rowcount:
                db.add(
                    RuntimeServiceLease(
                        service_name=normalized_name,
                        owner_token=owner_token,
                        acquired_at=now,
                        expires_at=expires_at,
                    )
                )
                await db.flush()
    except IntegrityError:
        return None
    return RuntimeLeaseOwnership(
        service_name=normalized_name,
        owner_token=owner_token,
        expires_at=expires_at,
    )


async def renew_runtime_service_lease(
    ownership: RuntimeLeaseOwnership,
    *,
    duration: timedelta,
) -> RuntimeLeaseOwnership | None:
    now = datetime.now(timezone.utc)
    expires_at = now + duration
    async with async_session() as db, sqlite_write_gate(), db.begin():
        result = await db.execute(
            update(RuntimeServiceLease)
            .where(
                RuntimeServiceLease.service_name == ownership.service_name,
                RuntimeServiceLease.owner_token == ownership.owner_token,
                RuntimeServiceLease.expires_at > now,
            )
            .values(expires_at=expires_at)
        )
    if result.rowcount != 1:
        return None
    return RuntimeLeaseOwnership(
        service_name=ownership.service_name,
        owner_token=ownership.owner_token,
        expires_at=expires_at,
    )


async def release_runtime_service_lease(ownership: RuntimeLeaseOwnership) -> bool:
    async with async_session() as db, sqlite_write_gate(), db.begin():
        result = await db.execute(
            delete(RuntimeServiceLease).where(
                RuntimeServiceLease.service_name == ownership.service_name,
                RuntimeServiceLease.owner_token == ownership.owner_token,
            )
        )
    return result.rowcount == 1


async def recover_expired_runtime_service_leases(*, force: bool = False) -> int:
    now = datetime.now(timezone.utc)
    async with async_session() as db, sqlite_write_gate(), db.begin():
        statement = delete(RuntimeServiceLease)
        if not force:
            statement = statement.where(RuntimeServiceLease.expires_at <= now)
        result = await db.execute(statement)
    return int(result.rowcount or 0)
