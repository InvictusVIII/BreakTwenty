from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.database as database
from app.models import ConnectionAuthArtifact, Institution
from app.services.auth_artifact_utils import (
    normalize_artifact_kind,
    normalize_provider,
    run_async_from_sync,
    utc_now,
)
from app.services.app_encryption import encrypt_json_payload, load_json_payload
from app.services.sqlite_write_gate import sqlite_write_gate

SCRAPER_CREDENTIALS_ARTIFACT_KIND = "scraper_credentials"
API_CREDENTIALS_ARTIFACT_KIND = "api_credentials"
ACTIVE_SLOT = "active"
QUARANTINE_SLOT = "quarantine"
API_CREDENTIAL_REPLACEMENT_SCHEMA = "breaktwenty.api-credential-replacement.v1"
API_CREDENTIAL_REPLACEMENT_TTL = timedelta(minutes=30)
API_CREDENTIAL_REPLACEMENT_WATCHDOG_SECONDS = 60
logger = logging.getLogger("breaktwenty.connection_auth_storage")
_api_credential_replacement_watchdog_task: asyncio.Task[Any] | None = None
_SHELL_PLACEHOLDER_USERNAME_RE = re.compile(r"^\$(?:[A-Z][A-Z0-9_]*|\{[A-Z][A-Z0-9_]*\})$")
_PERCENT_PLACEHOLDER_USERNAME_RE = re.compile(r"^%[A-Z][A-Z0-9_]*%$")
_TEMPLATE_PLACEHOLDER_USERNAME_RE = re.compile(
    r"^(?:\{\{\s*(?:username|user_name|user|userid|user_id|login|email)\s*\}\}|"
    r"<\s*(?:username|user name|user_name|userid|user id|user_id|login|email)\s*>)$",
    re.IGNORECASE,
)


class ProviderAlreadyConnectedError(ValueError):
    pass


class ApiCredentialReplacementExpiredError(ValueError):
    pass


@dataclass(frozen=True)
class StoredScraperCredentials:
    username: str
    password: str
    updated_at: datetime | None = None
    institution_id: int | None = None


def _artifact_aad_context(
    user_id: int,
    institution_id: int,
    provider: str,
    artifact_kind: str,
    slot: str,
) -> dict[str, str | int]:
    return {
        "storage": "connection_auth_artifacts",
        "user_id": int(user_id),
        "institution_id": int(institution_id),
        "provider": provider,
        "artifact_kind": artifact_kind,
        "slot": slot,
    }


def _serialize_artifact_payload(
    payload: Any,
    *,
    user_id: int,
    institution_id: int,
    provider: str,
    artifact_kind: str,
    slot: str,
) -> str:
    return encrypt_json_payload(
        payload,
        aad_context=_artifact_aad_context(
            user_id,
            institution_id,
            provider,
            artifact_kind,
            slot,
        ),
    )


def _deserialize_artifact_payload(
    payload_json: str,
    *,
    user_id: int,
    institution_id: int,
    provider: str,
    artifact_kind: str,
    slot: str,
) -> Any:
    return load_json_payload(
        payload_json,
        aad_context=_artifact_aad_context(
            user_id,
            institution_id,
            provider,
            artifact_kind,
            slot,
        ),
    )


def _deserialize_stored_artifact(artifact: ConnectionAuthArtifact) -> Any:
    return _deserialize_artifact_payload(
        artifact.payload_json,
        user_id=artifact.user_id,
        institution_id=artifact.institution_id,
        provider=artifact.provider,
        artifact_kind=artifact.artifact_kind,
        slot=artifact.slot,
    )


def _api_storage_provider(provider: str) -> str:
    normalized_provider = normalize_provider(provider)
    try:
        from app.provider_catalog import get_provider_credential_storage_provider

        return normalize_provider(get_provider_credential_storage_provider(normalized_provider))
    except Exception:
        return normalized_provider


def is_scraper_username_placeholder(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        _SHELL_PLACEHOLDER_USERNAME_RE.fullmatch(text)
        or _PERCENT_PLACEHOLDER_USERNAME_RE.fullmatch(text)
        or _TEMPLATE_PLACEHOLDER_USERNAME_RE.fullmatch(text)
    )


def load_connection_artifact_sync(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> Any | None:
    return run_async_from_sync(
        load_connection_artifact_async(
            user_id,
            provider,
            artifact_kind,
            slot=slot,
            institution_id=institution_id,
        )
    )


def save_connection_artifact_sync(
    user_id: int,
    provider: str,
    artifact_kind: str,
    payload: Any,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> bool:
    return run_async_from_sync(
        save_connection_artifact_async(
            user_id,
            provider,
            artifact_kind,
            payload,
            slot=slot,
            institution_id=institution_id,
        )
    )


def describe_connection_artifact_sync(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> dict[str, Any] | None:
    return run_async_from_sync(
        describe_connection_artifact_async(
            user_id,
            provider,
            artifact_kind,
            slot=slot,
            institution_id=institution_id,
        )
    )


def provider_has_connection_artifacts_sync(
    user_id: int,
    provider: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> bool:
    return run_async_from_sync(
        provider_has_connection_artifacts_async(
            user_id,
            provider,
            slot=slot,
            institution_id=institution_id,
        )
    )


async def load_connection_artifact_async(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> Any | None:
    async with database.async_session() as db:
        connection_id = await resolve_connection_id(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if connection_id is None:
            return None
        artifact = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == int(user_id),
                    ConnectionAuthArtifact.institution_id == int(connection_id),
                    ConnectionAuthArtifact.artifact_kind == normalize_artifact_kind(artifact_kind),
                    ConnectionAuthArtifact.slot == (str(slot or ACTIVE_SLOT).strip() or ACTIVE_SLOT),
                )
            )
        ).scalar_one_or_none()
        if artifact is None:
            return None
        return _deserialize_stored_artifact(artifact)


async def save_connection_artifact_async(
    user_id: int,
    provider: str,
    artifact_kind: str,
    payload: Any,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> bool:
    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        return await upsert_connection_artifact(
            db,
            user_id,
            provider,
            artifact_kind,
            payload,
            slot=slot,
            institution_id=institution_id,
        )


async def upsert_connection_artifact(
    db: AsyncSession,
    user_id: int,
    provider: str,
    artifact_kind: str,
    payload: Any,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> bool:
    """Upsert one encrypted connection artifact in the caller's transaction."""
    connection_id = await resolve_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is None:
        return False
    normalized_slot = str(slot or ACTIVE_SLOT).strip() or ACTIVE_SLOT
    normalized_kind = normalize_artifact_kind(artifact_kind)
    normalized_provider = normalize_provider(provider)
    artifact = (
        await db.execute(
            select(ConnectionAuthArtifact).where(
                ConnectionAuthArtifact.user_id == int(user_id),
                ConnectionAuthArtifact.institution_id == int(connection_id),
                ConnectionAuthArtifact.artifact_kind == normalized_kind,
                ConnectionAuthArtifact.slot == normalized_slot,
            )
        )
    ).scalar_one_or_none()
    payload_json = _serialize_artifact_payload(
        payload,
        user_id=user_id,
        institution_id=connection_id,
        provider=normalized_provider,
        artifact_kind=normalized_kind,
        slot=normalized_slot,
    )
    updated_at = utc_now()
    if artifact is None:
        db.add(
            ConnectionAuthArtifact(
                user_id=int(user_id),
                institution_id=int(connection_id),
                provider=normalized_provider,
                artifact_kind=normalized_kind,
                slot=normalized_slot,
                payload_json=payload_json,
                updated_at=updated_at,
            )
        )
    else:
        artifact.provider = normalized_provider
        artifact.payload_json = payload_json
        artifact.updated_at = updated_at
    await db.flush()
    return True


async def describe_connection_artifact_async(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> dict[str, Any] | None:
    async with database.async_session() as db:
        connection_id = await resolve_connection_id(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if connection_id is None:
            return None
        artifact = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == int(user_id),
                    ConnectionAuthArtifact.institution_id == int(connection_id),
                    ConnectionAuthArtifact.artifact_kind == normalize_artifact_kind(artifact_kind),
                    ConnectionAuthArtifact.slot == (str(slot or ACTIVE_SLOT).strip() or ACTIVE_SLOT),
                )
            )
        ).scalar_one_or_none()
        if artifact is None:
            return None
        return {
            "storage": "db",
            "size_bytes": len((artifact.payload_json or "").encode("utf-8")),
            "modified_at": artifact.updated_at,
            "institution_id": connection_id,
            "slot": slot,
        }


async def provider_has_connection_artifacts_async(
    user_id: int,
    provider: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> bool:
    async with database.async_session() as db:
        connection_id = await resolve_connection_id(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if connection_id is None:
            return False
        row = (
            await db.execute(
                select(ConnectionAuthArtifact.id).where(
                    ConnectionAuthArtifact.user_id == int(user_id),
                    ConnectionAuthArtifact.institution_id == int(connection_id),
                    ConnectionAuthArtifact.slot == (str(slot or ACTIVE_SLOT).strip() or ACTIVE_SLOT),
                    ConnectionAuthArtifact.artifact_kind.in_(("cookies", "storage_state", "session")),
                )
            )
        ).first()
        return row is not None


async def resolve_connection_id(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> int | None:
    normalized_provider = normalize_provider(provider)
    if institution_id is None:
        try:
            from app.connectors.sync_context import get_connector_sync_context

            context = get_connector_sync_context()
            if context and context.user_id == int(user_id):
                institution_id = context.institution_id
        except ImportError:
            institution_id = None
    if institution_id:
        connection = (
            await db.execute(
                select(Institution).where(
                    Institution.id == int(institution_id),
                    Institution.user_id == int(user_id),
                    Institution.provider == normalized_provider,
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            raise ValueError("The selected institution connection does not belong to this provider.")
        return int(connection.id)
    connection = (
        await db.execute(
            select(Institution)
            .where(
                Institution.user_id == user_id,
                Institution.provider == normalized_provider,
            )
        )
    ).scalar_one_or_none()
    return int(connection.id) if connection is not None else None


async def delete_connection_artifacts(
    db: AsyncSession,
    user_id: int,
    provider: str,
    artifact_kinds: tuple[str, ...] | list[str] | None = None,
    *,
    slots: tuple[str, ...] | list[str] = (ACTIVE_SLOT,),
    institution_id: int | None = None,
) -> bool:
    connection_id = await resolve_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is None:
        return False

    kinds = tuple(str(kind).strip() for kind in (artifact_kinds or ()) if str(kind).strip())
    normalized_slots = tuple(str(slot).strip() or ACTIVE_SLOT for slot in slots)
    stmt = delete(ConnectionAuthArtifact).where(
        ConnectionAuthArtifact.user_id == user_id,
        ConnectionAuthArtifact.institution_id == connection_id,
        ConnectionAuthArtifact.slot.in_(normalized_slots),
    )
    if kinds:
        stmt = stmt.where(ConnectionAuthArtifact.artifact_kind.in_(kinds))
    await db.execute(stmt)
    await db.flush()
    return True


async def delete_provider_connection_artifacts(
    db: AsyncSession,
    user_id: int,
    provider: str,
    artifact_kinds: tuple[str, ...] | list[str] | None = None,
    *,
    slots: tuple[str, ...] | list[str] | None = None,
    institution_id: int | None = None,
) -> None:
    normalized_provider = normalize_provider(provider)
    if not normalized_provider:
        return

    kinds = tuple(str(kind).strip() for kind in (artifact_kinds or ()) if str(kind).strip())
    normalized_slots = tuple(str(slot).strip() or ACTIVE_SLOT for slot in (slots or ()) if str(slot).strip())
    stmt = delete(ConnectionAuthArtifact).where(
        ConnectionAuthArtifact.user_id == user_id,
        ConnectionAuthArtifact.provider == normalized_provider,
    )
    if institution_id is not None:
        stmt = stmt.where(ConnectionAuthArtifact.institution_id == int(institution_id))
    if kinds:
        stmt = stmt.where(ConnectionAuthArtifact.artifact_kind.in_(kinds))
    if normalized_slots:
        stmt = stmt.where(ConnectionAuthArtifact.slot.in_(normalized_slots))

    await db.execute(stmt)
    await db.flush()


async def move_connection_artifacts_to_slot(
    db: AsyncSession,
    user_id: int,
    provider: str,
    artifact_kinds: tuple[str, ...] | list[str],
    *,
    source_slot: str = ACTIVE_SLOT,
    target_slot: str = QUARANTINE_SLOT,
    institution_id: int | None = None,
) -> bool:
    connection_id = await resolve_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is None:
        return False

    normalized_provider = normalize_provider(provider)
    normalized_source_slot = str(source_slot or ACTIVE_SLOT).strip() or ACTIVE_SLOT
    normalized_target_slot = str(target_slot or QUARANTINE_SLOT).strip() or QUARANTINE_SLOT
    if normalized_source_slot == normalized_target_slot:
        return False
    moved = False
    for artifact_kind in artifact_kinds:
        normalized_kind = normalize_artifact_kind(artifact_kind)
        if not normalized_kind:
            continue

        source = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == normalized_kind,
                    ConnectionAuthArtifact.slot == normalized_source_slot,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            continue

        target = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == normalized_kind,
                    ConnectionAuthArtifact.slot == normalized_target_slot,
                )
            )
        ).scalar_one_or_none()

        payload = _deserialize_stored_artifact(source)
        target_payload_json = _serialize_artifact_payload(
            payload,
            user_id=user_id,
            institution_id=connection_id,
            provider=normalized_provider,
            artifact_kind=normalized_kind,
            slot=normalized_target_slot,
        )
        updated_at = utc_now()
        if target is None:
            source.payload_json = target_payload_json
            source.slot = normalized_target_slot
            source.provider = normalized_provider
            source.updated_at = updated_at
        else:
            target.provider = normalized_provider
            target.payload_json = target_payload_json
            target.updated_at = updated_at
            await db.delete(source)
        moved = True

    if moved:
        await db.flush()
    return moved


async def get_scraper_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> StoredScraperCredentials | None:
    connection_id = await resolve_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is not None:
        artifact = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                )
            )
        ).scalar_one_or_none()
        if artifact is not None:
            payload = _deserialize_stored_artifact(artifact)
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
            if not is_scraper_username_placeholder(username):
                return StoredScraperCredentials(
                    username=username,
                    password=password,
                    updated_at=artifact.updated_at,
                    institution_id=connection_id,
                )

    return None


async def upsert_scraper_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    username: str,
    password: str,
    *,
    institution_id: int | None = None,
) -> bool:
    if is_scraper_username_placeholder(username):
        return False

    connection_id = await resolve_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is None:
        return False

    normalized_provider = normalize_provider(provider)
    updated_at = utc_now()
    payload_json = _serialize_artifact_payload(
        {"username": username, "password": password},
        user_id=user_id,
        institution_id=connection_id,
        provider=normalized_provider,
        artifact_kind=SCRAPER_CREDENTIALS_ARTIFACT_KIND,
        slot=ACTIVE_SLOT,
    )
    artifact = (
        await db.execute(
            select(ConnectionAuthArtifact).where(
                ConnectionAuthArtifact.user_id == user_id,
                ConnectionAuthArtifact.institution_id == connection_id,
                ConnectionAuthArtifact.artifact_kind == SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                ConnectionAuthArtifact.slot == ACTIVE_SLOT,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        db.add(
            ConnectionAuthArtifact(
                user_id=user_id,
                institution_id=connection_id,
                provider=normalized_provider,
                artifact_kind=SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                slot=ACTIVE_SLOT,
                payload_json=payload_json,
                updated_at=updated_at,
            )
        )
    else:
        artifact.provider = normalized_provider
        artifact.payload_json = payload_json
        artifact.updated_at = updated_at

    await db.flush()
    return True


async def _resolve_api_credentials_connection_id(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> int | None:
    if institution_id:
        return await resolve_connection_id(
            db,
            user_id,
            _api_storage_provider(provider),
            institution_id=int(institution_id),
        )
    return await resolve_connection_id(
        db,
        user_id,
        _api_storage_provider(provider),
    )


async def ensure_api_credentials_institution(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    pending_add: bool = False,
    institution_id: int | None = None,
) -> int | None:
    storage_provider = _api_storage_provider(provider)
    if not storage_provider:
        return None

    return await ensure_connection(
        db,
        user_id,
        storage_provider,
        pending_add=pending_add,
        institution_id=institution_id,
    )


async def ensure_connection(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    pending_add: bool = False,
    institution_id: int | None = None,
) -> int | None:
    normalized_provider = normalize_provider(provider)
    if not normalized_provider:
        return None
    if institution_id is not None:
        connection_id = await resolve_connection_id(
            db,
            user_id,
            normalized_provider,
            institution_id=institution_id,
        )
        if pending_add:
            existing = await db.get(Institution, connection_id)
            if existing is not None and (bool(existing.enabled) or not bool(existing.hidden)):
                raise ProviderAlreadyConnectedError(
                    "This provider is already connected. Remove the existing institution before adding it again."
                )
        return connection_id

    existing = (
        await db.execute(
            select(Institution).where(
                Institution.user_id == user_id,
                Institution.provider == normalized_provider,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if pending_add and (bool(existing.enabled) or not bool(existing.hidden)):
            raise ProviderAlreadyConnectedError(
                "This provider is already connected. Remove the existing institution before adding it again."
            )
        return int(existing.id)

    try:
        from app.provider_catalog import get_provider_display_name, get_provider_institution_type

        name = get_provider_display_name(normalized_provider)
        inst_type = get_provider_institution_type(normalized_provider)
    except Exception:
        name = normalized_provider
        inst_type = "api"

    institution = Institution(
        user_id=user_id,
        name=name,
        type=inst_type,
        provider=normalized_provider,
        enabled=not pending_add,
        hidden=pending_add,
    )
    db.add(institution)
    await db.flush()
    return int(institution.id)


async def get_api_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
    setting_keys: tuple[str, ...] | list[str] | None = None,
) -> dict[str, str]:
    normalized_provider = normalize_provider(provider)
    connection_id = await _resolve_api_credentials_connection_id(
        db,
        user_id,
        normalized_provider,
        institution_id=institution_id,
    )
    payload: dict[str, str] = {}
    if connection_id is not None:
        artifact = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                )
            )
        ).scalar_one_or_none()
        if artifact is not None:
            loaded = _deserialize_stored_artifact(artifact)
            if isinstance(loaded, dict):
                payload.update({str(key): str(value) for key, value in loaded.items() if value is not None})

    if setting_keys:
        allowed_keys = set(str(key) for key in setting_keys)
        payload = {key: value for key, value in payload.items() if key in allowed_keys}
    return payload


async def upsert_api_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    credentials: dict[str, Any],
    *,
    institution_id: int | None = None,
    ensure_institution: bool = False,
    pending_add: bool = False,
    replace: bool = False,
) -> bool:
    normalized_provider = normalize_provider(provider)
    incoming = {
        str(key): str(value)
        for key, value in (credentials or {}).items()
        if str(key or "").strip()
    }
    if not normalized_provider or not incoming:
        return False

    if ensure_institution:
        connection_id = await ensure_api_credentials_institution(
            db,
            user_id,
            normalized_provider,
            pending_add=pending_add,
            institution_id=institution_id,
        )
    else:
        connection_id = await _resolve_api_credentials_connection_id(
            db,
            user_id,
            normalized_provider,
            institution_id=institution_id,
        )
    if connection_id is None:
        return False

    payload = {} if replace else await get_api_credentials(
        db,
        user_id,
        normalized_provider,
        institution_id=connection_id,
    )
    payload.update(incoming)

    updated_at = utc_now()
    payload_json = _serialize_artifact_payload(
        payload,
        user_id=user_id,
        institution_id=connection_id,
        provider=normalized_provider,
        artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
        slot=ACTIVE_SLOT,
    )
    artifact = (
        await db.execute(
            select(ConnectionAuthArtifact).where(
                ConnectionAuthArtifact.user_id == user_id,
                ConnectionAuthArtifact.institution_id == connection_id,
                ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                ConnectionAuthArtifact.slot == ACTIVE_SLOT,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        db.add(
            ConnectionAuthArtifact(
                user_id=user_id,
                institution_id=connection_id,
                provider=normalized_provider,
                artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
                slot=ACTIVE_SLOT,
                payload_json=payload_json,
                updated_at=updated_at,
            )
        )
    else:
        artifact.provider = normalized_provider
        artifact.payload_json = payload_json
        artifact.updated_at = updated_at

    await db.flush()
    return True


async def begin_api_credential_replacement(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int,
    previous_credentials: dict[str, str],
    candidate_credentials: dict[str, str],
) -> str | None:
    connection_id = await _resolve_api_credentials_connection_id(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if connection_id is None:
        return None
    existing_backup = (
        await db.execute(
            select(ConnectionAuthArtifact).where(
                ConnectionAuthArtifact.user_id == user_id,
                ConnectionAuthArtifact.institution_id == connection_id,
                ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
            )
        )
    ).scalar_one_or_none()
    if existing_backup is not None:
        raise ValueError("A credential replacement is already pending for this connection")
    replacement_id = str(uuid.uuid4())
    normalized_provider = normalize_provider(provider)
    db.add(
        ConnectionAuthArtifact(
            user_id=user_id,
            institution_id=connection_id,
            provider=normalized_provider,
            artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
            slot=QUARANTINE_SLOT,
            payload_json=_serialize_artifact_payload(
                {
                    "schema": API_CREDENTIAL_REPLACEMENT_SCHEMA,
                    "replacement_id": replacement_id,
                    "had_credentials": bool(previous_credentials),
                    "credentials": dict(previous_credentials),
                    "candidate_credentials": dict(candidate_credentials),
                },
                user_id=user_id,
                institution_id=connection_id,
                provider=normalized_provider,
                artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
                slot=QUARANTINE_SLOT,
            ),
            updated_at=utc_now(),
        )
    )
    await db.flush()
    return replacement_id


def _normalized_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _settle_api_credential_replacement_backup(
    db: AsyncSession,
    backup: ConnectionAuthArtifact,
    *,
    commit: bool,
) -> bool:
    payload = _deserialize_stored_artifact(backup)
    if payload.get("schema") != API_CREDENTIAL_REPLACEMENT_SCHEMA:
        return False
    credentials = payload.get("credentials")
    had_credentials = payload.get("had_credentials")
    if type(had_credentials) is not bool or not isinstance(credentials, dict):
        return False
    if not commit:
        active = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == backup.user_id,
                    ConnectionAuthArtifact.institution_id == backup.institution_id,
                    ConnectionAuthArtifact.provider == backup.provider,
                    ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                )
            )
        ).scalar_one_or_none()
        if backup.provider == "questrade" and active is not None:
            candidate_credentials = payload.get("candidate_credentials")
            active_credentials = _deserialize_stored_artifact(active)
            if isinstance(candidate_credentials, dict) and isinstance(active_credentials, dict):
                previous_refresh_token = str(credentials.get("questrade_refresh_token") or "")
                candidate_refresh_token = str(
                    candidate_credentials.get("questrade_refresh_token") or ""
                )
                active_refresh_token = str(
                    active_credentials.get("questrade_refresh_token") or ""
                )
                if (
                    active_refresh_token
                    and candidate_refresh_token
                    and active_refresh_token != candidate_refresh_token
                    and active_refresh_token != previous_refresh_token
                ):
                    logger.info(
                        "Preserved rotated Questrade credentials while settling replacement "
                        "user_id=%s institution_id=%s",
                        backup.user_id,
                        backup.institution_id,
                    )
                    await db.delete(backup)
                    await db.flush()
                    return True
        if had_credentials:
            if active is None:
                active = ConnectionAuthArtifact(
                    user_id=backup.user_id,
                    institution_id=backup.institution_id,
                    provider=backup.provider,
                    artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
                    slot=ACTIVE_SLOT,
                )
                db.add(active)
            active.payload_json = _serialize_artifact_payload(
                credentials,
                user_id=backup.user_id,
                institution_id=backup.institution_id,
                provider=backup.provider,
                artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
                slot=ACTIVE_SLOT,
            )
            active.updated_at = utc_now()
        elif active is not None:
            await db.delete(active)
    await db.delete(backup)
    await db.flush()
    return True


async def settle_api_credential_replacement(
    db: AsyncSession,
    user_id: int,
    replacement_id: str,
    *,
    commit: bool,
) -> bool:
    try:
        normalized_id = str(uuid.UUID(str(replacement_id or "")))
    except (ValueError, AttributeError):
        return False
    backups = (
        await db.execute(
            select(ConnectionAuthArtifact).where(
                ConnectionAuthArtifact.user_id == user_id,
                ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
            )
        )
    ).scalars().all()
    for backup in backups:
        payload = _deserialize_stored_artifact(backup)
        if (
            payload.get("schema") != API_CREDENTIAL_REPLACEMENT_SCHEMA
            or payload.get("replacement_id") != normalized_id
        ):
            continue
        expired = _normalized_timestamp(backup.updated_at) <= (
            utc_now() - API_CREDENTIAL_REPLACEMENT_TTL
        )
        settled = await _settle_api_credential_replacement_backup(
            db,
            backup,
            commit=commit and not expired,
        )
        if settled and commit and expired:
            raise ApiCredentialReplacementExpiredError(
                "Credential replacement expired and previous credentials were restored"
            )
        return settled
    return False


async def recover_expired_api_credential_replacements(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    now: datetime | None = None,
) -> int:
    cutoff = _normalized_timestamp(now or utc_now()) - API_CREDENTIAL_REPLACEMENT_TTL
    statement = select(ConnectionAuthArtifact).where(
        ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
        ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
        ConnectionAuthArtifact.updated_at <= cutoff,
    )
    if user_id is not None:
        statement = statement.where(ConnectionAuthArtifact.user_id == user_id)
    backups = (await db.execute(statement)).scalars().all()
    recovered = 0
    for backup in backups:
        if await _settle_api_credential_replacement_backup(db, backup, commit=False):
            recovered += 1
    return recovered


async def recover_api_credential_replacements() -> int:
    recovered = 0
    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        backups = (
            await db.execute(
                select(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
                )
            )
        ).scalars().all()
        for backup in backups:
            payload = _deserialize_stored_artifact(backup)
            replacement_id = str(payload.get("replacement_id") or "")
            if payload.get("schema") != API_CREDENTIAL_REPLACEMENT_SCHEMA or not replacement_id:
                continue
            if await _settle_api_credential_replacement_backup(db, backup, commit=False):
                recovered += 1
    return recovered


async def _api_credential_replacement_watchdog_loop() -> None:
    while True:
        await asyncio.sleep(API_CREDENTIAL_REPLACEMENT_WATCHDOG_SECONDS)
        try:
            async with database.async_session() as db, sqlite_write_gate(), db.begin():
                await recover_expired_api_credential_replacements(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "API credential replacement recovery failed error_type=%s",
                type(exc).__name__,
            )


def start_api_credential_replacement_watchdog() -> None:
    global _api_credential_replacement_watchdog_task
    if (
        _api_credential_replacement_watchdog_task
        and not _api_credential_replacement_watchdog_task.done()
    ):
        return
    from app.services.task_supervisor import create_tracked_task

    _api_credential_replacement_watchdog_task = create_tracked_task(
        _api_credential_replacement_watchdog_loop(),
        name="api-credential-replacement-watchdog",
    )


async def stop_api_credential_replacement_watchdog() -> None:
    global _api_credential_replacement_watchdog_task
    task = _api_credential_replacement_watchdog_task
    _api_credential_replacement_watchdog_task = None
    if not task or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def delete_api_credential_keys(
    db: AsyncSession,
    user_id: int,
    provider: str,
    keys: tuple[str, ...] | list[str],
    *,
    institution_id: int | None = None,
) -> bool:
    normalized_keys = tuple(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
    if not normalized_keys:
        return False
    credentials = await get_api_credentials(
        db,
        user_id,
        provider,
        institution_id=institution_id,
    )
    if not credentials:
        return False
    changed = False
    for key in normalized_keys:
        if key in credentials:
            credentials.pop(key, None)
            changed = True
    if not changed:
        return False
    if not credentials:
        connection_id = await _resolve_api_credentials_connection_id(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if connection_id is not None:
            await db.execute(
                delete(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                )
            )
        await db.flush()
        return True
    return await upsert_api_credentials(
        db,
        user_id,
        provider,
        credentials,
        institution_id=institution_id,
        replace=True,
    )


async def delete_api_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> None:
    normalized_provider = normalize_provider(provider)
    if not normalized_provider:
        return
    if institution_id is None:
        await delete_provider_connection_artifacts(
            db,
            user_id,
            normalized_provider,
            (API_CREDENTIALS_ARTIFACT_KIND,),
            slots=(ACTIVE_SLOT,),
        )
    else:
        connection_id = await _resolve_api_credentials_connection_id(
            db,
            user_id,
            normalized_provider,
            institution_id=institution_id,
        )
        if connection_id is not None:
            await db.execute(
                delete(ConnectionAuthArtifact).where(
                    ConnectionAuthArtifact.user_id == user_id,
                    ConnectionAuthArtifact.institution_id == connection_id,
                    ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                )
            )
    await db.flush()


async def delete_scraper_credentials(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> None:
    await delete_provider_connection_artifacts(
        db,
        user_id,
        provider,
        (SCRAPER_CREDENTIALS_ARTIFACT_KIND,),
        slots=(ACTIVE_SLOT,),
        institution_id=institution_id,
    )
    await db.flush()
