import asyncio
import importlib
import json
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.launch_auth import require_runner_binding
from app.connectors.connector_logging import (
    get_connector_logger,
    log_connector_event,
    safe_connector_public_message,
)
from app.connectors.types import SyncStatus
from app.database import async_session
from app.models import Account, Institution
from app.runtime_paths import runtime_display_path
from app.provider_catalog import (
    get_api_session_auth_metadata,
    get_desktop_visible_auth_metadata,
    get_provider_credential_storage_provider,
    get_provider_display_name,
    get_runtime_state_metadata,
    get_route_metadata,
    provider_uses_background_transaction_import,
    provider_uses_daily_transaction_import_dedupe,
    iter_api_session_otp_auth_providers,
    iter_standard_scraper_auth_providers,
    iter_sync_route_providers,
    resolve_sync_route_provider,
)
from app.services.transaction_import_jobs import enqueue_provider_transaction_import
from app.services.runtime_state import (
    get_provider_visible_auth_attempt_dir,
    get_provider_visible_auth_artifact_path,
    normalize_visible_auth_attempt_id,
    pop_runtime_state,
    set_runtime_state,
)
from app.services.institution_cleanup import (
    delete_institution_and_accounts,
    delete_provider_state,
)
from app.services.connection_auth_storage import (
    ACTIVE_SLOT,
    ProviderAlreadyConnectedError,
    QUARANTINE_SLOT,
    SCRAPER_CREDENTIALS_ARTIFACT_KIND,
    ensure_connection,
    get_scraper_credentials,
)
from app.services.network_preflight import run_sync_network_gate
from app.services.sync_utils import (
    acquire_sync_lock,
    is_network_error,
    release_sync_lock,
    set_institution_sync_status,
)
from app.services.auth_artifact_utils import normalize_provider as _normalize_visible_auth_provider
from app.services.sync_tracking import (
    ensure_provider_sync_attempt,
    finalize_provider_sync_attempt,
    get_provider_sync_id,
    start_provider_sync_attempt,
)
from app.services.sync_activity import (
    clear_provider_activity,
    get_sync_activity_payload,
    set_provider_activity,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.visible_auth_persistence import (
    load_provider_runtime_artifact,
    promote_visible_auth_attempt_state as promote_staged_visible_auth_state,
    save_provider_runtime_artifact,
    save_visible_auth_attempt_artifact,
)
from app.services.visible_auth_attempt_storage import delete_visible_auth_attempt_artifacts_async

router = APIRouter()

VISIBLE_AUTH_ATTEMPT_NAMESPACE_PREFIX = "visible_auth_attempt:"
MAX_VISIBLE_AUTH_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_VISIBLE_AUTH_RUNTIME_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_VISIBLE_AUTH_CREDENTIAL_LENGTH = 16_384
MAX_SYNC_BATCH_CONNECTIONS = 64
SYNC_BATCH_API_MODES = frozenset({"auto", "manual"})
TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS = 10.0
SYNC_INITIATION_SOURCES = frozenset(
    {
        "autosync",
        "sync_all",
        "individual_sync",
        "add_connection",
        "manual_reauthentication",
        "scheduled_sync",
    }
)


def _with_sync_id(response: dict, sync_id: str | None = None) -> dict:
    payload = dict(response or {})
    if sync_id:
        payload["sync_id"] = sync_id
    return payload


def _provider_sync_scope(provider: str) -> str:
    return "accounts" if provider_uses_background_transaction_import(provider) else "full"


async def _enqueue_transaction_import_after_sync(
    user_id: int,
    provider: str,
    response: dict,
    *,
    add_flow: bool,
    reason: str,
    institution_id: int | None,
    attempt_id: str | None = None,
) -> dict:
    if bool(response.get("transaction_import_deferred")):
        return response
    status = str(response.get("status") or "")
    can_enqueue = status == "ok" or (
        status == "skipped" and provider_uses_daily_transaction_import_dedupe(provider)
    )
    if add_flow or not can_enqueue:
        return response
    try:
        async with asyncio.timeout(TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS):
            job = await enqueue_provider_transaction_import(
                user_id,
                provider,
                source_sync_id=str(response.get("sync_id") or "") or None,
                attempt_id=attempt_id or None,
                reason=reason,
                institution_id=institution_id,
            )
    except TimeoutError:
        log_connector_event(
            get_connector_logger(provider),
            provider=provider,
            stage="transaction import enqueue timeout",
            level="warning",
            user_id=user_id,
            sync_id=str(response.get("sync_id") or "") or None,
            institution_id=institution_id,
            timeout_seconds=TRANSACTION_IMPORT_ENQUEUE_TIMEOUT_SECONDS,
        )
        return {
            **response,
            "transaction_import_enqueue_error": "Transaction import enqueue timed out.",
        }
    if not job:
        return response
    return {**response, "transaction_import_job": job}


def _already_syncing_response(
    user_id: int,
    provider: str,
    *,
    fallback_provider: str | None = None,
    institution_id: int | None = None,
) -> dict:
    sync_id = get_provider_sync_id(user_id, provider, institution_id=institution_id)
    if not sync_id and fallback_provider:
        sync_id = get_provider_sync_id(
            user_id,
            fallback_provider,
            institution_id=institution_id,
        )
    try:
        display_name = get_provider_display_name(provider)
    except KeyError:
        display_name = provider
    message = f"{display_name} sync is already in progress."
    response = _with_sync_id(
        {
            "status": "already_syncing",
            "message": message,
        },
        sync_id,
    )
    try:
        from app.services.support_auto_archive import archive_run_fire_and_forget

        archive_run_fire_and_forget(
            user_id=user_id,
            provider=provider,
            sync_id=sync_id,
            trigger="sync_result_already_syncing",
            error=message,
            extra_fields={
                "result_status": "already_syncing",
                "institution_id": institution_id,
                "fallback_provider": fallback_provider,
            },
        )
    except Exception:
        pass
    return response


def _normalize_visible_auth_artifact_type(artifact_type: str | None) -> str:
    return str(artifact_type or "").strip().lower()


def _strict_visible_auth_attempt_id(value: object) -> str:
    raw = str(value or "").strip()
    normalized = normalize_visible_auth_attempt_id(raw)
    if not raw or normalized != raw or len(raw) > 128:
        raise HTTPException(status_code=422, detail="Invalid visible auth attempt ID")
    return normalized


def _reject_unknown_body_keys(body: dict, allowed: frozenset[str]) -> None:
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unsupported request fields: {', '.join(unknown)}")


def _visible_auth_artifact_response_path(artifact_path: str) -> str:
    return runtime_display_path(artifact_path)


def _visible_auth_attempt_namespace(provider: str) -> str:
    return f"{VISIBLE_AUTH_ATTEMPT_NAMESPACE_PREFIX}{provider}"


def _payload_institution_id(body: dict | None) -> int | None:
    if not isinstance(body, dict):
        return None
    value = body.get("institution_id")
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise HTTPException(status_code=422, detail="institution_id must be a positive integer")
    return value


def _validate_sync_batch_payload(body: object) -> tuple[list[dict[str, object]], str]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Sync batch body must be an object")
    _reject_unknown_body_keys(body, frozenset({"connections", "mode"}))
    if "connections" not in body or "mode" not in body:
        raise HTTPException(status_code=422, detail="Sync batch requires connections and mode")

    mode = body["mode"]
    if type(mode) is not str or mode not in SYNC_BATCH_API_MODES:
        raise HTTPException(status_code=422, detail="Unsupported sync batch mode")

    raw_connections = body["connections"]
    if not isinstance(raw_connections, list):
        raise HTTPException(status_code=422, detail="connections must be an array")
    if not raw_connections:
        raise HTTPException(status_code=422, detail="At least one sync connection is required")
    if len(raw_connections) > MAX_SYNC_BATCH_CONNECTIONS:
        raise HTTPException(
            status_code=413,
            detail=f"A maximum of {MAX_SYNC_BATCH_CONNECTIONS} connections can be synced at once",
        )

    connections: list[dict[str, object]] = []
    seen_institution_ids: set[int] = set()
    seen_providers: set[str] = set()
    for index, connection in enumerate(raw_connections):
        if not isinstance(connection, dict):
            raise HTTPException(status_code=422, detail=f"connections[{index}] must be an object")
        unknown = sorted(str(key) for key in connection if key not in {"provider", "institution_id"})
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported connections[{index}] fields: {', '.join(unknown)}",
            )
        if set(connection) != {"provider", "institution_id"}:
            raise HTTPException(
                status_code=422,
                detail=f"connections[{index}] requires provider and institution_id",
            )

        provider = connection["provider"]
        if (
            type(provider) is not str
            or not provider
            or len(provider) > 64
            or provider != provider.strip().lower()
        ):
            raise HTTPException(status_code=422, detail=f"connections[{index}] has an invalid provider")
        try:
            route_provider = resolve_sync_route_provider(provider, mode)
        except KeyError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"connections[{index}] has an invalid provider",
            ) from exc
        if route_provider is None:
            raise HTTPException(
                status_code=422,
                detail=f"connections[{index}] has an invalid provider",
            )

        institution_id = connection["institution_id"]
        if type(institution_id) is not int or not 0 < institution_id <= 2_147_483_647:
            raise HTTPException(
                status_code=422,
                detail=f"connections[{index}] has an invalid institution_id",
            )
        if institution_id in seen_institution_ids or provider in seen_providers:
            raise HTTPException(status_code=422, detail="Sync batch connections must be unique")
        seen_institution_ids.add(institution_id)
        seen_providers.add(provider)
        connections.append({"provider": provider, "institution_id": institution_id})

    return connections, mode


def _set_visible_auth_attempt_scope(user_id: int, provider: str, attempt_id: str | None) -> bool:
    normalized_attempt_id = normalize_visible_auth_attempt_id(attempt_id)
    if not normalized_attempt_id:
        return False
    set_runtime_state(
        _visible_auth_attempt_namespace(provider),
        user_id,
        {"attempt_id": normalized_attempt_id},
    )
    return True


def _clear_visible_auth_attempt_scope(user_id: int, provider: str) -> None:
    pop_runtime_state(_visible_auth_attempt_namespace(provider), user_id, None)


async def _delete_visible_auth_attempt_dir_async(user_id: int, provider: str, attempt_id: str | None) -> None:
    normalized_attempt_id = normalize_visible_auth_attempt_id(attempt_id)
    if not normalized_attempt_id:
        return
    await delete_visible_auth_attempt_artifacts_async(user_id, provider, normalized_attempt_id)
    shutil.rmtree(
        get_provider_visible_auth_attempt_dir(user_id, provider, normalized_attempt_id),
        ignore_errors=True,
    )


def _desktop_visible_auth_flow_enabled(
    visible_auth_metadata: dict[str, Any] | None,
    *,
    add_flow: bool,
) -> bool:
    metadata = visible_auth_metadata if isinstance(visible_auth_metadata, dict) else {}
    if not metadata.get("enabled"):
        return False
    flow_key = "addFlow" if add_flow else "manualFlow"
    return bool(metadata.get(flow_key))


async def _run_connector_sync(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    sync_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    institution_id: int,
    sync_source: str | None = None,
    success_precommit_hook: Callable[
        [AsyncSession, int | None],
        Awaitable[dict[str, object] | None],
    ]
    | None = None,
    attempt_id: str | None = None,
):
    from app.connectors.orchestration import run_connector_sync

    _, response = await run_connector_sync(
        db,
        user_id,
        provider,
        sync_id=sync_id,
        add_flow=add_flow,
        sync_scope=sync_scope,
        institution_id=institution_id,
        sync_source=sync_source,
        success_precommit_hook=success_precommit_hook,
        attempt_id=attempt_id,
    )
    return response


async def _run_scraper_connector_login(
    db: AsyncSession,
    user_id: int,
    provider: str,
    username: str,
    password: str,
    *,
    add_flow: bool = False,
    sync_id: str | None = None,
    sync_scope: str = "full",
    institution_id: int,
):
    from app.connectors.orchestration import run_scraper_connector_login

    _, response = await run_scraper_connector_login(
        db,
        user_id,
        provider,
        username,
        password,
        add_flow=add_flow,
        sync_id=sync_id,
        sync_scope=sync_scope,
        institution_id=institution_id,
    )
    return response


async def _run_scraper_connector_2fa(
    db: AsyncSession,
    user_id: int,
    provider: str,
    code: str | None = None,
    *,
    sync_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    institution_id: int,
):
    from app.connectors.orchestration import run_scraper_connector_complete_2fa

    _, response = await run_scraper_connector_complete_2fa(
        db,
        user_id,
        provider,
        code,
        sync_id=sync_id,
        add_flow=add_flow,
        sync_scope=sync_scope,
        institution_id=institution_id,
    )
    return response


def _payload_bool(body: dict | None, key: str, default: bool = False) -> bool:
    if not body or key not in body:
        return default
    value = body.get(key)
    if type(value) is bool:
        return value
    raise HTTPException(status_code=422, detail=f"{key} must be a boolean")


def _sync_initiation_source(
    body: dict | None,
    *,
    add_flow: bool,
    attempt_id: str | None = None,
) -> str:
    payload = body if isinstance(body, dict) else {}
    explicit = str(payload.get("sync_source") or "").strip().lower()
    aliases = {
        "auto": "autosync",
        "manual": "individual_sync",
        "user_initiated": "individual_sync",
        "reauth": "manual_reauthentication",
    }
    explicit = aliases.get(explicit, explicit)
    if explicit in SYNC_INITIATION_SOURCES:
        return explicit
    if add_flow:
        return "add_connection"
    if attempt_id:
        return "manual_reauthentication"
    return "individual_sync"


def _add_flow_response_requires_cleanup(response: dict | None) -> bool:
    status = str((response or {}).get("status") or "").strip().lower()
    return status not in {"ok", "2fa_required", "waiting", "already_syncing"}


async def _cleanup_incomplete_institution_add(
    db: AsyncSession,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
) -> None:
    try:
        provider = get_provider_credential_storage_provider(provider)
    except KeyError:
        pass
    try:
        await db.rollback()
    except Exception:
        pass

    async with async_session() as cleanup_db, sqlite_write_gate(), cleanup_db.begin():
        result = await cleanup_db.execute(
            select(Institution).where(
                Institution.provider == provider,
                Institution.user_id == user_id,
                *([Institution.id == int(institution_id)] if institution_id is not None else []),
            )
        )
        institution = result.scalars().first()
        if institution is not None:
            accounts_result = await cleanup_db.execute(
                select(Account.id).where(
                    Account.institution_id == institution.id,
                    Account.user_id == user_id,
                ).limit(1)
            )
            has_accounts = accounts_result.scalar_one_or_none() is not None
            pending_add = not bool(institution.enabled) and bool(institution.hidden)
            if pending_add or not has_accounts:
                await delete_institution_and_accounts(
                    cleanup_db,
                    user_id,
                    institution,
                    purge_diagnostics=False,
                )
        else:
            await delete_provider_state(cleanup_db, user_id, provider)


async def _ensure_request_connection(
    db: AsyncSession,
    user_id: int,
    provider: str,
    payload: dict,
) -> int:
    institution_id = _payload_institution_id(payload)
    add_flow = _payload_bool(payload, "add_flow")
    try:
        storage_provider = get_provider_credential_storage_provider(provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown provider") from exc
    if not add_flow:
        filters = (
            Institution.user_id == user_id,
            Institution.provider == storage_provider,
            Institution.enabled.is_(True),
        )
        if institution_id is not None:
            filters = (*filters, Institution.id == institution_id)
        institution_id = (
            await db.execute(select(Institution.id).where(*filters))
        ).scalar_one_or_none()
        if institution_id is None:
            raise HTTPException(
                status_code=404,
                detail="Enabled institution connection not found for this provider",
            )
        institution_id = int(institution_id)
        payload["institution_id"] = institution_id
        return institution_id
    async with sqlite_write_gate():
        try:
            institution_id = await ensure_connection(
                db,
                user_id,
                storage_provider,
                pending_add=True,
                institution_id=institution_id,
            )
        except ProviderAlreadyConnectedError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await db.commit()
    if institution_id is None:
        raise HTTPException(status_code=409, detail="Institution connection could not be created")
    payload["institution_id"] = int(institution_id)
    return int(institution_id)


async def _promote_visible_auth_attempt_state(
    db: AsyncSession,
    *,
    user_id: int,
    provider: str,
    attempt_id: str,
    institution_id: int,
) -> tuple[bool, bool]:
    runtime_metadata = get_runtime_state_metadata(provider)
    return await promote_staged_visible_auth_state(
        db,
        user_id=user_id,
        provider=provider,
        attempt_id=attempt_id,
        institution_id=institution_id,
        artifact_kinds=tuple(runtime_metadata.get("artifactKinds") or ()),
    )


async def _sync_connector_route(
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    body: dict | None = None,
    lock_provider: str | None = None,
    status_provider: str | None = None,
    error_status: str = "error",
):
    lock_key = lock_provider or provider
    status_key = status_provider or provider
    payload = body or {}
    sync_id = str(payload.get("sync_id") or "").strip()
    add_flow = _payload_bool(payload, "add_flow")
    attempt_id = normalize_visible_auth_attempt_id(payload.get("attempt_id"))
    sync_source = _sync_initiation_source(
        payload,
        add_flow=add_flow,
        attempt_id=attempt_id,
    )
    visible_auth_scoped = False
    activity_id = None
    retain_visible_auth_attempt = False

    requested_institution_id = _payload_institution_id(payload)
    institution_id: int | None = None
    if not add_flow:
        # Resolve and authorize an existing connection before acquiring a lease or
        # running any preflight/provider side effects.
        institution_id = await _ensure_request_connection(db, user_id, provider, payload)
    lock_institution_id = institution_id if institution_id is not None else requested_institution_id
    if not await acquire_sync_lock(user_id, lock_key, institution_id=lock_institution_id):
        return _already_syncing_response(
            user_id,
            provider,
            fallback_provider=lock_key,
            institution_id=lock_institution_id,
        )
    try:
        if add_flow:
            institution_id = await _ensure_request_connection(db, user_id, provider, payload)

        if not add_flow and not attempt_id:
            network_gate = await run_sync_network_gate(
                [provider],
                user_id=user_id,
                flow="single_sync",
                mode=sync_source or "manual",
            )
            if network_gate.status == "blocked":
                return network_gate.route_response()

        if attempt_id:
            try:
                visible_auth_metadata = get_desktop_visible_auth_metadata(provider)
            except KeyError:
                visible_auth_metadata = {}
            if _desktop_visible_auth_flow_enabled(visible_auth_metadata, add_flow=add_flow):
                visible_auth_scoped = _set_visible_auth_attempt_scope(user_id, provider, attempt_id)

        attempt = (
            ensure_provider_sync_attempt(
                user_id,
                provider,
                sync_id=sync_id,
                institution_id=institution_id,
            )
            if sync_id
            else start_provider_sync_attempt(user_id, provider, institution_id=institution_id)
        )
        sync_id = attempt["sync_id"]
        activity_id = set_provider_activity(
            user_id,
            status_key,
            institution_id=institution_id,
        )
        if visible_auth_scoped and attempt_id:
            log_connector_event(
                get_connector_logger(provider),
                provider=provider,
                stage="desktop visible auth handoff",
                user_id=user_id,
                sync_id=sync_id,
                source="desktop_visible_auth",
                flow="add" if add_flow else "existing",
                attempt_id=attempt_id,
            )

        try:
            sync_scope = _provider_sync_scope(provider)
            success_precommit_hook = None
            if visible_auth_scoped and attempt_id:
                async def promote_visible_auth_state(
                    transaction_db: AsyncSession,
                    promoted_institution_id: int | None,
                ) -> dict[str, object]:
                    nonlocal retain_visible_auth_attempt
                    retain_visible_auth_attempt = True
                    if promoted_institution_id is None or promoted_institution_id <= 0:
                        raise RuntimeError(
                            "Successful visible-auth flow has no institution connection ID."
                        )
                    (
                        artifacts_promoted,
                        credentials_saved,
                    ) = await _promote_visible_auth_attempt_state(
                        transaction_db,
                        user_id=user_id,
                        provider=provider,
                        attempt_id=attempt_id,
                        institution_id=promoted_institution_id,
                    )
                    return {
                        "artifacts_promoted": artifacts_promoted,
                        "credentials_saved": credentials_saved,
                    }

                success_precommit_hook = promote_visible_auth_state
            response = await _run_connector_sync(
                db,
                user_id,
                provider,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
                sync_source=sync_source,
                success_precommit_hook=success_precommit_hook,
                attempt_id=attempt_id if visible_auth_scoped else None,
            )
            retain_visible_auth_attempt = False
            if add_flow and _add_flow_response_requires_cleanup(response):
                await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
            if visible_auth_scoped and attempt_id and response.get("status") == "ok":
                promoted_institution_id = int(response.get("institution_id") or institution_id or 0)
                if promoted_institution_id <= 0:
                    raise RuntimeError("Successful visible-auth flow has no institution connection ID.")
                institution_id = promoted_institution_id
            response = await _enqueue_transaction_import_after_sync(
                user_id,
                provider,
                response,
                add_flow=add_flow,
                reason=sync_source,
                institution_id=institution_id,
                attempt_id=attempt_id or None,
            )
            if attempt_id:
                response = {**response, "attempt_id": attempt_id}
            return response
        except Exception as e:
            finalize_provider_sync_attempt(
                user_id,
                provider,
                sync_id=sync_id,
                status="error",
                institution_id=institution_id,
            )
            if add_flow and not retain_visible_auth_attempt:
                await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
            await set_institution_sync_status(
                db,
                user_id,
                status_key,
                "network_error" if is_network_error(e) else error_status,
                institution_id=institution_id,
            )
            if is_network_error(e):
                return _with_sync_id({"status": "network_error", "message": "Connection failed"}, sync_id)
            return _with_sync_id(
                {"status": "error", "message": safe_connector_public_message(e, fallback="Sync failed")},
                sync_id,
            )
    finally:
        try:
            clear_provider_activity(activity_id)
            if visible_auth_scoped:
                _clear_visible_auth_attempt_scope(user_id, provider)
            if attempt_id and not retain_visible_auth_attempt:
                await _delete_visible_auth_attempt_dir_async(user_id, provider, attempt_id)
        finally:
            await release_sync_lock(user_id, lock_key, institution_id=lock_institution_id)


async def _scraper_login_route(
    body: dict,
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    missing_credentials_message: str,
    error_status: str = "error",
):
    username = body.get("username", "")
    password = body.get("password", "")
    add_flow = _payload_bool(body, "add_flow")
    sync_source = _sync_initiation_source(body, add_flow=add_flow)
    requested_institution_id = _payload_institution_id(body)
    institution_id: int | None = None
    if not add_flow:
        institution_id = await _ensure_request_connection(db, user_id, provider, body)
    if not username and not password and not add_flow:
        stored = await get_scraper_credentials(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if stored is not None:
            username, password = stored.username, stored.password
    if not username or not password:
        return {"status": "error", "message": missing_credentials_message}
    lock_institution_id = institution_id if institution_id is not None else requested_institution_id
    if not await acquire_sync_lock(user_id, provider, institution_id=lock_institution_id):
        return _already_syncing_response(user_id, provider, institution_id=lock_institution_id)
    if add_flow:
        try:
            institution_id = await _ensure_request_connection(db, user_id, provider, body)
        except BaseException:
            await release_sync_lock(user_id, provider, institution_id=lock_institution_id)
            raise
    activity_id = None

    try:
        sync_id = start_provider_sync_attempt(
            user_id,
            provider,
            institution_id=institution_id,
        )["sync_id"]
        activity_id = set_provider_activity(
            user_id,
            provider,
            institution_id=institution_id,
        )
        sync_scope = _provider_sync_scope(provider)
        response = await _run_scraper_connector_login(
            db,
            user_id,
            provider,
            username,
            password,
            add_flow=add_flow,
            sync_id=sync_id,
            sync_scope=sync_scope,
            institution_id=institution_id,
        )
        if add_flow and _add_flow_response_requires_cleanup(response):
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        if institution_id is not None:
            response.setdefault("institution_id", institution_id)
        response = await _enqueue_transaction_import_after_sync(
            user_id,
            provider,
            response,
            add_flow=add_flow,
            reason=sync_source,
            institution_id=institution_id,
        )
        return response
    except Exception as e:
        sync_id = get_provider_sync_id(user_id, provider, institution_id=institution_id)
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        if add_flow:
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            "network_error" if is_network_error(e) else error_status,
            institution_id=institution_id,
        )
        if is_network_error(e):
            return _with_sync_id({"status": "network_error", "message": "Connection failed"}, sync_id)
        return _with_sync_id(
            {"status": "error", "message": safe_connector_public_message(e, fallback="Sync failed")},
            sync_id,
        )
    finally:
        try:
            clear_provider_activity(activity_id)
        finally:
            await release_sync_lock(user_id, provider, institution_id=lock_institution_id)


async def _scraper_2fa_route(
    body: dict,
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    error_status: str = "error",
):
    add_flow = _payload_bool(body, "add_flow")
    sync_source = _sync_initiation_source(body, add_flow=add_flow)
    requested_institution_id = _payload_institution_id(body)
    institution_id: int | None = None
    if not add_flow:
        institution_id = await _ensure_request_connection(db, user_id, provider, body)
    lock_institution_id = institution_id if institution_id is not None else requested_institution_id
    if not await acquire_sync_lock(user_id, provider, institution_id=lock_institution_id):
        return _already_syncing_response(user_id, provider, institution_id=lock_institution_id)
    sync_id = ""
    activity_id = None

    try:
        if add_flow:
            institution_id = await _ensure_request_connection(db, user_id, provider, body)
        sync_id = ensure_provider_sync_attempt(
            user_id,
            provider,
            institution_id=institution_id,
        )["sync_id"]
        activity_id = set_provider_activity(
            user_id,
            provider,
            institution_id=institution_id,
        )
        sync_scope = _provider_sync_scope(provider)
        response = await _run_scraper_connector_2fa(
            db,
            user_id,
            provider,
            body.get("code"),
            sync_id=sync_id,
            add_flow=add_flow,
            sync_scope=sync_scope,
            institution_id=institution_id,
        )
        if add_flow and _add_flow_response_requires_cleanup(response):
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        if institution_id is not None:
            response.setdefault("institution_id", institution_id)
        response = await _enqueue_transaction_import_after_sync(
            user_id,
            provider,
            response,
            add_flow=add_flow,
            reason=sync_source,
            institution_id=institution_id,
        )
        return response
    except Exception as e:
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        if add_flow:
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            "network_error" if is_network_error(e) else error_status,
            institution_id=institution_id,
        )
        if is_network_error(e):
            return _with_sync_id({"status": "network_error", "message": "Connection failed"}, sync_id)
        return _with_sync_id(
            {"status": "error", "message": safe_connector_public_message(e, fallback="Sync failed")},
            sync_id,
        )
    finally:
        try:
            clear_provider_activity(activity_id)
        finally:
            await release_sync_lock(user_id, provider, institution_id=lock_institution_id)


def _get_api_session_otp_handler(provider: str):
    metadata = get_api_session_auth_metadata(provider)
    module = importlib.import_module(str(metadata["modulePath"]))
    cls = getattr(module, str(metadata["className"]))
    return cls()


def _auth_result_status_for_institution(result_status: SyncStatus, error_status: str) -> str:
    if result_status in (SyncStatus.AUTH_REQUIRED, SyncStatus.TWO_FA_REQUIRED):
        return "auth_required"
    if result_status == SyncStatus.NETWORK_ERROR:
        return "network_error"
    return error_status


async def _api_session_otp_login_route(
    body: dict,
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    missing_credentials_message: str,
    error_status: str = "error",
):
    username = body.get("username", "")
    password = body.get("password", "")
    add_flow = _payload_bool(body, "add_flow")
    sync_source = _sync_initiation_source(body, add_flow=add_flow)
    requested_institution_id = _payload_institution_id(body)
    institution_id: int | None = None
    if not add_flow:
        institution_id = await _ensure_request_connection(db, user_id, provider, body)
    if not username and not password and not add_flow:
        stored = await get_scraper_credentials(
            db,
            user_id,
            provider,
            institution_id=institution_id,
        )
        if stored is not None:
            username, password = stored.username, stored.password
    if not username or not password:
        return {"status": "error", "message": missing_credentials_message}
    lock_institution_id = institution_id if institution_id is not None else requested_institution_id
    if not await acquire_sync_lock(user_id, provider, institution_id=lock_institution_id):
        return _already_syncing_response(user_id, provider, institution_id=lock_institution_id)

    sync_id = ""
    logger = get_connector_logger(provider)
    activity_id = None

    try:
        if add_flow:
            institution_id = await _ensure_request_connection(db, user_id, provider, body)
        sync_id = start_provider_sync_attempt(
            user_id,
            provider,
            institution_id=institution_id,
        )["sync_id"]
        activity_id = set_provider_activity(
            user_id,
            provider,
            institution_id=institution_id,
        )
        log_connector_event(logger, provider=provider, stage="login start", user_id=user_id, sync_id=sync_id)
        result = await _get_api_session_otp_handler(provider).login(user_id, username, password)
        log_connector_event(
            logger,
            provider=provider,
            stage="login result",
            user_id=user_id,
            sync_id=sync_id,
            status=result.status.value,
            challenge_method=result.challenge_method,
            message=result.message,
        )
        if result.status == SyncStatus.OK:
            sync_scope = _provider_sync_scope(provider)
            response = await _run_connector_sync(
                db,
                user_id,
                provider,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
                sync_source=sync_source,
            )
            if add_flow and _add_flow_response_requires_cleanup(response):
                await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
            if institution_id is not None:
                response.setdefault("institution_id", institution_id)
            response = await _enqueue_transaction_import_after_sync(
                user_id,
                provider,
                response,
                add_flow=add_flow,
                reason=sync_source,
                institution_id=institution_id,
            )
            return response

        response = _with_sync_id(result.to_route_response(), sync_id)
        if add_flow and _add_flow_response_requires_cleanup(response):
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        if institution_id is not None:
            response.setdefault("institution_id", institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            _auth_result_status_for_institution(result.status, error_status),
            institution_id=institution_id,
        )
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status=response.get("status"),
            institution_id=institution_id,
        )
        return response
    except Exception as e:
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        if add_flow:
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            "network_error" if is_network_error(e) else error_status,
            institution_id=institution_id,
        )
        if is_network_error(e):
            return _with_sync_id({"status": "network_error", "message": "Connection failed"}, sync_id)
        return _with_sync_id(
            {"status": "error", "message": safe_connector_public_message(e, fallback="Sync failed")},
            sync_id,
        )
    finally:
        try:
            clear_provider_activity(activity_id)
        finally:
            await release_sync_lock(user_id, provider, institution_id=lock_institution_id)


async def _api_session_otp_2fa_route(
    body: dict,
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    error_status: str = "error",
):
    logger = get_connector_logger(provider)
    add_flow = _payload_bool(body, "add_flow")
    sync_source = _sync_initiation_source(body, add_flow=add_flow)
    requested_institution_id = _payload_institution_id(body)
    institution_id: int | None = None
    if not add_flow:
        institution_id = await _ensure_request_connection(db, user_id, provider, body)
    lock_institution_id = institution_id if institution_id is not None else requested_institution_id
    if not await acquire_sync_lock(user_id, provider, institution_id=lock_institution_id):
        return _already_syncing_response(user_id, provider, institution_id=lock_institution_id)
    sync_id = ""
    activity_id = None

    try:
        if add_flow:
            institution_id = await _ensure_request_connection(db, user_id, provider, body)
        sync_id = ensure_provider_sync_attempt(
            user_id,
            provider,
            institution_id=institution_id,
        )["sync_id"]
        activity_id = set_provider_activity(
            user_id,
            provider,
            institution_id=institution_id,
        )
        log_connector_event(logger, provider=provider, stage="2fa start", user_id=user_id, sync_id=sync_id)
        result = await _get_api_session_otp_handler(provider).complete_2fa(user_id, body.get("code"))
        log_connector_event(
            logger,
            provider=provider,
            stage="2fa result",
            user_id=user_id,
            sync_id=sync_id,
            status=result.status.value,
            message=result.message,
        )
        if result.status == SyncStatus.OK:
            sync_scope = _provider_sync_scope(provider)
            response = await _run_connector_sync(
                db,
                user_id,
                provider,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
                sync_source=sync_source,
            )
            if add_flow and _add_flow_response_requires_cleanup(response):
                await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
            if institution_id is not None:
                response.setdefault("institution_id", institution_id)
            response = await _enqueue_transaction_import_after_sync(
                user_id,
                provider,
                response,
                add_flow=add_flow,
                reason=sync_source,
                institution_id=institution_id,
            )
            return response

        response = _with_sync_id(result.to_route_response(), sync_id)
        if add_flow and _add_flow_response_requires_cleanup(response):
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        if institution_id is not None:
            response.setdefault("institution_id", institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            _auth_result_status_for_institution(result.status, error_status),
            institution_id=institution_id,
        )
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status=response.get("status"),
            institution_id=institution_id,
        )
        return response
    except Exception as e:
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        if add_flow:
            await _cleanup_incomplete_institution_add(db, user_id, provider, institution_id)
        await set_institution_sync_status(
            db,
            user_id,
            provider,
            "network_error" if is_network_error(e) else error_status,
            institution_id=institution_id,
        )
        if is_network_error(e):
            return _with_sync_id({"status": "network_error", "message": "Connection failed"}, sync_id)
        return _with_sync_id(
            {"status": "error", "message": safe_connector_public_message(e, fallback="Sync failed")},
            sync_id,
        )
    finally:
        try:
            clear_provider_activity(activity_id)
        finally:
            await release_sync_lock(user_id, provider, institution_id=lock_institution_id)


def _make_sync_handler(
    provider: str,
    *,
    lock_provider: str | None = None,
    status_provider: str | None = None,
    error_status: str = "error",
):
    async def handler(
        body: dict | None = None,
        db: AsyncSession = Depends(get_db),
        current_user: CurrentUser = Depends(get_current_user),
    ):
        return await _sync_connector_route(
            db,
            current_user.id,
            provider=provider,
            body=body,
            lock_provider=lock_provider,
            status_provider=status_provider,
            error_status=error_status,
        )

    return handler


def _make_scraper_login_handler(
    provider: str,
    *,
    missing_credentials_message: str,
    error_status: str = "error",
):
    async def handler(
        body: dict,
        db: AsyncSession = Depends(get_db),
        current_user: CurrentUser = Depends(get_current_user),
    ):
        return await _scraper_login_route(
            body,
            db,
            current_user.id,
            provider=provider,
            missing_credentials_message=missing_credentials_message,
            error_status=error_status,
        )

    return handler


def _make_scraper_2fa_handler(
    provider: str,
    *,
    error_status: str = "error",
):
    async def handler(
        body: dict,
        db: AsyncSession = Depends(get_db),
        current_user: CurrentUser = Depends(get_current_user),
    ):
        return await _scraper_2fa_route(
            body,
            db,
            current_user.id,
            provider=provider,
            error_status=error_status,
        )

    return handler


def _make_api_session_otp_login_handler(
    provider: str,
    *,
    missing_credentials_message: str,
    error_status: str = "error",
):
    async def handler(
        body: dict,
        db: AsyncSession = Depends(get_db),
        current_user: CurrentUser = Depends(get_current_user),
    ):
        return await _api_session_otp_login_route(
            body,
            db,
            current_user.id,
            provider=provider,
            missing_credentials_message=missing_credentials_message,
            error_status=error_status,
        )

    return handler


def _make_api_session_otp_2fa_handler(
    provider: str,
    *,
    error_status: str = "error",
):
    async def handler(
        body: dict,
        db: AsyncSession = Depends(get_db),
        current_user: CurrentUser = Depends(get_current_user),
    ):
        return await _api_session_otp_2fa_route(
            body,
            db,
            current_user.id,
            provider=provider,
            error_status=error_status,
        )

    return handler


def _register_catalog_sync_routes() -> None:
    for provider in iter_sync_route_providers():
        route = get_route_metadata(provider)
        sync_path = route.get("syncPath")
        if sync_path:
            router.add_api_route(
                sync_path,
                _make_sync_handler(
                    provider,
                    lock_provider=route.get("lockProvider"),
                    status_provider=route.get("statusProvider"),
                    error_status=str(route.get("errorStatus") or "error"),
                ),
                methods=["POST"],
                name=f"sync_{provider}",
            )

    for provider in iter_standard_scraper_auth_providers():
        route = get_route_metadata(provider)
        router.add_api_route(
            route["loginPath"],
            _make_scraper_login_handler(
                provider,
                missing_credentials_message=str(route["missingCredentialsMessage"]),
                error_status=str(route.get("errorStatus") or "error"),
            ),
            methods=["POST"],
            name=f"{provider}_login",
        )
        router.add_api_route(
            route["twofaPath"],
            _make_scraper_2fa_handler(
                provider,
                error_status=str(route.get("errorStatus") or "error"),
            ),
            methods=["POST"],
            name=f"{provider}_2fa",
        )

    for provider in iter_api_session_otp_auth_providers():
        route = get_route_metadata(provider)
        router.add_api_route(
            route["loginPath"],
            _make_api_session_otp_login_handler(
                provider,
                missing_credentials_message=str(route["missingCredentialsMessage"]),
                error_status=str(route.get("errorStatus") or "error"),
            ),
            methods=["POST"],
            name=f"{provider}_login",
        )
        router.add_api_route(
            route["twofaPath"],
            _make_api_session_otp_2fa_handler(
                provider,
                error_status=str(route.get("errorStatus") or "error"),
            ),
            methods=["POST"],
            name=f"{provider}_2fa",
        )


_register_catalog_sync_routes()


@router.post("/sync/batch")
async def start_batch_sync(
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.sync_batch import start_sync_batch

    connections, mode = _validate_sync_batch_payload(body)
    try:
        return await start_sync_batch(
            current_user.id,
            connections,
            mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sync/batches/active")
async def get_active_batch_syncs(
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.sync_batch import get_user_active_sync_batches

    return {"batches": await get_user_active_sync_batches(current_user.id)}


@router.get("/sync/activity")
async def get_sync_activity(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_sync_activity_payload(db, current_user.id)


@router.get("/sync/batch/{batch_id}")
async def get_batch_sync(
    batch_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.sync_batch import get_sync_batch

    job = await get_sync_batch(current_user.id, batch_id)
    if not job:
        return {"status": "not_found", "message": "Sync batch not found"}
    return job


@router.post("/sync/visible-auth-artifact")
async def save_visible_auth_artifact(
    body: dict,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    _reject_unknown_body_keys(
        body,
        frozenset({"provider", "artifact_type", "attempt_id", "add_flow", "payload"}),
    )
    provider = _normalize_visible_auth_provider(body.get("provider"))
    artifact_type = _normalize_visible_auth_artifact_type(body.get("artifact_type"))
    attempt_id = _strict_visible_auth_attempt_id(body.get("attempt_id"))
    add_flow = _payload_bool(body, "add_flow", default=True)
    require_runner_binding(
        request,
        provider=provider,
        attempt_id=attempt_id,
        add_flow=add_flow,
        purpose="visible-auth",
    )
    payload = body.get("payload")

    try:
        runtime_metadata = get_runtime_state_metadata(provider)
        visible_auth_metadata = get_desktop_visible_auth_metadata(provider)
    except KeyError:
        runtime_metadata = {}
        visible_auth_metadata = {}
    if not _desktop_visible_auth_flow_enabled(visible_auth_metadata, add_flow=add_flow):
        raise HTTPException(status_code=422, detail="Visible auth is not enabled for this provider flow")
    if artifact_type not in (runtime_metadata.get("artifactKinds") or ()):
        raise HTTPException(status_code=422, detail="Unsupported visible auth artifact")
    if payload is None:
        raise HTTPException(status_code=422, detail="Artifact payload is required")
    try:
        payload_size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Artifact payload must be JSON serializable") from exc
    if payload_size > MAX_VISIBLE_AUTH_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Visible auth artifact is too large")

    artifact_path = get_provider_visible_auth_artifact_path(
        current_user.id,
        provider,
        attempt_id,
        artifact_type,
    )
    await save_visible_auth_attempt_artifact(
        current_user.id,
        provider,
        attempt_id,
        artifact_type,
        payload,
    )
    return {
        "status": "ok",
        "attempt_id": attempt_id,
        "artifact_relative_path": _visible_auth_artifact_response_path(artifact_path),
        "staged": True,
    }


@router.post("/sync/visible-auth-credentials")
async def save_visible_auth_credentials(
    body: dict,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    _reject_unknown_body_keys(
        body,
        frozenset({"provider", "attempt_id", "add_flow", "username", "password"}),
    )
    provider = _normalize_visible_auth_provider(body.get("provider"))
    attempt_id = _strict_visible_auth_attempt_id(body.get("attempt_id"))
    add_flow = _payload_bool(body, "add_flow", default=True)
    require_runner_binding(
        request,
        provider=provider,
        attempt_id=attempt_id,
        add_flow=add_flow,
        purpose="visible-auth",
    )
    username = body.get("username")
    password = body.get("password")

    try:
        visible_auth_metadata = get_desktop_visible_auth_metadata(provider)
    except KeyError:
        visible_auth_metadata = {}
    if not _desktop_visible_auth_flow_enabled(visible_auth_metadata, add_flow=add_flow):
        raise HTTPException(
            status_code=422,
            detail="Visible auth credential capture is not enabled for this provider",
        )
    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(status_code=422, detail="Visible auth credentials must be strings")
    if not username or not password:
        raise HTTPException(status_code=422, detail="Visible auth credentials are required")
    if len(username) > MAX_VISIBLE_AUTH_CREDENTIAL_LENGTH or len(password) > MAX_VISIBLE_AUTH_CREDENTIAL_LENGTH:
        raise HTTPException(status_code=422, detail="Visible auth credentials are too long")
    await save_visible_auth_attempt_artifact(
        current_user.id,
        provider,
        attempt_id,
        SCRAPER_CREDENTIALS_ARTIFACT_KIND,
        {
            "username": username,
            "password": password,
        },
    )
    return {"status": "ok", "attempt_id": attempt_id, "staged": True}


@router.get("/sync/runtime-artifact/{provider}/{artifact_kind}")
async def get_runtime_artifact_for_visible_auth(
    provider: str,
    artifact_kind: str,
    request: Request,
    slots: list[str] | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
):
    normalized_provider = _normalize_visible_auth_provider(provider)
    runner_principal = require_runner_binding(
        request,
        provider=normalized_provider,
        purpose="visible-auth",
    )
    if runner_principal.add_flow:
        return {"status": "not_found"}
    normalized_artifact_kind = _normalize_visible_auth_artifact_type(artifact_kind)
    try:
        runtime_metadata = get_runtime_state_metadata(normalized_provider)
    except KeyError:
        runtime_metadata = {}
    if normalized_artifact_kind not in (runtime_metadata.get("artifactKinds") or ()):
        return {"status": "error", "message": "Unsupported runtime artifact"}

    requested_slots = tuple(
        str(slot or "").strip().lower()
        for slot in (slots or [ACTIVE_SLOT])
        if str(slot or "").strip()
    )
    for slot in requested_slots:
        if slot not in {ACTIVE_SLOT, QUARANTINE_SLOT}:
            continue
        payload = await load_provider_runtime_artifact(
            current_user.id,
            normalized_provider,
            normalized_artifact_kind,
            slot=slot,
            institution_id=runner_principal.institution_id,
        )
        if payload is not None:
            return {
                "status": "ok",
                "provider": normalized_provider,
                "artifact_kind": normalized_artifact_kind,
                "slot": slot,
                "payload": payload,
            }
    return {"status": "not_found"}


@router.post("/sync/runtime-artifact/{provider}/{artifact_kind}")
async def save_runtime_artifact_for_visible_auth(
    provider: str,
    artifact_kind: str,
    body: dict,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    _reject_unknown_body_keys(body, frozenset({"payload"}))
    normalized_provider = _normalize_visible_auth_provider(provider)
    runner_principal = require_runner_binding(
        request,
        provider=normalized_provider,
        purpose="visible-auth",
    )
    if runner_principal.add_flow or runner_principal.institution_id is None:
        return {"status": "not_ready", "message": "Visible auth runtime artifact is not promoted yet"}
    normalized_artifact_kind = _normalize_visible_auth_artifact_type(artifact_kind)
    try:
        runtime_metadata = get_runtime_state_metadata(normalized_provider)
    except KeyError:
        runtime_metadata = {}
    if normalized_artifact_kind not in (runtime_metadata.get("artifactKinds") or ()):
        raise HTTPException(status_code=422, detail="Unsupported runtime artifact")
    payload = body.get("payload")
    if payload is None:
        raise HTTPException(status_code=422, detail="Artifact payload is required")
    try:
        payload_size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Artifact payload must be JSON serializable") from exc
    if payload_size > MAX_VISIBLE_AUTH_RUNTIME_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Visible auth artifact is too large")

    await save_provider_runtime_artifact(
        current_user.id,
        normalized_provider,
        normalized_artifact_kind,
        payload,
        institution_id=runner_principal.institution_id,
    )
    return {
        "status": "ok",
        "provider": normalized_provider,
        "artifact_kind": normalized_artifact_kind,
        "slot": ACTIVE_SLOT,
    }


@router.post("/sync/visible-auth-attempt-cleanup")
async def cleanup_visible_auth_attempt(
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
):
    _reject_unknown_body_keys(body, frozenset({"provider", "attempt_id", "add_flow"}))
    provider = _normalize_visible_auth_provider(body.get("provider"))
    attempt_id = _strict_visible_auth_attempt_id(body.get("attempt_id"))
    add_flow = _payload_bool(body, "add_flow", default=True)

    try:
        visible_auth_metadata = get_desktop_visible_auth_metadata(provider)
    except KeyError:
        visible_auth_metadata = {}
    if not _desktop_visible_auth_flow_enabled(visible_auth_metadata, add_flow=add_flow):
        return {"status": "error", "message": "Unsupported visible auth attempt cleanup"}
    await _delete_visible_auth_attempt_dir_async(current_user.id, provider, attempt_id)
    return {"status": "ok", "attempt_id": attempt_id}
