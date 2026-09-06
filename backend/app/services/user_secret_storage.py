from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserSecretArtifact
from app.services.app_encryption import encrypt_json_payload, load_json_payload

MARKET_DATA_API_KEY_SECRET_KIND = "market_data_api_key"


def _normalize_secret_kind(secret_kind: str | None) -> str:
    return str(secret_kind or "").strip()


def _secret_aad_context(user_id: int, secret_kind: str) -> dict[str, str | int]:
    return {
        "storage": "user_secret_artifacts",
        "user_id": int(user_id),
        "secret_kind": secret_kind,
    }


async def get_user_secret_artifact(
    db: AsyncSession,
    user_id: int,
    secret_kind: str,
) -> Any | None:
    normalized_kind = _normalize_secret_kind(secret_kind)
    if not normalized_kind:
        return None
    artifact = (
        await db.execute(
            select(UserSecretArtifact).where(
                UserSecretArtifact.user_id == user_id,
                UserSecretArtifact.secret_kind == normalized_kind,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        return None
    return load_json_payload(
        artifact.payload_json,
        aad_context=_secret_aad_context(user_id, normalized_kind),
    )


async def upsert_user_secret_artifact(
    db: AsyncSession,
    user_id: int,
    secret_kind: str,
    payload: Any,
) -> bool:
    normalized_kind = _normalize_secret_kind(secret_kind)
    if not normalized_kind:
        return False
    payload_json = encrypt_json_payload(
        payload,
        aad_context=_secret_aad_context(user_id, normalized_kind),
    )
    artifact = (
        await db.execute(
            select(UserSecretArtifact).where(
                UserSecretArtifact.user_id == user_id,
                UserSecretArtifact.secret_kind == normalized_kind,
            )
        )
    ).scalar_one_or_none()
    updated_at = datetime.now(timezone.utc)
    if artifact is None:
        db.add(
            UserSecretArtifact(
                user_id=user_id,
                secret_kind=normalized_kind,
                payload_json=payload_json,
                updated_at=updated_at,
            )
        )
    else:
        artifact.payload_json = payload_json
        artifact.updated_at = updated_at
    await db.flush()
    return True


async def delete_user_secret_artifact(
    db: AsyncSession,
    user_id: int,
    secret_kind: str,
) -> None:
    normalized_kind = _normalize_secret_kind(secret_kind)
    if not normalized_kind:
        return
    await db.execute(
        delete(UserSecretArtifact).where(
            UserSecretArtifact.user_id == user_id,
            UserSecretArtifact.secret_kind == normalized_kind,
        )
    )
    await db.flush()
