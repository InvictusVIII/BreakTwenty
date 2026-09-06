from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.auth import get_auth_principal
from app.launch_auth import (
    AuthPrincipal,
    issue_runner_session,
    register_runner_grant,
    require_runner_binding,
    revoke_launch_generation,
    revoke_runner_scope,
    revoke_runner_session,
)
from app.models import Institution
from app.provider_catalog import (
    get_desktop_visible_auth_metadata,
    get_provider_credential_storage_provider,
    get_provider_institution_type,
)
from app.services.connection_auth_storage import get_scraper_credentials

router = APIRouter()


@router.post("/auth/launch/revoke")
async def revoke_current_launch(
    principal: AuthPrincipal = Depends(get_auth_principal),
):
    if principal.kind != "desktop":
        raise HTTPException(status_code=403, detail="Desktop launch authentication required")
    if not revoke_launch_generation(principal):
        raise HTTPException(status_code=409, detail="Launch authentication already ended")
    return {"status": "ok"}


def _strict_object(body: object, *, allowed: frozenset[str]) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be an object")
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported fields: {', '.join(unknown)}",
        )
    return body


@router.post("/auth/runner-grants")
async def create_runner_grant(
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
    principal: AuthPrincipal = Depends(get_auth_principal),
    db: AsyncSession = Depends(get_db),
):
    if principal.kind != "desktop":
        raise HTTPException(status_code=403, detail="Launch authentication required")
    payload = _strict_object(
        body,
        allowed=frozenset({"grant", "provider", "attempt_id", "institution_id", "add_flow", "purpose"}),
    )
    grant = payload.get("grant")
    provider = str(payload.get("provider") or "").strip().lower()
    attempt_id = payload.get("attempt_id")
    institution_id = payload.get("institution_id")
    add_flow = payload.get("add_flow")
    purpose = str(payload.get("purpose") or "visible-auth").strip().lower()
    if not isinstance(grant, str) or not isinstance(attempt_id, str):
        raise HTTPException(status_code=422, detail="grant and attempt_id must be strings")
    if type(add_flow) is not bool:
        raise HTTPException(status_code=422, detail="add_flow must be a boolean")
    if institution_id is not None and type(institution_id) is not int:
        raise HTTPException(status_code=422, detail="institution_id must be an integer or null")
    if purpose != "visible-auth":
        raise HTTPException(status_code=422, detail="Unsupported runner grant purpose")
    try:
        visible_auth = get_desktop_visible_auth_metadata(provider)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Unsupported visible-auth provider") from exc
    flow_key = "addFlow" if add_flow else "manualFlow"
    if not visible_auth.get("enabled") or not visible_auth.get(flow_key):
        raise HTTPException(status_code=422, detail="Visible auth is not enabled for this flow")
    if add_flow and institution_id is not None:
        raise HTTPException(status_code=422, detail="institution_id must be null for an add flow")
    if not add_flow and institution_id is None:
        raise HTTPException(status_code=422, detail="institution_id is required for an existing connection")
    try:
        stored_provider = get_provider_credential_storage_provider(provider)
        stored_institution_type = get_provider_institution_type(stored_provider)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Unsupported credential provider") from exc
    if add_flow:
        existing_connection = (
            await db.execute(
                select(Institution.id).where(
                    Institution.user_id == current_user.id,
                    Institution.provider == stored_provider,
                    Institution.type == stored_institution_type,
                )
            )
        ).scalar_one_or_none()
        if existing_connection is not None:
            raise HTTPException(status_code=409, detail="Provider is already connected")
    if institution_id is not None:
        connection = (
            await db.execute(
                select(Institution).where(
                    Institution.id == institution_id,
                    Institution.user_id == current_user.id,
                    Institution.provider == stored_provider,
                    Institution.type == stored_institution_type,
                )
            )
        ).scalar_one_or_none()
        if connection is None:
            raise HTTPException(status_code=404, detail="Institution not found")
        if not add_flow and not connection.enabled:
            raise HTTPException(status_code=409, detail="Institution is disabled")
    try:
        register_runner_grant(
            token=grant,
            user_id=current_user.id,
            provider=provider,
            attempt_id=attempt_id,
            institution_id=institution_id,
            add_flow=add_flow,
            purpose=purpose,
            launch_generation=principal.launch_generation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok"}


@router.post("/auth/runner-session")
async def exchange_runner_grant(
    principal: AuthPrincipal = Depends(get_auth_principal),
):
    if principal.kind != "runner-grant":
        raise HTTPException(status_code=403, detail="Runner grant required")
    try:
        token, expires_in = issue_runner_session(principal)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Runner grant expired") from exc
    return {
        "status": "ok",
        "access_token": token,
        "token_type": "bearer",
        "expires_in": int(expires_in),
    }


@router.post("/auth/runner-session/revoke")
async def revoke_current_runner_session(request: Request):
    principal = require_runner_binding(request)
    revoke_runner_session(principal)
    return {"status": "ok"}


@router.post("/auth/runner-sessions/revoke")
async def revoke_scoped_runner_sessions(
    body: dict,
    current_user: CurrentUser = Depends(get_current_user),
    principal: AuthPrincipal = Depends(get_auth_principal),
):
    if principal.kind != "desktop":
        raise HTTPException(status_code=403, detail="Launch authentication required")
    payload = _strict_object(
        body,
        allowed=frozenset({"provider", "attempt_id", "purpose"}),
    )
    provider = str(payload.get("provider") or "").strip().lower()
    attempt_id = str(payload.get("attempt_id") or "").strip()
    purpose = str(payload.get("purpose") or "").strip().lower()
    if (
        not provider
        or not attempt_id
        or len(provider) > 64
        or len(attempt_id) > 128
        or purpose != "visible-auth"
    ):
        raise HTTPException(status_code=422, detail="Invalid runner session scope")
    revoked = revoke_runner_scope(
        user_id=current_user.id,
        provider=provider,
        attempt_id=attempt_id,
        purpose=purpose,
    )
    return {"status": "ok", "revoked": revoked}


@router.get("/auth/runner-credentials")
async def get_runner_credentials(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    principal = require_runner_binding(request, purpose="visible-auth")
    if principal.add_flow:
        return {"status": "not_found"}
    credentials = await get_scraper_credentials(
        db,
        principal.user_id,
        str(principal.provider or ""),
        institution_id=principal.institution_id,
    )
    if credentials is None:
        return {"status": "not_found"}
    return {
        "status": "ok",
        "username": credentials.username,
        "password": credentials.password,
    }
