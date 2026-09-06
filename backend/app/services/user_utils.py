from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import DEFAULT_DEV_USER_ID
from app.database import async_session
from app.models import Setting, User


DEFAULT_USER_TIMEZONE = "UTC"


def normalize_user_timezone(value: object, default: str = DEFAULT_USER_TIMEZONE) -> str:
    candidate = str(value or "").strip() or default
    try:
        ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError):
        fallback = str(default or DEFAULT_USER_TIMEZONE).strip() or DEFAULT_USER_TIMEZONE
        try:
            ZoneInfo(fallback)
        except (ValueError, ZoneInfoNotFoundError):
            return DEFAULT_USER_TIMEZONE
        return fallback
    return candidate


def get_timezone_info(value: object, default: str = DEFAULT_USER_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(normalize_user_timezone(value, default=default))


async def ensure_default_dev_user(db: AsyncSession) -> User:
    user = await db.get(User, DEFAULT_DEV_USER_ID)
    if user is None:
        user = User(id=DEFAULT_DEV_USER_ID, email=None)
        db.add(user)
        await db.flush()
    # Idempotent: no-op if the user already has seeded system categories.
    from app.services.categories import seed_default_categories_for_user
    await seed_default_categories_for_user(db, user.id)
    # The Cash holder is provisioned lazily on first cash activity (so a brand-new
    # account list shows no Cash tile until the user adds cash), and the ATM
    # cash-tracking cutoff is now per-institution — nothing to seed at bootstrap.
    return user


async def ensure_default_dev_user_exists() -> None:
    async with async_session() as db:
        user = await db.get(User, DEFAULT_DEV_USER_ID)
        if user is None:
            await ensure_default_dev_user(db)
        else:
            # Prune any vestigial empty Cash holder left by earlier eager provisioning
            # (the holder is now created lazily on first cash activity). Idempotent, so
            # a single-user desktop install self-heals on the next launch.
            from app.services.manual_accounts import prune_empty_wallet_cash_accounts
            await prune_empty_wallet_cash_accounts(db, user.id)
        await db.commit()


async def get_user_timezone_from_db(
    db: AsyncSession,
    user_id: int,
    default: str = DEFAULT_USER_TIMEZONE,
) -> str:
    result = await db.execute(
        select(Setting.value).where(
            Setting.user_id == user_id,
            Setting.key == "user_timezone",
        )
    )
    return normalize_user_timezone(result.scalar_one_or_none(), default=default)


async def get_user_timezone_info_from_db(
    db: AsyncSession,
    user_id: int,
    default: str = DEFAULT_USER_TIMEZONE,
) -> ZoneInfo:
    return get_timezone_info(
        await get_user_timezone_from_db(db, user_id, default=default),
        default=default,
    )


async def get_user_local_today_from_db(
    db: AsyncSession,
    user_id: int,
    default: str = DEFAULT_USER_TIMEZONE,
    now: datetime | None = None,
) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    user_tz = await get_user_timezone_info_from_db(db, user_id, default=default)
    return current.astimezone(user_tz).date()


async def get_user_timezone_info(user_id: int, default: str = DEFAULT_USER_TIMEZONE) -> ZoneInfo:
    async with async_session() as db:
        return await get_user_timezone_info_from_db(db, user_id, default=default)


async def list_scheduler_users() -> list[tuple[int, str]]:
    async with async_session() as db:
        result = await db.execute(
            select(User.id, Setting.value)
            .outerjoin(
                Setting,
                and_(Setting.user_id == User.id, Setting.key == "user_timezone"),
            )
            .order_by(User.id)
        )
        rows = result.all()
        if rows:
            return [
                (user_id, normalize_user_timezone(timezone_value, default=DEFAULT_USER_TIMEZONE))
                for user_id, timezone_value in rows
            ]

        await ensure_default_dev_user(db)
        await db.commit()
        return [(DEFAULT_DEV_USER_ID, DEFAULT_USER_TIMEZONE)]
