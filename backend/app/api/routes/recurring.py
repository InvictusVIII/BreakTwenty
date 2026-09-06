"""Recurring series API.

User steering for recurring series detected by ``app.services.recurrence``.
Dismissal is durable and survives re-detection. The series list itself is
served inside the ``/cash-flow`` payload, not from here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.models import RecurringSeries


router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _owned_series(db: AsyncSession, user_id: int, series_id: int) -> RecurringSeries:
    series = (
        await db.execute(
            select(RecurringSeries).where(
                RecurringSeries.id == series_id,
                RecurringSeries.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Recurring series not found")
    return series


@router.post("/recurring/{series_id}/dismiss")
async def dismiss_series(
    series_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    series = await _owned_series(db, current_user.id, series_id)
    series.status = "dismissed"
    series.updated_at = _now()
    await db.commit()
    return {"status": "ok", "id": series_id, "series_status": series.status}
