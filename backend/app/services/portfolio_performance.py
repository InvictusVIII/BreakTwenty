from __future__ import annotations

import logging
import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BalanceHistory, BenchmarkPrice, Institution, Setting, Transaction
from app.services.fx_history import build_to_primary_converter
from app.services.market_data_logging import safe_market_data_error_message
from app.services.market_data_provider import (
    FMP_DAILY_ENDPOINT_LIGHT,
    fetch_market_data_current_quote,
    fetch_market_data_daily_series,
    get_with_timeout_retry,
    provider_supports_current_quote,
)
from app.services.market_data_settings import (
    MARKET_DATA_PROVIDER_FMP,
    MARKET_DATA_PROVIDER_POLYGON,
    MARKET_DATA_PROVIDER_TWELVE_DATA,
    get_effective_market_data_config,
    get_market_data_provider_label,
)
from app.services.user_utils import get_user_timezone_info_from_db

logger = logging.getLogger("breaktwenty.performance")

INVESTMENT_ACCOUNT_TYPES = frozenset({"margin", "tfsa", "rrsp", "fhsa", "resp", "lira", "crypto"})
EXTERNAL_CASH_FLOW_TYPES = frozenset({"deposit", "withdrawal"})
FRED_GRAPH_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
MARKET_DATA_PROVIDER_FRED = "fred"
BENCHMARK_SAME_DAY_CACHE_TTL = timedelta(minutes=15)
BENCHMARK_BASELINE_LOOKBACK_DAYS = 7
TIMELINE_DAY_COUNTS = {
    "1D": 1,
    "5D": 5,
    "30D": 30,
    "90D": 90,
    "6M": 180,
    "1Y": 365,
}

BENCHMARKS = (
    {
        "id": "sp500",
        "name": "S&P 500",
        "symbol": "^GSPC",
        "provider_symbols": {
            MARKET_DATA_PROVIDER_FMP: "^GSPC",
            MARKET_DATA_PROVIDER_POLYGON: "I:SPX",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "SPX",
            MARKET_DATA_PROVIDER_FRED: "SP500",
        },
    },
    {
        "id": "dow",
        "name": "DOW",
        "symbol": "^DJI",
        "provider_symbols": {
            MARKET_DATA_PROVIDER_FMP: "^DJI",
            MARKET_DATA_PROVIDER_POLYGON: "I:DJI",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "DJI",
            MARKET_DATA_PROVIDER_FRED: "DJIA",
        },
    },
    {
        "id": "nasdaq",
        "name": "NASDAQ",
        "symbol": "^IXIC",
        "provider_symbols": {
            MARKET_DATA_PROVIDER_FMP: "^IXIC",
            MARKET_DATA_PROVIDER_POLYGON: "I:COMP",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "IXIC",
            MARKET_DATA_PROVIDER_FRED: "NASDAQCOM",
        },
    },
    {
        "id": "tsx",
        "name": "S&P/TSX",
        "symbol": "^GSPTSE",
        "provider_symbols": {
            MARKET_DATA_PROVIDER_FMP: "^GSPTSE",
            MARKET_DATA_PROVIDER_TWELVE_DATA: "GSPTSE",
        },
    },
)

@dataclass(frozen=True)
class PerformanceBounds:
    timeline: str
    start_date: date | None
    end_date: date


def _parse_date_value(value: str | date | datetime | None) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _date_to_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _local_date(value: date | datetime, user_tz) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return dt.astimezone(user_tz).date()


def _normalize_timeline(value: str | None) -> str:
    normalized = str(value or "ALL").strip().upper()
    if normalized in {*TIMELINE_DAY_COUNTS.keys(), "YTD", "ALL", "CUSTOM"}:
        return normalized
    return "ALL"


def _resolve_performance_bounds(
    timeline: str | None,
    start_date: str | None,
    end_date: str | None,
    today: date,
) -> PerformanceBounds:
    normalized_timeline = _normalize_timeline(timeline)
    parsed_end = _parse_date_value(end_date) or today
    end_value = min(parsed_end, today)

    if normalized_timeline == "CUSTOM":
        start_value = _parse_date_value(start_date)
    elif normalized_timeline == "YTD":
        start_value = date(end_value.year, 1, 1)
    elif normalized_timeline in TIMELINE_DAY_COUNTS:
        start_value = end_value - timedelta(days=TIMELINE_DAY_COUNTS[normalized_timeline])
    else:
        start_value = None

    if start_value and start_value > end_value:
        start_value = end_value

    return PerformanceBounds(
        timeline=normalized_timeline,
        start_date=start_value,
        end_date=end_value,
    )


async def _get_primary_currency(db: AsyncSession, user_id: int) -> str:
    result = await db.execute(
        select(Setting.value).where(
            Setting.user_id == user_id,
            Setting.key == "primary_currency",
        )
    )
    return str(result.scalar_one_or_none() or "CAD").strip().upper() or "CAD"


async def _load_investment_accounts(
    db: AsyncSession,
    user_id: int,
    account_ids: list[int] | None,
) -> list[Account]:
    filters = [
        Account.user_id == user_id,
        Institution.user_id == user_id,
        Account.hidden.is_(False),
        Institution.hidden.is_(False),
        Account.is_liability.is_(False),
        Account.account_type.in_(tuple(INVESTMENT_ACCOUNT_TYPES)),
    ]
    if account_ids:
        filters.append(Account.id.in_(account_ids))

    result = await db.execute(
        select(Account)
        .join(Institution, Account.institution_id == Institution.id)
        .where(*filters)
        .order_by(Institution.name, Account.name)
    )
    return list(result.scalars().all())


async def _load_balance_history(
    db: AsyncSession,
    user_id: int,
    account_ids: list[int],
    end_date_value: date,
    user_tz,
    to_primary,
    account_currency_by_id: dict[int, str],
) -> tuple[dict[int, list[tuple[date, float]]], set[date]]:
    if not account_ids:
        return {}, set()

    result = await db.execute(
        select(BalanceHistory.account_id, BalanceHistory.date, BalanceHistory.balance)
        .where(
            BalanceHistory.user_id == user_id,
            BalanceHistory.account_id.in_(account_ids),
        )
        .order_by(BalanceHistory.date)
    )

    rows_by_account_day: dict[int, dict[date, float]] = defaultdict(dict)
    valuation_dates: set[date] = set()

    for row in result.all():
        local_day = _local_date(row.date, user_tz)
        if local_day > end_date_value:
            continue
        converted_balance = to_primary(
            row.balance,
            account_currency_by_id.get(row.account_id),
            local_day.isoformat(),
        )
        rows_by_account_day[int(row.account_id)][local_day] = converted_balance
        valuation_dates.add(local_day)

    return (
        {
            account_id: sorted(day_values.items())
            for account_id, day_values in rows_by_account_day.items()
        },
        valuation_dates,
    )


async def _load_external_cash_flows(
    db: AsyncSession,
    user_id: int,
    account_ids: list[int],
    end_date_value: date,
    user_tz,
    to_primary,
) -> tuple[dict[date, dict[int, float]], dict]:
    if not account_ids:
        return {}, {"deposit_count": 0, "withdrawal_count": 0, "transaction_count": 0}

    result = await db.execute(
        select(Transaction.account_id, Transaction.date, Transaction.type, Transaction.amount, Transaction.currency)
        .where(
            Transaction.user_id == user_id,
            Transaction.account_id.in_(account_ids),
            Transaction.type.in_(EXTERNAL_CASH_FLOW_TYPES),
        )
        .order_by(Transaction.date)
    )

    cash_flows_by_date_account: dict[date, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    deposit_count = 0
    withdrawal_count = 0

    for row in result.all():
        local_day = _local_date(row.date, user_tz)
        if local_day > end_date_value:
            continue

        amount = to_primary(row.amount, row.currency, local_day.isoformat())
        tx_type = str(row.type or "").lower()
        if tx_type == "deposit":
            amount = abs(amount)
            deposit_count += 1
        elif tx_type == "withdrawal":
            amount = -abs(amount)
            withdrawal_count += 1
        else:
            continue

        cash_flows_by_date_account[local_day][int(row.account_id)] += amount

    return (
        {flow_date: dict(account_flows) for flow_date, account_flows in cash_flows_by_date_account.items()},
        {
            "deposit_count": deposit_count,
            "withdrawal_count": withdrawal_count,
            "transaction_count": deposit_count + withdrawal_count,
        },
    )


def _latest_balance_on_or_before(rows: list[tuple[date, float]], day: date) -> tuple[date, float] | None:
    latest: tuple[date, float] | None = None
    for row_day, balance in rows:
        if row_day > day:
            break
        latest = (row_day, balance)
    return latest


def _sum_cash_flows_for_interval(
    cash_flows_by_date_account: dict[date, dict[int, float]],
    seen_account_ids: set[int],
    previous_date: date,
    current_date: date,
) -> tuple[float, float, float]:
    deposits = 0.0
    withdrawals = 0.0

    for flow_date, account_flows in cash_flows_by_date_account.items():
        if flow_date <= previous_date or flow_date > current_date:
            continue
        for account_id, amount in account_flows.items():
            if account_id not in seen_account_ids:
                continue
            if amount >= 0:
                deposits += amount
            else:
                withdrawals += amount

    return deposits + withdrawals, deposits, withdrawals


def _build_portfolio_performance_series(
    accounts: list[Account],
    histories_by_account: dict[int, list[tuple[date, float]]],
    valuation_dates: set[date],
    cash_flows_by_date_account: dict[date, dict[int, float]],
    bounds: PerformanceBounds,
) -> dict:
    if not accounts or not valuation_dates:
        return {
            "series": [],
            "summary": {
                "start_value": 0.0,
                "end_value": 0.0,
                "adjusted_change": 0.0,
                "return_pct": None,
                "deposits": 0.0,
                "withdrawals": 0.0,
                "net_external_cash_flow": 0.0,
                "baseline_additions": 0.0,
            },
            "effective_start_date": bounds.start_date,
        }

    earliest_valuation_date = min(valuation_dates)
    effective_start_date = bounds.start_date or earliest_valuation_date
    if effective_start_date < earliest_valuation_date:
        effective_start_date = earliest_valuation_date

    running_balances: dict[int, float] = {}
    seen_account_ids: set[int] = set()

    for account in accounts:
        account_rows = histories_by_account.get(account.id, [])
        latest = _latest_balance_on_or_before(account_rows, effective_start_date)
        if latest is None:
            continue
        running_balances[account.id] = latest[1]
        seen_account_ids.add(account.id)

    portfolio_index = 1.0
    adjusted_change = 0.0
    total_deposits = 0.0
    total_withdrawals = 0.0
    total_baseline_additions = 0.0
    previous_date = effective_start_date
    previous_value = sum(running_balances.values())

    series = [{
        "date": effective_start_date.isoformat(),
        "portfolio_value": previous_value,
        "portfolio_return_pct": 0.0,
        "adjusted_change": 0.0,
    }]

    process_dates = sorted(
        day
        for day in valuation_dates
        if effective_start_date < day <= bounds.end_date
    )

    for day in process_dates:
        net_flow, deposits, withdrawals = _sum_cash_flows_for_interval(
            cash_flows_by_date_account,
            seen_account_ids,
            previous_date,
            day,
        )

        baseline_addition = 0.0
        for account in accounts:
            account_rows = histories_by_account.get(account.id, [])
            day_balance = dict(account_rows).get(day)
            if day_balance is None:
                continue
            if account.id not in seen_account_ids:
                baseline_addition += day_balance
                seen_account_ids.add(account.id)
            running_balances[account.id] = day_balance

        current_value = sum(running_balances.values())
        adjusted_flow = net_flow + baseline_addition
        interval_change = current_value - previous_value - adjusted_flow
        denominator = previous_value + max(adjusted_flow, 0)
        interval_return = interval_change / denominator if abs(denominator) > 0.0001 else 0.0

        portfolio_index *= 1 + interval_return
        adjusted_change += interval_change
        total_deposits += deposits
        total_withdrawals += withdrawals
        total_baseline_additions += baseline_addition

        series.append({
            "date": day.isoformat(),
            "portfolio_value": current_value,
            "portfolio_return_pct": (portfolio_index - 1) * 100,
            "adjusted_change": adjusted_change,
        })

        previous_date = day
        previous_value = current_value

    ending_value = series[-1]["portfolio_value"] if series else 0.0
    return_pct = (portfolio_index - 1) * 100 if len(series) > 1 else 0.0

    return {
        "series": series,
        "summary": {
            "start_value": series[0]["portfolio_value"] if series else 0.0,
            "end_value": ending_value,
            "adjusted_change": adjusted_change,
            "return_pct": return_pct,
            "deposits": total_deposits,
            "withdrawals": total_withdrawals,
            "net_external_cash_flow": total_deposits + total_withdrawals,
            "baseline_additions": total_baseline_additions,
        },
        "effective_start_date": effective_start_date,
    }


async def _read_cached_benchmark_prices(
    db: AsyncSession,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
) -> list[dict]:
    result = await db.execute(
        select(BenchmarkPrice.date, BenchmarkPrice.close, BenchmarkPrice.fetched_at)
        .where(
            BenchmarkPrice.provider == provider,
            BenchmarkPrice.symbol == symbol,
            BenchmarkPrice.date >= start_date_value,
            BenchmarkPrice.date <= end_date_value,
        )
        .order_by(BenchmarkPrice.date)
    )
    return [
        {
            "date": _date_to_iso(row.date),
            "close": float(row.close),
            "fetched_at": getattr(row, "fetched_at", None),
        }
        for row in result.all()
    ]


async def _upsert_benchmark_prices(
    db: AsyncSession,
    provider: str,
    symbol: str,
    prices: list[dict],
) -> None:
    if not prices:
        return

    now = datetime.now(timezone.utc)
    rows = []
    for price in prices:
        price_date = _parse_date_value(price.get("date"))
        if not price_date or price.get("close") is None:
            continue
        rows.append({
            "provider": provider,
            "symbol": symbol,
            "date": price_date,
            "close": float(price["close"]),
            "fetched_at": now,
        })
    if not rows:
        return

    statement = sqlite_insert(BenchmarkPrice).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=["provider", "symbol", "date"],
        set_={
            "close": statement.excluded.close,
            "fetched_at": statement.excluded.fetched_at,
        },
    )
    await db.execute(statement)
    await db.commit()


def _cache_covers_range(prices: list[dict], start_date_value: date, end_date_value: date) -> bool:
    if not prices:
        return False
    cached_dates = [
        price_date
        for price in prices
        if (price_date := _parse_date_value(price.get("date")))
    ]
    if not cached_dates:
        return False
    earliest_required = start_date_value
    while earliest_required.weekday() >= 5 and earliest_required < end_date_value:
        earliest_required += timedelta(days=1)
    latest_required = end_date_value
    while latest_required.weekday() >= 5:
        latest_required -= timedelta(days=1)
    return min(cached_dates) <= earliest_required and max(cached_dates) >= latest_required


def _benchmark_lookup_start_date(start_date_value: date) -> date:
    return start_date_value - timedelta(days=BENCHMARK_BASELINE_LOOKBACK_DAYS)


def _benchmark_baseline_index(prices: list[dict], start_date_value: str) -> int:
    baseline_index = 0
    while baseline_index + 1 < len(prices) and prices[baseline_index + 1]["date"] <= start_date_value:
        baseline_index += 1
    return baseline_index


def _benchmark_period_return(prices: list[dict], start_date_value: date) -> float | None:
    if not prices:
        return None
    baseline_index = _benchmark_baseline_index(prices, start_date_value.isoformat())
    baseline_close = prices[baseline_index]["close"]
    latest_close = prices[-1]["close"]
    if not baseline_close:
        return None
    return ((latest_close / baseline_close) - 1) * 100


def _normalize_fetched_at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _same_day_cache_is_fresh(prices: list[dict], target_date: date, now: datetime) -> bool:
    target = target_date.isoformat()
    for price in prices:
        if price.get("date") != target:
            continue
        fetched_at = _normalize_fetched_at(price.get("fetched_at"))
        return bool(fetched_at and now - fetched_at <= BENCHMARK_SAME_DAY_CACHE_TTL)
    return False


def _benchmark_provider_requires_key(provider: str) -> bool:
    return provider != MARKET_DATA_PROVIDER_FRED


def _benchmark_provider_supports_current_price(provider: str) -> bool:
    return provider_supports_current_quote(provider)


def _provider_label(provider: str) -> str:
    if provider == MARKET_DATA_PROVIDER_FRED:
        return "FRED"
    return get_market_data_provider_label(provider)


async def _fetch_fmp_benchmark_prices(
    client: httpx.AsyncClient,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
) -> list[dict]:
    return await fetch_market_data_daily_series(
        client,
        MARKET_DATA_PROVIDER_FMP,
        symbol,
        start_date_value,
        end_date_value,
        api_key,
        fmp_endpoint=FMP_DAILY_ENDPOINT_LIGHT,
        output_size=5000,
    )


async def _fetch_polygon_benchmark_prices(
    client: httpx.AsyncClient,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
) -> list[dict]:
    return await fetch_market_data_daily_series(
        client,
        MARKET_DATA_PROVIDER_POLYGON,
        symbol,
        start_date_value,
        end_date_value,
        api_key,
        output_size=5000,
    )


async def _fetch_twelve_data_benchmark_prices(
    client: httpx.AsyncClient,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
) -> list[dict]:
    return await fetch_market_data_daily_series(
        client,
        MARKET_DATA_PROVIDER_TWELVE_DATA,
        symbol,
        start_date_value,
        end_date_value,
        api_key,
        output_size=5000,
    )


async def _fetch_fred_benchmark_prices(
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
    prices = []
    reader = csv.DictReader(io.StringIO(response.text))
    for row in reader:
        row_date = str(row.get("observation_date") or "").strip()
        close = str(row.get(symbol) or "").strip()
        if not row_date or not close or close == ".":
            continue
        try:
            parsed_date = date.fromisoformat(row_date[:10])
            close_value = float(close)
        except (TypeError, ValueError):
            continue
        if start_date_value <= parsed_date <= end_date_value:
            prices.append({"date": parsed_date.isoformat(), "close": close_value})
    return prices


async def _fetch_current_benchmark_price(
    client: httpx.AsyncClient,
    provider: str,
    symbol: str,
    api_key: str,
) -> float | None:
    return await fetch_market_data_current_quote(client, provider, symbol, api_key)


async def _get_benchmark_prices(
    db: AsyncSession,
    provider: str,
    symbol: str,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
    today: date,
) -> tuple[list[dict], str]:
    cached_prices = await _read_cached_benchmark_prices(db, provider, symbol, start_date_value, end_date_value)
    now = datetime.now(timezone.utc)
    coverage_end_date = end_date_value
    if end_date_value == today and coverage_end_date > start_date_value:
        coverage_end_date = coverage_end_date - timedelta(days=1)
        while coverage_end_date.weekday() >= 5 and coverage_end_date > start_date_value:
            coverage_end_date -= timedelta(days=1)
    covers_historical_range = _cache_covers_range(cached_prices, start_date_value, coverage_end_date)
    needs_current_price = (
        end_date_value == today
        and today.weekday() < 5
        and _benchmark_provider_supports_current_price(provider)
    )
    current_price_is_fresh = (
        not needs_current_price
        or _same_day_cache_is_fresh(cached_prices, end_date_value, now)
    )
    if covers_historical_range and current_price_is_fresh:
        return cached_prices, "cached"

    if _benchmark_provider_requires_key(provider) and not api_key:
        return cached_prices, "missing_market_data_key" if not cached_prices else "cached_partial"

    if not covers_historical_range:
        try:
            fetched_prices = []
            async with httpx.AsyncClient(timeout=20) as client:
                if provider == MARKET_DATA_PROVIDER_POLYGON:
                    fetched_prices = await _fetch_polygon_benchmark_prices(
                        client,
                        symbol,
                        start_date_value,
                        coverage_end_date,
                        api_key,
                    )
                elif provider == MARKET_DATA_PROVIDER_TWELVE_DATA:
                    fetched_prices = await _fetch_twelve_data_benchmark_prices(
                        client,
                        symbol,
                        start_date_value,
                        coverage_end_date,
                        api_key,
                    )
                elif provider == MARKET_DATA_PROVIDER_FRED:
                    fetched_prices = await _fetch_fred_benchmark_prices(
                        client,
                        symbol,
                        start_date_value,
                        coverage_end_date,
                    )
                else:
                    fetched_prices = await _fetch_fmp_benchmark_prices(
                        client,
                        symbol,
                        start_date_value,
                        coverage_end_date,
                        api_key,
                    )
            if fetched_prices:
                await _upsert_benchmark_prices(db, provider, symbol, fetched_prices)
                cached_prices = await _read_cached_benchmark_prices(
                    db,
                    provider,
                    symbol,
                    start_date_value,
                    end_date_value,
                )
                covers_historical_range = _cache_covers_range(
                    cached_prices,
                    start_date_value,
                    coverage_end_date,
                )
            else:
                return cached_prices, "cached_stale" if cached_prices else "fetch_failed"
        except Exception as exc:
            logger.warning(
                "benchmark price fetch failed provider=%s symbol=%s: %s",
                provider,
                symbol,
                safe_market_data_error_message(exc),
            )
            return cached_prices, "fetch_failed" if not cached_prices else "cached_stale"

    if needs_current_price and not _same_day_cache_is_fresh(cached_prices, end_date_value, now):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                current_price = await _fetch_current_benchmark_price(client, provider, symbol, api_key)
            if current_price is not None:
                await _upsert_benchmark_prices(db, provider, symbol, [{
                    "date": end_date_value.isoformat(),
                    "close": current_price,
                }])
                cached_prices = await _read_cached_benchmark_prices(
                    db,
                    provider,
                    symbol,
                    start_date_value,
                    end_date_value,
                )
                return cached_prices, "ok" if covers_historical_range else "cached_partial"
        except Exception as exc:
            logger.warning(
                "current benchmark price fetch failed provider=%s symbol=%s: %s",
                provider,
                symbol,
                safe_market_data_error_message(exc),
            )
        return cached_prices, "current_quote_unavailable" if covers_historical_range else "cached_partial"

    return cached_prices, "ok" if covers_historical_range else "cached_partial"


async def _get_benchmark_prices_with_fallback(
    db: AsyncSession,
    preferred_provider: str,
    benchmark: dict,
    start_date_value: date,
    end_date_value: date,
    api_key: str,
    today: date,
) -> tuple[list[dict], str, str, str]:
    attempts: list[tuple[str, str, str]] = []
    preferred_symbol = benchmark["provider_symbols"].get(preferred_provider)
    if preferred_symbol and (api_key or not _benchmark_provider_requires_key(preferred_provider)):
        attempts.append((preferred_provider, preferred_symbol, api_key))

    fallback_symbol = benchmark["provider_symbols"].get(MARKET_DATA_PROVIDER_FRED)
    if fallback_symbol and preferred_provider != MARKET_DATA_PROVIDER_FRED:
        attempts.append((MARKET_DATA_PROVIDER_FRED, fallback_symbol, ""))

    last_status = "fetch_failed"
    partial_result: tuple[list[dict], str, str, str] | None = None
    for provider, symbol, provider_api_key in attempts:
        prices, status = await _get_benchmark_prices(
            db,
            provider,
            symbol,
            start_date_value,
            end_date_value,
            provider_api_key,
            today,
        )
        last_status = status
        if prices and status not in {"cached_partial", "cached_stale"}:
            final_status = status if provider == preferred_provider else f"fallback_{status}"
            return prices, final_status, provider, symbol
        if prices and partial_result is None:
            final_status = status if provider == preferred_provider else f"fallback_{status}"
            partial_result = (prices, final_status, provider, symbol)
        if provider == preferred_provider and status in {"missing_market_data_key", "fetch_failed"}:
            continue

    if partial_result is not None:
        return partial_result
    return [], last_status, preferred_provider, preferred_symbol or benchmark["symbol"]


def _benchmark_return_for_date(
    prices: list[dict],
    baseline_close: float | None,
    target_date: str,
    cursor_index: int,
) -> tuple[float | None, int]:
    if prices and (target_date < prices[0]["date"] or target_date > prices[-1]["date"]):
        return None, cursor_index
    while cursor_index + 1 < len(prices) and prices[cursor_index + 1]["date"] <= target_date:
        cursor_index += 1
    if cursor_index < 0 or not prices or baseline_close in (None, 0):
        return None, cursor_index
    close = prices[cursor_index]["close"]
    return ((close / baseline_close) - 1) * 100, cursor_index


def _merge_benchmark_returns(
    series: list[dict],
    benchmark_price_sets: dict[str, list[dict]],
) -> list[dict]:
    if not series:
        return []

    merged = [dict(point) for point in series]
    start_date_value = merged[0]["date"]

    for benchmark in BENCHMARKS:
        benchmark_id = benchmark["id"]
        prices = benchmark_price_sets.get(benchmark_id, [])
        if not prices:
            for point in merged:
                point[f"{benchmark_id}_return_pct"] = None
            continue

        baseline_index = _benchmark_baseline_index(prices, start_date_value)
        baseline_close = prices[baseline_index]["close"] if prices else None
        cursor_index = baseline_index

        for point in merged:
            benchmark_return, cursor_index = _benchmark_return_for_date(
                prices,
                baseline_close,
                point["date"],
                cursor_index,
            )
            point[f"{benchmark_id}_return_pct"] = benchmark_return

    return merged


async def get_portfolio_performance_payload(
    db: AsyncSession,
    user_id: int,
    account_ids: list[int] | None = None,
    timeline: str | None = "ALL",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    today = datetime.now(timezone.utc).astimezone(user_tz).date()
    bounds = _resolve_performance_bounds(timeline, start_date, end_date, today)
    primary_currency = await _get_primary_currency(db, user_id)

    accounts = await _load_investment_accounts(db, user_id, account_ids)
    account_ids_for_query = [account.id for account in accounts]
    account_currency_by_id = {
        account.id: str(account.currency or primary_currency).upper()
        for account in accounts
    }
    to_primary = await build_to_primary_converter(
        db, primary_currency, set(account_currency_by_id.values()), bounds.end_date.isoformat()
    )

    histories_by_account, valuation_dates = await _load_balance_history(
        db,
        user_id,
        account_ids_for_query,
        bounds.end_date,
        user_tz,
        to_primary,
        account_currency_by_id,
    )
    cash_flows_by_date_account, cash_flow_counts = await _load_external_cash_flows(
        db,
        user_id,
        account_ids_for_query,
        bounds.end_date,
        user_tz,
        to_primary,
    )
    portfolio_payload = _build_portfolio_performance_series(
        accounts,
        histories_by_account,
        valuation_dates,
        cash_flows_by_date_account,
        bounds,
    )

    performance_series = portfolio_payload["series"]
    effective_start_date = portfolio_payload["effective_start_date"]
    benchmark_price_sets: dict[str, list[dict]] = {}
    benchmark_payloads = []

    market_data_config = await get_effective_market_data_config(db, user_id)
    provider = market_data_config["provider"]
    api_key = market_data_config["api_key"]
    benchmark_start_date = effective_start_date or bounds.start_date or bounds.end_date
    benchmark_lookup_start_date = _benchmark_lookup_start_date(benchmark_start_date)

    for benchmark in BENCHMARKS:
        prices, benchmark_status, data_provider, provider_symbol = await _get_benchmark_prices_with_fallback(
            db,
            provider,
            benchmark,
            benchmark_lookup_start_date,
            bounds.end_date,
            api_key,
            today,
        )
        benchmark_price_sets[benchmark["id"]] = prices
        benchmark_return = _benchmark_period_return(prices, benchmark_start_date)
        benchmark_payloads.append({
            "id": benchmark["id"],
            "name": benchmark["name"],
            "symbol": benchmark["symbol"],
            "provider_symbol": provider_symbol,
            "provider": data_provider,
            "provider_label": _provider_label(data_provider),
            "as_of_date": prices[-1]["date"] if prices else None,
            "return_pct": benchmark_return,
            "status": benchmark_status,
        })

    series = _merge_benchmark_returns(performance_series, benchmark_price_sets)

    return {
        "status": "ok",
        "currency": primary_currency,
        "timeline": {
            "key": bounds.timeline,
            "start_date": _date_to_iso(effective_start_date or bounds.start_date),
            "end_date": bounds.end_date.isoformat(),
        },
        "accounts": {
            "selected": len(accounts),
            "ids": account_ids_for_query,
        },
        "summary": portfolio_payload["summary"],
        "series": series,
        "benchmarks": benchmark_payloads,
        "market_data": {
            "mode": market_data_config["mode"],
            "provider": provider,
            "provider_label": market_data_config["provider_label"],
            "source": market_data_config["source"],
            "has_key": bool(api_key),
            "config_revision": market_data_config["config_revision"],
        },
        "data_quality": {
            **cash_flow_counts,
            "cash_flow_types": sorted(EXTERNAL_CASH_FLOW_TYPES),
        },
    }
