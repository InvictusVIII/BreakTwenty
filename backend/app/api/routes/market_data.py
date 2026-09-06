from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.services.market_strip import (
    get_market_strip_payload,
    save_market_strip_watchlist,
    search_market_strip_symbols,
)
from app.services.market_news import get_market_news_payload

router = APIRouter()


@router.get("/market-data/strip")
async def get_market_data_strip(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_market_strip_payload(db, current_user.id)


@router.put("/market-data/strip/watchlist")
async def save_market_data_strip_watchlist(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    items = await save_market_strip_watchlist(db, current_user.id, body.get("items"))
    return {"status": "ok", "watchlist": items}


@router.get("/market-data/search")
async def search_market_data_symbols(
    q: str = "",
    current_user: CurrentUser = Depends(get_current_user),
):
    return {"status": "ok", "results": search_market_strip_symbols(q)}


@router.get("/market-data/news")
async def get_market_data_news(
    current_user: CurrentUser = Depends(get_current_user),
):
    _ = current_user
    return await get_market_news_payload()
