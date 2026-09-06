from __future__ import annotations

from threading import Lock
from typing import Any
from uuid import uuid4

from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.connectors.sync_context import get_connector_sync_context
from app.services.time_utils import utc_now as _utc_now

PENDING_SYNC_STATUSES = frozenset({"2fa_required", "waiting"})

_sync_attempt_lock = Lock()
_sync_attempts: dict[tuple[int, str, int | None], dict[str, Any]] = {}


def _institution_id(institution_id: int | None = None) -> int | None:
    if institution_id is not None:
        return int(institution_id)
    context = get_connector_sync_context()
    return int(context.institution_id) if context and context.institution_id is not None else None


def _normalize_sync_id(sync_id: str | None) -> str:
    return str(sync_id or "").strip()


def generate_sync_id(provider: str | None = None) -> str:
    prefix = _normalize_provider(provider) or "sync"
    return f"{prefix}-{uuid4().hex[:10]}"


def start_provider_sync_attempt(
    user_id: int,
    provider: str,
    *,
    sync_id: str | None = None,
    institution_id: int | None = None,
) -> dict[str, Any]:
    normalized_provider = _normalize_provider(provider)
    attempt_sync_id = _normalize_sync_id(sync_id) or generate_sync_id(normalized_provider)
    now_iso = _utc_now().isoformat()
    attempt = {
        "sync_id": attempt_sync_id,
        "started_at": now_iso,
        "updated_at": now_iso,
        "status": "started",
    }
    with _sync_attempt_lock:
        _sync_attempts[(user_id, normalized_provider, _institution_id(institution_id))] = dict(attempt)
    return dict(attempt)


def ensure_provider_sync_attempt(
    user_id: int,
    provider: str,
    *,
    sync_id: str | None = None,
    institution_id: int | None = None,
) -> dict[str, Any]:
    normalized_provider = _normalize_provider(provider)
    normalized_sync_id = _normalize_sync_id(sync_id)
    with _sync_attempt_lock:
        existing = _sync_attempts.get((user_id, normalized_provider, _institution_id(institution_id)))
        if existing and not normalized_sync_id:
            existing["updated_at"] = _utc_now().isoformat()
            return dict(existing)
        if existing and normalized_sync_id and existing.get("sync_id") == normalized_sync_id:
            existing["updated_at"] = _utc_now().isoformat()
            return dict(existing)
    return start_provider_sync_attempt(
        user_id,
        normalized_provider,
        sync_id=normalized_sync_id,
        institution_id=institution_id,
    )


def get_provider_sync_attempt(
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> dict[str, Any] | None:
    normalized_provider = _normalize_provider(provider)
    with _sync_attempt_lock:
        attempt = _sync_attempts.get((user_id, normalized_provider, _institution_id(institution_id)))
        return dict(attempt) if attempt else None


def get_provider_sync_id(
    user_id: int,
    provider: str,
    *,
    institution_id: int | None = None,
) -> str | None:
    attempt = get_provider_sync_attempt(user_id, provider, institution_id=institution_id)
    return attempt.get("sync_id") if attempt else None


def list_user_sync_attempt_ids(user_id: int) -> set[str]:
    with _sync_attempt_lock:
        return {
            str(attempt.get("sync_id") or "").strip()
            for (attempt_user_id, _provider, _institution_id), attempt in _sync_attempts.items()
            if int(attempt_user_id) == int(user_id) and str(attempt.get("sync_id") or "").strip()
        }


def finalize_provider_sync_attempt(
    user_id: int,
    provider: str,
    *,
    sync_id: str | None = None,
    status: str | None = None,
    institution_id: int | None = None,
) -> str | None:
    normalized_provider = _normalize_provider(provider)
    normalized_sync_id = _normalize_sync_id(sync_id)
    normalized_status = str(status or "").strip().lower()
    with _sync_attempt_lock:
        key = (user_id, normalized_provider, _institution_id(institution_id))
        current = _sync_attempts.get(key)
        if normalized_status in PENDING_SYNC_STATUSES:
            if not current:
                return None
            if normalized_sync_id and current.get("sync_id") != normalized_sync_id:
                return current.get("sync_id")
            current["status"] = normalized_status
            current["updated_at"] = _utc_now().isoformat()
            return current.get("sync_id")

        # Desktop visible-auth support events start before the backend knows the
        # owned institution ID, so they are tracked under the provider's
        # unscoped key. The subsequent connector phase uses the institution key.
        # A terminal sync_id is the shared attempt identity: clear every matching
        # scoped/unscoped record or the completed attempt stays falsely active and
        # its finalized diagnostics pill remains hidden forever.
        if normalized_sync_id:
            matching_keys = [
                attempt_key
                for attempt_key, attempt in _sync_attempts.items()
                if attempt_key[0] == user_id
                and attempt_key[1] == normalized_provider
                and attempt.get("sync_id") == normalized_sync_id
            ]
            for matching_key in matching_keys:
                _sync_attempts.pop(matching_key, None)
            if matching_keys:
                return normalized_sync_id
            return current.get("sync_id") if current else None

        if not current:
            return None
        completed_sync_id = current.get("sync_id")
        _sync_attempts.pop(key, None)
        return completed_sync_id
