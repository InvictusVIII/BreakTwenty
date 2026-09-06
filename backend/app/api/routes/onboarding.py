from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.models import Setting

router = APIRouter()

ONBOARDING_COMPLETED_SETTING = "onboarding_completed"


async def _get_setting(db: AsyncSession, user_id: int, key: str) -> Setting | None:
    return (
        await db.execute(
            select(Setting).where(Setting.user_id == user_id, Setting.key == key)
        )
    ).scalar_one_or_none()


@router.get("/onboarding/status")
async def onboarding_status(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    setting = await _get_setting(db, current_user.id, ONBOARDING_COMPLETED_SETTING)
    # The setting's presence (a completion timestamp) means onboarding is done.
    completed = setting is not None and bool(str(setting.value or "").strip())
    return {"completed": completed}


@router.post("/onboarding/complete")
async def onboarding_complete(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Finish first-run onboarding: mark it complete so the welcome screen doesn't
    reappear. Opening cash balances aren't captured here — the Cash holder is
    provisioned lazily on first cash activity and its starting balance is set from the
    Cash tile; the ATM cash-tracking cutoff is now per-institution (set when each
    institution is connected), not a global onboarding setting."""
    user_id = current_user.id

    setting = await _get_setting(db, user_id, ONBOARDING_COMPLETED_SETTING)
    if setting is None:
        db.add(
            Setting(
                user_id=user_id,
                key=ONBOARDING_COMPLETED_SETTING,
                value=datetime.now(timezone.utc).isoformat(),
            )
        )
    else:
        setting.value = datetime.now(timezone.utc).isoformat()
    await db.commit()
    return {"completed": True}
