from __future__ import annotations

from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Institution, TransactionImportJob
from app.provider_catalog import get_provider_display_name, get_status_provider_for_result
from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.services.event_bus import EVENT_SYNC_ACTIVITY, mark_dirty

ACTIVE_SYNC_STATUSES = frozenset({"queued", "running"})

_activity_lock = Lock()
_activities: dict[str, dict[str, Any]] = {}


def _status_provider(provider: str) -> str:
    try:
        return _normalize_provider(get_status_provider_for_result(provider))
    except Exception:
        return _normalize_provider(provider)


def set_provider_activity(
    user_id: int,
    provider: str,
    *,
    activity_id: str | None = None,
    status: str = "running",
    institution_id: int | None = None,
) -> str:
    normalized_provider = _status_provider(provider)
    normalized_status = str(status or "running").strip().lower()
    resolved_activity_id = activity_id or f"activity-{uuid4().hex[:12]}"
    with _activity_lock:
        _activities[resolved_activity_id] = {
            "activity_id": resolved_activity_id,
            "user_id": int(user_id),
            "provider": normalized_provider,
            "institution_id": int(institution_id) if institution_id is not None else None,
            "kind": "accounts_sync",
            "status": normalized_status,
        }
    mark_dirty(int(user_id), EVENT_SYNC_ACTIVITY)
    return resolved_activity_id


def clear_provider_activity(activity_id: str | None) -> None:
    if not activity_id:
        return
    removed_user_id: int | None = None
    with _activity_lock:
        existing = _activities.pop(str(activity_id), None)
        if existing:
            try:
                removed_user_id = int(existing.get("user_id") or 0) or None
            except (TypeError, ValueError):
                removed_user_id = None
    if removed_user_id is not None:
        mark_dirty(removed_user_id, EVENT_SYNC_ACTIVITY)


def _memory_activities(user_id: int) -> list[dict[str, Any]]:
    with _activity_lock:
        return [
            dict(activity)
            for activity in _activities.values()
            if int(activity.get("user_id") or 0) == int(user_id)
            and str(activity.get("status") or "").lower() in ACTIVE_SYNC_STATUSES
        ]


def _provider_label(provider: str) -> str:
    try:
        return get_provider_display_name(provider)
    except Exception:
        return provider


def _activity_payload(
    activity: dict[str, Any],
    *,
    institution_by_id: dict[int, Institution],
) -> dict[str, Any]:
    provider = _normalize_provider(activity.get("provider"))
    institution_id = activity.get("institution_id")
    institution = institution_by_id.get(int(institution_id)) if institution_id else None
    label = institution.name if institution and institution.name else _provider_label(provider)

    return {
        "activity_id": activity.get("activity_id"),
        "provider": provider,
        "institution_id": int(institution_id) if institution_id else None,
        "label": label,
        "kind": activity.get("kind") or "accounts_sync",
        "status": activity.get("status") or "running",
    }


def _activity_priority(activity: dict[str, Any]) -> tuple[int, int]:
    kind = str(activity.get("kind") or "")
    status = str(activity.get("status") or "")
    kind_priority = 2 if kind == "accounts_sync" else 1
    status_priority = 2 if status in {"running", "syncing"} else 1
    return kind_priority, status_priority


async def get_sync_activity_payload(db: AsyncSession, user_id: int) -> dict[str, Any]:
    institutions = (
        await db.execute(
            select(Institution).where(Institution.user_id == user_id)
        )
    ).scalars().all()
    institution_by_id = {int(institution.id): institution for institution in institutions}
    active_items = [
        _activity_payload(
            activity,
            institution_by_id=institution_by_id,
        )
        for activity in _memory_activities(user_id)
    ]

    transaction_jobs = (
        await db.execute(
            select(TransactionImportJob).where(
                TransactionImportJob.user_id == user_id,
                TransactionImportJob.status.in_(ACTIVE_SYNC_STATUSES),
            )
        )
    ).scalars().all()
    for job in transaction_jobs:
        provider = _status_provider(job.provider)
        institution = None
        if job.institution_id:
            candidate = institution_by_id.get(int(job.institution_id or 0))
            if candidate is not None and _normalize_provider(candidate.provider) == provider:
                institution = candidate
        label = institution.name if institution else _provider_label(provider)
        active_items.append(
            {
                "activity_id": f"transaction-import:{job.job_id}",
                "provider": provider,
                "institution_id": int(job.institution_id) if job.institution_id else None,
                "label": label,
                "kind": "transaction_import",
                "status": job.status,
            }
        )

    by_connection: dict[tuple[str, int | None], dict[str, Any]] = {}
    for activity in active_items:
        provider = _normalize_provider(activity.get("provider"))
        if not provider:
            continue
        connection_key = (provider, activity.get("institution_id"))
        existing = by_connection.get(connection_key)
        if not existing or _activity_priority(activity) > _activity_priority(existing):
            by_connection[connection_key] = activity

    active = sorted(by_connection.values(), key=lambda item: str(item.get("label") or item.get("provider") or ""))
    return {"active": active}
