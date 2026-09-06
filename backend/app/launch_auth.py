from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.brand import APP_BRAND_NAME
from app.key_material import get_key_material

AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "Bearer "
LAUNCH_TOKEN_FILE_ENV = "BREAKTWENTY_LAUNCH_TOKEN_FILE"
TOKEN_BYTES = 32
MAX_TOKEN_FILE_BYTES = 512
RUNNER_GRANT_TTL_SECONDS = 60.0
RUNNER_SESSION_TTL_SECONDS = 2 * 60 * 60.0
logger = logging.getLogger("breaktwenty.launch_auth")


@dataclass(frozen=True)
class AuthPrincipal:
    kind: str
    user_id: int
    provider: str | None = None
    attempt_id: str | None = None
    institution_id: int | None = None
    add_flow: bool = False
    purpose: str = ""
    launch_generation: int = 0


@dataclass(frozen=True)
class _TimedPrincipal:
    principal: AuthPrincipal
    expires_at: float
    launch_generation: int


@dataclass(frozen=True)
class LaunchTokenBundle:
    desktop: str
    renderer: str
    generation: int


class _LaunchTokenSource:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fingerprint: tuple[int, int, int, int] | None = None
        self._bundle: LaunchTokenBundle | None = None
        self._generation = 0

    def read(self) -> LaunchTokenBundle:
        material = get_key_material()
        if material.desktop_launch_token and material.renderer_launch_token:
            desktop = validate_token(
                material.desktop_launch_token,
                label="desktop launch authentication token",
            )
            renderer = validate_token(
                material.renderer_launch_token,
                label="renderer launch authentication token",
            )
            if secrets.compare_digest(desktop, renderer):
                raise RuntimeError(f"{APP_BRAND_NAME} launch authentication roles are invalid")
            return LaunchTokenBundle(
                desktop=desktop,
                renderer=renderer,
                generation=1,
            )

        configured_path = str(os.getenv(LAUNCH_TOKEN_FILE_ENV) or "").strip()
        if not configured_path:
            raise RuntimeError(
                f"{APP_BRAND_NAME} launch authentication is not configured; "
                f"set {LAUNCH_TOKEN_FILE_ENV} to the private launch-token file"
            )
        path = Path(configured_path)
        try:
            file_stat = path.lstat()
        except OSError as exc:
            raise RuntimeError(
                f"{APP_BRAND_NAME} launch authentication token is missing: {path}"
            ) from exc
        if stat.S_ISLNK(file_stat.st_mode):
            raise RuntimeError(f"{APP_BRAND_NAME} launch authentication token cannot be a symlink")
        if not stat.S_ISREG(file_stat.st_mode):
            raise RuntimeError(f"{APP_BRAND_NAME} launch authentication token is not a file")
        if file_stat.st_mode & 0o077:
            raise RuntimeError(
                f"{APP_BRAND_NAME} launch authentication token must have mode 0600"
            )
        fingerprint = (
            int(file_stat.st_dev),
            int(file_stat.st_ino),
            int(file_stat.st_size),
            int(file_stat.st_mtime_ns),
        )
        with self._lock:
            if fingerprint == self._fingerprint and self._bundle is not None:
                return self._bundle
            descriptor = -1
            try:
                open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, open_flags)
                final_stat = os.fstat(descriptor)
                if not stat.S_ISREG(final_stat.st_mode) or final_stat.st_mode & 0o077:
                    raise RuntimeError(
                        f"{APP_BRAND_NAME} launch authentication token permissions changed while loading"
                    )
                with os.fdopen(descriptor, "rb") as token_file:
                    descriptor = -1
                    raw = token_file.read(MAX_TOKEN_FILE_BYTES + 1)
            except OSError as exc:
                raise RuntimeError(
                    f"{APP_BRAND_NAME} launch authentication token could not be read"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            final_fingerprint = (
                int(final_stat.st_dev),
                int(final_stat.st_ino),
                int(final_stat.st_size),
                int(final_stat.st_mtime_ns),
            )
            if final_fingerprint != fingerprint or len(raw) > MAX_TOKEN_FILE_BYTES:
                raise RuntimeError(
                    f"{APP_BRAND_NAME} launch authentication token changed while loading"
                )
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"{APP_BRAND_NAME} launch authentication token is invalid"
                ) from exc
            if not isinstance(payload, dict) or set(payload) != {"version", "desktopToken", "rendererToken"}:
                raise RuntimeError(f"{APP_BRAND_NAME} launch authentication token is invalid")
            if payload.get("version") != 1:
                raise RuntimeError(f"{APP_BRAND_NAME} launch authentication token is unsupported")
            try:
                self._generation += 1
                bundle = LaunchTokenBundle(
                    desktop=validate_token(
                        payload.get("desktopToken"),
                        label="desktop launch authentication token",
                    ),
                    renderer=validate_token(
                        payload.get("rendererToken"),
                        label="renderer launch authentication token",
                    ),
                    generation=self._generation,
                )
            except ValueError as exc:
                raise RuntimeError(f"{APP_BRAND_NAME} launch authentication token is invalid") from exc
            if secrets.compare_digest(bundle.desktop, bundle.renderer):
                raise RuntimeError(f"{APP_BRAND_NAME} launch authentication roles are invalid")
            self._fingerprint = fingerprint
            self._bundle = bundle
            return bundle

    def reset(self) -> None:
        with self._lock:
            self._fingerprint = None
            self._bundle = None
            self._generation = 0


_launch_token_source = _LaunchTokenSource()
_runner_lock = threading.Lock()
_runner_grants: dict[bytes, _TimedPrincipal] = {}
_runner_sessions: dict[bytes, _TimedPrincipal] = {}
_clock: Callable[[], float] = time.monotonic
_launch_generation_lock = threading.Lock()
_active_launch_generation: int | None = None
_revoked_launch_generation: int | None = None


def generate_token() -> str:
    return base64.b64encode(secrets.token_bytes(TOKEN_BYTES)).decode("ascii")


def validate_token(value: str, *, label: str = "token") -> str:
    token = str(value or "")
    if not token or token != token.strip():
        raise ValueError(f"{label} is invalid")
    try:
        decoded = base64.b64decode(token.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if len(decoded) != TOKEN_BYTES or base64.b64encode(decoded).decode("ascii") != token:
        raise ValueError(f"{label} must contain exactly 256 bits")
    return token


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _purge_expired_locked(now: float) -> None:
    for registry in (_runner_grants, _runner_sessions):
        expired = [digest for digest, record in registry.items() if record.expires_at <= now]
        for digest in expired:
            registry.pop(digest, None)


def _accept_launch_generation(generation: int) -> None:
    global _active_launch_generation, _revoked_launch_generation
    with _launch_generation_lock:
        if _active_launch_generation is None:
            _active_launch_generation = generation
            return
        if _active_launch_generation == generation:
            return
        # The token source lock has already been released before this function is
        # called, so registry revocation cannot invert lock order with token reload.
        with _runner_lock:
            _runner_grants.clear()
            _runner_sessions.clear()
        _active_launch_generation = generation
        _revoked_launch_generation = None


def _launch_generation_is_revoked(generation: int) -> bool:
    with _launch_generation_lock:
        return _revoked_launch_generation == generation


def revoke_launch_generation(principal: AuthPrincipal) -> bool:
    global _revoked_launch_generation
    if principal.kind != "desktop" or principal.launch_generation <= 0:
        return False
    with _launch_generation_lock:
        if principal.launch_generation != _active_launch_generation:
            return False
        with _runner_lock:
            _runner_grants.clear()
            _runner_sessions.clear()
        _revoked_launch_generation = principal.launch_generation
    return True


def register_runner_grant(
    *,
    token: str,
    user_id: int,
    provider: str,
    attempt_id: str,
    institution_id: int | None,
    add_flow: bool,
    purpose: str = "visible-auth",
    launch_generation: int | None = None,
) -> None:
    validate_token(token, label="runner grant")
    launch_tokens = _launch_token_source.read()
    _accept_launch_generation(launch_tokens.generation)
    if _launch_generation_is_revoked(launch_tokens.generation):
        raise ValueError("launch authentication has ended")
    if launch_generation is not None and launch_generation != launch_tokens.generation:
        raise ValueError("launch authentication rotated before runner authorization")
    provider_value = str(provider or "").strip().lower()
    attempt_value = str(attempt_id or "").strip()
    if not provider_value or len(provider_value) > 64:
        raise ValueError("provider is invalid")
    if (
        not attempt_value
        or len(attempt_value) > 128
        or not all(char.isalnum() or char in "-_" for char in attempt_value)
    ):
        raise ValueError("attempt_id is invalid")
    if user_id <= 0:
        raise ValueError("user_id is invalid")
    if institution_id is not None and institution_id <= 0:
        raise ValueError("institution_id is invalid")
    principal = AuthPrincipal(
        kind="runner-grant",
        user_id=user_id,
        provider=provider_value,
        attempt_id=attempt_value,
        institution_id=institution_id,
        add_flow=bool(add_flow),
        purpose=str(purpose or "").strip().lower(),
        launch_generation=launch_tokens.generation,
    )
    now = _clock()
    digest = _token_digest(token)
    with _runner_lock:
        _purge_expired_locked(now)
        existing_grant = _runner_grants.get(digest)
        if existing_grant is not None:
            if (
                existing_grant.principal == principal
                and existing_grant.launch_generation == launch_tokens.generation
            ):
                # The desktop broker may lose the first HTTP response while the
                # embedded backend is entering recovery. Repeating the exact
                # registration is safe and does not extend the one-time TTL.
                return
            raise ValueError("runner grant is already registered")
        if digest in _runner_sessions:
            raise ValueError("runner grant is already registered")
        _runner_grants[digest] = _TimedPrincipal(
            principal=principal,
            expires_at=now + RUNNER_GRANT_TTL_SECONDS,
            launch_generation=launch_tokens.generation,
        )


def issue_runner_session(grant_principal: AuthPrincipal) -> tuple[str, float]:
    if grant_principal.kind != "runner-grant":
        raise ValueError("runner grant principal is required")
    token = generate_token()
    session_principal = AuthPrincipal(
        kind="runner-session",
        user_id=grant_principal.user_id,
        provider=grant_principal.provider,
        attempt_id=grant_principal.attempt_id,
        institution_id=grant_principal.institution_id,
        add_flow=grant_principal.add_flow,
        purpose=grant_principal.purpose,
        launch_generation=grant_principal.launch_generation,
    )
    now = _clock()
    with _launch_generation_lock:
        generation = _active_launch_generation or 0
        if (
            not generation
            or grant_principal.launch_generation != generation
            or _revoked_launch_generation == generation
        ):
            raise ValueError("runner grant belongs to an expired launch")
        with _runner_lock:
            _purge_expired_locked(now)
            _runner_sessions[_token_digest(token)] = _TimedPrincipal(
                principal=session_principal,
                expires_at=now + RUNNER_SESSION_TTL_SECONDS,
                launch_generation=generation,
            )
    return token, RUNNER_SESSION_TTL_SECONDS


def revoke_runner_session(principal: AuthPrincipal) -> bool:
    if principal.kind != "runner-session":
        return False
    revoked = False
    with _runner_lock:
        digests = [
            digest
            for digest, record in _runner_sessions.items()
            if record.principal == principal
        ]
        for digest in digests:
            _runner_sessions.pop(digest, None)
            revoked = True
    return revoked


def revoke_runner_scope(
    *,
    user_id: int,
    provider: str,
    attempt_id: str,
    purpose: str,
) -> int:
    revoked = 0
    with _runner_lock:
        for registry in (_runner_grants, _runner_sessions):
            digests = [
                digest
                for digest, record in registry.items()
                if record.principal.user_id == user_id
                and record.principal.provider == provider
                and record.principal.attempt_id == attempt_id
                and record.principal.purpose == purpose
            ]
            for digest in digests:
                registry.pop(digest, None)
                revoked += 1
    return revoked


def require_runner_binding(
    request: Request,
    *,
    provider: str | None = None,
    attempt_id: str | None = None,
    add_flow: bool | None = None,
    purpose: str | None = None,
) -> AuthPrincipal:
    principal = getattr(request.state, "auth_principal", None)
    if not isinstance(principal, AuthPrincipal) or principal.kind != "runner-session":
        raise HTTPException(status_code=403, detail="Runner session required")
    if provider is not None and str(provider or "").strip().lower() != principal.provider:
        raise HTTPException(status_code=403, detail="Runner provider scope mismatch")
    if attempt_id is not None and str(attempt_id or "").strip() != principal.attempt_id:
        raise HTTPException(status_code=403, detail="Runner attempt scope mismatch")
    if add_flow is not None and bool(add_flow) != principal.add_flow:
        raise HTTPException(status_code=403, detail="Runner flow scope mismatch")
    if purpose is not None and str(purpose or "").strip().lower() != principal.purpose:
        raise HTTPException(status_code=403, detail="Runner purpose scope mismatch")
    return principal


def _extract_bearer(scope: Scope) -> str:
    headers = scope.get("headers") or []
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1:
        return ""
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return ""
    if not value.startswith(BEARER_PREFIX):
        return ""
    token = value[len(BEARER_PREFIX) :]
    try:
        return validate_token(token)
    except ValueError:
        return ""


def _is_runner_exchange(path: str, method: str) -> bool:
    return path == "/api/auth/runner-session" and method == "POST"


def _is_desktop_only(path: str, method: str) -> bool:
    return method == "POST" and path in {
        "/api/auth/launch/revoke",
        "/api/auth/runner-grants",
        "/api/auth/runner-sessions/revoke",
    }


def _is_runner_only(path: str, method: str) -> bool:
    if path == "/api/auth/runner-credentials" and method == "GET":
        return True
    if path.startswith("/api/sync/runtime-artifact/") and method == "GET":
        return True
    if path in {
        "/api/sync/visible-auth-artifact",
        "/api/sync/visible-auth-credentials",
    } and method == "POST":
        return True
    if path == "/api/auth/runner-session/revoke" and method == "POST":
        return True
    return False


def _runner_session_path_allowed(path: str, method: str) -> bool:
    return _is_runner_only(path, method) or (
        path == "/api/settings/support-logs/client-event" and method == "POST"
    )


def _authenticate(scope: Scope) -> AuthPrincipal | None:
    token = _extract_bearer(scope)
    if not token:
        return None
    path = str(scope.get("path") or "")
    method = str(scope.get("method") or "").upper()

    # Refresh the launch generation before accepting *any* credential. This makes
    # token-file rotation revoke outstanding runner grants/sessions even when the
    # first post-rotation request presents only a runner token.
    launch_tokens = _launch_token_source.read()
    _accept_launch_generation(launch_tokens.generation)
    if _launch_generation_is_revoked(launch_tokens.generation):
        return None

    if not _is_runner_exchange(path, method) and not _is_runner_only(path, method):
        if secrets.compare_digest(token, launch_tokens.desktop):
            return AuthPrincipal(
                kind="desktop",
                user_id=1,
                launch_generation=launch_tokens.generation,
            )
        if not _is_desktop_only(path, method) and secrets.compare_digest(
            token,
            launch_tokens.renderer,
        ):
            return AuthPrincipal(
                kind="renderer",
                user_id=1,
                launch_generation=launch_tokens.generation,
            )

    digest = _token_digest(token)
    now = _clock()
    with _runner_lock:
        _purge_expired_locked(now)
        if _is_runner_exchange(path, method):
            record = _runner_grants.pop(digest, None)
            return (
                record.principal
                if record and record.launch_generation == (_active_launch_generation or 0)
                else None
            )
        if _runner_session_path_allowed(path, method):
            record = _runner_sessions.get(digest)
            return (
                record.principal
                if record and record.launch_generation == (_active_launch_generation or 0)
                else None
            )
    return None


class LaunchAuthMiddleware:
    """Authenticate every actual API operation before routing.

    Browser CORS preflight is infrastructure rather than an API operation, so OPTIONS
    is passed through. CORSMiddleware must wrap this middleware so its headers are
    also present on authentication failures.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "").upper()
        # Moomoo returns an OAuth authorization code by ordinary browser navigation,
        # which cannot carry the renderer bearer token. A high-entropy, one-use,
        # short-lived state value authenticates this exact callback instead.
        if path == "/api/auth/moomoo/oauth/callback" and method == "GET":
            await self.app(scope, receive, send)
            return
        if scope.get("type") != "http" or not path.startswith("/api") or method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        try:
            principal = _authenticate(scope)
        except RuntimeError as exc:
            logger.error("launch authentication is unavailable: %s", exc)
            response = JSONResponse(
                status_code=503,
                content={"detail": "Launch authentication is unavailable"},
            )
            await response(scope, receive, send)
            return
        if principal is None:
            response = JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["auth_principal"] = principal
        await self.app(scope, receive, send)


def reset_auth_state_for_tests() -> None:
    global _active_launch_generation, _revoked_launch_generation
    _launch_token_source.reset()
    with _launch_generation_lock:
        with _runner_lock:
            _runner_grants.clear()
            _runner_sessions.clear()
        _active_launch_generation = None
        _revoked_launch_generation = None
