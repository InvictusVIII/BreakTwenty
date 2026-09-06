from datetime import date

from fastapi import APIRouter, HTTPException

from app.auth import CurrentUser as CurrentUser, get_current_user as get_current_user
from app.database import async_session


async def get_db():
    async with async_session() as session:
        yield session


def _parse_csv_ints(raw: str | None) -> list[int]:
    """Parse a bounded comma-separated ID list or reject the whole malformed filter."""
    if raw is None or raw == "":
        return []
    pieces = str(raw).split(",")
    if len(pieces) > 250:
        raise HTTPException(status_code=422, detail="Too many IDs in filter")
    out: list[int] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            raise HTTPException(status_code=422, detail="ID filters cannot contain empty values")
        try:
            value = int(piece)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid integer ID: {piece}") from exc
        if abs(value) > 2_147_483_647:
            raise HTTPException(status_code=422, detail="ID is outside the supported range")
        out.append(value)
    return out


def _parse_csv_strings(
    raw: str | None,
    *,
    field_name: str,
    max_items: int = 50,
    max_item_length: int = 64,
) -> list[str]:
    """Parse a bounded comma-separated string filter or reject malformed input."""
    if raw is None or raw == "":
        return []
    pieces = raw.split(",")
    if len(pieces) > max_items:
        raise HTTPException(status_code=422, detail=f"Too many {field_name} values")
    values: list[str] = []
    for piece in pieces:
        value = piece.strip()
        if not value:
            raise HTTPException(status_code=422, detail=f"{field_name} cannot contain empty values")
        if len(value) > max_item_length:
            raise HTTPException(status_code=422, detail=f"{field_name} value is too long")
        values.append(value)
    return values


def _parse_iso_date(raw: str | None, *, field_name: str = "date") -> date | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or raw != raw.strip() or len(raw) != 10:
        raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD") from exc


router = APIRouter(prefix="/api")

from .accounts import router as accounts_router  # noqa: E402
from .auth_sessions import router as auth_sessions_router  # noqa: E402
from .categories import router as categories_router  # noqa: E402
from .cash_flow import router as cash_flow_router  # noqa: E402
from .events import router as events_router  # noqa: E402
from .exports import router as exports_router  # noqa: E402
from .health import router as health_router  # noqa: E402
from .imports import router as imports_router  # noqa: E402
from .institutions import router as institutions_router  # noqa: E402
from .market_data import router as market_data_router  # noqa: E402
from .moomoo_oauth import router as moomoo_oauth_router  # noqa: E402
from .onboarding import router as onboarding_router  # noqa: E402
from .performance import router as performance_router  # noqa: E402
from .recurring import router as recurring_router  # noqa: E402
from .settings import router as settings_router  # noqa: E402
from .sync import router as sync_router  # noqa: E402
from .transactions import router as transactions_router  # noqa: E402


router.include_router(accounts_router)
router.include_router(auth_sessions_router)
router.include_router(institutions_router)
router.include_router(settings_router)
router.include_router(sync_router)
router.include_router(health_router)
router.include_router(transactions_router)
router.include_router(categories_router)
router.include_router(cash_flow_router)
router.include_router(imports_router)
router.include_router(exports_router)
router.include_router(performance_router)
router.include_router(market_data_router)
router.include_router(moomoo_oauth_router)
router.include_router(events_router)
router.include_router(recurring_router)
router.include_router(onboarding_router)
