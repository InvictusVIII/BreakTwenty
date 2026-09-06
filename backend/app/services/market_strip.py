from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import MarketStripSnapshotPoint, RuntimeServiceLease, Setting, User
from app.services.market_data_logging import safe_market_data_error_message
from app.services.market_data_provider import (
    fetch_market_data_daily_series,
    fetch_market_data_intraday_series,
    get_with_timeout_retry,
    market_data_error_code,
)
from app.services.market_data_settings import (
    MARKET_DATA_MODE_KEYLESS,
    MARKET_DATA_PROVIDER_FMP,
    MARKET_DATA_PROVIDER_POLYGON,
    MARKET_DATA_PROVIDER_TWELVE_DATA,
    get_effective_market_data_config,
)
from app.services.runtime_service_leases import (
    RuntimeLeaseOwnership,
    acquire_runtime_service_lease,
    release_runtime_service_lease,
    renew_runtime_service_lease,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.task_supervisor import create_tracked_task
from app.services.user_utils import get_user_timezone_info_from_db

logger = logging.getLogger("breaktwenty.market_strip")

FRED_GRAPH_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
COINGECKO_BITCOIN_MARKET_CHART_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
MARKET_STRIP_CACHE_TTL = timedelta(minutes=15)
MARKET_STRIP_INTRADAY_DENIAL_TTL = timedelta(days=1)
MARKET_STRIP_PROVIDER_DENIAL_TTL = timedelta(days=1)
MARKET_STRIP_WATCHLIST_SETTING = "market_strip_watchlist"
MARKET_STRIP_MAX_TILES = 7
CUSTOM_SYMBOL_PREFIX = "custom:"
MARKET_STRIP_SAMPLER_SERVICE_NAME = "market-strip-sampler"
MARKET_STRIP_SAMPLER_INTERVAL_SECONDS = 15 * 60.0
MARKET_STRIP_SAMPLER_LEASE_DURATION = timedelta(seconds=90)
MARKET_STRIP_SAMPLER_LEASE_RENEW_SECONDS = 30.0
MARKET_STRIP_SAMPLER_LEASE_RETRY_SECONDS = 5.0
MARKET_STRIP_SAMPLER_RENEW_ERROR_RETRY_SECONDS = 2.0
MARKET_STRIP_PROVIDER_BACKOFF_STATUSES = {401, 403, 429}
MARKET_STRIP_MARKET_TZ = ZoneInfo("America/New_York")
MARKET_STRIP_PROVIDER_FETCH_START_MINUTE = 9 * 60 + 30
MARKET_STRIP_PROVIDER_FETCH_END_MINUTE = 16 * 60 + 15


@dataclass(frozen=True)
class MarketStripTileConfig:
    id: str
    label: str
    provider_symbols: dict[str, str]
    fred_symbol: str | None = None
    keyless_source: str | None = None
    precision: int = 2


@dataclass(frozen=True)
class MarketStripSnapshotCandidate:
    user_id: int
    provider: str
    source: str
    mode: str
    symbol_key: str
    local_date: date
    sample_slot: str
    close: Decimal
    observed_at: datetime


@dataclass
class _MarketStripSamplerLeaseState:
    ownership: RuntimeLeaseOwnership
    lost: asyncio.Event
    confirmed_deadline: float = float("inf")


MARKET_STRIP_TILES = (
    MarketStripTileConfig(
        id="sp500",
        label="S&P 500",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "^GSPC",
            MARKET_DATA_PROVIDER_POLYGON: "I:SPX",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "SPX",
        },
        fred_symbol="SP500",
    ),
    MarketStripTileConfig(
        id="dow",
        label="DOW",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "^DJI",
            MARKET_DATA_PROVIDER_POLYGON: "I:DJI",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "DJI",
        },
        fred_symbol="DJIA",
    ),
    MarketStripTileConfig(
        id="nasdaq",
        label="NASDAQ",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "^IXIC",
            MARKET_DATA_PROVIDER_POLYGON: "I:COMP",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "IXIC",
        },
        fred_symbol="NASDAQCOM",
    ),
    MarketStripTileConfig(
        id="russell2000",
        label="RUSSELL 2000",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "^RUT",
            MARKET_DATA_PROVIDER_POLYGON: "I:RUT",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "RUT",
        },
    ),
    MarketStripTileConfig(
        id="volatility",
        label="VOLATILITY",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "^VIX",
            MARKET_DATA_PROVIDER_POLYGON: "I:VIX",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "VIX",
        },
        fred_symbol="VIXCLS",
    ),
    MarketStripTileConfig(
        id="gold",
        label="GOLD",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "GCUSD",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "XAU/USD",
        },
    ),
    MarketStripTileConfig(
        id="bitcoin",
        label="Bitcoin USD",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "BTCUSD",
            MARKET_DATA_PROVIDER_POLYGON: "X:BTCUSD",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "BTC/USD",
        },
        keyless_source="coingecko",
    ),
    MarketStripTileConfig(
        id="usd_cad",
        label="USD/CAD",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "USDCAD",
            MARKET_DATA_PROVIDER_POLYGON: "C:USDCAD",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "USD/CAD",
        },
        fred_symbol="DEXCAUS",
        precision=4,
    ),
    MarketStripTileConfig(
        id="crude_oil",
        label="Crude Oil",
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: "CLUSD",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "WTI/USD",
        },
        fred_symbol="DCOILWTICO",
    ),
)
MARKET_STRIP_TILE_BY_ID = {tile.id: tile for tile in MARKET_STRIP_TILES}
DEFAULT_MARKET_STRIP_WATCHLIST = ("sp500", "nasdaq", "dow", "usd_cad", "bitcoin")
KEYLESS_MARKET_STRIP_WATCHLIST = DEFAULT_MARKET_STRIP_WATCHLIST
KEYLESS_MARKET_STRIP_TILE_IDS = frozenset(KEYLESS_MARKET_STRIP_WATCHLIST)

_market_strip_cache: dict[str, tuple[datetime, dict]] = {}
_market_strip_tile_cache: dict[str, tuple[datetime, dict]] = {}
_market_strip_intraday_denials: dict[str, datetime] = {}
_market_strip_provider_denials: dict[str, datetime] = {}
_market_strip_sampler_task: asyncio.Task[Any] | None = None


def _cache_key(
    user_id: int,
    provider: str,
    source: str,
    mode: str,
    config_revision: str,
    api_key: str,
    today: date,
    watchlist_key: str,
) -> str:
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12] if api_key else "no-key"
    revision = config_revision or "0"
    return f"{int(user_id)}:{provider}:{source}:{mode}:{revision}:{key_hash}:{today.isoformat()}:{watchlist_key}"


def _tile_cache_key(
    user_id: int,
    provider: str,
    source: str,
    mode: str,
    config_revision: str,
    api_key: str,
    today: date,
    item: dict,
) -> str:
    return _cache_key(
        user_id,
        provider,
        source,
        mode,
        config_revision,
        api_key,
        today,
        _watchlist_key([item]),
    )


def _intraday_denial_key(provider: str, api_key: str) -> str:
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"{provider}:{key_hash}"


def _provider_denial_key(provider: str, api_key: str) -> str:
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"{provider}:{key_hash}"


def _is_provider_denied(provider: str, api_key: str) -> bool:
    denied_at = _market_strip_provider_denials.get(_provider_denial_key(provider, api_key))
    if denied_at is None:
        return False
    if datetime.now(timezone.utc) - denied_at <= MARKET_STRIP_PROVIDER_DENIAL_TTL:
        return True
    _market_strip_provider_denials.pop(_provider_denial_key(provider, api_key), None)
    return False


def _mark_provider_denied(provider: str, api_key: str) -> None:
    _market_strip_provider_denials[_provider_denial_key(provider, api_key)] = datetime.now(timezone.utc)


def _is_intraday_denied(provider: str, api_key: str) -> bool:
    denied_at = _market_strip_intraday_denials.get(_intraday_denial_key(provider, api_key))
    if denied_at is None:
        return False
    if datetime.now(timezone.utc) - denied_at <= MARKET_STRIP_INTRADAY_DENIAL_TTL:
        return True
    _market_strip_intraday_denials.pop(_intraday_denial_key(provider, api_key), None)
    return False


def _mark_intraday_denied(provider: str, api_key: str) -> None:
    _market_strip_intraday_denials[_intraday_denial_key(provider, api_key)] = datetime.now(timezone.utc)


def _is_intraday_plan_denial(exc: Exception) -> bool:
    return _provider_error_code(exc) == 402


def _provider_error_code(exc: Exception) -> int | None:
    return market_data_error_code(exc)


def _is_market_strip_provider_fetch_window(now: datetime | None = None) -> bool:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    market_now = value.astimezone(MARKET_STRIP_MARKET_TZ)
    if market_now.weekday() >= 5:
        return False
    minute = market_now.hour * 60 + market_now.minute
    return MARKET_STRIP_PROVIDER_FETCH_START_MINUTE <= minute <= MARKET_STRIP_PROVIDER_FETCH_END_MINUTE


def _snapshot_sample_slot(now: datetime) -> str:
    minute = (now.minute // 15) * 15
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()


def _snapshot_symbol_key(item: dict, tile: MarketStripTileConfig) -> str:
    item_id = str(item.get("id") or "").strip()
    return item_id or tile.id


async def _load_snapshot_series(
    db: AsyncSession,
    user_id: int,
    *,
    provider: str,
    source: str,
    mode: str,
    symbol_key: str,
    today: date,
) -> list[dict] | None:
    result = await db.execute(
        select(MarketStripSnapshotPoint)
        .where(
            MarketStripSnapshotPoint.user_id == user_id,
            MarketStripSnapshotPoint.provider == provider,
            MarketStripSnapshotPoint.source == source,
            MarketStripSnapshotPoint.mode == mode,
            MarketStripSnapshotPoint.symbol_key == symbol_key,
            MarketStripSnapshotPoint.local_date == today,
        )
        .order_by(MarketStripSnapshotPoint.observed_at.asc())
    )
    points = result.scalars().all()
    return [{"date": point.observed_at.isoformat(), "close": point.close} for point in points]


def _snapshot_candidate(
    user_id: int,
    *,
    provider: str,
    source: str,
    mode: str,
    symbol_key: str,
    reference: dict | None,
    latest: dict,
    today: date,
    now: datetime,
) -> MarketStripSnapshotCandidate | None:
    if not reference or str(latest.get("date") or "")[:10] != today.isoformat():
        return None
    return MarketStripSnapshotCandidate(
        user_id=int(user_id),
        provider=provider,
        source=source,
        mode=mode,
        symbol_key=symbol_key,
        local_date=today,
        sample_slot=_snapshot_sample_slot(now),
        close=Decimal(str(latest["close"])),
        observed_at=now,
    )


def _parse_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_custom_symbol(value: str | None) -> str:
    return str(value or "").strip().upper()[:32]


def _custom_tile_id(symbol: str) -> str:
    return f"{CUSTOM_SYMBOL_PREFIX}{symbol}"


def _custom_tile_from_symbol(symbol: str, label: str | None = None) -> MarketStripTileConfig:
    normalized_symbol = _normalize_custom_symbol(symbol)
    display_label = str(label or normalized_symbol).strip()[:48] or normalized_symbol
    keyless_source = "coingecko" if normalized_symbol in {"BTC", "BTCUSD", "BTC/USD", "X:BTCUSD"} else None
    fred_symbol = None
    provider_symbol = normalized_symbol
    if normalized_symbol.startswith("FRED:"):
        fred_symbol = normalized_symbol.split(":", 1)[1].strip()
        provider_symbol = fred_symbol
        display_label = display_label if label else fred_symbol

    return MarketStripTileConfig(
        id=_custom_tile_id(normalized_symbol),
        label=display_label,
        provider_symbols={
            MARKET_DATA_PROVIDER_FMP: provider_symbol,
            MARKET_DATA_PROVIDER_POLYGON: provider_symbol,
            MARKET_DATA_PROVIDER_TWELVE_DATA: provider_symbol,
        },
        fred_symbol=fred_symbol,
        keyless_source=keyless_source,
    )


def _tile_catalog_payload(tile: MarketStripTileConfig) -> dict:
    primary_symbol = (
        tile.provider_symbols.get(MARKET_DATA_PROVIDER_FMP)
        or tile.provider_symbols.get(MARKET_DATA_PROVIDER_TWELVE_DATA)
        or tile.provider_symbols.get(MARKET_DATA_PROVIDER_POLYGON)
        or tile.fred_symbol
    )
    return {
        "id": tile.id,
        "label": tile.label,
        "symbol": primary_symbol,
        "fred_symbol": tile.fred_symbol,
        "custom": tile.id.startswith(CUSTOM_SYMBOL_PREFIX),
    }


def _default_watchlist_items(item_ids: tuple[str, ...] = DEFAULT_MARKET_STRIP_WATCHLIST) -> list[dict]:
    return [{"id": item_id} for item_id in item_ids[:MARKET_STRIP_MAX_TILES]]


def _normalize_watchlist_items(raw_items, default_item_ids: tuple[str, ...] = DEFAULT_MARKET_STRIP_WATCHLIST) -> list[dict]:
    if not isinstance(raw_items, list):
        return _default_watchlist_items(default_item_ids)

    items = []
    seen = set()
    for raw_item in raw_items:
        item = raw_item if isinstance(raw_item, dict) else {"id": raw_item}
        item_id = str(item.get("id") or "").strip()
        symbol = _normalize_custom_symbol(item.get("symbol"))
        label = str(item.get("label") or "").strip()[:48]

        if item_id in MARKET_STRIP_TILE_BY_ID:
            normalized = {"id": item_id}
        elif item_id.startswith(CUSTOM_SYMBOL_PREFIX):
            custom_symbol = _normalize_custom_symbol(item_id[len(CUSTOM_SYMBOL_PREFIX):])
            if not custom_symbol:
                continue
            normalized = {
                "id": _custom_tile_id(custom_symbol),
                "symbol": custom_symbol,
                "label": label or custom_symbol,
            }
        elif symbol:
            normalized = {
                "id": _custom_tile_id(symbol),
                "symbol": symbol,
                "label": label or symbol,
            }
        else:
            continue

        normalized_id = normalized["id"]
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        items.append(normalized)

    return items[:MARKET_STRIP_MAX_TILES] or _default_watchlist_items(default_item_ids)


def _watchlist_key(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _filter_keyless_watchlist(items: list[dict]) -> list[dict]:
    filtered = [
        {"id": item["id"]}
        for item in items
        if item.get("id") in KEYLESS_MARKET_STRIP_TILE_IDS
    ]
    return filtered[:MARKET_STRIP_MAX_TILES] or _default_watchlist_items(KEYLESS_MARKET_STRIP_WATCHLIST)


def _tile_from_watchlist_item(item: dict) -> MarketStripTileConfig | None:
    item_id = str(item.get("id") or "").strip()
    if item_id in MARKET_STRIP_TILE_BY_ID:
        return MARKET_STRIP_TILE_BY_ID[item_id]
    if item_id.startswith(CUSTOM_SYMBOL_PREFIX):
        symbol = _normalize_custom_symbol(item.get("symbol") or item_id[len(CUSTOM_SYMBOL_PREFIX):])
        if symbol:
            return _custom_tile_from_symbol(symbol, item.get("label"))
    return None


async def get_market_strip_watchlist(
    db: AsyncSession,
    user_id: int,
    default_item_ids: tuple[str, ...] = DEFAULT_MARKET_STRIP_WATCHLIST,
) -> list[dict]:
    result = await db.execute(
        select(Setting.value).where(
            Setting.user_id == user_id,
            Setting.key == MARKET_STRIP_WATCHLIST_SETTING,
        )
    )
    raw_value = result.scalar_one_or_none()
    if not raw_value:
        return _default_watchlist_items(default_item_ids)

    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        parsed = None
    return _normalize_watchlist_items(parsed, default_item_ids)


async def save_market_strip_watchlist(db: AsyncSession, user_id: int, raw_items) -> list[dict]:
    market_data_config = await get_effective_market_data_config(db, user_id)
    default_watchlist = (
        KEYLESS_MARKET_STRIP_WATCHLIST
        if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS
        else DEFAULT_MARKET_STRIP_WATCHLIST
    )
    items = _normalize_watchlist_items(raw_items, default_watchlist)
    if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS:
        items = _filter_keyless_watchlist(items)
    result = await db.execute(
        select(Setting).where(
            Setting.user_id == user_id,
            Setting.key == MARKET_STRIP_WATCHLIST_SETTING,
        )
    )
    setting = result.scalar_one_or_none()
    payload = json.dumps(items, separators=(",", ":"))
    if setting:
        setting.value = payload
    else:
        db.add(Setting(user_id=user_id, key=MARKET_STRIP_WATCHLIST_SETTING, value=payload))
    await db.commit()
    return items


def search_market_strip_symbols(query: str) -> list[dict]:
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return [
            _tile_catalog_payload(MARKET_STRIP_TILE_BY_ID[tile_id])
            for tile_id in DEFAULT_MARKET_STRIP_WATCHLIST
            if tile_id in MARKET_STRIP_TILE_BY_ID
        ]

    query_upper = normalized_query.upper()
    results = []
    for tile in MARKET_STRIP_TILES:
        payload = _tile_catalog_payload(tile)
        haystack = " ".join(
            str(value or "")
            for value in (
                payload.get("id"),
                payload.get("label"),
                payload.get("symbol"),
                payload.get("fred_symbol"),
            )
        ).upper()
        if query_upper in haystack:
            results.append(payload)

    results.append(_tile_catalog_payload(_custom_tile_from_symbol(query_upper)))
    return results[:12]


def _build_tile_payload(
    tile: MarketStripTileConfig,
    series: list[dict],
    *,
    source: str,
    source_label: str,
    provider_symbol: str | None,
    snapshot_series: list[dict] | None = None,
    model_daily_session_points: bool = False,
) -> dict:
    clean_series = [
        {"date": str(point["date"]), "close": float(point["close"])}
        for point in sorted(series, key=lambda item: str(item.get("date") or ""))
        if point.get("date") and _parse_float(point.get("close")) is not None
    ]
    if not clean_series:
        return {
            "id": tile.id,
            "label": tile.label,
            "symbol": provider_symbol,
            "value": None,
            "change": None,
            "change_pct": None,
            "points": [],
            "point_mode": "none",
            "reference_value": None,
            "reference_label": None,
            "precision": tile.precision,
            "source": source,
            "source_label": source_label,
            "status": "no_data",
        }

    latest = clean_series[-1]
    is_intraday = any(len(point["date"]) > 10 for point in clean_series)
    reference = clean_series[0] if is_intraday else clean_series[-2] if len(clean_series) > 1 else None
    has_snapshot_series = not is_intraday and bool(snapshot_series) and len(snapshot_series or []) >= 2
    visual_series = (
        clean_series
        if is_intraday
        else snapshot_series
        if has_snapshot_series
        else [point for point in (reference, latest) if point]
        if model_daily_session_points
        else []
    )
    change = None
    change_pct = None
    if reference and reference["close"]:
        change = latest["close"] - reference["close"]
        change_pct = (change / reference["close"]) * 100
    visual_points = [point["close"] for point in visual_series]

    return {
        "id": tile.id,
        "label": tile.label,
        "symbol": provider_symbol,
        "value": latest["close"],
        "change": change,
        "change_pct": change_pct,
        "points": visual_points,
        "point_mode": "intraday" if is_intraday else "snapshot" if has_snapshot_series else "daily",
        "reference_value": reference["close"] if reference else None,
        "reference_label": "Day start" if is_intraday else "Previous close" if reference else None,
        "precision": tile.precision,
        "as_of": latest["date"],
        "source": source,
        "source_label": source_label,
        "status": "ok",
    }


async def _fetch_fred_series(
    client: httpx.AsyncClient,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
) -> list[dict]:
    response = await get_with_timeout_retry(
        client,
        FRED_GRAPH_CSV_URL,
        params={
            "id": symbol,
            "cosd": start_date_value.isoformat(),
            "coed": end_date_value.isoformat(),
        },
    )
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.text))
    series = []
    for row in reader:
        row_date = str(row.get("observation_date") or "").strip()
        close = _parse_float(row.get(symbol))
        if not row_date or close is None:
            continue
        try:
            parsed_date = date.fromisoformat(row_date[:10])
        except ValueError:
            continue
        if start_date_value <= parsed_date <= end_date_value:
            series.append({"date": parsed_date.isoformat(), "close": close})
    return series


async def _fetch_keyless_bitcoin_series(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get(
        COINGECKO_BITCOIN_MARKET_CHART_URL,
        params={"vs_currency": "usd", "days": 1},
        timeout=8,
    )
    response.raise_for_status()
    payload = response.json()
    series = []
    for timestamp, price in payload.get("prices") or []:
        close = _parse_float(price)
        if timestamp is None or close is None:
            continue
        row_time = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
        series.append({"date": row_time.isoformat(), "close": close})
    return series


async def _fetch_provider_series(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
) -> list[dict]:
    return await fetch_market_data_daily_series(
        client,
        provider,
        symbol,
        start_date_value,
        end_date_value,
        api_key,
    )


async def _fetch_provider_intraday_series(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
) -> list[dict]:
    return await fetch_market_data_intraday_series(
        client,
        provider,
        symbol,
        start_date_value,
        end_date_value,
        api_key,
    )


async def _fetch_tile(
    db: AsyncSession | None,
    client: httpx.AsyncClient,
    user_id: int,
    tile: MarketStripTileConfig,
    provider: str,
    provider_label: str,
    api_key: str,
    start_date_value: date,
    end_date_value: date,
    source: str,
    mode: str,
    symbol_key: str | None = None,
    now: datetime | None = None,
    snapshot_candidates: list[MarketStripSnapshotCandidate] | None = None,
) -> dict:
    provider_symbol = tile.provider_symbols.get(provider)
    observation_time = now or datetime.now(timezone.utc)
    provider_fetch_allowed = bool(
        provider_symbol
        and api_key
        and _is_market_strip_provider_fetch_window(observation_time)
        and not _is_provider_denied(provider, api_key)
    )
    if tile.keyless_source == "coingecko":
        try:
            series = await _fetch_keyless_bitcoin_series(client)
            if series:
                return _build_tile_payload(
                    tile,
                    series,
                    source="coingecko",
                    source_label="CoinGecko",
                    provider_symbol="BTC/USD",
                )
        except Exception as exc:
            logger.warning("market strip keyless bitcoin fetch failed: %s", safe_market_data_error_message(exc))

    provider_daily_allowed = True
    if provider_fetch_allowed and not _is_intraday_denied(provider, api_key):
        try:
            series = await _fetch_provider_intraday_series(
                client,
                provider,
                provider_symbol,
                end_date_value - timedelta(days=7),
                end_date_value,
                api_key,
            )
            if series:
                return _build_tile_payload(
                    tile,
                    series,
                    source=provider,
                    source_label=provider_label,
                    provider_symbol=provider_symbol,
                )
            if provider == MARKET_DATA_PROVIDER_TWELVE_DATA:
                provider_daily_allowed = False
        except Exception as exc:
            error_code = _provider_error_code(exc)
            if error_code in MARKET_STRIP_PROVIDER_BACKOFF_STATUSES:
                _mark_provider_denied(provider, api_key)
                provider_daily_allowed = False
            elif _is_intraday_plan_denial(exc):
                _mark_intraday_denied(provider, api_key)
            elif provider == MARKET_DATA_PROVIDER_TWELVE_DATA and error_code != 402:
                provider_daily_allowed = False
            logger.info(
                "market strip intraday provider fetch unavailable provider=%s symbol=%s: %s",
                provider,
                provider_symbol,
                safe_market_data_error_message(exc),
            )

    if provider_fetch_allowed and provider_daily_allowed:
        try:
            series = await _fetch_provider_series(
                client,
                provider,
                provider_symbol,
                start_date_value,
                end_date_value,
                api_key,
            )
            if series:
                clean_series = [
                    {"date": str(point["date"]), "close": float(point["close"])}
                    for point in sorted(series, key=lambda item: str(item.get("date") or ""))
                    if point.get("date") and _parse_float(point.get("close")) is not None
                ]
                reference = clean_series[-2] if len(clean_series) > 1 else None
                observation_time = now or datetime.now(timezone.utc)
                snapshot_series = (
                    await _load_snapshot_series(
                        db,
                        user_id,
                        provider=provider,
                        source=source,
                        mode=mode,
                        symbol_key=symbol_key,
                        today=end_date_value,
                    )
                    if db is not None and symbol_key and clean_series
                    else None
                )
                candidate = (
                    _snapshot_candidate(
                        user_id,
                        provider=provider,
                        source=source,
                        mode=mode,
                        symbol_key=symbol_key,
                        reference=reference,
                        latest=clean_series[-1],
                        today=end_date_value,
                        now=observation_time,
                    )
                    if symbol_key and clean_series
                    else None
                )
                if candidate is not None and snapshot_candidates is not None:
                    snapshot_candidates.append(candidate)
                return _build_tile_payload(
                    tile,
                    series,
                    source=provider,
                    source_label=provider_label,
                    provider_symbol=provider_symbol,
                    snapshot_series=snapshot_series,
                )
        except Exception as exc:
            error_code = _provider_error_code(exc)
            if error_code in MARKET_STRIP_PROVIDER_BACKOFF_STATUSES:
                _mark_provider_denied(provider, api_key)
            logger.warning(
                "market strip provider fetch failed provider=%s symbol=%s: %s",
                provider,
                provider_symbol,
                safe_market_data_error_message(exc),
            )

    if tile.fred_symbol:
        try:
            series = await _fetch_fred_series(client, tile.fred_symbol, start_date_value, end_date_value)
            if series:
                return _build_tile_payload(
                    tile,
                    series,
                    source="fred",
                    source_label="FRED",
                    provider_symbol=tile.fred_symbol,
                    model_daily_session_points=mode == MARKET_DATA_MODE_KEYLESS,
                )
        except Exception as exc:
            logger.warning("market strip FRED fetch failed symbol=%s: %s", tile.fred_symbol, safe_market_data_error_message(exc))

    return _build_tile_payload(
        tile,
        [],
        source=provider if provider_symbol and api_key else "none",
        source_label=provider_label if provider_symbol and api_key else "No keyless source",
        provider_symbol=provider_symbol,
    )


async def _fetch_tile_cached(
    db: AsyncSession,
    client: httpx.AsyncClient,
    user_id: int,
    item: dict,
    tile: MarketStripTileConfig,
    provider: str,
    provider_label: str,
    api_key: str,
    source: str,
    mode: str,
    config_revision: str,
    start_date_value: date,
    end_date_value: date,
    now: datetime,
) -> dict:
    cache_key = _tile_cache_key(
        user_id,
        provider,
        source,
        mode,
        config_revision,
        api_key,
        end_date_value,
        item,
    )
    cached = _market_strip_tile_cache.get(cache_key)
    if cached and now - cached[0] <= MARKET_STRIP_CACHE_TTL:
        return cached[1]

    payload = await _fetch_tile(
        db,
        client,
        user_id,
        tile,
        provider,
        provider_label,
        api_key,
        start_date_value,
        end_date_value,
        source=source,
        mode=mode,
        symbol_key=_snapshot_symbol_key(item, tile),
        now=now,
    )
    _market_strip_tile_cache[cache_key] = (now, payload)
    return payload


async def get_market_strip_payload(db: AsyncSession, user_id: int) -> dict:
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    today = datetime.now(timezone.utc).astimezone(user_tz).date()
    start_date_value = today - timedelta(days=45)
    market_data_config = await get_effective_market_data_config(db, user_id)
    provider = market_data_config["provider"]
    provider_label = market_data_config["provider_label"]
    api_key = market_data_config["api_key"]
    default_watchlist = (
        KEYLESS_MARKET_STRIP_WATCHLIST
        if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS
        else DEFAULT_MARKET_STRIP_WATCHLIST
    )
    watchlist = await get_market_strip_watchlist(db, user_id, default_watchlist)
    if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS:
        watchlist = _filter_keyless_watchlist(watchlist)
    cache_key = _cache_key(
        user_id,
        provider,
        market_data_config["source"],
        market_data_config["mode"],
        market_data_config["config_revision"],
        api_key,
        today,
        _watchlist_key(watchlist),
    )
    now = datetime.now(timezone.utc)
    cached = _market_strip_cache.get(cache_key)
    if cached and now - cached[0] <= MARKET_STRIP_CACHE_TTL:
        return cached[1]

    async with httpx.AsyncClient(timeout=20) as client:
        tiles = []
        for item in watchlist:
            tile = _tile_from_watchlist_item(item)
            if not tile:
                continue
            tiles.append(
                await _fetch_tile_cached(
                    db,
                    client,
                    user_id,
                    item,
                    tile,
                    provider,
                    provider_label,
                    api_key,
                    market_data_config["source"],
                    market_data_config["mode"],
                    market_data_config["config_revision"],
                    start_date_value,
                    today,
                    now,
                )
            )

    payload = {
        "status": "ok",
        "updated_at": now.isoformat(),
        "refresh_seconds": int(MARKET_STRIP_CACHE_TTL.total_seconds()),
        "market_data": {
            "mode": market_data_config["mode"],
            "provider": provider,
            "provider_label": provider_label,
            "source": market_data_config["source"],
            "has_key": bool(api_key),
            "config_revision": market_data_config["config_revision"],
        },
        "tiles": tiles,
        "watchlist": watchlist,
        "available_tiles": [
            _tile_catalog_payload(MARKET_STRIP_TILE_BY_ID[tile_id])
            for tile_id in DEFAULT_MARKET_STRIP_WATCHLIST
            if tile_id in MARKET_STRIP_TILE_BY_ID
        ],
    }
    _market_strip_cache[cache_key] = (now, payload)
    return payload


def _invalidate_market_strip_cache(user_id: int) -> None:
    prefix = f"{int(user_id)}:"
    for cache in (_market_strip_cache, _market_strip_tile_cache):
        for key in tuple(cache):
            if key.startswith(prefix):
                cache.pop(key, None)


async def _collect_market_strip_snapshot_candidates(
    user_id: int,
) -> list[MarketStripSnapshotCandidate]:
    async with async_session() as db:
        user_tz = await get_user_timezone_info_from_db(db, user_id)
        market_data_config = await get_effective_market_data_config(db, user_id)
        default_watchlist = (
            KEYLESS_MARKET_STRIP_WATCHLIST
            if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS
            else DEFAULT_MARKET_STRIP_WATCHLIST
        )
        watchlist = await get_market_strip_watchlist(db, user_id, default_watchlist)

    if market_data_config["mode"] == MARKET_DATA_MODE_KEYLESS:
        watchlist = _filter_keyless_watchlist(watchlist)
    now = datetime.now(timezone.utc)
    today = now.astimezone(user_tz).date()
    candidates: list[MarketStripSnapshotCandidate] = []
    async with httpx.AsyncClient(timeout=20) as client:
        for item in watchlist:
            tile = _tile_from_watchlist_item(item)
            if tile is None:
                continue
            await _fetch_tile(
                None,
                client,
                user_id,
                tile,
                market_data_config["provider"],
                market_data_config["provider_label"],
                market_data_config["api_key"],
                today - timedelta(days=45),
                today,
                source=market_data_config["source"],
                mode=market_data_config["mode"],
                symbol_key=_snapshot_symbol_key(item, tile),
                now=now,
                snapshot_candidates=candidates,
            )
    return candidates


async def _persist_market_strip_snapshot_candidates(
    ownership: RuntimeLeaseOwnership,
    candidates: list[MarketStripSnapshotCandidate],
) -> bool:
    if not candidates:
        return True
    affected_users: set[int] = set()
    async with async_session() as db, sqlite_write_gate(), db.begin():
        # The first token-matched UPDATE obtains the database row/write lock. Check
        # expiry only after that lock is held so time spent waiting for the lock
        # cannot let a stale owner pass an earlier application-time predicate.
        fence = await db.execute(
            update(RuntimeServiceLease)
            .where(
                RuntimeServiceLease.service_name == ownership.service_name,
                RuntimeServiceLease.owner_token == ownership.owner_token,
            )
            .values(owner_token=ownership.owner_token)
        )
        if fence.rowcount != 1:
            return False
        fence_now = datetime.now(timezone.utc)
        live_fence = await db.execute(
            update(RuntimeServiceLease)
            .where(
                RuntimeServiceLease.service_name == ownership.service_name,
                RuntimeServiceLease.owner_token == ownership.owner_token,
                RuntimeServiceLease.expires_at > fence_now,
            )
            .values(owner_token=ownership.owner_token)
        )
        if live_fence.rowcount != 1:
            return False

        local_dates_by_user = {
            (candidate.user_id, candidate.local_date) for candidate in candidates
        }
        for user_id, local_date in local_dates_by_user:
            affected_users.add(user_id)
            await db.execute(
                delete(MarketStripSnapshotPoint).where(
                    MarketStripSnapshotPoint.user_id == user_id,
                    MarketStripSnapshotPoint.local_date != local_date,
                )
            )

        for candidate in candidates:
            result = await db.execute(
                select(MarketStripSnapshotPoint).where(
                    MarketStripSnapshotPoint.user_id == candidate.user_id,
                    MarketStripSnapshotPoint.provider == candidate.provider,
                    MarketStripSnapshotPoint.source == candidate.source,
                    MarketStripSnapshotPoint.mode == candidate.mode,
                    MarketStripSnapshotPoint.symbol_key == candidate.symbol_key,
                    MarketStripSnapshotPoint.local_date == candidate.local_date,
                    MarketStripSnapshotPoint.sample_slot == candidate.sample_slot,
                )
            )
            point = result.scalar_one_or_none()
            if point is None:
                db.add(
                    MarketStripSnapshotPoint(
                        user_id=candidate.user_id,
                        provider=candidate.provider,
                        source=candidate.source,
                        mode=candidate.mode,
                        symbol_key=candidate.symbol_key,
                        local_date=candidate.local_date,
                        sample_slot=candidate.sample_slot,
                        close=candidate.close,
                        observed_at=candidate.observed_at,
                    )
                )
            else:
                point.close = candidate.close
                point.observed_at = candidate.observed_at
        await db.flush()

    for user_id in affected_users:
        _invalidate_market_strip_cache(user_id)
    return True


async def _renew_market_strip_sampler_lease(
    state: _MarketStripSamplerLeaseState,
) -> None:
    loop = asyncio.get_running_loop()
    duration_seconds = max(MARKET_STRIP_SAMPLER_LEASE_DURATION.total_seconds(), 0.001)
    retry_delay = min(
        max(MARKET_STRIP_SAMPLER_RENEW_ERROR_RETRY_SECONDS, 0.001),
        duration_seconds / 2,
    )
    next_delay = max(MARKET_STRIP_SAMPLER_LEASE_RENEW_SECONDS, 0.001)
    if state.confirmed_deadline == float("inf"):
        state.confirmed_deadline = (
            loop.time()
            + max(
                (
                    state.ownership.expires_at - datetime.now(timezone.utc)
                ).total_seconds(),
                0,
            )
            - retry_delay
        )
    try:
        while not state.lost.is_set():
            remaining = state.confirmed_deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(next_delay, remaining))
            if state.lost.is_set():
                return
            remaining = state.confirmed_deadline - loop.time()
            if remaining <= 0:
                return
            try:
                async with asyncio.timeout(remaining):
                    renewed = await renew_runtime_service_lease(
                        state.ownership,
                        duration=MARKET_STRIP_SAMPLER_LEASE_DURATION,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "market strip sampler lease renewal failed error_type=%s",
                    type(exc).__name__,
                )
                next_delay = retry_delay
                continue
            if renewed is None:
                return
            state.ownership = renewed
            state.confirmed_deadline = loop.time() + duration_seconds - retry_delay
            next_delay = max(MARKET_STRIP_SAMPLER_LEASE_RENEW_SECONDS, 0.001)
    finally:
        state.lost.set()


async def _sample_market_strip_for_all_users(
    state: _MarketStripSamplerLeaseState,
) -> None:
    loop = asyncio.get_running_loop()
    if loop.time() >= state.confirmed_deadline:
        state.lost.set()
        return
    if not _is_market_strip_provider_fetch_window():
        return
    user_ids = await _list_market_strip_sampler_users()
    for user_id in user_ids:
        if state.lost.is_set() or loop.time() >= state.confirmed_deadline:
            state.lost.set()
            return
        try:
            candidates = await _collect_market_strip_snapshot_candidates(user_id)
            if state.lost.is_set():
                return
            if not await _persist_market_strip_snapshot_candidates(
                state.ownership,
                candidates,
            ):
                state.lost.set()
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market strip sampling failed user_id=%s error_type=%s",
                user_id,
                type(exc).__name__,
            )


async def _list_market_strip_sampler_users() -> list[int]:
    async with async_session() as db:
        rows = (
            await db.execute(select(User.id).order_by(User.id))
        ).scalars().all()
    return [int(user_id) for user_id in rows]


async def _cancel_and_drain(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _run_owned_market_strip_sampler(
    ownership: RuntimeLeaseOwnership,
) -> None:
    lease_margin = min(
        max(MARKET_STRIP_SAMPLER_RENEW_ERROR_RETRY_SECONDS, 0.001),
        max(MARKET_STRIP_SAMPLER_LEASE_DURATION.total_seconds(), 0.001) / 2,
    )
    state = _MarketStripSamplerLeaseState(
        ownership=ownership,
        lost=asyncio.Event(),
        confirmed_deadline=(
            asyncio.get_running_loop().time()
            + max(
                (ownership.expires_at - datetime.now(timezone.utc)).total_seconds(),
                0,
            )
            - lease_margin
        ),
    )
    heartbeat_task = create_tracked_task(
        _renew_market_strip_sampler_lease(state),
        name="market-strip-sampler-lease-renewal",
    )
    sample_task: asyncio.Task[Any] | None = None
    lost_wait_task: asyncio.Task[Any] | None = None
    try:
        while not state.lost.is_set():
            sample_task = create_tracked_task(
                _sample_market_strip_for_all_users(state),
                name="market-strip-sample-pass",
            )
            lost_wait_task = create_tracked_task(
                state.lost.wait(),
                name="market-strip-sampler-lease-loss-wait",
            )
            done, _pending = await asyncio.wait(
                (sample_task, lost_wait_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lost_wait_task in done:
                await _cancel_and_drain(sample_task)
                return
            await _cancel_and_drain(lost_wait_task)
            lost_wait_task = None
            try:
                await sample_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "market strip sample pass failed error_type=%s",
                    type(exc).__name__,
                )
            sample_task = None
            if state.lost.is_set():
                return
            try:
                await asyncio.wait_for(
                    state.lost.wait(),
                    timeout=max(MARKET_STRIP_SAMPLER_INTERVAL_SECONDS, 0.001),
                )
            except asyncio.TimeoutError:
                continue
    finally:
        state.lost.set()
        await _cancel_and_drain(sample_task)
        await _cancel_and_drain(lost_wait_task)
        await _cancel_and_drain(heartbeat_task)
        try:
            await release_runtime_service_lease(state.ownership)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market strip sampler lease release failed error_type=%s",
                type(exc).__name__,
            )


async def _run_market_strip_sampler() -> None:
    while True:
        try:
            ownership = await acquire_runtime_service_lease(
                MARKET_STRIP_SAMPLER_SERVICE_NAME,
                duration=MARKET_STRIP_SAMPLER_LEASE_DURATION,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market strip sampler lease acquisition failed error_type=%s",
                type(exc).__name__,
            )
            await asyncio.sleep(max(MARKET_STRIP_SAMPLER_LEASE_RETRY_SECONDS, 0.001))
            continue
        if ownership is None:
            await asyncio.sleep(max(MARKET_STRIP_SAMPLER_LEASE_RETRY_SECONDS, 0.001))
            continue
        try:
            await _run_owned_market_strip_sampler(ownership)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market strip owned sampler failed error_type=%s",
                type(exc).__name__,
            )
            await asyncio.sleep(max(MARKET_STRIP_SAMPLER_LEASE_RETRY_SECONDS, 0.001))


def start_market_strip_sampler() -> None:
    global _market_strip_sampler_task
    if _market_strip_sampler_task is not None and not _market_strip_sampler_task.done():
        return
    _market_strip_sampler_task = create_tracked_task(
        _run_market_strip_sampler(),
        name="market-strip-sampler",
    )


async def stop_market_strip_sampler() -> None:
    global _market_strip_sampler_task
    task = _market_strip_sampler_task
    _market_strip_sampler_task = None
    await _cancel_and_drain(task)
