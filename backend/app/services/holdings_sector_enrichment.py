from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Holding, SymbolSector
from app.services.market_data_logging import raise_safe_market_data_status
from app.services.market_data_settings import (
    MARKET_DATA_PROVIDER_FMP,
    MARKET_DATA_PROVIDER_POLYGON,
    MARKET_DATA_PROVIDER_TWELVE_DATA,
    get_effective_market_data_config,
)

FMP_PROFILE_URL = "https://financialmodelingprep.com/stable/profile"
FMP_ETF_INFO_URL = "https://financialmodelingprep.com/stable/etf/info"
POLYGON_TICKER_DETAILS_URL = "https://api.polygon.io/v3/reference/tickers/{ticker}"
TWELVE_DATA_PROFILE_URL = "https://api.twelvedata.com/profile"
logger = logging.getLogger("breaktwenty.holdings_sector_enrichment")
ELIGIBLE_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
CASH_CURRENCY_CODES = {
    "CAD",
    "USD",
    "EUR",
    "JPY",
    "GBP",
    "CHF",
    "AUD",
    "HKD",
    "NZD",
    "SEK",
    "NOK",
    "DKK",
    "SGD",
    "CNH",
}
MARKET_DATA_ABORT_STATUSES = {401, 402, 403, 429}
CRYPTO_WALLET_SYMBOLS = {"BTC", "ETH", "SOL", "USDC"}
ETF_LIKE_NAME_MARKERS = (
    " ETF",
    "ETF ",
    " ETF-",
    "INDEX ETF",
    "INDEX FUND",
    "INDEX FD",
    "MUTUAL FUND",
    "EXCHANGE TRADED FUND",
    " ULTRAYLD",
    " ENH YLD",
    " YLD IDX",
)
FUND_LIKE_NAME_MARKERS = (
    " FUND ",
    " FUND-",
    " INCOME SHARES",
    " ETF TRUST",
    " IDX FD",
    " HIGH INCOME",
    " HIGH INC ",
    " COVERED CALL",
    " ENHANCED YIELD",
    " ENHANCED INCOME",
    " OPTION INCOME",
    " MONTHLY INCOME",
    " YIELD ETF",
    " INCOME ETF",
    " INCOME FUND",
    " STRATEGY ETF",
    " STRATEGY FUND",
    " SPLIT SHARE",
    " SPLIT CORP",
)
ETF_WRAPPER_NAME_MARKERS = (
    " BULL ",
    " BEAR ",
    " LONG ",
    " SHORT ",
    " INVERSE ",
    " LEVERAGED ",
    " DAILY ",
    " SINGLE STOCK",
    " SINGLE-STOCK",
    " ULTRA ",
)
FUND_DESCRIPTION_MARKERS = (
    " EXCHANGE TRADED FUND",
    " INDEX ETF",
    " INDEX FUND",
    " MUTUAL FUND",
    " NET OF EXPENSES",
    " SEEKS TO REPLICATE",
    " SEEKS TO TRACK",
    " UNITS OF THE ETF",
    " COVERED CALL",
    " ENHANCED YIELD",
    " ENHANCED INCOME",
    " OPTION INCOME",
    " MONTHLY INCOME",
    " SPLIT SHARE",
    " SPLIT CORP",
)
CANADIAN_NORMALIZATION_SUFFIXES = (".TO", ".UN", ".U", ".V", ".CN", ".NE")
CANADIAN_EXCHANGE_SUFFIX_CANDIDATES = {
    "TSE": (".TO",),
    "TSX": (".TO",),
    "TSXV": (".V",),
    "VENTURE": (".V",),
    "CSE": (".CN",),
    "CNQ": (".CN",),
    "NEO": (".NE", ".CN"),
    "AEQLIT": (".NE", ".CN"),
}
CANADIAN_SUFFIX_SIBLING_CANDIDATES = {
    ".TO": (".TO", ".NE", ".CN"),
    ".NE": (".NE", ".CN", ".TO"),
    ".CN": (".CN", ".NE", ".TO"),
    ".V": (".V", ".TO", ".CN"),
    ".U": (".TO", ".NE", ".CN"),
    ".UN": (".TO", ".NE", ".CN"),
}
COMMON_STOCK_NAME_MARKERS = (
    " INC",
    " CORP",
    " CORPORATION",
    " TECHNOLOGIES",
    " MARKETS",
    " COMMON STOCK",
)
LEVERAGE_TOKEN_PATTERN = re.compile(r"(?<![A-Z0-9])(?:-?\d+(?:\.\d+)?X)(?![A-Z0-9])")


def _normalize_sector_value(value: str | None) -> str | None:
    sector = str(value or "").strip()
    return sector or None


def _is_market_data_abort_code(value) -> bool:
    try:
        return int(value) in MARKET_DATA_ABORT_STATUSES
    except (TypeError, ValueError):
        return False


def _is_reusable_cached_classification(row: SymbolSector) -> bool:
    return (
        row.sector is not None
        or _is_fund_instrument_kind(row.instrument_kind)
        or str(row.source or "").strip().lower() == "local"
    )


def _log_provider_denial(provider: str, endpoint: str, status_code: int) -> None:
    logger.warning(
        "holdings sector enrichment provider denied request provider=%s endpoint=%s status=%s",
        provider,
        endpoint,
        int(status_code),
    )


def _get_symbol_family_key(symbol: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    for suffix in CANADIAN_NORMALIZATION_SUFFIXES:
        if normalized_symbol.endswith(suffix):
            return normalized_symbol[: -len(suffix)].strip() or normalized_symbol
    return normalized_symbol


def _looks_like_wrapped_etf_or_fund_name(name: str) -> bool:
    normalized_name = f" {str(name or '').strip().upper()} "

    if not normalized_name.strip():
        return False
    if any(marker in normalized_name for marker in ETF_WRAPPER_NAME_MARKERS) and (
        LEVERAGE_TOKEN_PATTERN.search(normalized_name)
        or " ETF" in normalized_name
        or " FUND" in normalized_name
        or " SHARES" in normalized_name
    ):
        return True
    return False


def _looks_like_etf_or_fund(symbol: str, name: str) -> bool:
    if any(marker in name for marker in ETF_LIKE_NAME_MARKERS):
        return True
    if any(marker in name for marker in FUND_LIKE_NAME_MARKERS):
        return True
    if _looks_like_wrapped_etf_or_fund_name(name):
        return True
    if symbol.endswith(".U") and "SHARES" in name:
        return True
    return False


def _looks_like_fund_description(description: str) -> bool:
    normalized_description = f" {str(description or '').strip().upper()} "

    if not normalized_description.strip():
        return False
    return any(marker in normalized_description for marker in FUND_DESCRIPTION_MARKERS)


def _is_fund_instrument_kind(value: str | None) -> bool:
    normalized_value = str(value or "").strip().lower()
    return normalized_value in {"etf", "fund"}


def _is_wallet_or_cash_like(symbol: str, name: str) -> bool:
    if symbol in CASH_CURRENCY_CODES and "CASH" in name:
        return True
    if symbol in CRYPTO_WALLET_SYMBOLS and "WALLET" in name:
        return True
    if " WALLET" in name:
        return True
    return False


def _holding_has_strong_fund_evidence(
    holding: Holding,
    fund_like_symbol_families: set[str],
) -> bool:
    symbol = _normalize_holding_symbol(holding.symbol)
    name = str(holding.name or "").strip().upper()
    return _looks_like_etf_or_fund(symbol, name) or _get_symbol_family_key(symbol) in fund_like_symbol_families


def _get_canadian_symbol_roots(symbol: str) -> tuple[str, ...]:
    normalized_symbol = str(symbol or "").strip().upper()
    roots: list[str] = []

    if ELIGIBLE_TICKER_PATTERN.fullmatch(normalized_symbol):
        roots.append(normalized_symbol)

    family_key = _get_symbol_family_key(normalized_symbol)
    if family_key and family_key != normalized_symbol and ELIGIBLE_TICKER_PATTERN.fullmatch(family_key):
        roots.append(family_key)

    if "." in family_key:
        dot_root = family_key.split(".", 1)[0].strip()
        if dot_root and dot_root != family_key and ELIGIBLE_TICKER_PATTERN.fullmatch(dot_root):
            roots.append(dot_root)

    return tuple(dict.fromkeys(roots))


def _get_canadian_family_members(symbol: str) -> tuple[str, ...]:
    roots = _get_canadian_symbol_roots(symbol)
    family_members: list[str] = []

    for root in roots:
        if ELIGIBLE_TICKER_PATTERN.fullmatch(root):
            family_members.append(root)
        for suffix in CANADIAN_NORMALIZATION_SUFFIXES:
            candidate = f"{root}{suffix}"
            if ELIGIBLE_TICKER_PATTERN.fullmatch(candidate):
                family_members.append(candidate)

    return tuple(dict.fromkeys(family_members))


def _normalize_canadian_lookup_candidates(symbol: str, exchange_hint: str | None = None) -> tuple[str, ...]:
    normalized_symbol = str(symbol or "").strip().upper()
    candidate_roots = _get_canadian_symbol_roots(normalized_symbol)

    sibling_suffixes: tuple[str, ...] = ()
    for suffix in CANADIAN_NORMALIZATION_SUFFIXES:
        if normalized_symbol.endswith(suffix):
            sibling_suffixes = CANADIAN_SUFFIX_SIBLING_CANDIDATES.get(suffix, ())
            break

    decorated_suffixes = CANADIAN_EXCHANGE_SUFFIX_CANDIDATES.get(str(exchange_hint or "").strip().upper(), ())
    candidate_suffixes = sibling_suffixes or decorated_suffixes
    if candidate_suffixes and candidate_roots:
        candidates: list[str] = []
        for root in candidate_roots:
            for suffix in candidate_suffixes:
                candidate = f"{root}{suffix}"
                if ELIGIBLE_TICKER_PATTERN.fullmatch(candidate):
                    candidates.append(candidate)
        candidates.extend(candidate_roots)
        return tuple(dict.fromkeys(candidates))

    return (normalized_symbol,)


def _get_lookup_candidates(
    holding: Holding,
    allow_fund_lookups: bool = False,
) -> tuple[tuple[str, ...], str]:
    raw_symbol = str(holding.symbol or "").strip().upper()
    exchange_hint = None
    symbol = raw_symbol
    # Strip IBKR exchange suffix (e.g. "BANK @TSE" → "BANK", "BEP.UN @TSE" → "BEP.UN")
    # The suffix is kept in holdings.symbol for display but should not reach FMP.
    if " @" in raw_symbol:
        symbol, exchange_hint = raw_symbol.split(" @", 1)
        symbol = symbol.strip()
        exchange_hint = exchange_hint.strip()
    name = str(holding.name or "").strip().upper()
    is_fund_like = _looks_like_etf_or_fund(symbol, name)

    if not symbol:
        return (), "missing_symbol"
    if " " in symbol or "/" in symbol or ":" in symbol:
        return (), "complex_symbol"
    if not ELIGIBLE_TICKER_PATTERN.fullmatch(symbol):
        return (), "ineligible_symbol"
    if _is_wallet_or_cash_like(symbol, name):
        return (), "cash_or_wallet"
    if is_fund_like and not allow_fund_lookups:
        return (), "etf_or_fund"

    candidates = _normalize_canadian_lookup_candidates(symbol, exchange_hint)
    if len(candidates) > 1:
        return candidates, "eligible_canadian_normalized_fund" if is_fund_like else "eligible_canadian_normalized"

    return candidates, "eligible_fund_lookup" if is_fund_like else "eligible"


def _get_lookup_priority(holding: Holding) -> tuple[int, float, str]:
    symbol = _normalize_holding_symbol(holding.symbol)
    name = str(holding.name or "").strip().upper()
    market_value = float(holding.market_value or 0)
    common_stock_penalty = 0 if any(marker in name for marker in COMMON_STOCK_NAME_MARKERS) else 1
    dot_penalty = 1 if "." in symbol else 0
    return (common_stock_penalty, dot_penalty, -market_value, symbol)


def _extract_payload_item(payload: object) -> dict | None:
    if isinstance(payload, list):
        first_item = payload[0] if payload else None
        return first_item if isinstance(first_item, dict) else None
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict):
            return results
        if isinstance(results, list):
            first_item = results[0] if results else None
            return first_item if isinstance(first_item, dict) else None
        return payload
    return None


def _extract_entity_name(item: dict | None) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("companyName", "name", "fundName", "symbolName"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return None


def _has_explicit_fund_metadata(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("isEtf") is True or item.get("isETF") is True or item.get("isFund") is True:
        return True

    metadata_values = [
        item.get("type"),
        item.get("instrumentType"),
        item.get("securityType"),
        item.get("assetType"),
        item.get("quoteType"),
        item.get("category"),
        item.get("exchangeTradedFund"),
        item.get("fundFamily"),
    ]
    for value in metadata_values:
        normalized = str(value or "").strip().upper()
        if not normalized:
            continue
        if any(marker in normalized for marker in ("ETF", "FUND", "TRUST", "MUTUAL", "EXCHANGE TRADED")):
            return True
    return False


def _provider_indicates_fund(
    symbol: str,
    item: dict | None,
    entity_name: str | None,
) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_name = str(entity_name or "").strip().upper()
    description = str((item or {}).get("description") or "").strip().upper()

    return (
        _has_explicit_fund_metadata(item)
        or _looks_like_etf_or_fund(normalized_symbol, normalized_name)
        or _looks_like_fund_description(description)
    )


def _is_low_signal_entity_name(symbol: str, entity_name: str | None) -> bool:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_name = str(entity_name or "").strip().upper()

    return not normalized_name or normalized_name == normalized_symbol


async def _fetch_polygon_classification(
    client: httpx.AsyncClient,
    api_key: str,
    ticker: str,
) -> tuple[str | None, str | None, str | None, bool]:
    response = await client.get(
        POLYGON_TICKER_DETAILS_URL.format(ticker=ticker),
        params={"apiKey": api_key},
    )

    if response.status_code == 404:
        return None, None, None, False
    if response.status_code in MARKET_DATA_ABORT_STATUSES:
        _log_provider_denial(MARKET_DATA_PROVIDER_POLYGON, "ticker_details", response.status_code)
        return None, None, None, True

    raise_safe_market_data_status(response)
    item = _extract_payload_item(response.json())
    entity_name = _extract_entity_name(item)
    sector = _normalize_sector_value((item or {}).get("sic_description"))
    instrument_kind = "fund" if _provider_indicates_fund(ticker, item, entity_name) else None
    if instrument_kind == "fund":
        sector = None
    return sector, entity_name, instrument_kind, False


async def _fetch_fmp_profile_classification(
    client: httpx.AsyncClient,
    api_key: str,
    ticker: str,
) -> tuple[str | None, str | None, str | None, bool]:
    response = await client.get(
        FMP_PROFILE_URL,
        params={"symbol": ticker, "apikey": api_key},
    )

    if response.status_code == 404:
        return None, None, None, False
    if response.status_code in MARKET_DATA_ABORT_STATUSES:
        _log_provider_denial(MARKET_DATA_PROVIDER_FMP, "profile", response.status_code)
        return None, None, None, True

    raise_safe_market_data_status(response)
    item = _extract_payload_item(response.json())
    entity_name = _extract_entity_name(item)
    sector = _normalize_sector_value((item or {}).get("sector"))
    instrument_kind = "fund" if _provider_indicates_fund(ticker, item, entity_name) else None
    if instrument_kind == "fund":
        sector = None
    return sector, entity_name, instrument_kind, False


async def _fetch_fmp_fund_classification(
    client: httpx.AsyncClient,
    api_key: str,
    ticker: str,
) -> tuple[str | None, bool]:
    response = await client.get(
        FMP_ETF_INFO_URL,
        params={"symbol": ticker, "apikey": api_key},
    )

    if response.status_code == 404:
        return None, False
    if response.status_code in MARKET_DATA_ABORT_STATUSES:
        _log_provider_denial(MARKET_DATA_PROVIDER_FMP, "etf_info", response.status_code)
        return None, True

    raise_safe_market_data_status(response)
    item = _extract_payload_item(response.json())
    return _extract_entity_name(item), False


async def _fetch_twelve_data_profile_classification(
    client: httpx.AsyncClient,
    api_key: str,
    ticker: str,
) -> tuple[str | None, str | None, str | None, bool]:
    response = await client.get(
        TWELVE_DATA_PROFILE_URL,
        params={"symbol": ticker, "apikey": api_key},
    )

    if response.status_code == 404:
        return None, None, None, False
    if response.status_code in MARKET_DATA_ABORT_STATUSES:
        _log_provider_denial(MARKET_DATA_PROVIDER_TWELVE_DATA, "profile", response.status_code)
        return None, None, None, True

    raise_safe_market_data_status(response)
    item = _extract_payload_item(response.json())
    if item and str(item.get("status") or "").strip().lower() == "error":
        if _is_market_data_abort_code(item.get("code")):
            _log_provider_denial(
                MARKET_DATA_PROVIDER_TWELVE_DATA,
                "profile",
                int(item["code"]),
            )
            return None, None, None, True
        return None, None, None, False

    entity_name = _extract_entity_name(item)
    sector = _normalize_sector_value((item or {}).get("sector"))
    instrument_kind = "fund" if _provider_indicates_fund(ticker, item, entity_name) else None
    if instrument_kind == "fund":
        sector = None
    return sector, entity_name, instrument_kind, False


async def _fetch_market_data_classification(
    provider: str,
    client: httpx.AsyncClient,
    api_key: str,
    ticker: str,
) -> tuple[str | None, str | None, str | None, bool]:
    if provider == MARKET_DATA_PROVIDER_POLYGON:
        return await _fetch_polygon_classification(client, api_key, ticker)
    if provider == MARKET_DATA_PROVIDER_TWELVE_DATA:
        return await _fetch_twelve_data_profile_classification(client, api_key, ticker)

    sector, entity_name, instrument_kind, should_abort = await _fetch_fmp_profile_classification(client, api_key, ticker)
    if should_abort or instrument_kind == "fund":
        return sector, entity_name, instrument_kind, should_abort

    should_confirm_fund_metadata = sector is None or _is_low_signal_entity_name(ticker, entity_name)

    if should_confirm_fund_metadata:
        fund_name, should_abort = await _fetch_fmp_fund_classification(client, api_key, ticker)
        if should_abort:
            return None, None, None, True
        if fund_name:
            return None, fund_name, "fund", False

    if sector:
        return sector, entity_name, None, False

    return sector, entity_name, None, False


def _normalize_holding_symbol(symbol: str | None) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    if " @" in normalized_symbol:
        normalized_symbol = normalized_symbol.split(" @", 1)[0].strip()
    return normalized_symbol


def _group_holdings_by_symbol(holdings: list[Holding]) -> dict[str, list[Holding]]:
    symbol_map: dict[str, list[Holding]] = defaultdict(list)
    for holding in holdings:
        normalized_symbol = _normalize_holding_symbol(holding.symbol)
        if normalized_symbol:
            symbol_map[normalized_symbol].append(holding)
    return symbol_map


def _get_sample_holding(holdings: list[Holding]) -> Holding:
    return max(holdings, key=lambda holding: float(holding.market_value or 0))


async def _load_symbol_sector_cache(
    db: AsyncSession,
    lookup_symbols: list[str],
) -> dict[str, SymbolSector]:
    if not lookup_symbols:
        return {}

    with db.no_autoflush:
        existing_rows = await db.execute(
            select(SymbolSector).where(SymbolSector.symbol.in_(lookup_symbols))
        )
    return {
        row.symbol: row
        for row in existing_rows.scalars().all()
        if _is_reusable_cached_classification(row)
    }


async def _upsert_symbol_sector(
    db: AsyncSession,
    symbol_sector_cache: dict[str, SymbolSector],
    *,
    symbol: str,
    sector: str | None,
    instrument_kind: str | None,
    source: str,
) -> SymbolSector:
    cached_row = symbol_sector_cache.get(symbol)

    if cached_row is not None:
        cached_row.sector = sector
        cached_row.instrument_kind = instrument_kind
        cached_row.source = source
        return cached_row

    with db.no_autoflush:
        merged_row = await db.merge(
            SymbolSector(
                symbol=symbol,
                sector=sector,
                instrument_kind=instrument_kind,
                source=source,
            )
        )
    symbol_sector_cache[symbol] = merged_row
    return merged_row


async def _apply_symbol_sector_updates(
    db: AsyncSession,
    symbol_sector_cache: dict[str, SymbolSector],
    symbol_sector_updates: dict[str, tuple[str | None, str | None, str]],
) -> None:
    for sym, (sector, instrument_kind, source) in symbol_sector_updates.items():
        await _upsert_symbol_sector(
            db,
            symbol_sector_cache,
            symbol=sym,
            sector=sector,
            instrument_kind=instrument_kind,
            source=source,
        )


def _find_family_fund_row(
    symbol: str,
    family_members_by_symbol: dict[str, tuple[str, ...]],
    symbol_sector_cache: dict[str, SymbolSector],
) -> SymbolSector | None:
    return next(
        (
            cached_row
            for candidate in family_members_by_symbol[symbol]
            if (cached_row := symbol_sector_cache.get(candidate))
            and cached_row.sector is None
            and _is_fund_instrument_kind(cached_row.instrument_kind)
        ),
        None,
    )


def _apply_sector_to_holdings(
    holdings: list[Holding],
    sector: str | None,
) -> bool:
    changed = False
    for holding in holdings:
        if holding.sector != sector:
            holding.sector = sector
            changed = True
    return changed


def _symbol_sector_fields(
    symbol: str,
    symbol_sector_cache: dict[str, SymbolSector],
    symbol_sector_updates: dict[str, tuple[str | None, str | None, str]],
) -> tuple[str | None, str | None]:
    pending_update = symbol_sector_updates.get(symbol)
    if pending_update is not None:
        return pending_update[0], pending_update[1]
    cached_row = symbol_sector_cache.get(symbol)
    if cached_row is None:
        return None, None
    return cached_row.sector, cached_row.instrument_kind


async def _classify_symbol_from_candidates(
    provider: str,
    client: httpx.AsyncClient,
    api_key: str,
    candidates: tuple[str, ...],
) -> tuple[str | None, str | None, bool]:
    for candidate in candidates:
        try:
            sector, _, instrument_kind, should_abort = await _fetch_market_data_classification(
                provider,
                client,
                api_key,
                candidate,
            )
        except httpx.HTTPError:
            continue

        if should_abort:
            return None, None, True
        if instrument_kind == "fund":
            return None, "etf", False
        if sector:
            return sector, None, False

    return None, None, False


async def enrich_holding_rows_with_sectors(
    db: AsyncSession,
    user_id: int,
    holdings: list[Holding],
) -> bool:
    if not holdings:
        return False

    with db.no_autoflush:
        market_data_config = await get_effective_market_data_config(db, user_id)
    provider = market_data_config["provider"]
    api_key = market_data_config["api_key"]

    # Phase 1: symbol normalization and family grouping.
    symbol_map = _group_holdings_by_symbol(holdings)
    if not symbol_map:
        return False

    all_symbols = list(symbol_map.keys())
    sample_holding_by_symbol = {
        symbol: _get_sample_holding(symbol_holdings)
        for symbol, symbol_holdings in symbol_map.items()
    }
    family_members_by_symbol = {
        sym: _get_canadian_family_members(sym)
        for sym in all_symbols
    }
    family_lookup_symbols = sorted({
        candidate
        for members in family_members_by_symbol.values()
        for candidate in members
    })

    # Phase 2: cache load and family ETF propagation.
    symbol_sector_cache = await _load_symbol_sector_cache(db, family_lookup_symbols)
    symbol_sector_updates: dict[str, tuple[str | None, str | None, str]] = {}

    for sym in all_symbols:
        cached_sector, cached_instrument_kind = _symbol_sector_fields(
            sym,
            symbol_sector_cache,
            symbol_sector_updates,
        )
        if cached_sector is not None or _is_fund_instrument_kind(cached_instrument_kind):
            continue

        cached = symbol_sector_cache.get(sym)
        family_fund_row = _find_family_fund_row(sym, family_members_by_symbol, symbol_sector_cache)
        if family_fund_row is None:
            continue

        symbol_sector_updates[sym] = (
            None,
            "etf",
            (cached.source or family_fund_row.source or "local")
            if cached
            else (family_fund_row.source or "local"),
        )

    # Phase 3: immediate holding-sector application from the current cache view.
    changed = False
    for sym, sym_holdings in symbol_map.items():
        if sym in symbol_sector_cache or sym in symbol_sector_updates:
            sector, _instrument_kind = _symbol_sector_fields(
                sym,
                symbol_sector_cache,
                symbol_sector_updates,
            )
            changed = _apply_sector_to_holdings(sym_holdings, sector) or changed
            continue

        changed = _apply_sector_to_holdings(sym_holdings, None) or changed

    # Phase 4: unresolved lookup preparation.
    fund_like_symbol_families = {
        _get_symbol_family_key(sym)
        for sym in all_symbols
        if _looks_like_etf_or_fund(
            sym,
            str(sample_holding_by_symbol[sym].name or "").strip().upper(),
        )
    }

    unresolved: dict[str, dict] = {}
    skip_counts: dict[str, int] = defaultdict(int)
    for sym, sym_holdings in symbol_map.items():
        if sym in symbol_sector_cache or sym in symbol_sector_updates:
            continue

        sample = sample_holding_by_symbol[sym]
        strong_fund = _holding_has_strong_fund_evidence(sample, fund_like_symbol_families)
        candidates, reason = _get_lookup_candidates(
            sample,
            allow_fund_lookups=provider == MARKET_DATA_PROVIDER_FMP,
        )
        if strong_fund or not candidates:
            skip_counts[reason if not strong_fund else "fund_like_local"] += 1
            symbol_sector_updates[sym] = (None, "etf" if strong_fund else None, "local")
            continue

        unresolved[sym] = {
            "holdings": sym_holdings,
            "sample": sample,
            "candidates": candidates,
        }

    if not unresolved:
        await _apply_symbol_sector_updates(db, symbol_sector_cache, symbol_sector_updates)
        return changed or bool(symbol_sector_updates)

    if not api_key:
        await _apply_symbol_sector_updates(db, symbol_sector_cache, symbol_sector_updates)
        return changed or bool(symbol_sector_updates)

    # Phase 5: provider fetch and classification ordering.
    prioritized = sorted(
        unresolved.keys(),
        key=lambda sym: _get_lookup_priority(unresolved[sym]["sample"]),
    )

    # Phase 6: provider fetches; DB writes are applied after network waits.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        trust_env=False,
    ) as client:
        resolved_count = 0
        retryable_miss_count = 0
        aborted = False
        for sym in prioritized:
            target = unresolved[sym]
            resolved_sector, instrument_kind, should_abort = await _classify_symbol_from_candidates(
                provider,
                client,
                api_key,
                target["candidates"],
            )
            if should_abort:
                aborted = True
                break

            if resolved_sector is None and instrument_kind is None:
                retryable_miss_count += 1
            else:
                symbol_sector_updates[sym] = (resolved_sector, instrument_kind, provider)
                resolved_count += 1
                changed = _apply_sector_to_holdings(target["holdings"], resolved_sector) or changed

            await asyncio.sleep(0.2)

    logger.info(
        "holdings sector enrichment completed user_id=%s provider=%s symbols=%s cached=%s resolved=%s retryable_misses=%s local_terminal=%s aborted=%s",
        user_id,
        provider,
        len(all_symbols),
        len(symbol_sector_cache),
        resolved_count,
        retryable_miss_count,
        sum(skip_counts.values()),
        aborted,
    )

    # Phase 7: persist accumulated classifications.
    await _apply_symbol_sector_updates(db, symbol_sector_cache, symbol_sector_updates)

    return changed or bool(symbol_sector_updates)


async def enrich_existing_user_holdings_with_sectors(
    db: AsyncSession,
    user_id: int,
) -> bool:
    holdings = (
        await db.execute(
            select(Holding).where(Holding.user_id == user_id)
        )
    ).scalars().all()
    return await enrich_holding_rows_with_sectors(db, user_id, list(holdings))
