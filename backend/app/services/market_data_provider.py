from __future__ import annotations

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from app.services.market_data_logging import (
    raise_safe_market_data_payload_error,
    raise_safe_market_data_status,
)
from app.services.market_data_settings import (
    MARKET_DATA_PROVIDER_FMP,
    MARKET_DATA_PROVIDER_POLYGON,
    MARKET_DATA_PROVIDER_TWELVE_DATA,
)

FMP_HISTORICAL_PRICE_FULL_URL = "https://financialmodelingprep.com/stable/historical-price-eod/full"
FMP_HISTORICAL_PRICE_LIGHT_URL = "https://financialmodelingprep.com/stable/historical-price-eod/light"
FMP_LEGACY_INDEX_HISTORICAL_PRICE_URL = "https://financialmodelingprep.com/api/v3/historical-price-full/index/{symbol}"
FMP_INTRADAY_CHART_URL = "https://financialmodelingprep.com/stable/historical-chart/15min"
FMP_QUOTE_SHORT_URL = "https://financialmodelingprep.com/stable/quote-short"
POLYGON_AGGREGATES_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}"
POLYGON_INTRADAY_AGGREGATES_URL = "https://api.polygon.io/v2/aggs/ticker/{symbol}/range/15/minute/{start}/{end}"
POLYGON_INDICES_SNAPSHOT_URL = "https://api.polygon.io/v3/snapshot/indices"
TWELVE_DATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"

MARKET_DATA_RESPONSE_CACHE_TTL = timedelta(minutes=15)
MARKET_DATA_PROVIDER_DENIAL_TTL = timedelta(days=1)
MARKET_DATA_ABORT_STATUSES = {401, 402, 403, 429}
MARKET_DATA_PROVIDER_DENIAL_STATUSES = {401, 402, 403, 429}
TWELVE_DATA_CALLS_PER_MINUTE = 8

FMP_DAILY_ENDPOINT_FULL = "full"
FMP_DAILY_ENDPOINT_LIGHT = "light"


@dataclass(frozen=True)
class MarketDataProviderContext:
    provider: str
    api_key: str


_market_data_response_cache: dict[str, tuple[datetime, object]] = {}
_market_data_request_locks: dict[str, asyncio.Lock] = {}
_market_data_provider_denials: dict[str, datetime] = {}
_market_data_rate_windows: dict[str, list[datetime]] = {}
_market_data_rate_locks: dict[str, asyncio.Lock] = {}


def market_data_key_hash(api_key: str | None) -> str:
    value = str(api_key or "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else "no-key"


def market_data_error_code(exc: Exception) -> int | None:
    provider_code = getattr(exc, "breaktwenty_provider_code", None)
    if provider_code is not None:
        try:
            return int(provider_code)
        except (TypeError, ValueError):
            return None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code
    return None


async def get_with_timeout_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict,
) -> httpx.Response:
    last_timeout: httpx.TimeoutException | None = None
    for timeout in (8, 20):
        try:
            return await client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException as exc:
            last_timeout = exc
    if last_timeout:
        raise last_timeout
    raise httpx.TimeoutException("request timed out")


def provider_supports_current_quote(provider: str) -> bool:
    return provider in {
        MARKET_DATA_PROVIDER_FMP,
        MARKET_DATA_PROVIDER_POLYGON,
    }


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_from_mapping(row: dict | None) -> float | None:
    if not isinstance(row, dict):
        return None
    for key in ("price", "value", "close", "last", "lastSalePrice"):
        price = _parse_float(row.get(key))
        if price is not None:
            return price
    session = row.get("session")
    if isinstance(session, dict):
        return _price_from_mapping(session)
    return None


def _latest_intraday_session(series: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for point in series:
        point_date = str(point.get("date") or "").strip()
        if point_date:
            grouped.setdefault(point_date[:10], []).append(point)
    for session_date in sorted(grouped.keys(), reverse=True):
        session = sorted(grouped[session_date], key=lambda item: str(item.get("date") or ""))
        if len(session) >= 2:
            return session
    return []


def _copy_cached_value(value: object) -> object:
    return deepcopy(value)


def _params_cache_part(params: dict) -> str:
    safe_items = []
    for key, value in sorted(params.items()):
        if value is None:
            continue
        rendered_value = "<key>" if str(key).lower() in {"apikey", "api_key"} else str(value)
        safe_items.append(f"{key}={rendered_value}")
    return "&".join(safe_items)


def _response_cache_key(ctx: MarketDataProviderContext, operation: str, url: str, params: dict) -> str:
    raw_key = f"{ctx.provider}:{market_data_key_hash(ctx.api_key)}:{operation}:{url}:{_params_cache_part(params)}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _denial_key(
    ctx: MarketDataProviderContext,
    operation: str,
    symbol: str | None,
    *,
    broad: bool,
) -> str:
    symbol_part = "*" if broad else str(symbol or "").upper()
    return f"{ctx.provider}:{market_data_key_hash(ctx.api_key)}:{operation}:{symbol_part}"


def _is_operation_denied(
    ctx: MarketDataProviderContext,
    operation: str,
    symbol: str | None = None,
    *,
    broad: bool = False,
) -> bool:
    key = _denial_key(ctx, operation, symbol, broad=broad)
    denied_at = _market_data_provider_denials.get(key)
    if denied_at is None:
        return False
    if datetime.now(timezone.utc) - denied_at <= MARKET_DATA_PROVIDER_DENIAL_TTL:
        return True
    _market_data_provider_denials.pop(key, None)
    return False


def _mark_operation_denied(
    ctx: MarketDataProviderContext,
    operation: str,
    symbol: str | None = None,
    *,
    broad: bool = False,
) -> None:
    _market_data_provider_denials[_denial_key(ctx, operation, symbol, broad=broad)] = datetime.now(timezone.utc)


async def _wait_for_rate_limit_slot(ctx: MarketDataProviderContext) -> None:
    if ctx.provider != MARKET_DATA_PROVIDER_TWELVE_DATA:
        return
    key = f"{ctx.provider}:{market_data_key_hash(ctx.api_key)}"
    lock = _market_data_rate_locks.setdefault(key, asyncio.Lock())
    while True:
        async with lock:
            now = datetime.now(timezone.utc)
            window = [
                timestamp
                for timestamp in _market_data_rate_windows.get(key, [])
                if now - timestamp < timedelta(minutes=1)
            ]
            if len(window) < TWELVE_DATA_CALLS_PER_MINUTE:
                window.append(now)
                _market_data_rate_windows[key] = window
                return
            oldest = min(window)
            wait_seconds = max(0.05, 60 - (now - oldest).total_seconds() + 0.05)
            _market_data_rate_windows[key] = window
        await asyncio.sleep(wait_seconds)


def _format_twelve_time_series_bound(value: date, *, interval: str, is_end: bool) -> str:
    if interval == "1day":
        return value.isoformat()
    suffix = "23:59:59" if is_end else "00:00:00"
    return f"{value.isoformat()} {suffix}"


def _filter_current_intraday_session(series: list[dict], end_date_value: date) -> list[dict]:
    session = _latest_intraday_session(series)
    if end_date_value.weekday() >= 5:
        return session
    expected_date = end_date_value.isoformat()
    current_session = [point for point in session if str(point.get("date") or "")[:10] == expected_date]
    return current_session if len(current_session) >= 2 else []


async def _request_json(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    operation: str,
    url: str,
    *,
    params: dict,
    ttl: timedelta = MARKET_DATA_RESPONSE_CACHE_TTL,
) -> object:
    payload, _ = await _request_json_with_response(
        client,
        ctx,
        operation,
        url,
        params=params,
        ttl=ttl,
    )
    return payload


async def _request_json_with_response(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    operation: str,
    url: str,
    *,
    params: dict,
    ttl: timedelta = MARKET_DATA_RESPONSE_CACHE_TTL,
) -> tuple[object, httpx.Response | None]:
    cache_key = _response_cache_key(ctx, operation, url, params)
    now = datetime.now(timezone.utc)
    cached = _market_data_response_cache.get(cache_key)
    if cached and now - cached[0] <= ttl:
        return _copy_cached_value(cached[1]), None

    lock = _market_data_request_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        now = datetime.now(timezone.utc)
        cached = _market_data_response_cache.get(cache_key)
        if cached and now - cached[0] <= ttl:
            return _copy_cached_value(cached[1]), None

        await _wait_for_rate_limit_slot(ctx)
        response = await client.get(url, params=params)
        raise_safe_market_data_status(response)
        payload = response.json()
        if not (isinstance(payload, dict) and payload.get("status") == "error"):
            _market_data_response_cache[cache_key] = (datetime.now(timezone.utc), payload)
        return _copy_cached_value(payload), response


def _raise_provider_payload_error_if_needed(response: httpx.Response, payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("status") != "error":
        return
    code = market_data_error_code_value(payload.get("code"))
    if code in MARKET_DATA_ABORT_STATUSES:
        raise_safe_market_data_payload_error(response, payload)


def _is_twelve_data_symbol_unavailable(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("status") != "error":
        return False
    message = str(payload.get("message") or "").lower()
    return any(
        phrase in message
        for phrase in (
            "available starting with",
            "symbol or figi",
            "missing or invalid",
            "invalid symbol",
            "not found",
            "not available",
        )
    )


def market_data_error_code_value(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_fmp_daily_payload(payload: object) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("historical", []) if isinstance(payload, dict) else []
    series = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_date = str(row.get("date") or "").strip()
        close = _parse_float(row.get("adjClose") or row.get("close") or row.get("price"))
        if row_date and close is not None:
            series.append({"date": row_date[:10], "close": close})
    return sorted(series, key=lambda item: item["date"])


async def _fetch_fmp_daily_series(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    *,
    endpoint_preference: str,
) -> list[dict]:
    params = {
        "symbol": symbol,
        "from": start_date_value.isoformat(),
        "to": end_date_value.isoformat(),
        "apikey": ctx.api_key,
    }
    url = FMP_HISTORICAL_PRICE_LIGHT_URL if endpoint_preference == FMP_DAILY_ENDPOINT_LIGHT else FMP_HISTORICAL_PRICE_FULL_URL
    try:
        payload = await _request_json(client, ctx, "daily", url, params=params)
    except httpx.HTTPStatusError as exc:
        if (
            endpoint_preference != FMP_DAILY_ENDPOINT_LIGHT
            or not symbol.startswith("^")
            or market_data_error_code(exc) != 402
        ):
            if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
                _mark_operation_denied(ctx, "daily", symbol)
            raise
        try:
            payload = await _request_json(
                client,
                ctx,
                "daily",
                FMP_LEGACY_INDEX_HISTORICAL_PRICE_URL.format(symbol=quote(symbol, safe="")),
                params={
                    "from": start_date_value.isoformat(),
                    "to": end_date_value.isoformat(),
                    "apikey": ctx.api_key,
                },
            )
        except httpx.HTTPStatusError as legacy_exc:
            if market_data_error_code(legacy_exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
                _mark_operation_denied(ctx, "daily", symbol)
            raise

    series = _parse_fmp_daily_payload(payload)
    if series or endpoint_preference != FMP_DAILY_ENDPOINT_LIGHT or not symbol.startswith("^"):
        return series

    try:
        payload = await _request_json(
            client,
            ctx,
            "daily",
            FMP_LEGACY_INDEX_HISTORICAL_PRICE_URL.format(symbol=quote(symbol, safe="")),
            params={
                "from": start_date_value.isoformat(),
                "to": end_date_value.isoformat(),
                "apikey": ctx.api_key,
            },
        )
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, "daily", symbol)
        raise
    return _parse_fmp_daily_payload(payload)


async def _fetch_fmp_intraday_series(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    symbol: str,
) -> list[dict]:
    if _is_operation_denied(ctx, "intraday", broad=True):
        return []
    try:
        payload = await _request_json(
            client,
            ctx,
            "intraday",
            FMP_INTRADAY_CHART_URL,
            params={
                "symbol": symbol,
                "apikey": ctx.api_key,
            },
        )
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, "intraday", broad=True)
        raise
    rows = payload if isinstance(payload, list) else payload.get("historical", []) if isinstance(payload, dict) else []
    series = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_time = str(row.get("date") or row.get("datetime") or "").strip()
        close = _parse_float(row.get("close"))
        if row_time and close is not None:
            series.append({"date": row_time.replace(" ", "T"), "close": close})
    return _latest_intraday_session(series)


async def _fetch_polygon_daily_series(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
) -> list[dict]:
    try:
        payload = await _request_json(
            client,
            ctx,
            "daily",
            POLYGON_AGGREGATES_URL.format(
                symbol=symbol,
                start=start_date_value.isoformat(),
                end=end_date_value.isoformat(),
            ),
            params={
                "adjusted": "true",
                "sort": "asc",
                "limit": 50000,
                "apiKey": ctx.api_key,
            },
        )
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, "daily", symbol)
        raise
    series = []
    rows = payload.get("results") if isinstance(payload, dict) else []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("t")
        close = _parse_float(row.get("c"))
        if timestamp is None or close is None:
            continue
        row_date = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc).date().isoformat()
        series.append({"date": row_date, "close": close})
    return series


async def _fetch_polygon_intraday_series(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
) -> list[dict]:
    if _is_operation_denied(ctx, "intraday", broad=True):
        return []
    try:
        payload = await _request_json(
            client,
            ctx,
            "intraday",
            POLYGON_INTRADAY_AGGREGATES_URL.format(
                symbol=symbol,
                start=start_date_value.isoformat(),
                end=end_date_value.isoformat(),
            ),
            params={
                "adjusted": "true",
                "sort": "asc",
                "limit": 50000,
                "apiKey": ctx.api_key,
            },
        )
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, "intraday", broad=True)
        raise
    series = []
    rows = payload.get("results") if isinstance(payload, dict) else []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        timestamp = row.get("t")
        close = _parse_float(row.get("c"))
        if timestamp is None or close is None:
            continue
        row_time = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
        series.append({"date": row_time.isoformat(), "close": close})
    return _latest_intraday_session(series)


async def _fetch_twelve_data_series(
    client: httpx.AsyncClient,
    ctx: MarketDataProviderContext,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    *,
    interval: str,
    output_size: int,
) -> list[dict]:
    operation = "intraday" if interval != "1day" else "daily"
    if _is_operation_denied(ctx, operation, symbol):
        return []
    try:
        params = {
            "symbol": symbol,
            "interval": interval,
            "start_date": _format_twelve_time_series_bound(start_date_value, interval=interval, is_end=False),
            "end_date": _format_twelve_time_series_bound(end_date_value, interval=interval, is_end=True),
            "outputsize": output_size,
            "apikey": ctx.api_key,
        }
        payload, response = await _request_json_with_response(
            client,
            ctx,
            operation,
            TWELVE_DATA_TIME_SERIES_URL,
            params=params,
        )
        if response is not None:
            if _is_twelve_data_symbol_unavailable(payload):
                _mark_operation_denied(ctx, operation, symbol)
                return []
            _raise_provider_payload_error_if_needed(response, payload)
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, operation, symbol)
        raise

    rows = payload.get("values") if isinstance(payload, dict) else []
    series = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_time = str(row.get("datetime") or row.get("date") or "").strip()
        close = _parse_float(row.get("close"))
        if row_time and close is not None:
            point_date = row_time[:10] if interval == "1day" else row_time.replace(" ", "T")
            series.append({"date": point_date, "close": close})
    series = sorted(series, key=lambda item: item["date"])
    return _filter_current_intraday_session(series, end_date_value) if interval != "1day" else series


async def fetch_market_data_daily_series(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
    *,
    fmp_endpoint: str = FMP_DAILY_ENDPOINT_FULL,
    output_size: int | None = None,
) -> list[dict]:
    ctx = MarketDataProviderContext(provider=provider, api_key=api_key)
    if _is_operation_denied(ctx, "daily", symbol):
        return []
    if provider == MARKET_DATA_PROVIDER_POLYGON:
        return await _fetch_polygon_daily_series(client, ctx, symbol, start_date_value, end_date_value)
    if provider == MARKET_DATA_PROVIDER_TWELVE_DATA:
        return await _fetch_twelve_data_series(
            client,
            ctx,
            symbol,
            start_date_value,
            end_date_value,
            interval="1day",
            output_size=output_size or 30,
        )
    return await _fetch_fmp_daily_series(
        client,
        ctx,
        symbol,
        start_date_value,
        end_date_value,
        endpoint_preference=fmp_endpoint,
    )


async def fetch_market_data_intraday_series(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
    *,
    interval: str = "15min",
    output_size: int = 96,
) -> list[dict]:
    ctx = MarketDataProviderContext(provider=provider, api_key=api_key)
    if provider == MARKET_DATA_PROVIDER_POLYGON:
        return await _fetch_polygon_intraday_series(client, ctx, symbol, start_date_value, end_date_value)
    if provider == MARKET_DATA_PROVIDER_TWELVE_DATA:
        return await _fetch_twelve_data_series(
            client,
            ctx,
            symbol,
            start_date_value,
            end_date_value,
            interval=interval,
            output_size=output_size,
        )
    return await _fetch_fmp_intraday_series(client, ctx, symbol)


async def fetch_market_data_current_quote(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    api_key: str,
) -> float | None:
    if not provider_supports_current_quote(provider):
        return None

    ctx = MarketDataProviderContext(provider=provider, api_key=api_key)
    if _is_operation_denied(ctx, "current_quote", symbol):
        return None

    try:
        if provider == MARKET_DATA_PROVIDER_POLYGON:
            payload = await _request_json(
                client,
                ctx,
                "current_quote",
                POLYGON_INDICES_SNAPSHOT_URL,
                params={
                    "ticker": symbol,
                    "apiKey": ctx.api_key,
                },
            )
            rows = payload.get("results") if isinstance(payload, dict) else []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                row_symbol = str(row.get("ticker") or row.get("symbol") or "").upper()
                if row_symbol and row_symbol != symbol.upper():
                    continue
                price = _price_from_mapping(row)
                if price is not None:
                    return price
            return None

        payload = await _request_json(
            client,
            ctx,
            "current_quote",
            FMP_QUOTE_SHORT_URL,
            params={
                "symbol": symbol,
                "apikey": ctx.api_key,
            },
        )
    except httpx.HTTPStatusError as exc:
        if market_data_error_code(exc) in MARKET_DATA_PROVIDER_DENIAL_STATUSES:
            _mark_operation_denied(ctx, "current_quote", symbol)
        raise

    rows = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    for row in rows:
        price = _price_from_mapping(row if isinstance(row, dict) else None)
        if price is not None:
            return price
    return None
