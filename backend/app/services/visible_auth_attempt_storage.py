from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

import app.database as database
from app.models import VisibleAuthAttemptArtifact
from app.services.auth_artifact_utils import (
    normalize_artifact_kind,
    normalize_provider,
    run_async_from_sync,
    utc_now,
)
from app.services.app_encryption import encrypt_json_payload, load_json_payload
from app.services.sqlite_write_gate import sqlite_write_gate


def _normalize_attempt_id(attempt_id: str | None) -> str:
    return str(attempt_id or "").strip()


def _attempt_artifact_aad_context(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> dict[str, str | int]:
    return {
        "storage": "visible_auth_attempt_artifacts",
        "user_id": int(user_id),
        "provider": provider,
        "attempt_id": attempt_id,
        "artifact_kind": artifact_kind,
    }


def save_visible_auth_attempt_artifact_sync(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
    payload: Any,
) -> bool:
    return run_async_from_sync(
        save_visible_auth_attempt_artifact_async(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
            payload,
        )
    )


async def save_visible_auth_attempt_artifact_async(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
    payload: Any,
) -> bool:
    normalized_provider = normalize_provider(provider)
    normalized_attempt_id = _normalize_attempt_id(attempt_id)
    normalized_kind = normalize_artifact_kind(artifact_kind)
    if not normalized_provider or not normalized_attempt_id or not normalized_kind:
        return False
    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        artifact = (
            await db.execute(
                select(VisibleAuthAttemptArtifact).where(
                    VisibleAuthAttemptArtifact.user_id == int(user_id),
                    VisibleAuthAttemptArtifact.provider == normalized_provider,
                    VisibleAuthAttemptArtifact.attempt_id == normalized_attempt_id,
                    VisibleAuthAttemptArtifact.artifact_kind == normalized_kind,
                )
            )
        ).scalar_one_or_none()
        payload_json = encrypt_json_payload(
            payload,
            aad_context=_attempt_artifact_aad_context(
                user_id,
                normalized_provider,
                normalized_attempt_id,
                normalized_kind,
            ),
        )
        updated_at = utc_now()
        if artifact is None:
            db.add(
                VisibleAuthAttemptArtifact(
                    user_id=int(user_id),
                    provider=normalized_provider,
                    attempt_id=normalized_attempt_id,
                    artifact_kind=normalized_kind,
                    payload_json=payload_json,
                    updated_at=updated_at,
                )
            )
        else:
            artifact.payload_json = payload_json
            artifact.updated_at = updated_at
        return True


def load_visible_auth_attempt_artifact_sync(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> Any | None:
    return run_async_from_sync(
        load_visible_auth_attempt_artifact_async(
            user_id,
            provider,
            attempt_id,
            artifact_kind,
        )
    )


async def load_visible_auth_attempt_artifact_async(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> Any | None:
    normalized_provider = normalize_provider(provider)
    normalized_attempt_id = _normalize_attempt_id(attempt_id)
    normalized_kind = normalize_artifact_kind(artifact_kind)
    if not normalized_provider or not normalized_attempt_id or not normalized_kind:
        return None
    async with database.async_session() as db:
        artifact = (
            await db.execute(
                select(VisibleAuthAttemptArtifact).where(
                    VisibleAuthAttemptArtifact.user_id == int(user_id),
                    VisibleAuthAttemptArtifact.provider == normalized_provider,
                    VisibleAuthAttemptArtifact.attempt_id == normalized_attempt_id,
                    VisibleAuthAttemptArtifact.artifact_kind == normalized_kind,
                )
            )
        ).scalar_one_or_none()
        if artifact is None:
            return None
        return load_json_payload(
            artifact.payload_json,
            aad_context=_attempt_artifact_aad_context(
                user_id,
                normalized_provider,
                normalized_attempt_id,
                normalized_kind,
            ),
        )


async def load_visible_auth_attempt_artifact(
    db,
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> Any | None:
    """Load one staged artifact through the caller's transaction/session."""
    normalized_provider = normalize_provider(provider)
    normalized_attempt_id = _normalize_attempt_id(attempt_id)
    normalized_kind = normalize_artifact_kind(artifact_kind)
    if not normalized_provider or not normalized_attempt_id or not normalized_kind:
        return None
    artifact = (
        await db.execute(
            select(VisibleAuthAttemptArtifact).where(
                VisibleAuthAttemptArtifact.user_id == int(user_id),
                VisibleAuthAttemptArtifact.provider == normalized_provider,
                VisibleAuthAttemptArtifact.attempt_id == normalized_attempt_id,
                VisibleAuthAttemptArtifact.artifact_kind == normalized_kind,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        return None
    return load_json_payload(
        artifact.payload_json,
        aad_context=_attempt_artifact_aad_context(
            user_id,
            normalized_provider,
            normalized_attempt_id,
            normalized_kind,
        ),
    )


async def delete_visible_auth_attempt_artifacts(
    db,
    user_id: int,
    provider: str,
    attempt_id: str,
) -> bool:
    """Delete staged artifacts inside the caller's transaction."""
    normalized_provider = normalize_provider(provider)
    normalized_attempt_id = _normalize_attempt_id(attempt_id)
    if not normalized_provider or not normalized_attempt_id:
        return False
    await db.execute(
        delete(VisibleAuthAttemptArtifact).where(
            VisibleAuthAttemptArtifact.user_id == int(user_id),
            VisibleAuthAttemptArtifact.provider == normalized_provider,
            VisibleAuthAttemptArtifact.attempt_id == normalized_attempt_id,
        )
    )
    await db.flush()
    return True


async def delete_visible_auth_attempt_artifacts_async(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kinds: tuple[str, ...] | list[str] | None = None,
) -> bool:
    normalized_provider = normalize_provider(provider)
    normalized_attempt_id = _normalize_attempt_id(attempt_id)
    if not normalized_provider or not normalized_attempt_id:
        return False
    kinds = tuple(str(kind).strip() for kind in (artifact_kinds or ()) if str(kind).strip())
    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        stmt = delete(VisibleAuthAttemptArtifact).where(
            VisibleAuthAttemptArtifact.user_id == int(user_id),
            VisibleAuthAttemptArtifact.provider == normalized_provider,
            VisibleAuthAttemptArtifact.attempt_id == normalized_attempt_id,
        )
        if kinds:
            stmt = stmt.where(VisibleAuthAttemptArtifact.artifact_kind.in_(kinds))
        await db.execute(stmt)
        return True
