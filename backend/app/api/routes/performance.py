from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, _parse_csv_ints, _parse_iso_date, get_current_user, get_db
from app.services.portfolio_performance import TIMELINE_DAY_COUNTS, get_portfolio_performance_payload

router = APIRouter()


@router.get("/investments/performance")
async def get_investments_performance(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    account_ids: str | None = Query(None),
    timeline: str = Query("ALL"),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    selected_account_ids = _parse_csv_ints(account_ids)
    normalized_timeline = str(timeline or "").strip().upper()
    if normalized_timeline not in {*TIMELINE_DAY_COUNTS, "YTD", "ALL", "CUSTOM"}:
        raise HTTPException(status_code=422, detail="Unsupported performance timeline")
    _parse_iso_date(start_date, field_name="start_date")
    _parse_iso_date(end_date, field_name="end_date")
    return await get_portfolio_performance_payload(
        db,
        current_user.id,
        account_ids=selected_account_ids or None,
        timeline=normalized_timeline,
        start_date=start_date,
        end_date=end_date,
    )
