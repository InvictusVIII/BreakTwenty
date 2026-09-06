from __future__ import annotations

from typing import Any

from app.provider_catalog import get_provider_credential_storage_provider
from app.services.connection_auth_storage import (
    ACTIVE_SLOT,
    SCRAPER_CREDENTIALS_ARTIFACT_KIND,
    get_scraper_credentials,
    is_scraper_username_placeholder,
    load_connection_artifact_async,
    save_connection_artifact_async,
    upsert_connection_artifact,
    upsert_scraper_credentials,
)
from app.services.visible_auth_attempt_storage import (
    delete_visible_auth_attempt_artifacts,
    load_visible_auth_attempt_artifact as load_visible_auth_attempt_artifact_in_session,
    load_visible_auth_attempt_artifact_async,
    save_visible_auth_attempt_artifact_async,
)


async def load_visible_auth_attempt_artifact(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
) -> Any | None:
    return await load_visible_auth_attempt_artifact_async(
        user_id,
        provider,
        attempt_id,
        artifact_kind,
    )


async def save_visible_auth_attempt_artifact(
    user_id: int,
    provider: str,
    attempt_id: str,
    artifact_kind: str,
    payload: Any,
) -> None:
    saved = await save_visible_auth_attempt_artifact_async(
        user_id,
        provider,
        attempt_id,
        artifact_kind,
        payload,
    )
    if not saved:
        raise RuntimeError("Failed to save visible auth attempt artifact")


async def load_provider_runtime_artifact(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> Any | None:
    return await load_connection_artifact_async(
        user_id,
        provider,
        artifact_kind,
        slot=slot,
        institution_id=institution_id,
    )


async def save_provider_runtime_artifact(
    user_id: int,
    provider: str,
    artifact_kind: str,
    payload: Any,
    *,
    slot: str = ACTIVE_SLOT,
    institution_id: int | None = None,
) -> None:
    saved = await save_connection_artifact_async(
        user_id,
        provider,
        artifact_kind,
        payload,
        slot=slot,
        institution_id=institution_id,
    )
    if not saved:
        raise RuntimeError("Failed to save provider runtime artifact")


async def promote_visible_auth_attempt_state(
    db,
    *,
    user_id: int,
    provider: str,
    attempt_id: str,
    institution_id: int,
    artifact_kinds: tuple[str, ...] | list[str],
) -> tuple[bool, bool]:
    """Atomically promote staged runtime artifacts and credentials.

    The caller owns both the transaction and SQLite write gate. Staging rows are
    deleted only after every canonical upsert has succeeded, so rollback leaves a
    complete retryable attempt instead of a mixed canonical state.
    """
    try:
        storage_provider = get_provider_credential_storage_provider(provider)
    except KeyError:
        storage_provider = provider
    staged_artifacts: dict[str, Any] = {}
    for artifact_kind in artifact_kinds:
        payload = await load_visible_auth_attempt_artifact_in_session(
            db,
            user_id,
            provider,
            attempt_id,
            artifact_kind,
        )
        if payload is not None:
            staged_artifacts[str(artifact_kind)] = payload

    credentials_payload = await load_visible_auth_attempt_artifact_in_session(
        db,
        user_id,
        provider,
        attempt_id,
        SCRAPER_CREDENTIALS_ARTIFACT_KIND,
    )
    username = ""
    password = ""
    if isinstance(credentials_payload, dict):
        username = str(credentials_payload.get("username") or "")
        password = str(credentials_payload.get("password") or "")
    if is_scraper_username_placeholder(username):
        username = ""
        password = ""

    for artifact_kind, payload in staged_artifacts.items():
        saved = await upsert_connection_artifact(
            db,
            user_id,
            storage_provider,
            artifact_kind,
            payload,
            institution_id=institution_id,
        )
        if not saved:
            raise RuntimeError("Failed to promote provider runtime artifact")

    credentials_saved = False
    if username and password:
        existing = await get_scraper_credentials(
            db,
            user_id,
            storage_provider,
            institution_id=institution_id,
        )
        changed = existing is None or existing.username != username or existing.password != password
        if changed:
            from app.services.institution_cleanup import invalidate_provider_auth_state

            await invalidate_provider_auth_state(
                db,
                user_id,
                storage_provider,
                preserve_pending_session=True,
                institution_id=institution_id,
            )
        credentials_saved = await upsert_scraper_credentials(
            db,
            user_id,
            storage_provider,
            username,
            password,
            institution_id=institution_id,
        )
        if not credentials_saved:
            raise RuntimeError("Failed to promote visible auth credentials")

    await delete_visible_auth_attempt_artifacts(
        db,
        user_id,
        provider,
        attempt_id,
    )
    return bool(staged_artifacts), credentials_saved
