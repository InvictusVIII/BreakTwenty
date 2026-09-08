import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urlparse

from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    default_scraper_user_agent,
    refresh_session_artifact,
    scraper_session_user_agent,
)
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    sanitize_scraper_log_url,
    scraper_debug_enabled,
)
from app.scrapers.results import scraper_auth_required
from app.services.sync_utils import is_network_error

logger = logging.getLogger("breaktwenty.scrapers.rbc")

PROVIDER = "rbc"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
RBC_SUMMARY_URL = "https://www1.royalbank.com/sgw1/olb/index-en/#/summary"
RBC_OLB_INDEX_URL = "https://www1.royalbank.com/sgw1/olb/index-en/"
RBC_ACCOUNT_LIST_URL = "https://www1.royalbank.com/sgw5/digital/product-summary-presentation-service-v3/v3/accountListSummary"
RBC_CC_ARRANGEMENT_URL = "https://www1.royalbank.com/sgw5/digital/account-presentation-service-v3/v3/arrangements/credit-card"
RBC_CC_LINE_OF_BUSINESS_URL = "https://www1.royalbank.com/sgw5/digital/account-presentation-service-v3/v3/arrangements"
RBC_TXN_PDA_URL = "https://www1.royalbank.com/sgw5/digital/transaction-presentation-service-v3-dbb/v3/transactions/pda/account"
RBC_TXN_CC_SEARCH_URL = "https://www1.royalbank.com/sgw5/digital/transaction-presentation-service-v3-dbb/v3/search/cc/posted/account"
RBC_TXN_CC_URL = "https://www1.royalbank.com/sgw5/digital/transaction-presentation-service-v3-dbb/v3/transactions/cc/posted/account"
RBC_TXN_CC_AUTHORIZED_URL = "https://www1.royalbank.com/sgw5/digital/transaction-presentation-service-v3-dbb/v3/transactions/cc/authorized/account"
RBC_LOC_HISTORY_URL = "https://www1.royalbank.com/sgw5/SECOLBH/3m00/ISAMSecureRequest/v1/eBGRenderPage"
RBC_BACKFILL_DAYS = 1825
RBC_INCREMENTAL_DAYS = 60
RBC_CC_BACKFILL_CHUNK_DAYS = 365
RBC_CC_SEARCH_LIMIT = 50
RBC_CC_MIN_SEARCH_WINDOW_DAYS = 14
RBC_CC_FAILED_SEARCH_MAX_FAIL_SPLIT_DEPTH = 0
RBC_CC_SEARCH_RETRY_ATTEMPTS = 3
RBC_CC_SEARCH_MIN_WINDOW_RETRY_ATTEMPTS = 3
RBC_CC_SEARCH_RETRY_WAIT_SECONDS = 2
RBC_CC_SEARCH_RETRY_WAIT_MAX_SECONDS = 5
RBC_CC_SEARCH_HTTP_500_FINAL_WAIT_SECONDS = 15
RBC_CC_SEARCH_WINDOW_DEADLINE_SECONDS = 150
RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS = (20, 45, 90)
RBC_CC_SEARCH_DEFERRED_RECOVERY_BUDGET_SECONDS = 480
RBC_CC_SEARCH_REWARM_EVERY_FAILURES = 0
RBC_CC_SEARCH_CONCURRENCY = 1
RBC_LOC_HISTORY_DAYS = 548
RBC_DIRECT_TIMEOUT_SECONDS = 30
RBC_ACCOUNT_CATEGORY_KEYS = ("depositAccounts", "creditCards", "linesLoans", "mortgages", "investments")

ModeResolver = Callable[[list[dict]], Awaitable[dict[str, str | dict[str, Any]]]]
AccountSyncCallback = Callable[[list[dict]], Awaitable[None]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]
RBCCreditCardSearchWindow = tuple[date, date, list[dict], bool]
RBCCreditCardSearchChunkResult = tuple[list[dict] | None, bool, str | None]
RBCCreditCardSearchFailure = tuple[date, date, str]
RBC_CC_SEARCH_FAILURE_HTTP_500 = "http_500"
RBC_CC_SEARCH_FAILURE_OTHER = "other"
RBC_CC_SEARCH_FAILURE_DEADLINE = "deadline"


class RBCAuthRequired(PermissionError):
    pass


@dataclass
class RBCCreditCardBackfillResult:
    start_date: date
    end_date: date
    transactions: list[dict]
    succeeded: bool
    leaf_windows: list[RBCCreditCardSearchWindow]
    leaf_failures: list[RBCCreditCardSearchFailure]


def _rbc_monotonic_seconds() -> float:
    return asyncio.get_running_loop().time()


def _rbc_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_RBC_DEBUG",))


def _rbc_log_event(
    stage: str,
    *,
    level: str = "info",
    debug: bool = False,
    **fields: Any,
) -> None:
    log_scraper_event(
        logger,
        provider=PROVIDER,
        stage=stage,
        level=level,
        debug=debug,
        debug_enabled=_rbc_debug_logs_enabled(),
        **fields,
    )


def _rbc_sanitize_log_text(text: str | None, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit)


def _rbc_sanitize_url_for_log(url: str | None) -> str:
    return sanitize_scraper_log_url(url)


def _rbc_account_list_has_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    error_state = result.get("errorState") or {}
    return bool(error_state.get("hasError"))


def _rbc_account_list_authorized(result: Any) -> bool:
    if not isinstance(result, dict) or _rbc_account_list_has_error(result):
        return False
    return any(isinstance(result.get(category_key), dict) for category_key in RBC_ACCOUNT_CATEGORY_KEYS)


def _rbc_account_type(product_identifier: str, product_type_name: str) -> str:
    key = (product_identifier or "").upper()
    mapping = {
        "SAVINGS": "savings",
        "CHEQUING": "chequing",
        "CREDITCARD": "credit_card",
        "CREDIT_CARD": "credit_card",
        "LOC": "loc",
        "MORTGAGE": "mortgage",
    }
    if key in mapping:
        return mapping[key]
    name = (product_type_name or "").lower()
    if "savings" in name:
        return "savings"
    if "chequing" in name or "checking" in name:
        return "chequing"
    if "credit" in name:
        return "credit_card"
    if "loc" in name or "credit line" in name or "line of credit" in name:
        return "loc"
    if "mortgage" in name:
        return "mortgage"
    if "tfsa" in name:
        return "tfsa"
    if "rrsp" in name:
        return "rrsp"
    return "savings"


def _rbc_is_liability(account_type: str) -> bool:
    return account_type in {"credit_card", "loc", "mortgage"}


def _rbc_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _rbc_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _rbc_cc_transaction_list(result) -> list[dict] | None:
    if not isinstance(result, dict):
        return None
    txns = result.get("transactionList")
    if not isinstance(txns, list):
        return None
    return txns


def _parse_rbc_api_date(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except (TypeError, ValueError):
            continue
    return None


def _rbc_cc_search_dates_within_window(
    txns: list[dict],
    *,
    start_date: date,
    end_date: date,
) -> bool:
    for raw in txns:
        parsed = _parse_rbc_api_date(
            raw.get("bookingDate")
            or raw.get("transactionDate")
            or raw.get("txnDate")
            or raw.get("postedDate")
            or raw.get("date")
            or raw.get("effectiveDate")
        )
        if parsed is None:
            continue
        if parsed < start_date or parsed > end_date:
            return False
    return True


def _filter_rbc_transactions_to_window(
    txns: list[dict],
    *,
    start_date: date,
    end_date: date,
) -> list[dict]:
    filtered: list[dict] = []
    for raw in txns:
        parsed = _parse_rbc_api_date(
            raw.get("bookingDate")
            or raw.get("transactionDate")
            or raw.get("txnDate")
            or raw.get("postedDate")
            or raw.get("date")
            or raw.get("effectiveDate")
        )
        if parsed is not None and (parsed < start_date or parsed > end_date):
            continue
        filtered.append(raw)
    return filtered


def _parse_rbc_sync_date(value: Any) -> date | None:
    text = " ".join(str(value or "").replace("\xa0", " ").split()).replace(".", "").replace(",", "")
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _rbc_clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _strip_rbc_loc_field_prefix(value: Any, *labels: str) -> str:
    text = _rbc_clean_text(value)
    lower_text = text.lower()
    for label in labels:
        lower_label = label.lower()
        if lower_text == lower_label:
            return ""
        for prefix in (f"{lower_label}: ", f"{lower_label}:", f"{lower_label} "):
            if lower_text.startswith(prefix):
                return text[len(prefix):].strip()
    return text


def _parse_rbc_loc_amount(value: Any) -> float | None:
    text = _strip_rbc_loc_field_prefix(value, "debit", "debits", "credit", "credits", "balance")
    if not text or text in {"-", "--"}:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    if text.startswith("-"):
        negative = True

    text = text.lstrip("+-")
    for token in ("$", ",", "CAD", "USD", "CR", "DR"):
        text = text.replace(token, "")
    text = text.strip()
    if not text:
        return None

    try:
        amount = float(text)
    except ValueError:
        return None
    return -abs(amount) if negative else abs(amount)


def _parse_rbc_loc_date(value: Any) -> str | None:
    text = _strip_rbc_loc_field_prefix(value, "date", "transaction date", "posted date")
    if not text:
        return None

    candidates: list[str] = []
    for candidate in (
        text,
        text.replace(".", ""),
        text.replace(",", ""),
        text.replace(".", "").replace(",", ""),
    ):
        normalized = " ".join(candidate.split())
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    full_formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
        "%d %b %Y",
        "%d %B %Y",
    )
    partial_formats = ("%b %d", "%B %d", "%d %b", "%d %B", "%m/%d")

    for candidate in candidates:
        for fmt in full_formats:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
        for fmt in partial_formats:
            try:
                partial = datetime.strptime(candidate, fmt)
                parsed_date = date(date.today().year, partial.month, partial.day)
            except ValueError:
                continue
            if parsed_date > date.today() + timedelta(days=1):
                try:
                    parsed_date = date(date.today().year - 1, partial.month, partial.day)
                except ValueError:
                    continue
            return parsed_date.isoformat()
    return None


def _build_rbc_loc_raw_transaction(row: dict[str, Any], *, currency: str) -> dict[str, Any] | None:
    booking_date = _parse_rbc_loc_date(row.get("date"))
    description = _strip_rbc_loc_field_prefix(row.get("description"), "description")
    if not booking_date or not description:
        return None

    debit_amount = _parse_rbc_loc_amount(row.get("debit"))
    credit_amount = _parse_rbc_loc_amount(row.get("credit"))
    if debit_amount is not None and abs(debit_amount) > 0:
        amount = -abs(debit_amount)
        indicator = "DEBIT"
    elif credit_amount is not None and abs(credit_amount) > 0:
        amount = abs(credit_amount)
        indicator = "CREDIT"
    else:
        return None

    balance = _parse_rbc_loc_amount(row.get("balance"))
    identity = (
        f"{row.get('account_external_id') or ''}|{booking_date}|{description.strip().lower()}|"
        f"{round(amount, 2)}|{'' if balance is None else round(balance, 2)}"
    )
    raw = {
        "transactionId": f"loc_{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
        "bookingDate": booking_date,
        "postedDate": booking_date,
        "amount": amount,
        "currencyCode": currency or "CAD",
        "creditDebitIndicator": indicator,
        "description": description,
    }
    if balance is not None:
        raw["balance"] = balance
    return raw


class _RBCLegacyTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str]]] = []
        self._in_row = False
        self._in_cell = False
        self._cell_tag = ""
        self._cell_chunks: list[str] = []
        self._row_cells: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._row_cells = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_tag = tag
            self._cell_chunks = []
        elif self._in_cell and tag in {"br", "p", "div"}:
            self._cell_chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_cell and tag in {"td", "th"}:
            self._row_cells.append((self._cell_tag, _rbc_clean_text("".join(self._cell_chunks))))
            self._in_cell = False
            self._cell_tag = ""
            self._cell_chunks = []
        elif self._in_row and tag == "tr":
            if any(text for _tag, text in self._row_cells):
                self.rows.append(self._row_cells)
            self._in_row = False
            self._row_cells = []


def _rbc_loc_history_page_detected(html_text: str) -> bool:
    lower = str(html_text or "").lower()
    return any(
        marker in lower
        for marker in (
            "accttransactioninquiry",
            "you can view any transaction that has occurred over the last 18 months",
            'name="drorcr"',
            "dlhistsort",
            "rclstatement",
        )
    )


def _rbc_loc_header_field(value: Any) -> str | None:
    lower = _rbc_clean_text(value).lower()
    if not lower:
        return None
    if "description" in lower:
        return "description"
    if "debit" in lower or "withdrawal" in lower or "withdrawals" in lower:
        return "debit"
    if "credit" in lower or "deposit" in lower or "deposits" in lower:
        return "credit"
    if "balance" in lower:
        return "balance"
    if "date" in lower and "from" not in lower and "to" not in lower:
        return "date"
    return None


def _rbc_loc_row_from_cells(
    cells: list[tuple[str, str]],
    *,
    header_map: dict[int, str],
) -> dict[str, str] | None:
    texts = [text for _tag, text in cells]
    if not any(texts):
        return None

    row: dict[str, str] = {}
    for text in texts:
        for field, labels in {
            "date": ("date", "transaction date", "posted date"),
            "description": ("description",),
            "debit": ("debit", "debits", "withdrawal", "withdrawals"),
            "credit": ("credit", "credits", "deposit", "deposits"),
            "balance": ("balance",),
        }.items():
            value = _strip_rbc_loc_field_prefix(text, *labels)
            if value != _rbc_clean_text(text) and value:
                row[field] = value

    for idx, field in header_map.items():
        if idx < len(texts) and texts[idx]:
            row[field] = texts[idx]

    if "date" not in row:
        date_index = next(
            (idx for idx, text in enumerate(texts) if _parse_rbc_loc_date(text)),
            None,
        )
        if date_index is None:
            return None
        trailing = texts[date_index:]
        row.setdefault("date", trailing[0] if len(trailing) > 0 else "")
        row.setdefault("description", trailing[1] if len(trailing) > 1 else "")
        row.setdefault("debit", trailing[2] if len(trailing) > 2 else "")
        row.setdefault("credit", trailing[3] if len(trailing) > 3 else "")
        row.setdefault("balance", trailing[4] if len(trailing) > 4 else "")

    return row


def _rbc_loc_js_string_literals(args_text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"', args_text or ""):
        literal = f'"{match.group(1)}"'
        try:
            values.append(str(json.loads(literal)))
        except json.JSONDecodeError:
            values.append(match.group(1).replace(r"\"", '"').replace(r"\\", "\\"))
    return values


def _parse_rbc_loc_script_transactions_html(html_text: str, acct: dict[str, Any]) -> list[dict[str, Any]]:
    raw_transactions: list[dict[str, Any]] = []
    for match in re.finditer(
        r"new\s+f3mdetrcl_HistLine\s*\((.*?)\)\s*;",
        html_text or "",
        re.IGNORECASE | re.DOTALL,
    ):
        values = _rbc_loc_js_string_literals(match.group(1))
        if len(values) < 6:
            continue
        date_value = values[1] if len(values) > 1 else values[0]
        description_parts = [
            value
            for value in (
                values[2] if len(values) > 2 else "",
                values[6] if len(values) > 6 else "",
                values[7] if len(values) > 7 else "",
            )
            if _rbc_clean_text(value)
        ]
        row = {
            "date": date_value,
            "description": " ".join(description_parts),
            "debit": values[3] if len(values) > 3 else "",
            "credit": values[4] if len(values) > 4 else "",
            "balance": values[5] if len(values) > 5 else "",
            "account_external_id": acct.get("external_id") or "",
        }
        raw = _build_rbc_loc_raw_transaction(row, currency=acct.get("currency") or "CAD")
        if raw:
            raw_transactions.append(raw)
    return _deduplicate_rbc_raw_transactions(raw_transactions)


def _parse_rbc_loc_transactions_html(html_text: str, acct: dict[str, Any]) -> list[dict[str, Any]]:
    parser = _RBCLegacyTableParser()
    parser.feed(html_text or "")

    rows: list[dict[str, str]] = []
    header_map: dict[int, str] = {}
    for cells in parser.rows:
        fields = {
            idx: field
            for idx, (_tag, text) in enumerate(cells)
            for field in [_rbc_loc_header_field(text)]
            if field
        }
        if {"date", "description"} <= set(fields.values()):
            header_map = fields
            continue
        row = _rbc_loc_row_from_cells(cells, header_map=header_map)
        if row:
            rows.append(row)

    raw_transactions = [
        raw
        for raw in (
            _build_rbc_loc_raw_transaction(
                {**row, "account_external_id": acct.get("external_id") or ""},
                currency=acct.get("currency") or "CAD",
            )
            for row in rows
        )
        if raw
    ]
    raw_transactions.extend(_parse_rbc_loc_script_transactions_html(html_text, acct))
    return _deduplicate_rbc_raw_transactions(raw_transactions)


def _rbc_sync_state_entry(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
) -> dict[str, Any]:
    if not sync_state_per_account:
        return {}
    entry = sync_state_per_account.get(external_id)
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        return {"mode": entry}
    return {}


def _rbc_sync_state_mode(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
) -> str:
    entry = _rbc_sync_state_entry(sync_state_per_account, external_id)
    return entry.get("mode") or "incremental"


def _rbc_sync_state_date(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
    key: str,
) -> date | None:
    entry = _rbc_sync_state_entry(sync_state_per_account, external_id)
    return _parse_rbc_sync_date(entry.get(key))


def _rbc_sync_state_window_dates(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
    *,
    fallback_days: int,
) -> tuple[date, date]:
    today = date.today()
    window_start = _rbc_sync_state_date(sync_state_per_account, external_id, "start_date")
    window_end = _rbc_sync_state_date(sync_state_per_account, external_id, "end_date") or today
    if window_start is None:
        window_start = window_end - timedelta(days=fallback_days)
    if window_start > window_end:
        window_start = window_end
    return window_start, window_end


def _rbc_sync_state_interval_value(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
    *,
    fallback_days: int,
) -> int:
    window_start, window_end = _rbc_sync_state_window_dates(
        sync_state_per_account,
        external_id,
        fallback_days=fallback_days,
    )
    return max((window_end - window_start).days, 1)


def _rbc_sync_state_backfill_window_dates(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
    *,
    fallback_days: int,
) -> tuple[date, date]:
    today = date.today()
    window_start = _rbc_sync_state_date(sync_state_per_account, external_id, "backfill_start_date")
    window_end = _rbc_sync_state_date(sync_state_per_account, external_id, "backfill_end_date") or today
    if window_start is None:
        window_start = window_end - timedelta(days=fallback_days)
    if window_start > window_end:
        window_start = window_end
    return window_start, window_end


def _rbc_sync_state_pending_backfill_windows(
    sync_state_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str,
    *,
    fallback_days: int,
) -> list[tuple[date, date]]:
    entry = _rbc_sync_state_entry(sync_state_per_account, external_id)
    raw_windows = entry.get("pending_backfill_windows")
    windows: list[tuple[date, date]] = []
    if isinstance(raw_windows, list):
        for raw in raw_windows:
            if not isinstance(raw, dict):
                continue
            start = _parse_rbc_sync_date(raw.get("start"))
            end = _parse_rbc_sync_date(raw.get("end"))
            if start and end:
                windows.append((start, end))
    if windows:
        return windows
    start, end = _rbc_sync_state_backfill_window_dates(
        sync_state_per_account,
        external_id,
        fallback_days=fallback_days,
    )
    return [(start, end)]


async def _record_rbc_transaction_window(
    recorder: TransactionWindowRecorder | None,
    *,
    event: str,
    account: dict,
    mode: str,
    start_date: date,
    end_date: date,
    transactions: list[dict] | None = None,
    error: str | None = None,
) -> None:
    if recorder is None:
        return
    payload: dict[str, Any] = {
        "event": event,
        "account": account,
        "mode": mode,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    if transactions is not None:
        payload["transactions"] = transactions
    if error:
        payload["error"] = error
    try:
        await recorder(payload)
    except Exception as exc:
        _rbc_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_rbc_sanitize_log_text(str(exc), limit=140),
        )


def _rbc_raw_txn_identity(raw: dict) -> tuple:
    txn_id = (
        raw.get("transactionId")
        or raw.get("id")
        or raw.get("referenceNumber")
        or raw.get("transactionReferenceNumber")
    )
    if txn_id:
        return ("id", str(txn_id))

    desc = raw.get("description")
    if isinstance(desc, list):
        desc = desc[0] if desc else ""
    return (
        "fallback",
        raw.get("bookingDate") or raw.get("postedDate") or raw.get("transactionDate") or "",
        round(float(raw.get("amount") or 0), 2),
        (desc or raw.get("merchantName") or raw.get("narrative") or "").strip().lower(),
        (raw.get("creditDebitIndicator") or "").upper(),
    )


def _deduplicate_rbc_raw_transactions(txns: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple] = set()
    for raw in txns:
        identity = _rbc_raw_txn_identity(raw)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(raw)
    return result


def _rbc_result_debug(result) -> dict:
    if not isinstance(result, dict):
        return {}
    debug = result.get("__breaktwenty_rbc_debug")
    return debug if isinstance(debug, dict) else {}


def _rbc_xsrf_token_from_storage_state(storage_state: dict[str, Any] | None) -> str:
    if not isinstance(storage_state, dict):
        return ""
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip().lower()
        if name in {"xsrf-token", "xsrf_token", "x-xsrf-token", "csrftoken", "csrf-token", "csrf_token"}:
            return unquote(str(cookie.get("value") or "").strip())
    return ""


def _rbc_encoded_account_id(encrypted_id: str) -> str:
    return quote(unquote(str(encrypted_id or "").strip()), safe="")


def _rbc_timestamp_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _rbc_client_hint_platform(user_agent: str) -> str:
    normalized = user_agent.lower()
    if "windows" in normalized:
        return "Windows"
    if "mac os" in normalized or "macintosh" in normalized:
        return "macOS"
    if "linux" in normalized:
        return "Linux"
    return ""


def _rbc_direct_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    json_body: bool = False,
) -> dict[str, str]:
    normalized_user_agent = _rbc_user_agent(user_agent)
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-CA, en;q=0.8, fr-CA;q=0.7, fr;q=0.6",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": RBC_OLB_INDEX_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-GPC": "1",
        "User-Agent": normalized_user_agent,
    }
    major_match = re.search(r"(?:Chrome|Chromium)/(\d+)", normalized_user_agent)
    if major_match:
        major = major_match.group(1)
        headers["sec-ch-ua"] = f'"Chromium";v="{major}", "Brave";v="{major}", "Not/A)Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        platform = _rbc_client_hint_platform(normalized_user_agent)
        if platform:
            headers["sec-ch-ua-platform"] = f'"{platform}"'
    if json_body:
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://www1.royalbank.com"
        xsrf_token = _rbc_xsrf_token_from_storage_state(storage_state)
        if xsrf_token:
            headers["x-xsrf-token"] = xsrf_token
    return headers


def _rbc_legacy_html_headers(user_agent: str) -> dict[str, str]:
    normalized_user_agent = _rbc_user_agent(user_agent)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA, en;q=0.8, fr-CA;q=0.7, fr;q=0.6",
        "Cache-Control": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www1.royalbank.com",
        "Pragma": "no-cache",
        "Referer": RBC_OLB_INDEX_URL,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": normalized_user_agent,
    }
    major_match = re.search(r"(?:Chrome|Chromium)/(\d+)", normalized_user_agent)
    if major_match:
        major = major_match.group(1)
        headers["sec-ch-ua"] = f'"Chromium";v="{major}", "Brave";v="{major}", "Not/A)Brand";v="99"'
        headers["sec-ch-ua-mobile"] = "?0"
        platform = _rbc_client_hint_platform(normalized_user_agent)
        if platform:
            headers["sec-ch-ua-platform"] = f'"{platform}"'
    return headers


async def _rbc_direct_request(
    storage_state: dict[str, Any],
    method: str,
    url: str,
    *,
    user_agent: str,
    json_body: Any | None = None,
) -> dict[str, Any]:
    headers = _rbc_direct_headers(
        storage_state,
        url,
        user_agent=user_agent,
        json_body=json_body is not None,
    )
    return await SavedArtifactHttpClient.request_once(
        storage_state,
        method,
        url,
        user_agent=_rbc_user_agent(user_agent),
        headers=headers,
        timeout=RBC_DIRECT_TIMEOUT_SECONDS,
        seed_cookies=True,
        json=json_body,
    )


async def _post_rbc_form_direct(
    storage_state: dict[str, Any],
    url: str,
    payload: dict[str, Any],
    *,
    user_agent: str,
) -> dict[str, Any]:
    return await SavedArtifactHttpClient.request_once(
        storage_state,
        "POST",
        url,
        user_agent=_rbc_user_agent(user_agent),
        headers=_rbc_legacy_html_headers(user_agent),
        timeout=RBC_DIRECT_TIMEOUT_SECONDS,
        seed_cookies=True,
        parse_json=None,
        data=payload,
    )


def _rbc_response_auth_signal(response: dict[str, Any]) -> str | None:
    status = int(response.get("status") or 0)
    if status in (401, 403):
        return f"http_{status}"
    try:
        parsed = urlparse(str(response.get("url") or ""))
    except ValueError:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "royalbank.com" in host and any(marker in path for marker in ("rbunxcgi", "signin", "login")):
        return "login_redirect"
    if "omni.royalbank.com" in host and "mfa" in path:
        return "mfa_redirect"
    text = str(response.get("text") or "").lower()
    if (
        "<form" in text
        and "rbunxcgi" in text
        and ("name=\"k1\"" in text or "id=\"k1\"" in text)
        and ("name=\"qq\"" in text or "id=\"qq\"" in text)
    ):
        return "login_form"
    return None


def _rbc_response_requires_auth(response: dict[str, Any]) -> bool:
    return _rbc_response_auth_signal(response) is not None


async def _fetch_rbc_json_direct(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    label: str,
) -> dict[str, Any] | None:
    response = await _rbc_direct_request(storage_state, "GET", url, user_agent=user_agent)
    auth_signal = _rbc_response_auth_signal(response)
    if auth_signal:
        _rbc_log_event(
            f"{label} direct fetch auth_required",
            level="warning",
            auth_signal=auth_signal,
            status_code=response.get("status"),
            url=_rbc_sanitize_url_for_log(response.get("url")),
            payload_type=type(response.get("payload")).__name__,
        )
        raise RBCAuthRequired("RBC sign-in required")
    payload = response.get("payload")
    if int(response.get("status") or 0) >= 400 or not isinstance(payload, dict):
        _rbc_log_event(
            f"{label} direct fetch unusable_response",
            level="warning",
            status_code=response.get("status"),
            url=_rbc_sanitize_url_for_log(response.get("url")),
            body=_rbc_sanitize_log_text(response.get("text"), limit=140),
        )
        return None
    return payload


async def _post_rbc_json_direct(
    storage_state: dict[str, Any],
    url: str,
    payload: dict[str, Any],
    *,
    user_agent: str,
    label: str,
) -> dict[str, Any]:
    response = await _rbc_direct_request(
        storage_state,
        "POST",
        url,
        user_agent=user_agent,
        json_body=payload,
    )
    if _rbc_response_requires_auth(response):
        raise RBCAuthRequired("RBC sign-in required")
    debug = {
        "xsrf_found": bool(_rbc_xsrf_token_from_storage_state(storage_state)),
        "http_non_ok": not bool(response.get("ok")),
        "http_status": response.get("status"),
        "parse_failed": not isinstance(response.get("payload"), dict),
        "parse_text_kind": None,
        "fetch_error": None,
    }
    parsed_payload = response.get("payload")
    if isinstance(parsed_payload, dict):
        parsed_payload["__breaktwenty_rbc_debug"] = debug
        return parsed_payload
    text = str(response.get("text") or "").strip()
    if not text:
        debug["parse_text_kind"] = "empty"
    elif text.lower().startswith("<!doctype html") or text.lower().startswith("<html"):
        debug["parse_text_kind"] = "html"
    elif text.startswith("{") or text.startswith("["):
        debug["parse_text_kind"] = "json-like"
    else:
        debug["parse_text_kind"] = "other"
    _rbc_log_event(
        f"{label} direct post unusable_response",
        level="warning",
        status_code=response.get("status"),
        url=_rbc_sanitize_url_for_log(response.get("url")),
        body=_rbc_sanitize_log_text(response.get("text"), limit=140),
    )
    return {"__breaktwenty_rbc_debug": debug}


def _rbc_account_link_params(acct: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}
    container_keys = {
        "params",
        "parameters",
        "formParams",
        "queryParams",
        "linkParams",
        "linkParameters",
        "values",
    }
    metadata_keys = {"type", "linkType", "key", "name", "paramName", "parameterName", "paramKey", "value", "paramValue", "parameterValue", "additions"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for map_key, map_value in value.items():
                if map_key in container_keys or map_key in metadata_keys:
                    continue
                if isinstance(map_value, (str, int, float, bool)) and str(map_key).strip():
                    params[str(map_key)] = str(map_value)
            key = (
                value.get("key")
                or value.get("name")
                or value.get("paramName")
                or value.get("parameterName")
                or value.get("paramKey")
                or value.get("id")
            )
            raw_value = value.get("value")
            if raw_value is None:
                raw_value = value.get("paramValue")
            if raw_value is None:
                raw_value = value.get("parameterValue")
            if key and raw_value is not None:
                if isinstance(raw_value, (dict, list)):
                    visit(raw_value)
                else:
                    params[str(key)] = str(raw_value)
            for child_key in container_keys:
                visit(value.get(child_key))
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(acct.get("linkParams"))
    visit(acct.get("linkParameters"))
    return params


def _rbc_loc_history_form_payload(acct: dict[str, Any]) -> dict[str, str]:
    link_params = acct.get("link_params") if isinstance(acct.get("link_params"), dict) else {}
    payload: dict[str, str] = {}
    for key in (
        "F22",
        "_AK_",
        "REQUEST",
        "ACCOUNT_TYPE",
        "SHORT_NUMBER",
        "PLOAN",
        "NOTE",
        "NICK_NAME",
        "ACCOUNT_NUMBER",
        "WM",
        "DS",
        "DI",
        "LANGUAGE",
    ):
        value = link_params.get(key)
        if value is not None and str(value).strip() != "":
            payload[key] = str(value)

    payload.setdefault("F22", "HTPCBINET")
    payload.setdefault("REQUEST", "AcctTransactionInquiry")
    payload.setdefault("ACCOUNT_TYPE", "R")
    payload.setdefault("LANGUAGE", "ENGLISH")
    payload.setdefault("WM", "N")
    payload.setdefault("DS", "N")
    payload.setdefault("DI", "N")

    account_number = str(acct.get("account_number") or "").strip()
    if account_number:
        payload.setdefault("ACCOUNT_NUMBER", account_number)
    return payload


def _rbc_clamp_loc_history_window(start_date: date, end_date: date) -> tuple[date, date]:
    max_start = end_date - timedelta(days=RBC_LOC_HISTORY_DAYS)
    if start_date < max_start:
        start_date = max_start
    if start_date > end_date:
        start_date = end_date
    return start_date, end_date


async def _fetch_rbc_loc_transactions_direct(
    storage_state: dict[str, Any],
    acct: dict[str, Any],
    *,
    start_date: date,
    end_date: date,
    user_agent: str,
) -> tuple[list[dict[str, Any]], bool]:
    external_id = acct.get("external_id") or ""
    payload = _rbc_loc_history_form_payload(acct)
    has_account_reference = any(payload.get(key) for key in ("PLOAN", "ACCOUNT_NUMBER", "SHORT_NUMBER", "NOTE"))
    if not has_account_reference:
        _rbc_log_event(
            "loc history form params missing",
            level="warning",
            account=external_id,
            available_param_keys=sorted(payload.keys()),
        )
        return [], False

    response = await _post_rbc_form_direct(
        storage_state,
        RBC_LOC_HISTORY_URL,
        payload,
        user_agent=user_agent,
    )
    if _rbc_response_requires_auth(response):
        raise RBCAuthRequired("RBC sign-in required")

    html_text = str(response.get("text") or "")
    if int(response.get("status") or 0) >= 400 or not html_text:
        _rbc_log_event(
            "loc history html unusable_response",
            level="warning",
            account=external_id,
            status_code=response.get("status"),
            url=_rbc_sanitize_url_for_log(response.get("url")),
            body=_rbc_sanitize_log_text(html_text, limit=140),
        )
        return [], False

    if not _rbc_loc_history_page_detected(html_text):
        _rbc_log_event(
            "loc history html unrecognized",
            level="warning",
            account=external_id,
            status_code=response.get("status"),
            body=_rbc_sanitize_log_text(html_text, limit=140),
        )
        return [], False

    raw_txns = _parse_rbc_loc_transactions_html(html_text, acct)
    window_txns = _filter_rbc_transactions_to_window(
        raw_txns,
        start_date=start_date,
        end_date=end_date,
    )
    _rbc_log_event(
        "loc history html parsed",
        debug=True,
        account=external_id,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        transactions=len(window_txns),
        form_param_keys=sorted(payload.keys()),
    )
    return window_txns, True


async def _fetch_rbc_cc_search_chunk_direct(
    storage_state: dict[str, Any],
    *,
    encrypted_id: str,
    external_id: str,
    from_date: str,
    to_date: str,
    user_agent: str,
    attempt: int = 1,
    max_attempts: int = 1,
) -> RBCCreditCardSearchChunkResult:
    search_url = f"{RBC_TXN_CC_SEARCH_URL}/{_rbc_encoded_account_id(encrypted_id)}"
    search_payload = {
        "transactionFromDate": from_date,
        "transactionToDate": to_date,
        "limit": RBC_CC_SEARCH_LIMIT,
    }
    try:
        search_result = await _post_rbc_json_direct(
            storage_state,
            search_url,
            search_payload,
            user_agent=user_agent,
            label=f"credit card search post fetch {external_id} {from_date} {to_date}",
        )
    except RBCAuthRequired:
        raise
    except Exception as e:
        _rbc_log_event(
            "credit card search fetch failed",
            level="warning",
            account=external_id,
            start=from_date,
            end=to_date,
            attempt=attempt,
            max_attempts=max_attempts,
            error=_rbc_sanitize_log_text(str(e), limit=140),
        )
        return None, False, RBC_CC_SEARCH_FAILURE_OTHER

    chunk_txns = _rbc_cc_transaction_list(search_result)
    search_debug = _rbc_result_debug(search_result)
    if (
        not isinstance(search_result, dict)
        or search_result.get("hasError")
        or search_debug.get("http_non_ok")
        or chunk_txns is None
    ):
        _rbc_log_event(
            "credit card search unusable_response",
            level="warning" if search_debug.get("http_non_ok") or search_result else "info",
            debug=not search_debug.get("http_non_ok"),
            account=external_id,
            start=from_date,
            end=to_date,
            attempt=attempt,
            max_attempts=max_attempts,
            has_error=bool(isinstance(search_result, dict) and search_result.get("hasError")),
            http_status=search_debug.get("http_status"),
            http_non_ok=search_debug.get("http_non_ok"),
            parse_failed=search_debug.get("parse_failed"),
            parse_text_kind=search_debug.get("parse_text_kind"),
            fetch_error=_rbc_sanitize_log_text(search_debug.get("fetch_error"), limit=140),
        )
        exact_http_500 = bool(search_debug.get("http_non_ok")) and str(
            search_debug.get("http_status") or ""
        ) == "500"
        return (
            None,
            False,
            RBC_CC_SEARCH_FAILURE_HTTP_500 if exact_http_500 else RBC_CC_SEARCH_FAILURE_OTHER,
        )

    return chunk_txns, True, None


async def _fetch_rbc_cc_search_window_direct(
    storage_state: dict[str, Any],
    *,
    encrypted_id: str,
    external_id: str,
    start_date: date,
    end_date: date,
    user_agent: str,
    split_depth: int = 0,
    window_results: list[RBCCreditCardSearchWindow] | None = None,
    failure_results: list[RBCCreditCardSearchFailure] | None = None,
    refresh_context: Callable[[], Awaitable[None]] | None = None,
    max_attempts_override: int | None = None,
) -> tuple[list[dict] | None, bool]:
    span_days = (end_date - start_date).days + 1
    max_attempts = max_attempts_override or (
        RBC_CC_SEARCH_MIN_WINDOW_RETRY_ATTEMPTS
        if span_days <= RBC_CC_MIN_SEARCH_WINDOW_DAYS
        else RBC_CC_SEARCH_RETRY_ATTEMPTS
    )
    window_deadline = _rbc_monotonic_seconds() + RBC_CC_SEARCH_WINDOW_DEADLINE_SECONDS
    chunk_txns: list[dict] | None = None
    chunk_succeeded = False
    failure_kinds: list[str] = []
    planned_attempts = max_attempts
    attempt = 0
    while attempt < planned_attempts:
        remaining_seconds = window_deadline - _rbc_monotonic_seconds()
        if remaining_seconds <= 0:
            failure_kinds.append(RBC_CC_SEARCH_FAILURE_DEADLINE)
            break
        attempt += 1
        try:
            async with asyncio.timeout(remaining_seconds):
                chunk_txns, chunk_succeeded, failure_kind = await _fetch_rbc_cc_search_chunk_direct(
                    storage_state,
                    encrypted_id=encrypted_id,
                    external_id=external_id,
                    from_date=start_date.isoformat(),
                    to_date=end_date.isoformat(),
                    user_agent=user_agent,
                    attempt=attempt,
                    max_attempts=planned_attempts,
                )
        except TimeoutError:
            chunk_txns = None
            chunk_succeeded = False
            failure_kind = RBC_CC_SEARCH_FAILURE_DEADLINE
            _rbc_log_event(
                "credit card search window_deadline_exhausted",
                level="warning",
                account=external_id,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                attempt=attempt,
                split_depth=split_depth,
                deadline_seconds=RBC_CC_SEARCH_WINDOW_DEADLINE_SECONDS,
            )
        if chunk_succeeded and chunk_txns is not None:
            break
        failure_kinds.append(failure_kind or RBC_CC_SEARCH_FAILURE_OTHER)
        if attempt >= max_attempts:
            if (
                attempt == max_attempts
                and max_attempts_override is None
                and len(failure_kinds) == max_attempts
                and all(kind == RBC_CC_SEARCH_FAILURE_HTTP_500 for kind in failure_kinds)
            ):
                planned_attempts = max_attempts + 1
                retry_wait_seconds = RBC_CC_SEARCH_HTTP_500_FINAL_WAIT_SECONDS
            else:
                break
        else:
            retry_wait_seconds = min(
                RBC_CC_SEARCH_RETRY_WAIT_SECONDS * (2 ** (attempt - 1)),
                RBC_CC_SEARCH_RETRY_WAIT_MAX_SECONDS,
            )
        remaining_seconds = window_deadline - _rbc_monotonic_seconds()
        if remaining_seconds <= 0:
            break
        bounded_wait_seconds = min(retry_wait_seconds, remaining_seconds)
        _rbc_log_event(
            "credit card search retry_same_window",
            level="warning",
            account=external_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            attempt=attempt,
            max_attempts=planned_attempts,
            split_depth=split_depth,
            failure_kind=failure_kinds[-1],
            retry_wait_seconds=bounded_wait_seconds,
        )
        if (
            refresh_context is not None
            and RBC_CC_SEARCH_REWARM_EVERY_FAILURES > 0
            and attempt % RBC_CC_SEARCH_REWARM_EVERY_FAILURES == 0
        ):
            await refresh_context()
        await asyncio.sleep(bounded_wait_seconds)
        if bounded_wait_seconds < retry_wait_seconds:
            failure_kinds.append(RBC_CC_SEARCH_FAILURE_DEADLINE)
            break
    if not chunk_succeeded or chunk_txns is None:
        if (
            span_days > RBC_CC_MIN_SEARCH_WINDOW_DAYS
            and split_depth < RBC_CC_FAILED_SEARCH_MAX_FAIL_SPLIT_DEPTH
        ):
            left_span_days = max(span_days // 2, 1)
            left_end = start_date + timedelta(days=left_span_days - 1)
            right_start = left_end + timedelta(days=1)

            _rbc_log_event(
                "credit card search split_failed_window",
                level="warning",
                account=external_id,
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                split_depth=split_depth,
                message="retrying failed credit-card search window as smaller ranges",
            )

            left_txns, left_succeeded = await _fetch_rbc_cc_search_window_direct(
                storage_state,
                encrypted_id=encrypted_id,
                external_id=external_id,
                start_date=start_date,
                end_date=left_end,
                user_agent=user_agent,
                split_depth=split_depth + 1,
                window_results=window_results,
                failure_results=failure_results,
                refresh_context=refresh_context,
                max_attempts_override=max_attempts_override,
            )
            right_txns, right_succeeded = await _fetch_rbc_cc_search_window_direct(
                storage_state,
                encrypted_id=encrypted_id,
                external_id=external_id,
                start_date=right_start,
                end_date=end_date,
                user_agent=user_agent,
                split_depth=split_depth + 1,
                window_results=window_results,
                failure_results=failure_results,
                refresh_context=refresh_context,
                max_attempts_override=max_attempts_override,
            )
            combined_txns = _deduplicate_rbc_raw_transactions(
                (left_txns or []) + (right_txns or [])
            )
            return combined_txns, left_succeeded and right_succeeded

        _rbc_log_event(
            "credit card search failed_window",
            level="warning",
            account=external_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            split_depth=split_depth,
            message="keeping other credit-card backfill windows after this failed search subwindow",
        )
        deduped_chunk_txns = _deduplicate_rbc_raw_transactions(chunk_txns or [])
        if window_results is not None:
            window_results.append((start_date, end_date, deduped_chunk_txns, False))
        if failure_results is not None:
            terminal_failure_kind = (
                RBC_CC_SEARCH_FAILURE_HTTP_500
                if failure_kinds
                and len(failure_kinds) == planned_attempts
                and all(kind == RBC_CC_SEARCH_FAILURE_HTTP_500 for kind in failure_kinds)
                else (failure_kinds[-1] if failure_kinds else RBC_CC_SEARCH_FAILURE_OTHER)
            )
            failure_results.append((start_date, end_date, terminal_failure_kind))
        return deduped_chunk_txns, chunk_succeeded

    deduped_chunk_txns = _deduplicate_rbc_raw_transactions(chunk_txns)
    if not _rbc_cc_search_dates_within_window(
        deduped_chunk_txns,
        start_date=start_date,
        end_date=end_date,
    ):
        _rbc_log_event(
            "credit card search date_mismatch",
            level="warning",
            account=external_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            split_depth=split_depth,
            message="search response included transactions outside the requested date window",
        )
        if window_results is not None:
            window_results.append((start_date, end_date, [], False))
        if failure_results is not None:
            failure_results.append((start_date, end_date, RBC_CC_SEARCH_FAILURE_OTHER))
        return None, False

    if len(deduped_chunk_txns) < RBC_CC_SEARCH_LIMIT:
        if window_results is not None:
            window_results.append((start_date, end_date, deduped_chunk_txns, True))
        return deduped_chunk_txns, True

    if span_days <= RBC_CC_MIN_SEARCH_WINDOW_DAYS:
        _rbc_log_event(
            "credit card search capped_min_window",
            level="warning",
            account=external_id,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            transactions=len(deduped_chunk_txns),
            split_depth=split_depth,
            message="keeping capped credit-card search window because the remaining date range is already narrow",
        )
        if window_results is not None:
            window_results.append((start_date, end_date, deduped_chunk_txns, True))
        return deduped_chunk_txns, True

    left_span_days = max(span_days // 2, 1)
    left_end = start_date + timedelta(days=left_span_days - 1)
    right_start = left_end + timedelta(days=1)

    _rbc_log_event(
        "credit card search split_window",
        debug=True,
        account=external_id,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        transactions=len(deduped_chunk_txns),
        split_depth=split_depth,
    )

    left_txns, left_succeeded = await _fetch_rbc_cc_search_window_direct(
        storage_state,
        encrypted_id=encrypted_id,
        external_id=external_id,
        start_date=start_date,
        end_date=left_end,
        user_agent=user_agent,
        split_depth=split_depth + 1,
        window_results=window_results,
        failure_results=failure_results,
        refresh_context=refresh_context,
    )
    right_txns, right_succeeded = await _fetch_rbc_cc_search_window_direct(
        storage_state,
        encrypted_id=encrypted_id,
        external_id=external_id,
        start_date=right_start,
        end_date=end_date,
        user_agent=user_agent,
        split_depth=split_depth + 1,
        window_results=window_results,
        failure_results=failure_results,
        refresh_context=refresh_context,
    )
    combined_txns = _deduplicate_rbc_raw_transactions((left_txns or []) + (right_txns or []))
    return combined_txns, left_succeeded and right_succeeded


async def _warm_rbc_cc_account_context_direct(
    storage_state: dict[str, Any],
    *,
    encrypted_id: str,
    external_id: str,
    user_agent: str,
) -> None:
    encoded_id = _rbc_encoded_account_id(encrypted_id)
    timestamp = _rbc_timestamp_ms()
    warmup_urls = [
        (
            "arrangement",
            f"{RBC_CC_ARRANGEMENT_URL}/{encoded_id}?timestamp={timestamp}",
        ),
        (
            "activation",
            f"{RBC_CC_LINE_OF_BUSINESS_URL}/{encoded_id}/credit-card-activation?timestamp={timestamp + 1}",
        ),
        (
            "line_of_business",
            f"{RBC_CC_LINE_OF_BUSINESS_URL}/{encoded_id}/line-of-business?timestamp={timestamp + 2}",
        ),
        (
            "posted_probe",
            f"{RBC_TXN_CC_URL}/{encoded_id}?billingStatus=posted&txType=postedCreditCard&timestamp={timestamp + 3}",
        ),
        (
            "authorized_probe",
            f"{RBC_TXN_CC_AUTHORIZED_URL}/{encoded_id}?billingStatus=pending&txType=authorizedCreditCard&timestamp={timestamp + 4}",
        ),
    ]
    for step, url in warmup_urls:
        try:
            result = await _fetch_rbc_json_direct(
                storage_state,
                url,
                user_agent=user_agent,
                label=f"credit card warmup {step} {external_id}",
            )
            _rbc_log_event(
                "credit card warmup",
                debug=True,
                account=external_id,
                step=step,
                succeeded=isinstance(result, dict),
                has_error=bool(isinstance(result, dict) and result.get("hasError")),
            )
        except RBCAuthRequired:
            raise
        except Exception as e:
            _rbc_log_event(
                "credit card warmup failed",
                level="warning",
                account=external_id,
                step=step,
                error=_rbc_sanitize_log_text(str(e), limit=140),
            )


def _rbc_cc_backfill_windows(
    backfill_days: int,
    *,
    today: date | None = None,
) -> list[tuple[date, date]]:
    current_day = today or date.today()
    absolute_floor = current_day - timedelta(days=backfill_days)
    current_end = current_day
    windows: list[tuple[date, date]] = []

    while current_end >= absolute_floor:
        current_start = max(
            absolute_floor,
            current_end - timedelta(days=RBC_CC_BACKFILL_CHUNK_DAYS - 1),
        )
        windows.append((current_start, current_end))
        if current_start <= absolute_floor:
            break
        current_end = current_start - timedelta(days=1)

    return windows


async def _collect_rbc_cc_backward_history_direct(
    storage_state: dict[str, Any],
    *,
    account: dict,
    encrypted_id: str,
    external_id: str,
    backfill_days: int,
    user_agent: str,
    windows: list[tuple[date, date]] | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[list[dict], bool]:
    semaphore = asyncio.Semaphore(RBC_CC_SEARCH_CONCURRENCY)
    windows = windows if windows is not None else _rbc_cc_backfill_windows(backfill_days)
    if not windows:
        return [], True

    async def fetch_window(
        window: tuple[date, date],
    ) -> RBCCreditCardBackfillResult:
        current_start, current_end = window
        leaf_windows: list[RBCCreditCardSearchWindow] = []
        leaf_failures: list[RBCCreditCardSearchFailure] = []
        window_storage_state = copy.deepcopy(storage_state)

        async def refresh_window_context() -> None:
            await _warm_rbc_cc_account_context_direct(
                window_storage_state,
                encrypted_id=encrypted_id,
                external_id=external_id,
                user_agent=user_agent,
            )

        async with semaphore:
            await _record_rbc_transaction_window(
                transaction_window_recorder,
                event="started",
                account=account,
                mode="backfill",
                start_date=current_start,
                end_date=current_end,
            )
            chunk_txns, chunk_succeeded = await _fetch_rbc_cc_search_window_direct(
                window_storage_state,
                encrypted_id=encrypted_id,
                external_id=external_id,
                start_date=current_start,
                end_date=current_end,
                user_agent=user_agent,
                window_results=leaf_windows,
                failure_results=leaf_failures,
                refresh_context=refresh_window_context,
            )
        deduped_chunk_txns = _deduplicate_rbc_raw_transactions(chunk_txns or [])
        if not leaf_windows:
            leaf_windows = [(current_start, current_end, deduped_chunk_txns, chunk_succeeded)]
        deferred_failure_ranges = {
            (failed_start, failed_end)
            for failed_start, failed_end, failure_kind in leaf_failures
            if failure_kind == RBC_CC_SEARCH_FAILURE_HTTP_500
        }

        split_into_leaf_windows = any(
            (leaf_start, leaf_end) != (current_start, current_end)
            for leaf_start, leaf_end, _leaf_txns, _leaf_succeeded in leaf_windows
        )
        for leaf_start, leaf_end, leaf_txns, leaf_succeeded in leaf_windows:
            if (leaf_start, leaf_end) != (current_start, current_end):
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=account,
                    mode="backfill",
                    start_date=leaf_start,
                    end_date=leaf_end,
                )
            if leaf_succeeded:
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=account,
                    mode="backfill",
                    start_date=leaf_start,
                    end_date=leaf_end,
                    transactions=leaf_txns,
                )
            elif (leaf_start, leaf_end) not in deferred_failure_ranges:
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode="backfill",
                    start_date=leaf_start,
                    end_date=leaf_end,
                    transactions=leaf_txns,
                    error="RBC credit-card search window failed or returned unusable data.",
                )
            else:
                _rbc_log_event(
                    "credit card search deferred_recovery_candidate",
                    level="warning",
                    account=external_id,
                    start=leaf_start.isoformat(),
                    end=leaf_end.isoformat(),
                )
            _rbc_log_event(
                "credit card backfill window",
                debug=True,
                account=external_id,
                start=leaf_start.isoformat(),
                end=leaf_end.isoformat(),
                parent_start=current_start.isoformat(),
                parent_end=current_end.isoformat(),
                transactions=len(leaf_txns),
                succeeded=bool(leaf_succeeded),
            )
        if split_into_leaf_windows and not deferred_failure_ranges:
            await _record_rbc_transaction_window(
                transaction_window_recorder,
                event="completed" if chunk_succeeded else "failed",
                account=account,
                mode="backfill",
                start_date=current_start,
                end_date=current_end,
                transactions=deduped_chunk_txns,
                error=None if chunk_succeeded else "RBC credit-card split search window failed or returned unusable data.",
            )
        return RBCCreditCardBackfillResult(
            start_date=current_start,
            end_date=current_end,
            transactions=deduped_chunk_txns,
            succeeded=chunk_succeeded,
            leaf_windows=leaf_windows,
            leaf_failures=leaf_failures,
        )

    window_results = list(await asyncio.gather(*(fetch_window(window) for window in windows)))
    recovery_candidates: list[tuple[RBCCreditCardBackfillResult, date, date]] = []
    for result in window_results:
        exact_http_500_failures = {
            (failed_start, failed_end)
            for failed_start, failed_end, failure_kind in result.leaf_failures
            if failure_kind == RBC_CC_SEARCH_FAILURE_HTTP_500
        }
        for leaf_start, leaf_end, _leaf_txns, leaf_succeeded in result.leaf_windows:
            if not leaf_succeeded and (leaf_start, leaf_end) in exact_http_500_failures:
                recovery_candidates.append((result, leaf_start, leaf_end))

    recovered_parent_results: set[tuple[date, date]] = set()
    pending_recovery_candidates = recovery_candidates
    if pending_recovery_candidates:
        recovery_deadline = (
            _rbc_monotonic_seconds() + RBC_CC_SEARCH_DEFERRED_RECOVERY_BUDGET_SECONDS
        )
        _rbc_log_event(
            "credit card search deferred_recovery_queued",
            level="warning",
            account=external_id,
            windows=len(pending_recovery_candidates),
            cooldown_schedule_seconds=RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS,
            max_rounds=len(RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS),
            budget_seconds=RBC_CC_SEARCH_DEFERRED_RECOVERY_BUDGET_SECONDS,
        )
        recovery_storage_state = copy.deepcopy(storage_state)

        for recovery_round, cooldown_seconds in enumerate(
            RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS,
            start=1,
        ):
            if not pending_recovery_candidates:
                break
            remaining_seconds = recovery_deadline - _rbc_monotonic_seconds()
            if remaining_seconds <= 0:
                break
            await asyncio.sleep(min(cooldown_seconds, remaining_seconds))
            remaining_seconds = recovery_deadline - _rbc_monotonic_seconds()
            if remaining_seconds <= 0:
                break
            try:
                async with asyncio.timeout(remaining_seconds):
                    await _warm_rbc_cc_account_context_direct(
                        recovery_storage_state,
                        encrypted_id=encrypted_id,
                        external_id=external_id,
                        user_agent=user_agent,
                    )
            except TimeoutError:
                break

            round_candidates = pending_recovery_candidates
            pending_recovery_candidates = []
            _rbc_log_event(
                "credit card search deferred_recovery_round",
                level="warning",
                account=external_id,
                recovery_round=recovery_round,
                windows=len(round_candidates),
                cooldown_seconds=cooldown_seconds,
            )
            for candidate_index, (parent_result, candidate_start, candidate_end) in enumerate(
                round_candidates
            ):
                remaining_seconds = recovery_deadline - _rbc_monotonic_seconds()
                if remaining_seconds <= 0:
                    pending_recovery_candidates.extend(round_candidates[candidate_index:])
                    break

                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=account,
                    mode="backfill",
                    start_date=candidate_start,
                    end_date=candidate_end,
                )
                recovery_leaf_windows: list[RBCCreditCardSearchWindow] = []
                recovery_failures: list[RBCCreditCardSearchFailure] = []
                try:
                    async with asyncio.timeout(remaining_seconds):
                        recovered_txns, recovered_succeeded = await _fetch_rbc_cc_search_window_direct(
                            recovery_storage_state,
                            encrypted_id=encrypted_id,
                            external_id=external_id,
                            start_date=candidate_start,
                            end_date=candidate_end,
                            user_agent=user_agent,
                            window_results=recovery_leaf_windows,
                            failure_results=recovery_failures,
                            max_attempts_override=1,
                        )
                except TimeoutError:
                    recovered_txns = []
                    recovered_succeeded = False
                    recovery_failures.append(
                        (candidate_start, candidate_end, RBC_CC_SEARCH_FAILURE_DEADLINE)
                    )

                deduped_recovered_txns = _deduplicate_rbc_raw_transactions(recovered_txns or [])
                if not recovery_leaf_windows:
                    recovery_leaf_windows = [
                        (
                            candidate_start,
                            candidate_end,
                            deduped_recovered_txns,
                            recovered_succeeded,
                        )
                    ]
                exact_http_500_ranges = {
                    (failed_start, failed_end)
                    for failed_start, failed_end, failure_kind in recovery_failures
                    if failure_kind == RBC_CC_SEARCH_FAILURE_HTTP_500
                }
                for leaf_start, leaf_end, leaf_txns, leaf_succeeded in recovery_leaf_windows:
                    if (leaf_start, leaf_end) != (candidate_start, candidate_end):
                        await _record_rbc_transaction_window(
                            transaction_window_recorder,
                            event="started",
                            account=account,
                            mode="backfill",
                            start_date=leaf_start,
                            end_date=leaf_end,
                        )
                    if leaf_succeeded:
                        await _record_rbc_transaction_window(
                            transaction_window_recorder,
                            event="completed",
                            account=account,
                            mode="backfill",
                            start_date=leaf_start,
                            end_date=leaf_end,
                            transactions=leaf_txns,
                        )
                    elif (leaf_start, leaf_end) in exact_http_500_ranges:
                        pending_recovery_candidates.append(
                            (parent_result, leaf_start, leaf_end)
                        )
                    else:
                        await _record_rbc_transaction_window(
                            transaction_window_recorder,
                            event="failed",
                            account=account,
                            mode="backfill",
                            start_date=leaf_start,
                            end_date=leaf_end,
                            transactions=leaf_txns,
                            error="RBC credit-card deferred recovery failed or returned unusable data.",
                        )

                parent_result.leaf_windows = [
                    leaf
                    for leaf in parent_result.leaf_windows
                    if (leaf[0], leaf[1]) != (candidate_start, candidate_end)
                ] + recovery_leaf_windows
                parent_result.transactions = _deduplicate_rbc_raw_transactions(
                    [
                        transaction
                        for _leaf_start, _leaf_end, leaf_txns, _leaf_succeeded in parent_result.leaf_windows
                        for transaction in leaf_txns
                    ]
                )
                parent_result.succeeded = bool(parent_result.leaf_windows) and all(
                    leaf_succeeded
                    for _leaf_start, _leaf_end, _leaf_txns, leaf_succeeded in parent_result.leaf_windows
                )
                recovered_parent_results.add((parent_result.start_date, parent_result.end_date))
                _rbc_log_event(
                    "credit card search deferred_recovery_result",
                    level="info" if recovered_succeeded else "warning",
                    account=external_id,
                    start=candidate_start.isoformat(),
                    end=candidate_end.isoformat(),
                    recovery_round=recovery_round,
                    transactions=len(deduped_recovered_txns),
                    succeeded=bool(recovered_succeeded),
                    retry_needed=bool(exact_http_500_ranges),
                )

        if pending_recovery_candidates:
            _rbc_log_event(
                "credit card search deferred_recovery_exhausted",
                level="warning",
                account=external_id,
                remaining_windows=len(pending_recovery_candidates),
                max_rounds=len(RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS),
                budget_seconds=RBC_CC_SEARCH_DEFERRED_RECOVERY_BUDGET_SECONDS,
            )
            for parent_result, candidate_start, candidate_end in pending_recovery_candidates:
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode="backfill",
                    start_date=candidate_start,
                    end_date=candidate_end,
                    transactions=[],
                    error="RBC credit-card exact HTTP 500 recovery was exhausted; retry is still needed.",
                )
                recovered_parent_results.add((parent_result.start_date, parent_result.end_date))

        for result in window_results:
            if (result.start_date, result.end_date) not in recovered_parent_results:
                continue
            if not any(
                (leaf_start, leaf_end) != (result.start_date, result.end_date)
                for leaf_start, leaf_end, _leaf_txns, _leaf_succeeded in result.leaf_windows
            ):
                continue
            await _record_rbc_transaction_window(
                transaction_window_recorder,
                event="completed" if result.succeeded else "failed",
                account=account,
                mode="backfill",
                start_date=result.start_date,
                end_date=result.end_date,
                transactions=result.transactions,
                error=(
                    None
                    if result.succeeded
                    else "RBC credit-card split search recovery failed or returned unusable data."
                ),
            )

    search_backfill_succeeded = bool(window_results) and all(
        result.succeeded for result in window_results
    )

    collected: list[dict] = []
    for result in window_results:
        if result.transactions:
            collected.extend(result.transactions)

    return _deduplicate_rbc_raw_transactions(collected), search_backfill_succeeded


async def fetch_transactions_direct(
    storage_state: dict[str, Any],
    accounts: list[dict],
    *,
    user_agent: str,
    mode_per_account: dict[str, str | dict[str, Any]] | None = None,
    backfill_days: int = RBC_BACKFILL_DAYS,
    incremental_days: int = RBC_INCREMENTAL_DAYS,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, list[dict]], set[str], set[str], set[str]]:
    results: dict[str, list[dict]] = {}
    succeeded_accounts: set[str] = set()
    failed_accounts: set[str] = set()
    network_failed_accounts: set[str] = set()

    for acct in accounts:
        encrypted_id = acct.get("encrypted_id")
        external_id = acct.get("external_id")
        category = acct.get("category")

        if not encrypted_id or not external_id:
            continue

        if mode_per_account is None:
            mode = "backfill"
            interval_value = RBC_BACKFILL_DAYS + 1
            window_start = None
            window_end = None
        else:
            mode = _rbc_sync_state_mode(mode_per_account, external_id)
            if mode == "backfill":
                interval_value = backfill_days + 1
                window_start = None
                window_end = None
            else:
                window_start, window_end = _rbc_sync_state_window_dates(
                    mode_per_account,
                    external_id,
                    fallback_days=incremental_days,
                )
                interval_value = _rbc_sync_state_interval_value(
                    mode_per_account,
                    external_id,
                    fallback_days=incremental_days,
                ) + 1

        try:
            if category == "creditCards":
                await _warm_rbc_cc_account_context_direct(
                    storage_state,
                    encrypted_id=encrypted_id,
                    external_id=external_id,
                    user_agent=user_agent,
                )
                if mode == "backfill":
                    deduped_search_cc_txns, search_backfill_succeeded = await _collect_rbc_cc_backward_history_direct(
                        storage_state,
                        account=acct,
                        encrypted_id=encrypted_id,
                        external_id=external_id,
                        backfill_days=backfill_days,
                        user_agent=user_agent,
                        windows=_rbc_sync_state_pending_backfill_windows(
                            mode_per_account,
                            external_id,
                            fallback_days=backfill_days,
                        ) if mode_per_account is not None else None,
                        transaction_window_recorder=transaction_window_recorder,
                    )
                    deduped_cc_txns = _deduplicate_rbc_raw_transactions(deduped_search_cc_txns)

                    if search_backfill_succeeded and not deduped_cc_txns:
                        succeeded_accounts.add(external_id)
                        continue

                    if search_backfill_succeeded:
                        succeeded_accounts.add(external_id)
                        if deduped_cc_txns:
                            results[external_id] = deduped_cc_txns
                        continue

                    _rbc_log_event(
                        "credit card backfill search incomplete",
                        level="warning",
                        account=external_id,
                        partial_transactions=len(deduped_search_cc_txns),
                        message="leaving failed RBC credit-card search windows retryable",
                    )
                    failed_accounts.add(external_id)
                    if deduped_cc_txns:
                        results[external_id] = deduped_cc_txns
                    continue

                effective_start = window_start or (date.today() - timedelta(days=interval_value))
                effective_end = window_end or date.today()
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=acct,
                    mode=mode,
                    start_date=effective_start,
                    end_date=effective_end,
                )
                cc_txns, chunk_succeeded = await _fetch_rbc_cc_search_window_direct(
                    storage_state,
                    encrypted_id=encrypted_id,
                    external_id=external_id,
                    start_date=effective_start,
                    end_date=effective_end,
                    user_agent=user_agent,
                    refresh_context=lambda: _warm_rbc_cc_account_context_direct(
                        storage_state,
                        encrypted_id=encrypted_id,
                        external_id=external_id,
                        user_agent=user_agent,
                    ),
                )
                if chunk_succeeded and cc_txns is not None:
                    await _record_rbc_transaction_window(
                        transaction_window_recorder,
                        event="completed",
                        account=acct,
                        mode=mode,
                        start_date=effective_start,
                        end_date=effective_end,
                        transactions=cc_txns,
                    )
                    succeeded_accounts.add(external_id)
                    if cc_txns:
                        results[external_id] = cc_txns
                    continue

                cc_txns = _deduplicate_rbc_raw_transactions(cc_txns) if cc_txns else []
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=acct,
                    mode=mode,
                    start_date=effective_start,
                    end_date=effective_end,
                    error="RBC credit-card incremental search window failed.",
                )
                failed_accounts.add(external_id)
                if cc_txns:
                    results[external_id] = cc_txns
                continue

            standard_backfill_days = RBC_LOC_HISTORY_DAYS if category == "linesLoans" else backfill_days
            effective_start, effective_end = (
                _rbc_sync_state_backfill_window_dates(
                    mode_per_account,
                    external_id,
                    fallback_days=standard_backfill_days,
                )
                if mode == "backfill"
                else (
                    window_start or (date.today() - timedelta(days=interval_value)),
                    window_end or date.today(),
                )
            )
            if category == "linesLoans":
                effective_start, effective_end = _rbc_clamp_loc_history_window(effective_start, effective_end)
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=acct,
                    mode=mode,
                    start_date=effective_start,
                    end_date=effective_end,
                )
                loc_txns, loc_succeeded = await _fetch_rbc_loc_transactions_direct(
                    storage_state,
                    acct,
                    start_date=effective_start,
                    end_date=effective_end,
                    user_agent=user_agent,
                )
                if loc_succeeded:
                    deduped_loc_txns = _deduplicate_rbc_raw_transactions(loc_txns)
                    await _record_rbc_transaction_window(
                        transaction_window_recorder,
                        event="completed",
                        account=acct,
                        mode=mode,
                        start_date=effective_start,
                        end_date=effective_end,
                        transactions=deduped_loc_txns,
                    )
                    succeeded_accounts.add(external_id)
                    if deduped_loc_txns:
                        results[external_id] = deduped_loc_txns
                    continue

                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=acct,
                    mode=mode,
                    start_date=effective_start,
                    end_date=effective_end,
                    error="RBC line-of-credit legacy transaction page failed or was unrecognized.",
                )
                failed_accounts.add(external_id)
                continue

            await _record_rbc_transaction_window(
                transaction_window_recorder,
                event="started",
                account=acct,
                mode=mode,
                start_date=effective_start,
                end_date=effective_end,
            )
            url = (
                f"{RBC_TXN_PDA_URL}/{encrypted_id}"
                f"?intervalType=DAY&intervalValue={max((effective_end - effective_start).days, 1) + 1}"
                "&type=ALL&txType=pda&useColtOnly=response"
            )
            txn_result = await _fetch_rbc_json_direct(
                storage_state,
                url,
                user_agent=user_agent,
                label=f"transaction fetch {external_id}",
            )

            raw_txns = (txn_result.get("transactionList") or []) if isinstance(txn_result, dict) else []
            window_txns = (
                _filter_rbc_transactions_to_window(
                    raw_txns,
                    start_date=effective_start,
                    end_date=effective_end,
                )
                if raw_txns
                else []
            )
            deduped_txns = _deduplicate_rbc_transactions(window_txns) if window_txns else []

            if not txn_result or txn_result.get("hasError"):
                _rbc_log_event(
                    "transaction fetch unusable_response",
                    level="warning",
                    account=external_id,
                    category=category,
                    has_error=bool(txn_result and txn_result.get("hasError")),
                )
                await _record_rbc_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=acct,
                    mode=mode,
                    start_date=effective_start,
                    end_date=effective_end,
                    error="RBC transaction response was empty or marked as an error.",
                )
                failed_accounts.add(external_id)
                continue

            await _record_rbc_transaction_window(
                transaction_window_recorder,
                event="completed",
                account=acct,
                mode=mode,
                start_date=effective_start,
                end_date=effective_end,
                transactions=deduped_txns,
            )
            succeeded_accounts.add(external_id)
            if deduped_txns:
                results[external_id] = deduped_txns

        except RBCAuthRequired:
            raise
        except Exception as e:
            failed_accounts.add(external_id)
            if is_network_error(e):
                network_failed_accounts.add(external_id)
            try:
                if category and category != "creditCards":
                    failed_fallback_days = RBC_LOC_HISTORY_DAYS if category == "linesLoans" else backfill_days
                    failed_start, failed_end = (
                        _rbc_sync_state_backfill_window_dates(
                            mode_per_account,
                            external_id,
                            fallback_days=failed_fallback_days,
                        )
                        if mode == "backfill"
                        else (
                            window_start or (date.today() - timedelta(days=interval_value)),
                            window_end or date.today(),
                        )
                    )
                    if category == "linesLoans":
                        failed_start, failed_end = _rbc_clamp_loc_history_window(failed_start, failed_end)
                    await _record_rbc_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=acct,
                        mode=mode,
                        start_date=failed_start,
                        end_date=failed_end,
                        error=str(e),
                    )
            except Exception:
                pass
            _rbc_log_event(
                "transaction fetch failed",
                level="warning",
                account=external_id,
                category=category or "",
                error=_rbc_sanitize_log_text(str(e), limit=140),
            )
            continue

    return results, succeeded_accounts, failed_accounts, network_failed_accounts


async def _sync_rbc_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    *,
    user_id: int | None = None,
    sync_scope: str = "full",
    mode_resolver: ModeResolver | None = None,
    account_sync_callback: AccountSyncCallback | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> dict[str, Any] | None:
    started_at = datetime.now()
    user_agent = _rbc_session_user_agent(session_artifact)

    _rbc_log_event(
        "saved-session sync start",
        user_id=user_id,
        engine="direct",
        debug=True,
    )

    account_payload = await _fetch_rbc_json_direct(
        storage_state,
        RBC_ACCOUNT_LIST_URL,
        user_agent=user_agent,
        label="account list",
    )
    if not _rbc_account_list_authorized(account_payload):
        error_state = (
            account_payload.get("errorState")
            if isinstance(account_payload, dict)
            and isinstance(account_payload.get("errorState"), dict)
            else {}
        )
        error_code = next(
            (
                error_state.get(key)
                for key in ("errorCode", "code", "statusCode")
                if error_state.get(key) not in (None, "")
            ),
            None,
        )
        error_message = next(
            (
                error_state.get(key)
                for key in ("errorMessage", "message", "description")
                if error_state.get(key) not in (None, "")
            ),
            None,
        )
        _rbc_log_event(
            "saved-session sync accounts unavailable",
            user_id=user_id,
            level="warning",
            engine="direct",
            auth_signal=("account_payload_error_state" if error_state else "account_payload_unusable"),
            payload_type=type(account_payload).__name__,
            has_error=bool(error_state.get("hasError")),
            error_code=_rbc_sanitize_log_text(str(error_code), limit=80) if error_code else None,
            error_message=(
                _rbc_sanitize_log_text(str(error_message), limit=180)
                if error_message
                else None
            ),
            present_account_categories=[
                key
                for key in RBC_ACCOUNT_CATEGORY_KEYS
                if isinstance(account_payload, dict) and key in account_payload
            ],
        )
        return None

    accounts = _rbc_accounts_from_account_list_payload(account_payload)
    if not accounts:
        _rbc_log_event(
            "saved-session sync accounts empty",
            user_id=user_id,
            level="warning",
            engine="direct",
        )
        return None

    session_artifact.update(
        refresh_session_artifact(
            session_artifact,
            user_agent=user_agent,
            captured_at=datetime.now(timezone.utc).isoformat(),
            extra={"accounts_payload": account_payload},
        )
    )

    if account_sync_callback:
        await account_sync_callback(accounts)

    if sync_scope == "accounts":
        duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)
        _rbc_log_event(
            "saved-session account sync success",
            user_id=user_id,
            engine="direct",
            accounts=len(accounts),
            duration_ms=duration_ms,
        )
        return {
            "status": "ok",
            "accounts": accounts,
            "transactions": {},
            "transaction_fetch_succeeded_accounts": [],
        }

    mode_map = await mode_resolver(accounts) if mode_resolver else {}
    (
        raw_transactions,
        transaction_fetch_succeeded_accounts,
        transaction_fetch_failed_accounts,
        transaction_fetch_network_failed_accounts,
    ) = await fetch_transactions_direct(
        storage_state,
        accounts,
        user_agent=user_agent,
        mode_per_account=mode_map,
        transaction_window_recorder=transaction_window_recorder,
    )

    duration_ms = int((datetime.now() - started_at).total_seconds() * 1000)
    transaction_count = sum(len(items or []) for items in raw_transactions.values())
    if transaction_fetch_failed_accounts:
        status = (
            "network_error"
            if transaction_fetch_network_failed_accounts
            and transaction_fetch_network_failed_accounts == transaction_fetch_failed_accounts
            else "error"
        )
        failed_count = len(transaction_fetch_failed_accounts)
        message = (
            "Connection failed"
            if status == "network_error"
            else (
                f"RBC transaction fetch failed for {failed_count} "
                f"{'account' if failed_count == 1 else 'accounts'}."
            )
        )
        _rbc_log_event(
            "saved-session sync transaction fetch incomplete",
            user_id=user_id,
            engine="direct",
            level="warning",
            accounts=len(accounts),
            transaction_accounts=len(transaction_fetch_succeeded_accounts),
            failed_transaction_accounts=len(transaction_fetch_failed_accounts),
            network_failed_transaction_accounts=len(transaction_fetch_network_failed_accounts),
            transactions=transaction_count,
            duration_ms=duration_ms,
            message=message,
        )
        return {
            "status": status,
            "message": message,
            "accounts": accounts,
            "transactions": raw_transactions,
            "transaction_fetch_succeeded_accounts": sorted(transaction_fetch_succeeded_accounts),
            "transaction_fetch_failed_accounts": sorted(transaction_fetch_failed_accounts),
        }

    _rbc_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="direct",
        accounts=len(accounts),
        transaction_accounts=len(transaction_fetch_succeeded_accounts),
        failed_transaction_accounts=len(transaction_fetch_failed_accounts),
        transactions=transaction_count,
        duration_ms=duration_ms,
    )
    return {
        "status": "ok",
        "accounts": accounts,
        "transactions": raw_transactions,
        "transaction_fetch_succeeded_accounts": sorted(transaction_fetch_succeeded_accounts),
    }


async def try_headless_sync(
    user_id: int,
    *,
    sync_scope: str = "full",
    mode_resolver: ModeResolver | None = None,
    account_sync_callback: AccountSyncCallback | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    replay_session = SavedArtifactReplaySession(
        PROVIDER,
        user_id,
        visible_auth_attempt_namespace=VISIBLE_AUTH_ATTEMPT_NAMESPACE,
        user_agent_resolver=_rbc_session_user_agent,
        log_event=_rbc_log_event,
    )
    await replay_session.load_async()
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact
    if not isinstance(storage_state, dict):
        return replay_session.missing_artifact_result()

    _rbc_log_event("headless sync start", user_id=user_id, engine="direct")
    try:
        payload = await _sync_rbc_with_saved_artifacts(
            storage_state,
            session_artifact_state,
            user_id=user_id,
            sync_scope=sync_scope,
            mode_resolver=mode_resolver,
            account_sync_callback=account_sync_callback,
            transaction_window_recorder=transaction_window_recorder,
        )
    except RBCAuthRequired:
        _rbc_log_event(
            "headless sync auth_required",
            user_id=user_id,
            engine="direct",
            message="saved-session direct reuse did not reach authenticated RBC state",
        )
        return scraper_auth_required()
    except Exception as e:
        msg = str(e).lower()
        if any(kw in msg for kw in ("timeout", "connect", "dns", "network", "unreachable",
               "resolve", "name resolution", "name or service not known",
               "max retries exceeded", "connectionpool", "sslerror", "connectionrefused",
               "ns_error_unknown_host", "err_name_not_resolved", "host not found")):
            raise
        _rbc_log_event(
            "headless sync failed",
            user_id=user_id,
            level="warning",
            error=_rbc_sanitize_log_text(str(e), limit=160),
        )
        return scraper_auth_required()

    if isinstance(payload, dict) and payload.get("status") == "ok":
        await replay_session.refresh_runtime_artifacts_async(storage_state)
    return replay_session.normalize_result(payload)


def _rbc_accounts_from_account_list_payload(result: dict[str, Any]) -> list[dict]:
    accounts = []
    for category_key in RBC_ACCOUNT_CATEGORY_KEYS:
        category = result.get(category_key) or {}
        for acct in category.get("accounts") or []:
            product = acct.get("product") or {}
            account_type = _rbc_account_type(
                product.get("productIdentifier") or "",
                product.get("productTypeName") or "",
            )
            is_liability = _rbc_is_liability(account_type)
            balance = float(acct.get("currentBalance") or 0)
            currency = (acct.get("accountCurrency") or {}).get("currencyCode") or "CAD"
            name = acct.get("nickName") or product.get("productName") or acct.get("accountNumber") or "Account"
            account_id = acct.get("accountId") or ""
            encrypted_id = acct.get("encryptedAccountNumber") or ""
            link_params = _rbc_account_link_params(acct)
            accounts.append({
                "name": name,
                "account_type": account_type,
                "is_liability": is_liability,
                "balance": balance,
                "currency": currency,
                "external_id": f"rbc:{account_id}" if account_id else None,
                "encrypted_id": encrypted_id,
                "account_number": acct.get("accountNumber") or "",
                "category": category_key,
                "link_params": link_params,
            })
    return accounts


def _deduplicate_rbc_transactions(txns: list[dict]) -> list[dict]:
    """RBC returns both legs of internal transactions (debit + credit).
    Keep only the DEBIT leg to avoid double-counting."""
    # Build a set of (date, abs_amount, description) for DEBIT entries
    debit_fingerprints: set[tuple] = set()
    for t in txns:
        if (t.get("creditDebitIndicator") or "").upper() == "DEBIT":
            desc = t.get("description")
            if isinstance(desc, list):
                desc = desc[0] if desc else ""
            fp = (
                t.get("bookingDate") or t.get("postedDate") or "",
                round(float(t.get("amount") or 0), 2),
                (desc or "").strip().lower(),
            )
            debit_fingerprints.add(fp)

    result = []
    for t in txns:
        indicator = (t.get("creditDebitIndicator") or "").upper()
        desc = t.get("description")
        if isinstance(desc, list):
            desc = desc[0] if desc else ""
        fp = (
            t.get("bookingDate") or t.get("postedDate") or "",
            round(float(t.get("amount") or 0), 2),
            (desc or "").strip().lower(),
        )
        # Drop CREDIT entries that have a matching DEBIT entry
        if indicator == "CREDIT" and fp in debit_fingerprints:
            continue
        result.append(t)
    return result
