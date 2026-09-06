from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.connectors.connector_logging import get_connector_logger, log_connector_event, safe_connector_public_message
from app.services.connection_auth_storage import (
    ProviderAlreadyConnectedError,
    ensure_connection,
    upsert_connection_artifact,
)
from app.services.moomoo_cloud import (
    MOOMOO_CLOUD_ORIGIN,
    MoomooCloudError,
    exchange_authorization_code,
    register_public_client,
    sanitize_scope,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.support_auto_archive import archive_run_fire_and_forget
from app.services.sync_tracking import (
    ensure_provider_sync_attempt,
    finalize_provider_sync_attempt,
)


router = APIRouter()
logger = get_connector_logger("moomoo")
MOOMOO_OAUTH_FLOW_TTL_SECONDS = 10 * 60
MOOMOO_OAUTH_SESSION_SCHEMA = "breaktwenty.moomoo-cloud-oauth.v1"


@dataclass
class _MoomooOAuthFlow:
    flow_id: str
    user_id: int
    sync_id: str
    attempt_id: str
    add_flow: bool
    institution_id: int | None
    redirect_uri: str
    client_id: str
    verifier: str
    state: str
    created_at: float
    status: str = "waiting"
    refresh_token: str = ""
    scope: str = ""
    error: str = ""
    error_code: str = ""


_flows: dict[str, _MoomooOAuthFlow] = {}
_flow_ids_by_state: dict[str, str] = {}
_flow_lock = asyncio.Lock()
_client_ids_by_redirect: dict[str, str] = {}
_client_registration_lock = asyncio.Lock()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _callback_page(title: str, message: str, *, status_code: int = 200) -> HTMLResponse:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{safe_title}</title></head><body><main><h1>{safe_title}</h1>"
        f"<p>{safe_message}</p></main></body></html>",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _redirect_uri(request: Request) -> str:
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return f"http://localhost:{port}/api/auth/moomoo/oauth/callback"


def _flow_expired(flow: _MoomooOAuthFlow) -> bool:
    return time.monotonic() - flow.created_at > MOOMOO_OAUTH_FLOW_TTL_SECONDS


async def _remove_flow(flow: _MoomooOAuthFlow) -> None:
    async with _flow_lock:
        _flows.pop(flow.flow_id, None)
        if _flow_ids_by_state.get(flow.state) == flow.flow_id:
            _flow_ids_by_state.pop(flow.state, None)


async def _expire_stale_flows() -> None:
    now = time.monotonic()
    async with _flow_lock:
        stale = [
            flow
            for flow in _flows.values()
            if now - flow.created_at > MOOMOO_OAUTH_FLOW_TTL_SECONDS
        ]
        for flow in stale:
            _flows.pop(flow.flow_id, None)
            if _flow_ids_by_state.get(flow.state) == flow.flow_id:
                _flow_ids_by_state.pop(flow.state, None)
    for flow in stale:
        if flow.status not in {"error", "complete"}:
            await _fail_flow(
                flow,
                MoomooCloudError(
                    "Moomoo authorization timed out. Start the connection again.",
                    stage="authorization_timeout",
                    provider_code="timeout",
                ),
                stage="oauth abandoned flow expired",
            )


def _archive_oauth_failure(flow: _MoomooOAuthFlow, trigger: str) -> None:
    finalize_provider_sync_attempt(
        flow.user_id,
        "moomoo",
        sync_id=flow.sync_id,
        status="error",
        institution_id=flow.institution_id,
    )
    archive_run_fire_and_forget(
        user_id=flow.user_id,
        provider="moomoo",
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
        trigger=trigger,
        error=flow.error or None,
        extra_fields={
            "flow": "add" if flow.add_flow else "existing",
            "result_status": "error",
            "institution_id": flow.institution_id,
            "sync_source": "add_connection" if flow.add_flow else "manual_reauthentication",
            "oauth_error_code": flow.error_code or None,
        },
    )


def _archive_oauth_handoff(flow: _MoomooOAuthFlow, institution_id: int) -> None:
    # Close the authorization phase before returning to the renderer. The normal
    # /sync/moomoo call immediately re-opens the same sync_id; if the renderer is
    # closed in that handoff gap, this still leaves a selectable diagnostic pill.
    finalize_provider_sync_attempt(
        flow.user_id,
        "moomoo",
        sync_id=flow.sync_id,
        status="ok",
        institution_id=institution_id,
    )
    archive_run_fire_and_forget(
        user_id=flow.user_id,
        provider="moomoo",
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
        trigger="moomoo_cloud_oauth_authorized",
        extra_fields={
            "flow": "add" if flow.add_flow else "existing",
            "result_status": "oauth_authorized_pending_sync",
            "institution_id": institution_id,
            "sync_source": "add_connection" if flow.add_flow else "manual_reauthentication",
        },
    )


async def _fail_flow(flow: _MoomooOAuthFlow, error: Exception, *, stage: str) -> None:
    public_error = safe_connector_public_message(
        error,
        fallback="Moomoo authorization did not finish.",
    )
    error_code = str(getattr(error, "provider_code", "") or type(error).__name__)[:100]
    async with _flow_lock:
        if flow.status in {"error", "complete"}:
            return
        flow.status = "error"
        flow.error = public_error
        flow.error_code = error_code
    log_connector_event(
        logger,
        provider="moomoo",
        stage=stage,
        level="warning",
        user_id=flow.user_id,
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
        flow="add" if flow.add_flow else "existing",
        status_code=getattr(error, "status_code", None),
        error_code=flow.error_code,
        message=flow.error,
    )
    _archive_oauth_failure(flow, f"moomoo_cloud_oauth_{stage.replace(' ', '_')}")


async def _owned_flow(flow_id: str, user_id: int) -> _MoomooOAuthFlow:
    async with _flow_lock:
        flow = _flows.get(str(flow_id or "").strip())
    if flow is None or flow.user_id != user_id:
        raise HTTPException(status_code=404, detail="Moomoo authorization attempt was not found")
    if _flow_expired(flow) and flow.status not in {"error", "complete"}:
        await _fail_flow(
            flow,
            MoomooCloudError(
                "Moomoo authorization timed out. Start the connection again.",
                stage="authorization_timeout",
                provider_code="timeout",
            ),
            stage="oauth authorization timeout",
        )
    return flow


@router.post("/auth/moomoo/oauth/start")
async def start_moomoo_oauth(
    body: dict,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
):
    await _expire_stale_flows()
    add_flow = body.get("add_flow", True)
    if type(add_flow) is not bool:
        raise HTTPException(status_code=422, detail="add_flow must be a boolean")
    institution_id = body.get("institution_id")
    if institution_id not in {None, ""}:
        try:
            institution_id = int(institution_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="institution_id must be an integer") from exc
        if institution_id <= 0:
            raise HTTPException(status_code=422, detail="institution_id must be positive")
    else:
        institution_id = None
    attempt = ensure_provider_sync_attempt(
        current_user.id,
        "moomoo",
        sync_id=str(body.get("sync_id") or "").strip() or None,
        institution_id=institution_id,
    )
    sync_id = attempt["sync_id"]
    attempt_id = uuid4().hex
    redirect_uri = _redirect_uri(request)
    log_connector_event(
        logger,
        provider="moomoo",
        stage="oauth start",
        user_id=current_user.id,
        sync_id=sync_id,
        attempt_id=attempt_id,
        flow="add" if add_flow else "existing",
        callback_port=request.url.port,
    )
    try:
        async with _client_registration_lock:
            client_id = _client_ids_by_redirect.get(redirect_uri, "")
            client_reused = bool(client_id)
            if client_reused:
                offered_scope = ""
            else:
                client_id, offered_scope = await register_public_client(redirect_uri)
                _client_ids_by_redirect[redirect_uri] = client_id
    except MoomooCloudError as exc:
        temporary_flow = _MoomooOAuthFlow(
            flow_id=uuid4().hex,
            user_id=current_user.id,
            sync_id=sync_id,
            attempt_id=attempt_id,
            add_flow=add_flow,
            institution_id=institution_id,
            redirect_uri=redirect_uri,
            client_id="",
            verifier="",
            state="",
            created_at=time.monotonic(),
        )
        await _fail_flow(temporary_flow, exc, stage="oauth client registration failed")
        return {
            "status": "error",
            "message": temporary_flow.error,
            "sync_id": sync_id,
            "attempt_id": attempt_id,
        }

    verifier = _base64url(secrets.token_bytes(64))
    challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    flow = _MoomooOAuthFlow(
        flow_id=uuid4().hex,
        user_id=current_user.id,
        sync_id=sync_id,
        attempt_id=attempt_id,
        add_flow=add_flow,
        institution_id=institution_id,
        redirect_uri=redirect_uri,
        client_id=client_id,
        verifier=verifier,
        state=_base64url(secrets.token_bytes(32)),
        created_at=time.monotonic(),
    )
    async with _flow_lock:
        _flows[flow.flow_id] = flow
        _flow_ids_by_state[flow.state] = flow.flow_id
    authorization_url = f"{MOOMOO_CLOUD_ORIGIN}/oauth2/authorize/confirm?{urlencode({
        'client_id': flow.client_id,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'redirect_uri': flow.redirect_uri,
        'response_type': 'code',
        'state': flow.state,
    })}"
    log_connector_event(
        logger,
        provider="moomoo",
        stage="oauth authorization ready",
        user_id=current_user.id,
        sync_id=sync_id,
        attempt_id=attempt_id,
        offered_scopes=sanitize_scope(offered_scope),
        client_reused=client_reused,
    )
    return {
        "status": "waiting",
        "flow_id": flow.flow_id,
        "sync_id": sync_id,
        "attempt_id": attempt_id,
        "authorization_url": authorization_url,
        "expires_in": MOOMOO_OAUTH_FLOW_TTL_SECONDS,
    }


@router.get("/auth/moomoo/oauth/callback", response_class=HTMLResponse)
async def complete_moomoo_oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
):
    async with _flow_lock:
        flow_id = _flow_ids_by_state.get(state)
        flow = _flows.get(flow_id or "")
    if flow is None or not state or not secrets.compare_digest(flow.state, state):
        return _callback_page(
            "Moomoo authorization could not be matched",
            "Return to BreakTwenty and start the Moomoo connection again.",
            status_code=400,
        )
    if _flow_expired(flow):
        await _fail_flow(
            flow,
            MoomooCloudError(
                "Moomoo authorization timed out. Start the connection again.",
                stage="authorization_callback",
                provider_code="timeout",
            ),
            stage="oauth callback expired",
        )
        return _callback_page(
            "Moomoo authorization expired",
            "Return to BreakTwenty and try again.",
            status_code=400,
        )
    async with _flow_lock:
        callback_status = flow.status
        claimed = callback_status == "waiting"
        if claimed:
            flow.status = "exchanging"
    if callback_status in {"authorized", "complete"}:
        return _callback_page(
            "Moomoo is already connected to BreakTwenty",
            "You can close this tab and return to BreakTwenty.",
        )
    if callback_status == "error":
        return _callback_page(
            "Moomoo authorization was not completed",
            "Return to BreakTwenty to try again.",
            status_code=400,
        )
    if not claimed:
        return _callback_page(
            "Moomoo authorization is already being processed",
            "You can close this tab and return to BreakTwenty.",
        )
    if error:
        await _fail_flow(
            flow,
            MoomooCloudError(
                error_description or error,
                stage="authorization_callback",
                provider_code=error,
            ),
            stage="oauth authorization declined",
        )
        return _callback_page(
            "Moomoo authorization was not completed",
            "Return to BreakTwenty to try again.",
            status_code=400,
        )
    if not code:
        await _fail_flow(
            flow,
            MoomooCloudError(
                "Moomoo did not return an authorization code.",
                stage="authorization_callback",
                provider_code="missing_code",
            ),
            stage="oauth callback missing code",
        )
        return _callback_page(
            "Moomoo authorization did not finish",
            "Return to BreakTwenty and try again.",
            status_code=400,
        )
    log_connector_event(
        logger,
        provider="moomoo",
        stage="oauth callback received",
        user_id=flow.user_id,
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
    )
    try:
        tokens = await exchange_authorization_code(
            client_id=flow.client_id,
            code=code,
            redirect_uri=flow.redirect_uri,
            code_verifier=flow.verifier,
        )
    except MoomooCloudError as exc:
        async with _client_registration_lock:
            if _client_ids_by_redirect.get(flow.redirect_uri) == flow.client_id:
                _client_ids_by_redirect.pop(flow.redirect_uri, None)
        await _fail_flow(flow, exc, stage="oauth token exchange failed")
        return _callback_page(
            "Moomoo authorization did not finish",
            "Return to BreakTwenty to see the error.",
            status_code=400,
        )
    async with _flow_lock:
        if flow.status != "exchanging":
            return _callback_page(
                "Moomoo authorization was cancelled",
                "Return to BreakTwenty to start again.",
                status_code=400,
            )
        flow.refresh_token = tokens.refresh_token
        flow.scope = tokens.scope
        flow.verifier = ""
        flow.status = "authorized"
    log_connector_event(
        logger,
        provider="moomoo",
        stage="oauth authorized",
        user_id=flow.user_id,
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
        expires_in=tokens.expires_in,
        scopes=sanitize_scope(tokens.scope),
        authorized_account_scope_count=len([
            item for item in tokens.scope.split() if item.startswith("accid:")
        ]),
    )
    return _callback_page(
        "Moomoo connected to BreakTwenty",
        "You can close this tab and return to BreakTwenty while it imports your accounts.",
    )


@router.get("/auth/moomoo/oauth/{flow_id}/status")
async def moomoo_oauth_status(
    flow_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    flow = await _owned_flow(flow_id, current_user.id)
    payload = {
        "status": flow.status,
        "sync_id": flow.sync_id,
        "attempt_id": flow.attempt_id,
    }
    if flow.error:
        payload["message"] = flow.error
    return payload


@router.post("/auth/moomoo/oauth/{flow_id}/complete")
async def persist_moomoo_oauth(
    flow_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    flow = await _owned_flow(flow_id, current_user.id)
    if flow.status == "error":
        return {
            "status": "error",
            "message": flow.error,
            "sync_id": flow.sync_id,
            "attempt_id": flow.attempt_id,
        }
    async with _flow_lock:
        if flow.status != "authorized" or not flow.refresh_token:
            return {
                "status": "waiting",
                "sync_id": flow.sync_id,
                "attempt_id": flow.attempt_id,
            }
        flow.status = "persisting"
    try:
        async with sqlite_write_gate(), db.begin():
            institution_id = await ensure_connection(
                db,
                current_user.id,
                "moomoo",
                pending_add=flow.add_flow,
                institution_id=flow.institution_id,
            )
            if institution_id is None:
                raise RuntimeError("Moomoo connection could not be created")
            saved = await upsert_connection_artifact(
                db,
                current_user.id,
                "moomoo",
                "session",
                {
                    "schema": MOOMOO_OAUTH_SESSION_SCHEMA,
                    "auth_mode": "cloud_oauth",
                    "client_id": flow.client_id,
                    "refresh_token": flow.refresh_token,
                    "scope": flow.scope,
                },
                institution_id=institution_id,
            )
            if not saved:
                raise RuntimeError("Moomoo authorization could not be stored")
            async with _flow_lock:
                if flow.status != "persisting":
                    raise RuntimeError("Moomoo authorization was cancelled before storage completed")
    except ProviderAlreadyConnectedError as exc:
        await _fail_flow(flow, exc, stage="oauth connection persist failed")
        return {
            "status": "error",
            "message": flow.error,
            "sync_id": flow.sync_id,
            "attempt_id": flow.attempt_id,
        }
    except Exception as exc:
        await db.rollback()
        await _fail_flow(flow, exc, stage="oauth token persist failed")
        return {
            "status": "error",
            "message": flow.error,
            "sync_id": flow.sync_id,
            "attempt_id": flow.attempt_id,
        }

    async with _flow_lock:
        flow.status = "complete"
        flow.refresh_token = ""
    log_connector_event(
        logger,
        provider="moomoo",
        stage="oauth token stored",
        user_id=current_user.id,
        sync_id=flow.sync_id,
        attempt_id=flow.attempt_id,
        institution_id=institution_id,
        storage="encrypted_connection_artifact",
    )
    _archive_oauth_handoff(flow, institution_id)
    await _remove_flow(flow)
    return {
        "status": "ok",
        "sync_id": flow.sync_id,
        "attempt_id": flow.attempt_id,
        "institution_id": institution_id,
    }


@router.post("/auth/moomoo/oauth/{flow_id}/cancel")
async def cancel_moomoo_oauth(
    flow_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    flow = await _owned_flow(flow_id, current_user.id)
    if flow.status == "error":
        await _remove_flow(flow)
        return {
            "status": "ok",
            "sync_id": flow.sync_id,
            "attempt_id": flow.attempt_id,
        }
    await _fail_flow(
        flow,
        MoomooCloudError(
            "Moomoo authorization was cancelled.",
            stage="authorization_cancelled",
            provider_code="cancelled",
        ),
        stage="oauth cancelled",
    )
    await _remove_flow(flow)
    return {
        "status": "ok",
        "sync_id": flow.sync_id,
        "attempt_id": flow.attempt_id,
    }
