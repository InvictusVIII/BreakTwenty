import asyncio
import math
import re
import time
from datetime import date, datetime, timezone
from typing import Any

from defusedxml import ElementTree as DefusedElementTree
import httpx
from sqlalchemy import select

from app.connectors.connector_logging import get_connector_logger, log_connector_event
from app.connectors.ibkr_helpers import map_ibkr_account_type
from app.database import async_session
from app.models import Institution, Account
from app.services.connection_auth_storage import get_api_credentials
from app.services.sqlite_write_gate import sqlite_write_gate

FLEX_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
IBKR_FLEX_PROVIDER = "ibkr_flex"

logger = get_connector_logger(IBKR_FLEX_PROVIDER)

# IBKR's SendRequest transiently rejects statement generation ("could not be
# generated ... try again shortly", "too many requests ...") while its engine is
# busy — the same async-busy signal GetStatement already polls through. Retry the
# request step with backoff instead of surfacing it as a hard failure.
FLEX_REQUEST_MAX_ATTEMPTS = 8
FLEX_REQUEST_RETRY_DELAY_SECONDS = 10
FLEX_FETCH_MAX_ATTEMPTS = 20
FLEX_FETCH_RETRY_DELAY_SECONDS = 7
FLEX_HTTP_HEADERS = {"User-Agent": "BreakTwenty Flex Web Service Client"}
FLEX_RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
IBKR_FLEX_NETWORK_BLOCKED_MESSAGE = (
    "IBKR blocked Flex Web Service access from this network before validating your token. "
    "If you are using a VPN or proxy, try another exit location or turn it off; if your "
    "Flex token has a Valid For IP Address restriction, set it to this network's public "
    "IP address or leave it blank."
)

# One Flex statement already contains balances, holdings, cash and trades, but the
# accounts and transactions scopes sync as two passes seconds apart. Reuse a freshly
# generated statement across both passes so each sync hits IBKR once, not twice.
FLEX_REPORT_CACHE_TTL_SECONDS = 180

_FLEX_TRANSIENT_MARKERS = (
    "try again shortly",
    "could not be generated",
    "could not be retrieved",
    "generation in progress",
    "heavy load",
    "too many requests",
)


class IBKRFlexNetworkBlockedError(Exception):
    pass

IBKR_CASH_TX_TYPE_MAP = {
    "Dividends": "dividend",
    "Payment In Lieu Of Dividends": "dividend",
    "Withholding Tax": "withholding_tax",
    "Broker Interest Paid": "interest",
    "Broker Interest Received": "interest",
    "Commission Adjustments": "fee",
    "Other Fees": "fee",
    "Deposits/Withdrawals": None,  # determined by amount sign
}

_IBKR_DIVIDEND_RATE_PATTERN = re.compile(
    r"\b[A-Z]{3}\s+([\d]+(?:\.\d+)?)\s+PER\s+SHARE\b",
    re.IGNORECASE,
)

_QUESTRADE_SHARE_COUNT_PATTERN = re.compile(
    r"\bON\s+([\d]+(?:\.\d+)?)\s+SHS\b",
    re.IGNORECASE,
)


def derive_dividend_quantity_from_description(
    description: str | None,
    amount: float | None,
) -> float | None:
    """Derive share count from a dividend transaction description.

    Supports two provider formats:
    - IBKR cash dividend: "<SYM>(...) CASH DIVIDEND <CCY> <RATE> PER SHARE ..."
      → shares = abs(amount) / rate
    - Questrade activity: "... ON <N> SHS REC ..." → shares = N directly

    Returns None when neither pattern matches (e.g. IBKR payment-in-lieu).
    """
    if not description:
        return None

    match = _QUESTRADE_SHARE_COUNT_PATTERN.search(description)
    if match:
        try:
            qty = float(match.group(1))
            if qty > 0:
                return round(qty, 4)
        except (TypeError, ValueError):
            pass

    if amount:
        match = _IBKR_DIVIDEND_RATE_PATTERN.search(description)
        if match:
            try:
                rate = float(match.group(1))
                if rate > 0:
                    quantity = abs(float(amount)) / rate
                    # IBKR rounds dividend amounts to cents while keeping the
                    # per-share rate at higher precision, so naive amount/rate
                    # can land at e.g. 99.9822 for a true 100-share position.
                    # Snap to the nearest integer when the deviation is within
                    # the 0.005 rounding band implied by cent-precision amounts.
                    nearest = round(quantity)
                    if nearest > 0 and abs(quantity - nearest) <= 0.5 / rate / 100:
                        return float(nearest)
                    return round(quantity, 4)
            except (TypeError, ValueError):
                pass

    return None


def get_xml_float(value, default=None):
    if value in (None, ""):
        return default

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _parse_ibkr_report_date(value):
    """Parse an IBKR Flex ``reportDate`` (``YYYYMMDD`` or ISO) to a UTC datetime; None if unparseable."""
    if not value:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def derive_change_pct(close_price, prev_close_price):
    if close_price is None or prev_close_price is None or prev_close_price <= 0:
        return None

    return ((close_price - prev_close_price) / prev_close_price) * 100


def normalize_flex_position_average_cost(asset_category, cost_basis_money, quantity, multiplier):
    if cost_basis_money is None:
        return None

    if asset_category not in ("OPT", "FOP"):
        return cost_basis_money

    abs_quantity = abs(quantity or 0)
    if abs_quantity == 0 or not multiplier or multiplier <= 0:
        return None

    average_quote_price = abs(cost_basis_money) / (abs_quantity * multiplier)
    return average_quote_price * abs_quantity


def _parse_flex_service_xml(xml_text: str, operation: str) -> Any:
    try:
        return DefusedElementTree.fromstring(xml_text)
    except DefusedElementTree.ParseError as e:
        raise ValueError(f"Invalid IBKR Flex {operation} response XML: {e}") from e


def _flex_error_message(root: Any, fallback: str) -> str:
    error_text = (root.findtext("ErrorMessage") or "").strip()
    return error_text or fallback


def is_transient_flex_message(message: str) -> bool:
    """True when an IBKR Flex message reflects a transient/retryable condition
    (statement busy, server load, rate limit) rather than an auth or hard error.
    Used by the request-step retry and by the connector's error classification."""
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _FLEX_TRANSIENT_MARKERS)


def is_retryable_flex_http_status(status_code: int) -> bool:
    return status_code in FLEX_RETRYABLE_HTTP_STATUS_CODES


def is_retryable_flex_http_message(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "ibkr flex request failed with http 408",
            "ibkr flex request failed with http 429",
            "ibkr flex request failed with http 500",
            "ibkr flex request failed with http 502",
            "ibkr flex request failed with http 503",
            "ibkr flex request failed with http 504",
        )
    )


def is_flex_network_block_message(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "ibkr blocked flex web service access",
            "access denied for",
            "ip restriction",
        )
    )


def _is_edge_access_denied_response(resp: httpx.Response) -> bool:
    body = (resp.text or "").lower()
    content_type = (resp.headers.get("content-type") or "").lower()
    return (
        resp.status_code == 403
        and "text/html" in content_type
        and "access denied" in body
        and "flexstatementresponse" not in body
        and "flexqueryresponse" not in body
    )


def _is_flex_ip_restriction(error_text: str, error_code: str | None = None) -> bool:
    return str(error_code or "").strip() == "1013" or "ip restriction" in (error_text or "").lower()


async def get_flex_credentials(user_id: int):
    async with async_session() as db:
        settings = await get_api_credentials(
            db,
            user_id,
            IBKR_FLEX_PROVIDER,
            setting_keys=("ibkr_flex_token", "ibkr_flex_query_id"),
        )
        token = settings.get("ibkr_flex_token")
        query_id = settings.get("ibkr_flex_query_id")
        if not token or not query_id:
            return None, None
        return token, query_id


async def request_flex_report(token: str, query_id: str, *, user_id: int | None = None):
    """Step 1: Request the report generation.

    IBKR's SendRequest transiently rejects with "could not be generated ... try
    again shortly" / "too many requests ..." while its statement engine is busy.
    That is the same async-busy signal GetStatement already polls through, so retry
    the request here with backoff rather than surfacing it as a hard failure."""
    async with httpx.AsyncClient(timeout=30, trust_env=False, headers=FLEX_HTTP_HEADERS) as client:
        log_connector_event(
            logger,
            provider=IBKR_FLEX_PROVIDER,
            stage="request report start",
            user_id=user_id,
            debug=True,
        )
        for attempt in range(1, FLEX_REQUEST_MAX_ATTEMPTS + 1):
            resp = await client.get(
                f"{FLEX_URL}/SendRequest",
                params={"t": token, "q": query_id, "v": "3"},
            )
            if resp.status_code < 200 or resp.status_code >= 300:
                if _is_edge_access_denied_response(resp):
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="request report network blocked",
                        level="warning",
                        user_id=user_id,
                        status_code=resp.status_code,
                    )
                    raise IBKRFlexNetworkBlockedError(IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
                if is_retryable_flex_http_status(resp.status_code) and attempt < FLEX_REQUEST_MAX_ATTEMPTS:
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="request report http retry",
                        level="warning",
                        user_id=user_id,
                        status_code=resp.status_code,
                        attempt=attempt,
                        debug=True,
                    )
                    await asyncio.sleep(FLEX_REQUEST_RETRY_DELAY_SECONDS)
                    continue
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="request report http failure",
                    level="warning",
                    user_id=user_id,
                    status_code=resp.status_code,
                )
                raise Exception(f"IBKR Flex request failed with HTTP {resp.status_code}")
            root = _parse_flex_service_xml(resp.text, "request")
            status = root.find("Status")
            if status is not None and status.text == "Success":
                ref_code = root.find("ReferenceCode")
                reference_code = (ref_code.text or "").strip() if ref_code is not None else ""
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="request report result",
                    user_id=user_id,
                    status="success",
                    reference_code_present=bool(reference_code),
                    attempts=attempt,
                    debug=True,
                )
                return reference_code or None
            error_text = _flex_error_message(root, "IBKR Flex request failed")
            error_code = root.findtext("ErrorCode")
            if _is_flex_ip_restriction(error_text, error_code):
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="request report network blocked",
                    level="warning",
                    user_id=user_id,
                    status=status.text if status is not None else "unknown",
                    attempt=attempt,
                    error_code=error_code,
                )
                raise IBKRFlexNetworkBlockedError(IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
            if is_transient_flex_message(error_text) and attempt < FLEX_REQUEST_MAX_ATTEMPTS:
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="request report retry",
                    user_id=user_id,
                    status=status.text if status is not None else "unknown",
                    attempt=attempt,
                    message=error_text,
                    debug=True,
                )
                await asyncio.sleep(FLEX_REQUEST_RETRY_DELAY_SECONDS)
                continue
            log_connector_event(
                logger,
                provider=IBKR_FLEX_PROVIDER,
                stage="request report result",
                level="warning",
                user_id=user_id,
                status=status.text if status is not None else "unknown",
                attempt=attempt,
                message=error_text,
            )
            raise Exception(error_text)
        raise Exception("IBKR Flex request failed")


async def fetch_flex_report(
    token: str,
    reference_code: str,
    max_retries: int = FLEX_FETCH_MAX_ATTEMPTS,
    *,
    user_id: int | None = None,
):
    """Step 2: Poll until the report is ready and download it."""
    async with httpx.AsyncClient(timeout=60, trust_env=False, headers=FLEX_HTTP_HEADERS) as client:
        for attempt in range(max_retries):
            await asyncio.sleep(FLEX_FETCH_RETRY_DELAY_SECONDS)
            resp = await client.get(
                f"{FLEX_URL}/GetStatement",
                params={"t": token, "q": reference_code, "v": "3"},
            )
            if resp.status_code == 200 and "FlexQueryResponse" in resp.text:
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="fetch report result",
                    user_id=user_id,
                    status="success",
                    attempts=attempt + 1,
                    debug=True,
                )
                return resp.text
            if resp.status_code < 200 or resp.status_code >= 300:
                if _is_edge_access_denied_response(resp):
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="fetch report network blocked",
                        level="warning",
                        user_id=user_id,
                        status_code=resp.status_code,
                        attempt=attempt + 1,
                    )
                    raise IBKRFlexNetworkBlockedError(IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
                if resp.status_code in (401, 403):
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="fetch report auth failure",
                        level="warning",
                        user_id=user_id,
                        status_code=resp.status_code,
                        attempt=attempt + 1,
                    )
                    raise Exception(f"IBKR Flex fetch failed with HTTP {resp.status_code}")
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="fetch report http retry",
                    level="warning",
                    user_id=user_id,
                    status_code=resp.status_code,
                    attempt=attempt + 1,
                    debug=True,
                )
                continue
            root = _parse_flex_service_xml(resp.text, "fetch")
            status = root.find("Status")
            if status is not None and status.text == "Warn":
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="fetch report pending",
                    user_id=user_id,
                    attempt=attempt + 1,
                    message=_flex_error_message(root, ""),
                    debug=True,
                )
                continue
            error = root.find("ErrorMessage")
            if error is not None:
                error_text = (error.text or "").strip() or "IBKR Flex report error"
                error_code = root.findtext("ErrorCode")
                if _is_flex_ip_restriction(error_text, error_code):
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="fetch report network blocked",
                        level="warning",
                        user_id=user_id,
                        status=status.text if status is not None else "unknown",
                        error_code=error_code,
                    )
                    raise IBKRFlexNetworkBlockedError(IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="fetch report result",
                    level="warning",
                    user_id=user_id,
                    status=status.text if status is not None else "unknown",
                    message=error_text,
                )
                raise Exception(f"Flex report error: {error_text}")
    raise TimeoutError(f"Flex report timed out after {max_retries} fetch attempts")


_FLEX_REPORT_CACHE: dict[tuple[int | None, str, str], tuple[float, str]] = {}


def _flex_report_cache_key(
    user_id: int | None, token: str, query_id: str
) -> tuple[int | None, str, str]:
    return (user_id, token, query_id)


def _cached_flex_report(key: tuple[int | None, str, str]) -> str | None:
    entry = _FLEX_REPORT_CACHE.get(key)
    if entry is None:
        return None
    stored_at, xml_text = entry
    if (time.monotonic() - stored_at) > FLEX_REPORT_CACHE_TTL_SECONDS:
        _FLEX_REPORT_CACHE.pop(key, None)
        return None
    return xml_text


def _store_flex_report(key: tuple[int | None, str, str], xml_text: str) -> None:
    now = time.monotonic()
    _FLEX_REPORT_CACHE[key] = (now, xml_text)
    expired = [
        cache_key
        for cache_key, (stored_at, _) in _FLEX_REPORT_CACHE.items()
        if (now - stored_at) > FLEX_REPORT_CACHE_TTL_SECONDS
    ]
    for cache_key in expired:
        _FLEX_REPORT_CACHE.pop(cache_key, None)


def clear_flex_report_cache() -> None:
    _FLEX_REPORT_CACHE.clear()


async def get_flex_report(token: str, query_id: str, *, user_id: int | None = None) -> str:
    """Generate the Flex statement once and reuse it within a short window.

    The accounts and transactions sync scopes each need the same statement seconds
    apart; caching the fetched XML keeps each sync to a single IBKR generation,
    halving pressure on IBKR's per-token rate limit and statement engine."""
    key = _flex_report_cache_key(user_id, token, query_id)
    cached = _cached_flex_report(key)
    if cached is not None:
        log_connector_event(
            logger,
            provider=IBKR_FLEX_PROVIDER,
            stage="report cache hit",
            user_id=user_id,
            debug=True,
        )
        return cached
    reference_code = await request_flex_report(token, query_id, user_id=user_id)
    if not reference_code:
        raise Exception("Failed to get reference code from IBKR")
    xml_text = await fetch_flex_report(token, reference_code, user_id=user_id)
    _store_flex_report(key, xml_text)
    return xml_text


def _format_ibkr_date_token(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return ""
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%d%b%y").upper()
        except ValueError:
            return raw
    return raw


def _parse_ibkr_date(raw_value) -> date | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    raw = raw.split(";", 1)[0].strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _ibkr_date_iso(raw_value) -> str | None:
    parsed = _parse_ibkr_date(raw_value)
    return parsed.isoformat() if parsed else None


def _format_ibkr_option_expiry(pos_or_tr):
    expiry_raw = pos_or_tr.get("expiry") or ""
    last_trade_raw = pos_or_tr.get("lastTradeDateOrContractMonth") or ""

    for raw in (expiry_raw, last_trade_raw):
        raw_text = str(raw or "").strip()
        if len(raw_text) == 8 and raw_text.isdigit():
            return _format_ibkr_date_token(raw_text)

    for raw in (expiry_raw, last_trade_raw):
        formatted = _format_ibkr_date_token(raw)
        if formatted:
            return formatted

    return ""


def _format_ibkr_option_strike(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return ""
    try:
        strike_num = float(raw)
        return (f"{strike_num:.2f}".rstrip("0").rstrip(".")) or raw
    except ValueError:
        return raw


def _format_ibkr_put_call(raw_value):
    raw = str(raw_value or "").strip().upper()
    if raw in ("PUT", "P"):
        return "P"
    if raw in ("CALL", "C"):
        return "C"
    return raw


def _format_ibkr_monarch_option_expiry(pos: dict) -> str | None:
    for raw in (pos.get("expiry"), pos.get("lastTradeDateOrContractMonth")):
        text = str(raw or "").strip()
        if len(text) == 8 and text.isdigit():
            return f"{text[4:6]}/{text[6:8]}/{text[2:4]}"
    return None


def _format_ibkr_monarch_option_name(pos: dict, underlying_name: str | None) -> str | None:
    side_raw = str(pos.get("putCall") or "").strip().upper()
    if side_raw.startswith("C"):
        side = "CALL"
    elif side_raw.startswith("P"):
        side = "PUT"
    else:
        return None
    strike_text = _format_ibkr_option_strike(pos.get("strike"))
    if not strike_text:
        return None
    expiry = _format_ibkr_monarch_option_expiry(pos)
    if not expiry:
        return None
    label = underlying_name or str(pos.get("underlyingSymbol") or "").strip() or None
    if not label:
        return None
    return f"{side} {label} ${strike_text} EXP {expiry}"


def _format_ibkr_structured_option_symbol(pos_or_tr):
    underlying = (pos_or_tr.get("underlyingSymbol") or pos_or_tr.get("symbol") or "").strip()
    expiry_fmt = _format_ibkr_option_expiry(pos_or_tr)
    strike_fmt = _format_ibkr_option_strike(pos_or_tr.get("strike"))
    put_call = _format_ibkr_put_call(pos_or_tr.get("putCall"))

    if not all((underlying, expiry_fmt, strike_fmt, put_call)):
        return ""

    return " ".join((underlying, expiry_fmt, strike_fmt, put_call))


def _format_ibkr_option_symbol(pos_or_tr: dict) -> str:
    """Build a human-readable symbol for IBKR OPT/FOP positions and transactions."""
    base_symbol = pos_or_tr.get("symbol") or pos_or_tr.get("underlyingSymbol") or ""
    asset_category = (pos_or_tr.get("assetCategory") or "").upper()
    description = pos_or_tr.get("description") or ""
    listing_exchange = (pos_or_tr.get("listingExchange") or pos_or_tr.get("exchange") or "").upper()

    if asset_category in ("OPT", "FOP"):
        return _format_ibkr_structured_option_symbol(pos_or_tr) or description or base_symbol

    # Equity / other — append exchange suffix for Canadian/non-US listings
    if listing_exchange and listing_exchange not in ("NYSE", "NASDAQ", "ARCA", "BATS", "CBOE", "AMEX", "NYMEX", ""):
        return f"{base_symbol} @{listing_exchange}"

    return base_symbol


def parse_flex_report(xml_text: str):
    """Parse the Flex Query XML into structured data."""
    root = DefusedElementTree.fromstring(xml_text)
    statements_root = root.find(".//FlexStatements")
    accounts_data = []

    for statement in root.findall(".//FlexStatement"):
        acct_id = statement.get("accountId", "")
        currency = statement.get("currency", "CAD")

        def statement_attr(*names):
            for name in names:
                value = statement.get(name)
                if value:
                    return value
                if statements_root is not None:
                    value = statements_root.get(name)
                    if value:
                        return value
                value = root.get(name)
                if value:
                    return value
            return None

        # Account info from AccountInformation section
        acct_type_raw = ""
        acct_name = ""
        for ai in statement.findall(".//AccountInformation"):
            acct_type_raw = ai.get("customerType", "")
            acct_name = ai.get("acctAlias", "") or ai.get("name", "")

        account_info = {
            "account_id": acct_id,
            "currency": currency,
            "customer_type": acct_type_raw,
            "acct_name": acct_name,
            "statement_from_date": _ibkr_date_iso(
                statement_attr("fromDate", "startDate", "periodFrom")
            ),
            "statement_to_date": _ibkr_date_iso(
                statement_attr("toDate", "endDate", "periodTo")
            ),
            "statement_period": statement_attr("period") or "",
            "statement_when_generated": statement_attr("whenGenerated") or "",
            "positions": [],
            "cash": [],
            "nav": 0,
            "nav_history": [],
        }

        mtm_rows_by_key = {}

        for mtm_row in statement.findall(".//MTMPerformanceSummaryUnderlying"):
            mtm_account_id = mtm_row.get("accountId", acct_id)
            conid = (mtm_row.get("conid") or "").strip()

            if not mtm_account_id or not conid:
                continue

            close_price = get_xml_float(mtm_row.get("closePrice"))
            prev_close_price = get_xml_float(mtm_row.get("prevClosePrice"))
            daily_pnl = get_xml_float(mtm_row.get("total"))

            mtm_rows_by_key[(mtm_account_id, conid)] = {
                "daily_pnl": daily_pnl,
                "change_pct": derive_change_pct(close_price, prev_close_price),
            }

        # Pre-pass: build {underlyingSymbol -> STK description} for this statement
        underlying_names: dict[str, str] = {}
        statement_positions = statement.findall(".//OpenPosition")
        for pos in statement_positions:
            if (pos.get("assetCategory") or "").upper() != "STK":
                continue
            stk_symbol = (pos.get("symbol") or "").strip().upper()
            description = (pos.get("description") or "").strip()
            if stk_symbol and description:
                underlying_names[stk_symbol] = description

        # Parse positions (OpenPositions or equivalent)
        for pos in statement_positions:
            symbol = pos.get("symbol", "")
            if not symbol:
                continue

            position_account_id = pos.get("accountId", acct_id)
            conid = (pos.get("conid") or "").strip()
            mtm_row = mtm_rows_by_key.get((position_account_id, conid))
            pos_value = get_xml_float(pos.get("positionValue")) or get_xml_float(pos.get("markValue")) or 0
            fx_rate = get_xml_float(pos.get("fxRateToBase"), 1) or 1
            asset_category = (pos.get("assetCategory") or "").upper()
            quantity = get_xml_float(pos.get("position"), 0) or 0
            multiplier = get_xml_float(pos.get("multiplier"), 1) or 1
            cost_basis_money = get_xml_float(pos.get("costBasisMoney"))

            display_symbol = _format_ibkr_option_symbol(dict(pos.attrib))
            if asset_category in ("OPT", "FOP"):
                underlying_key = (pos.get("underlyingSymbol") or "").strip().upper()
                display_name = (
                    _format_ibkr_monarch_option_name(dict(pos.attrib), underlying_names.get(underlying_key))
                    or display_symbol
                    or pos.get("description")
                    or symbol
                )
            else:
                display_name = pos.get("description") or display_symbol or symbol
            account_info["positions"].append({
                "symbol": display_symbol or symbol,
                "name": display_name,
                "quantity": quantity,
                "market_value": pos_value,
                "market_value_base": pos_value * fx_rate,
                "average_cost": normalize_flex_position_average_cost(
                    asset_category,
                    cost_basis_money,
                    quantity,
                    multiplier,
                ),
                "last_price": get_xml_float(pos.get("markPrice")),
                "contract_multiplier": multiplier,
                "asset_category": asset_category,
                "change_pct": mtm_row["change_pct"] if mtm_row else None,
                "daily_pnl": mtm_row["daily_pnl"] if mtm_row else None,
                "currency": pos.get("currency", currency),
            })

        # Parse cash balances
        for cash in statement.findall(".//CashReportCurrency"):
            curr = cash.get("currency", "")
            balance = get_xml_float(cash.get("endingCash"), 0) or 0
            if balance != 0 or curr == "BASE_SUMMARY":
                account_info["cash"].append({
                    "currency": curr,
                    "amount": balance,
                })

        # NAV: capture the DAILY series for balance_history backfill. IBKR Flex carries daily
        # equity as <EquitySummaryByReportDateInBase reportDate=.. total=..> rows (one per day);
        # <EquitySummaryInBase> is just the wrapper (some configs put a single current row there).
        # Read dated rows wherever they live; the latest-dated row is the current NAV.
        equity_rows = statement.findall(".//EquitySummaryByReportDateInBase")
        if not equity_rows:
            equity_rows = statement.findall(".//EquitySummaryInBase")
        dated_navs = []
        for eq in equity_rows:
            nav_value = get_xml_float(eq.get("total", 0) or eq.get("totalLong", 0))
            if nav_value is None:
                continue
            report_dt = _parse_ibkr_report_date(eq.get("reportDate"))
            if report_dt is not None:
                dated_navs.append((report_dt, nav_value))
            else:
                account_info["nav"] = nav_value  # undated summary row → current value
        if dated_navs:
            dated_navs.sort(key=lambda pair: pair[0])
            account_info["nav_history"] = dated_navs
            account_info["nav"] = dated_navs[-1][1]  # latest report date = current NAV
        if dated_navs:
            logger.debug(
                "flex daily NAV acct=%s points=%d range=%s..%s",
                acct_id, len(dated_navs),
                dated_navs[0][0].date().isoformat(), dated_navs[-1][0].date().isoformat(),
            )
        else:
            # No daily NAV in this statement: the Flex query is missing the "Net Asset Value
            # (NAV) in Base" section, so this account's value history can't backfill.
            logger.info(
                "flex statement has no daily NAV acct=%s — enable 'Net Asset Value (NAV) in Base' "
                "in the IBKR Flex query to backfill account value history",
                acct_id,
            )

        # Alternative NAV from AccountInformation
        if account_info["nav"] == 0:
            for acct_info in statement.findall(".//AccountInformation"):
                nav = acct_info.get("netLiquidation", 0)
                nav_value = get_xml_float(nav)
                if nav_value is not None:
                    account_info["nav"] = nav_value

        # Parse cash transactions
        cash_transactions = []
        for ct in statement.findall(".//CashTransaction"):
            tx_id = ct.get("transactionID", "")
            if not tx_id:
                continue
            raw_type = ct.get("type", "")
            mapped_type = IBKR_CASH_TX_TYPE_MAP.get(raw_type)
            amount = get_xml_float(ct.get("amount"), 0) or 0
            if mapped_type is None and raw_type == "Deposits/Withdrawals":
                mapped_type = "deposit" if amount >= 0 else "withdrawal"
            if mapped_type is None:
                continue  # skip unmapped types
            date_str = ct.get("dateTime") or ct.get("reportDate") or ""
            tx_date = None
            for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d", "%Y-%m-%d;%H:%M:%S", "%Y-%m-%d"):
                try:
                    tx_date = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except (ValueError, TypeError):
                    continue
            if tx_date is None:
                continue
            description = ct.get("description") or None
            quantity = (
                derive_dividend_quantity_from_description(description, amount)
                if mapped_type == "dividend"
                else None
            )
            cash_transactions.append({
                "transaction_id": tx_id,
                "type": mapped_type,
                "date": tx_date,
                "amount": amount,
                "currency": ct.get("currency", currency),
                "symbol": ct.get("symbol") or None,
                "description": description,
                "quantity": quantity,
            })
        account_info["cash_transactions"] = cash_transactions

        # Parse trades (buys and sells, including options)
        trades = []
        for tr in statement.findall(".//Trade"):
            tr_id = tr.get("tradeID", "")
            if not tr_id:
                continue
            buy_sell = (tr.get("buySell") or "").upper()
            if buy_sell not in ("BUY", "SELL"):
                continue
            mapped_type = "buy" if buy_sell == "BUY" else "sell"
            date_str = tr.get("dateTime") or tr.get("tradeDate") or ""
            tx_date = None
            for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d", "%Y-%m-%d;%H:%M:%S", "%Y-%m-%d"):
                try:
                    tx_date = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except (ValueError, TypeError):
                    continue
            if tx_date is None:
                continue

            description = tr.get("description") or None
            display_symbol = _format_ibkr_option_symbol(dict(tr.attrib))

            quantity = get_xml_float(tr.get("quantity"), 0) or 0
            trade_price = get_xml_float(tr.get("tradePrice"))
            trade_money = get_xml_float(tr.get("tradeMoney"), 0) or 0
            ib_commission = get_xml_float(tr.get("ibCommission"), 0) or 0

            # Option expiries arrive as zero-price, zero-money trades.
            if abs(trade_money) < 0.01 and abs(trade_price or 0) < 0.01:
                mapped_type = "expired"
                display_amount = 0.0
            else:
                # IBKR's tradeMoney sign is the reverse of what users expect:
                # positive for buys (outflow), negative for sells (inflow).
                # Flip so buys show negative and sells show positive.
                display_amount = -trade_money

            trades.append({
                "trade_id": tr_id,
                "type": mapped_type,
                "date": tx_date,
                "symbol": display_symbol or None,
                "description": description,
                "amount": display_amount,
                "currency": tr.get("currency", currency),
                "quantity": abs(quantity) or None,
                "price": trade_price,
                "commission": abs(ib_commission) or None,
                "asset_category": (tr.get("assetCategory") or "").upper() or None,
                "realized_pnl": get_xml_float(tr.get("fifoPnlRealized")),
            })
        account_info["trades"] = trades

        accounts_data.append(account_info)

    return accounts_data


def _normalize_flex_transactions(acct_data: dict) -> list:
    """Build NormalizedTransactions from parsed Flex cash transactions + trades, reusing the
    live IBKR connector's external_id scheme (``ibkr_<id>`` / ``ibkr_trade_<id>``) so a later
    sync upserts the same rows in place. Mirrors ``IBKRFlexConnector._normalize_transactions``;
    feeding these to ``upsert_transactions_for_account`` makes file-imported rows categorized +
    deduped exactly like a live sync, instead of landing raw/uncategorized."""
    from app.connectors.types import NormalizedTransaction

    transactions = []
    for tx in acct_data.get("cash_transactions", []) or []:
        transactions.append(NormalizedTransaction(
            external_id=f"ibkr_{tx['transaction_id']}",
            date=tx["date"],
            type=tx["type"],
            amount=tx["amount"],
            currency=tx["currency"],
            symbol=tx.get("symbol"),
            description=tx.get("description"),
            quantity=tx.get("quantity"),
        ))
    for trade in acct_data.get("trades", []) or []:
        transactions.append(NormalizedTransaction(
            external_id=f"ibkr_trade_{trade['trade_id']}",
            date=trade["date"],
            type=trade["type"],
            amount=trade["amount"],
            currency=trade["currency"],
            symbol=trade.get("symbol"),
            description=trade.get("description"),
            quantity=trade.get("quantity"),
            price=trade.get("price"),
            commission=trade.get("commission"),
            asset_category=trade.get("asset_category"),
            realized_pnl=trade.get("realized_pnl"),
        ))
    return transactions


async def import_flex_transactions(
    xml_text: str,
    user_id: int,
    institution_id: int,
) -> dict:
    """Import transactions (trades + cash) from a Flex Query XML payload, AND backfill
    daily account-value history from the statement's ``EquitySummaryInBase`` rows (one
    ``balance_history`` row per user-local day). Unlike the 365-day-capped Flex web-service
    sync, an uploaded file can span the account's full statement range, so this extends the
    net-worth graph back as far as the file reaches. Does not touch holdings.

    An account whose number matches a live (or already-imported) account extends that
    account's history; an unmatched number — a closed/transferred account — is created as an
    ``is_imported`` account under the user's IBKR institution (mirroring the Questrade
    statement-import contract), so defunct brokerage accounts can be reconstructed too.
    """
    try:
        accounts_data = await asyncio.to_thread(parse_flex_report, xml_text)
    except DefusedElementTree.ParseError as e:
        return {"status": "error", "message": f"Invalid XML: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to parse Flex report: {e}"}

    if not accounts_data:
        return {"status": "error", "message": "No account data found in Flex report"}

    total_imported = 0
    total_skipped = 0
    total_errors = 0
    accounts_created = 0
    accounts_updated = 0

    async with async_session() as db, sqlite_write_gate(), db.begin():
        result = await db.execute(
            select(Institution).where(
                Institution.id == int(institution_id),
                Institution.provider == "ibkr",
                Institution.user_id == user_id,
                Institution.enabled.is_(True),
            )
        )
        institution = result.scalar_one_or_none()
        if not institution:
            return {"status": "error", "message": "The selected IBKR connection was not found"}

        # Transactions import through the shared categorizing/deduping path (provider="ibkr"),
        # the same one the live IBKR sync uses — so file-imported rows (including a never-synced
        # defunct account's) are categorized inline at import, not left for a later rule sweep.
        # NAV backfill reuses the connector persistence helper + user-local-day logic.
        from app.connectors.persistence import (
            TransactionUpsertCounts,
            _upsert_balance_history_series,
            upsert_transactions_for_account,
        )
        from app.services.categories import CategoryResolver
        from app.services.user_utils import get_user_timezone_info_from_db

        user_tz = await get_user_timezone_info_from_db(db, user_id)
        category_resolver = await CategoryResolver.build(db, user_id)

        for acct_data in accounts_data:
            acct_id = str(acct_data.get("account_id") or "").strip()
            if not acct_id:
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="manual import account rejected",
                    level="warning",
                    user_id=user_id,
                    error="missing_account_id",
                    message="Flex statement account is missing its stable provider account ID",
                )
                total_errors += 1
                continue
            acct_type = map_ibkr_account_type(acct_data.get("customer_type", ""))
            currency = acct_data.get("currency") or "CAD"
            acct_name = acct_data.get("acct_name", "")
            account_external_id = f"account:{acct_id}"

            result = await db.execute(
                select(Account).where(
                    Account.institution_id == institution.id,
                    Account.user_id == user_id,
                    Account.external_id == account_external_id,
                )
            )
            account = result.scalar_one_or_none()

            if not account:
                # No live or previously-imported match: this is a closed/transferred account
                # whose entire history lives in the uploaded statement. Mirror the Questrade
                # defunct-account path and create it is_imported — so it measures from its first
                # imported point (never clamped to the institution connect date), appears in the
                # Imported accounts manager, and is never synced or pruned. A later live sync with
                # the same external_id re-adopts it and clears the flag (see persistence upsert).
                account = Account(
                    user_id=user_id,
                    institution_id=institution.id,
                    external_id=account_external_id,
                    name=acct_name or f"IBKR {acct_id}",
                    account_type=acct_type,
                    currency=currency,
                    is_liability=False,
                    is_imported=True,
                )
                db.add(account)
                await db.flush()
                accounts_created += 1
            else:
                accounts_updated += 1

            try:
                normalized = _normalize_flex_transactions(acct_data)
                if normalized:
                    upsert_counts = TransactionUpsertCounts()
                    await upsert_transactions_for_account(
                        db,
                        user_id=user_id,
                        account=account,
                        transactions=normalized,
                        category_resolver=category_resolver,
                        provider="ibkr",
                        counts=upsert_counts,
                    )
                    total_imported += upsert_counts.inserted
                    total_skipped += upsert_counts.existing
            except Exception as e:
                log_connector_event(
                    logger,
                    provider=IBKR_FLEX_PROVIDER,
                    stage="manual import account failed",
                    level="warning",
                    user_id=user_id,
                    account_ref=acct_data.get("account_id"),
                    error=type(e).__name__,
                    message=str(e),
                )
                total_errors += 1

            # Backfill daily account value from the statement's EquitySummaryInBase rows
            # (parse already extracted them) — a file can span the account's full history,
            # so this extends the graph back as far as the file reaches.
            nav_history = acct_data.get("nav_history") or []
            if nav_history:
                try:
                    await _upsert_balance_history_series(
                        db,
                        user_id=user_id,
                        account_id=account.id,
                        points=nav_history,
                        user_tz=user_tz,
                    )
                except Exception as e:
                    log_connector_event(
                        logger,
                        provider=IBKR_FLEX_PROVIDER,
                        stage="manual import balance history failed",
                        level="warning",
                        user_id=user_id,
                        account_ref=acct_data.get("account_id"),
                        error=type(e).__name__,
                        message=str(e),
                    )

    return {
        "status": "ok",
        "imported": total_imported,
        "skipped": total_skipped,
        "errors": total_errors,
        "accounts_created": accounts_created,
        "accounts_updated": accounts_updated,
    }
