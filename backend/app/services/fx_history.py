"""Historical daily FX rates.

Stores one `cad_per_unit` value per (date, currency) in `fx_rates`, sourced from
the Bank of Canada Valet API (authoritative for its CAD pairs) and Frankfurter /
ECB (everything else, no key, no rate limit). Any from->to conversion at a date
crosses through CAD. Current-rate conversion stays in `currency.py`; this module
is for converting historical *flows* (transactions, dividends) at their own date.
"""
import bisect
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import Account, FxRate, Setting, Transaction
from app.services.currency import get_fx_rates

logger = logging.getLogger("breaktwenty.fx_history")

FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"
BOC_VALET_URL = "https://www.bankofcanada.ca/valet/observations"
COINBASE_EXCHANGE_URL = "https://api.exchange.coinbase.com"

# Crypto held as account currencies; priced via Coinbase Exchange daily candles
# (USD close) and converted to CAD with the day's USD rate, then stored in
# `fx_rates` like any other currency so historical conversion just works.
CRYPTO_CURRENCIES = {"BTC", "ETH"}

# Currencies the Bank of Canada publishes a daily FX<CUR>CAD series for. BoC is
# authoritative for these CAD pairs; everything else comes from Frankfurter
# (ECB). CAD itself is implicitly 1.0 and is never fetched or stored.
BOC_SUPPORTED = {
    "USD", "EUR", "GBP", "JPY", "AUD", "CHF", "CNY", "HKD", "INR", "MXN",
    "NZD", "NOK", "SEK", "ZAR", "KRW", "SGD", "BRL", "TRY", "RUB", "SAR",
    "IDR", "MYR", "PEN", "THB", "TWD", "VND",
}

# Currencies the app offers for user selection (primary/display); keep in sync
# with the frontend SUPPORTED_CURRENCIES (constants/currencies.js). CAD is the
# base (implicit 1.0). Every non-CAD supported currency is always backfilled —
# even before any matching transaction exists — so switching the primary
# currency to any of them yields accurate historical conversion immediately.
SUPPORTED_CURRENCIES = ("CAD", "USD", "EUR", "GBP", "CHF", "CZK")
# Always-backfilled set: the user-selectable currencies (so switching primary is
# instant) PLUS crypto — BTC/ETH are priced even before any crypto account exists,
# so crypto balances convert immediately on first load. CAD is the implicit base.
_DEFAULT_CURRENCIES = {c for c in SUPPORTED_CURRENCIES if c != "CAD"} | CRYPTO_CURRENCIES


def _normalize(code: str | None) -> str:
    return str(code or "").strip().upper()


async def _fetch_frankfurter(currencies, start: str, end: str) -> dict[str, dict[str, float]]:
    """Return {date: {currency: cad_per_unit}} from Frankfurter (ECB, base EUR).

    rates[date][X] = units of X per 1 EUR, so cad_per_unit(X) = (CAD per EUR) /
    (X per EUR) = rates[CAD] / rates[X]; EUR itself is rates[CAD]; CAD is 1.0.
    """
    wanted = {c for c in (_normalize(c) for c in currencies) if c and c != "CAD"}
    if not wanted:
        return {}
    symbols = sorted(wanted | {"CAD"})
    out: dict[str, dict[str, float]] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{FRANKFURTER_BASE_URL}/{start}..{end}",
                params={"base": "EUR", "symbols": ",".join(symbols)},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Frankfurter fetch failed (%s..%s): %s", start, end, exc)
        return out
    for day, day_rates in (data.get("rates") or {}).items():
        cad_per_eur = day_rates.get("CAD")
        if not cad_per_eur:
            continue
        per_day: dict[str, float] = {}
        for cur in wanted:
            if cur == "EUR":
                per_day["EUR"] = float(cad_per_eur)
                continue
            x_per_eur = day_rates.get(cur)
            if x_per_eur:
                per_day[cur] = float(cad_per_eur) / float(x_per_eur)
        if per_day:
            out[day] = per_day
    return out


async def _fetch_boc(currencies, start: str, end: str) -> dict[str, dict[str, float]]:
    """Return {date: {currency: cad_per_unit}} from Bank of Canada Valet.

    Series FX<CUR>CAD is CAD per 1 CUR, i.e. cad_per_unit directly.
    """
    wanted = sorted(
        c for c in (_normalize(c) for c in currencies)
        if c and c != "CAD" and c in BOC_SUPPORTED
    )
    if not wanted:
        return {}
    series = ",".join(f"FX{cur}CAD" for cur in wanted)
    out: dict[str, dict[str, float]] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{BOC_VALET_URL}/{series}/json",
                params={"start_date": start, "end_date": end},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Bank of Canada fetch failed (%s..%s): %s", start, end, exc)
        return out
    for obs in (data.get("observations") or []):
        day = obs.get("d")
        if not day:
            continue
        per_day: dict[str, float] = {}
        for cur in wanted:
            entry = obs.get(f"FX{cur}CAD")
            value = entry.get("v") if isinstance(entry, dict) else None
            if value in (None, ""):
                continue
            try:
                per_day[cur] = float(value)
            except (TypeError, ValueError):
                continue
        if per_day:
            out[day] = per_day
    return out


async def _fetch_crypto(symbols, start: str, end: str, cad_per_usd: dict[str, float]) -> dict[str, dict[str, float]]:
    """Return {date: {SYM: cad_per_unit}} for held crypto, from Coinbase Exchange
    daily candles (USD close) x that day's CAD-per-USD rate. Only emits dates that
    have a USD rate (business days); the chart's nearest-prior lookup covers gaps.
    Coinbase caps candles at 300 rows/call, so the range is paged in 300-day chunks."""
    wanted = sorted({_normalize(s) for s in symbols} & CRYPTO_CURRENCIES)
    if not wanted or not cad_per_usd:
        return {}
    try:
        start_d = date.fromisoformat(str(start)[:10])
        end_d = date.fromisoformat(str(end)[:10])
    except ValueError:
        return {}
    out: dict[str, dict[str, float]] = {}
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "breaktwenty-fx/1.0"}) as client:
            for sym in wanted:
                seg_start = start_d
                while seg_start <= end_d:
                    seg_end = min(seg_start + timedelta(days=299), end_d)
                    resp = await client.get(
                        f"{COINBASE_EXCHANGE_URL}/products/{sym}-USD/candles",
                        params={
                            "granularity": 86400,
                            "start": seg_start.isoformat(),
                            "end": seg_end.isoformat(),
                        },
                    )
                    resp.raise_for_status()
                    for candle in (resp.json() or []):
                        # Coinbase candle: [time, low, high, open, close, volume].
                        if not isinstance(candle, list) or len(candle) < 5:
                            continue
                        day = datetime.fromtimestamp(int(candle[0]), tz=timezone.utc).date().isoformat()
                        usd_rate = cad_per_usd.get(day)
                        close_usd = candle[4]
                        if usd_rate and close_usd:
                            out.setdefault(day, {})[sym] = float(close_usd) * float(usd_rate)
                    seg_start = seg_end + timedelta(days=1)
    except Exception as exc:
        logger.warning("Coinbase crypto candles fetch failed (%s..%s): %s", start, end, exc)
    return out


async def backfill_fx_rates(db: AsyncSession, currencies, start: str, end: str) -> int:
    """Fetch [start, end] from Frankfurter + BoC and insert missing rows.

    Historical rates are immutable, so existing (date, currency) rows are left
    as-is; only missing rows are inserted (idempotent).
    """
    start, end = str(start), str(end)
    start_date = date.fromisoformat(start[:10])
    end_date = date.fromisoformat(end[:10])
    frank = await _fetch_frankfurter(currencies, start, end)
    boc = await _fetch_boc(currencies, start, end)

    merged: dict[tuple[str, str], tuple[float, str]] = {}
    for day, per_day in frank.items():
        for cur, cpu in per_day.items():
            merged[(day, cur)] = (cpu, "frankfurter")
    for day, per_day in boc.items():
        for cur, cpu in per_day.items():
            merged[(day, cur)] = (cpu, "boc")

    # Crypto: USD candle close -> CAD via each day's USD rate. Prefer the USD rates just
    # fetched, but if USD wasn't part of this segment (it's already covered, so a
    # crypto-only gap segment never requests it) fall back to stored USD rates for the
    # range — otherwise crypto historical backfill silently fetches nothing and crypto is
    # stuck with only the recent refresh window.
    cad_per_usd = {day: cpu for (day, cur), (cpu, _src) in merged.items() if cur == "USD"}
    if {_normalize(c) for c in currencies} & CRYPTO_CURRENCIES:
        stored_usd = (
            await db.execute(
                select(FxRate.date, FxRate.cad_per_unit)
                .where(
                    FxRate.currency == "USD",
                    FxRate.date >= start_date,
                    FxRate.date <= end_date,
                )
            )
        ).all()
        for row in stored_usd:
            cad_per_usd.setdefault(row.date.isoformat(), float(row.cad_per_unit))
    for day, per_day in (await _fetch_crypto(currencies, start, end, cad_per_usd)).items():
        for cur, cpu in per_day.items():
            merged[(day, cur)] = (cpu, "coinbase")

    if not merged:
        return 0

    # Cover the full date span actually returned: providers sometimes include a
    # boundary day just outside [start, end], and checking only [start, end] would
    # try to re-insert an already-stored out-of-range day and hit the
    # UNIQUE(date, currency) constraint (this was the recurring "fx daily refresh
    # failed" warning).
    merged_dates = {day for (day, _cur) in merged}
    span_lo = min(start, min(merged_dates))
    span_hi = max(end, max(merged_dates))
    span_lo_date = date.fromisoformat(span_lo[:10])
    span_hi_date = date.fromisoformat(span_hi[:10])
    existing = {
        (row.date.isoformat(), row.currency)
        for row in (
            await db.execute(
                select(FxRate.date, FxRate.currency)
                .where(FxRate.date >= span_lo_date, FxRate.date <= span_hi_date)
            )
        ).all()
    }
    inserted = 0
    for (day, cur), (cpu, _) in merged.items():
        if (day, cur) in existing or not cpu or cpu <= 0:
            continue
        db.add(FxRate(
            date=date.fromisoformat(day[:10]),
            currency=cur,
            cad_per_unit=Decimal(str(cpu)),
        ))
        inserted += 1
    if inserted:
        await db.commit()
    return inserted


async def _needed_currencies(db: AsyncSession) -> set[str]:
    # Held transaction currencies plus every user's saved primary currency, so a
    # primary the user doesn't actually transact in (e.g. CHF) still gets full
    # historical rates instead of falling back to today's rate for past dates.
    tx_rows = (await db.execute(select(func.distinct(Transaction.currency)))).scalars().all()
    acct_rows = (await db.execute(select(func.distinct(Account.currency)))).scalars().all()
    primary_rows = (
        await db.execute(
            select(func.distinct(Setting.value)).where(Setting.key == "primary_currency")
        )
    ).scalars().all()
    currencies = (
        {_normalize(c) for c in tx_rows}
        | {_normalize(c) for c in acct_rows}
        | {_normalize(c) for c in primary_rows}
    )
    currencies.discard("")
    currencies.discard("CAD")
    return currencies | _DEFAULT_CURRENCIES


async def _earliest_transaction_date(db: AsyncSession):
    return (await db.execute(select(func.min(Transaction.date)))).scalar_one_or_none()


async def ensure_fx_backfill(db: AsyncSession) -> int:
    """Backfill fx_rates to cover [earliest transaction date, today] for every
    needed currency, filling only each currency's own missing date span
    (idempotent).

    Coverage is tracked per currency rather than globally, so a newly-added
    currency whose transactions fall inside the already-covered global date
    range still gets its full history fetched instead of only the shared edges.
    """
    currencies = await _needed_currencies(db)
    if not currencies:
        return 0
    today = datetime.now(timezone.utc).date()

    earliest = await _earliest_transaction_date(db)
    if isinstance(earliest, date) and not isinstance(earliest, datetime):
        start_date = earliest
    elif isinstance(earliest, datetime):
        start_date = earliest.date()
    elif isinstance(earliest, str) and len(earliest) >= 10:
        start_date = date.fromisoformat(earliest[:10])
    else:
        # No transactions yet (fresh install, pre-import): seed a generous 10-year
        # window so a subsequent history import is already FX-covered without
        # waiting for the next startup/daily refresh to extend it. Imports OLDER
        # than this still self-heal — the next ensure_fx_backfill run sees
        # earliest_tx < stored_min and fetches the earlier gap segment below.
        start_date = today - timedelta(days=365 * 10)
    start_str, today_str = start_date.isoformat(), today.isoformat()

    # Per-currency stored coverage (earliest/latest date each currency holds).
    coverage = {
        row.currency: (row.min_date.isoformat(), row.max_date.isoformat())
        for row in (
            await db.execute(
                select(
                    FxRate.currency,
                    func.min(FxRate.date).label("min_date"),
                    func.max(FxRate.date).label("max_date"),
                )
                .where(FxRate.currency.in_(currencies))
                .group_by(FxRate.currency)
            )
        ).all()
    }

    # Group currencies by the exact date segment they still need so currencies
    # sharing a gap are fetched together in one API call.
    segments: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cur in currencies:
        cmin, cmax = coverage.get(cur, (None, None))
        if cmin is None:  # nothing stored yet -> fetch the whole range
            segments[(start_str, today_str)].add(cur)
            continue
        if start_str < cmin:
            segments[(start_str, cmin)].add(cur)
        if cmax < today_str:
            segments[(cmax, today_str)].add(cur)

    total = 0
    for (seg_start, seg_end), seg_currencies in segments.items():
        total += await backfill_fx_rates(db, seg_currencies, seg_start, seg_end)
    if total:
        logger.info("fx_rates backfill inserted %d rows", total)
    return total


async def refresh_recent_fx_rates(db: AsyncSession, days: int = 10) -> int:
    """Re-fetch the last `days` days to pick up newly published rates."""
    currencies = await _needed_currencies(db)
    today = datetime.now(timezone.utc).date()
    return await backfill_fx_rates(
        db, currencies, (today - timedelta(days=days)).isoformat(), today.isoformat()
    )


async def run_fx_daily_refresh() -> None:
    """Scheduler/startup entrypoint: ensure backfill then refresh recent rates."""
    try:
        async with async_session() as db:
            await ensure_fx_backfill(db)
            await refresh_recent_fx_rates(db)
    except Exception as exc:
        logger.warning("fx daily refresh failed: %s", exc)


async def _load_cad_rate_map(db: AsyncSession, currencies, end: str) -> dict[str, tuple[list[str], list[float]]]:
    """Preload {currency: (sorted dates, cad_per_unit)} for in-memory lookup."""
    wanted = {_normalize(c) for c in currencies}
    wanted.discard("")
    wanted.discard("CAD")
    if not wanted:
        return {}
    end_date = date.fromisoformat(str(end)[:10])
    rows = (
        await db.execute(
            select(FxRate.currency, FxRate.date, FxRate.cad_per_unit)
            .where(FxRate.currency.in_(wanted), FxRate.date <= end_date)
            .order_by(FxRate.currency, FxRate.date)
        )
    ).all()
    out: dict[str, tuple[list[str], list[float]]] = {}
    for row in rows:
        dates, cpus = out.setdefault(row.currency, ([], []))
        dates.append(row.date.isoformat())
        cpus.append(float(row.cad_per_unit))
    return out


def _cad_per_unit_from_map(rate_map, currency: str, day: str):
    cur = _normalize(currency)
    if cur == "CAD":
        return 1.0
    pair = rate_map.get(cur)
    if not pair:
        return None
    dates, cpus = pair
    idx = bisect.bisect_right(dates, str(day)[:10]) - 1
    if idx < 0:
        return cpus[0] if cpus else None
    return cpus[idx]


async def build_to_primary_converter(db: AsyncSession, primary_currency: str, currencies, end: str):
    """Return a sync `to_primary(amount, currency, day)` that converts a flow to
    the primary currency using the historical rate on `day` (nearest-prior),
    falling back to the current rate. Loads all needed rates once up front."""
    primary = _normalize(primary_currency) or "CAD"
    needed = {_normalize(c) for c in currencies}
    needed.add(primary)
    rate_map = await _load_cad_rate_map(db, needed, end)
    current = await get_fx_rates("CAD")  # current[X] = X per 1 CAD

    def cad_per_unit(currency: str, day: str) -> float:
        cur = _normalize(currency)
        if not cur or cur == "CAD":
            return 1.0
        value = _cad_per_unit_from_map(rate_map, cur, day)
        if value is not None:
            return value
        rate = current.get(cur)
        return (1.0 / float(rate)) if rate else 1.0

    def to_primary(amount, currency: str, day: str) -> float:
        numeric = float(amount or 0)
        cur = _normalize(currency)
        if not cur or cur == primary:
            return numeric
        cad_to = cad_per_unit(primary, day)
        if not cad_to:
            return numeric
        return numeric * cad_per_unit(cur, day) / cad_to

    return to_primary
