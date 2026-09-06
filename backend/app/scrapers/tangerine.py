from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    parse_json_body,
    refresh_session_artifact,
    scraper_session_user_agent,
)
from app.scrapers.results import (
    scraper_auth_required,
    scraper_error,
    scraper_network_error,
)
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    sanitize_scraper_log_url,
    scraper_debug_enabled,
)
from app.services.sync_utils import is_network_error

PROVIDER = "tangerine"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.tangerine")

TANGERINE_ACCOUNTS_SOURCE_URL = "https://secure.tangerine.ca/web/rest/pfm/v1/accounts"
TANGERINE_ACCOUNT_DETAIL_URL_TEMPLATE = "https://secure.tangerine.ca/web/rest/v1/accounts/{account_id}"
TANGERINE_TRANSACTIONS_URL = "https://secure.tangerine.ca/web/rest/pfm/v1/transactions"
TANGERINE_BACKFILL_CHUNK_DAYS = 365
TANGERINE_EMPTY_HISTORY_STOP_CHUNKS = 2
TANGERINE_MAX_HISTORY_DAYS = 3650
TANGERINE_DIRECT_TIMEOUT_SECONDS = 30

ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, dict[str, Any]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


class TangerineAuthRequired(PermissionError):
    pass


class TangerineNetworkError(RuntimeError):
    pass


def _tangerine_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_TANGERINE_DEBUG",))


def _tangerine_log_event(
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
        debug_enabled=_tangerine_debug_logs_enabled(),
        **fields,
    )


def _tangerine_sanitize_log_text(text: str | None, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit)


def _tangerine_sanitize_url_for_log(url: str | None) -> str:
    return sanitize_scraper_log_url(url)


def _parse_tangerine_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if normalized.startswith(")]}',"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    return parse_json_body(normalized)


def _tangerine_payload_status_code(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for value in (
        payload.get("http_code"),
        (payload.get("response_status") or {}).get("http_code")
        if isinstance(payload.get("response_status"), dict)
        else None,
    ):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _tangerine_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _tangerine_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _tangerine_session_token(session_artifact: dict[str, Any] | None) -> str:
    if not isinstance(session_artifact, dict):
        return ""
    return str(
        session_artifact.get("transaction_token")
        or session_artifact.get("x_transaction_token")
        or ""
    ).strip()


def _raw_tangerine_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    if text.startswith("suffix:"):
        return None
    text = unquote(text)
    return text or None


def _looks_like_tangerine_provider_account_id(value: Any) -> bool:
    text = _raw_tangerine_account_id(value)
    if not text or re.fullmatch(r"\d{4}", text):
        return False
    return text.lower() not in {"account", "accounts", "product", "products", "summary", "details", "overview"}


def _tangerine_stable_account_external_id(provider_account_id: str) -> str:
    return f"account:{provider_account_id}"


def _set_tangerine_provider_account_id(acct: dict[str, Any], provider_account_id: Any) -> None:
    raw_id = _raw_tangerine_account_id(provider_account_id)
    if not _looks_like_tangerine_provider_account_id(raw_id):
        return

    current_external_id = acct.get("external_id")
    if current_external_id and str(current_external_id).startswith("suffix:"):
        acct.setdefault("alternate_external_id", current_external_id)

    acct["api_account_id"] = raw_id
    acct["provider_account_id"] = raw_id
    acct["external_id"] = _tangerine_stable_account_external_id(raw_id)


def _last4_from_text(text: str) -> str | None:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return None
    for pattern in (
        r"\*{2,}(\d{4})",
        r"[xX]{2,}(\d{4})",
        r"(?:last\s*4|lastFour|lastFourDigits|accountNumber|displayNumber|maskedNumber)[^0-9]{0,12}(\d{4})",
    ):
        match = re.search(pattern, normalized)
        if match:
            return match.group(1)

    digit_groups = re.findall(r"\d+", normalized)
    if digit_groups:
        last_group = digit_groups[-1]
        if len(last_group) >= 4:
            return last_group[-4:]
    return None


def _tangerine_pfm_account_text(record: dict[str, Any]) -> str:
    return " ".join(
        str(record.get(key) or "")
        for key in ("display_name", "nickname", "description", "type", "product_code")
    )


def _tangerine_pfm_account_record(record: dict[str, Any]) -> dict[str, Any] | None:
    number = _raw_tangerine_account_id(record.get("number"))
    if not _looks_like_tangerine_provider_account_id(number):
        return None

    display_name = str(record.get("display_name") or "")
    text = _tangerine_pfm_account_text(record)
    last4 = _last4_from_text(display_name) or _last4_from_text(str(record.get("nickname") or "")) or _last4_from_text(text)
    if not last4:
        return None

    return {
        "identifier": number,
        "last4": last4,
        "text": text.lower(),
        "display_name": record.get("display_name"),
        "nickname": record.get("nickname"),
        "description": record.get("description"),
        "type": record.get("type"),
        "product_code": record.get("product_code"),
    }


def _tangerine_money_value(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        negative = text.startswith("(") and text.endswith(")")
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
        if not match:
            return None
        try:
            amount = float(match.group(0).replace(",", ""))
        except ValueError:
            return None
        return -abs(amount) if negative else amount
    if isinstance(value, dict):
        for key in (
            "amount",
            "value",
            "balance",
            "current",
            "available",
            "book",
            "ledger",
            "display_value",
            "displayValue",
        ):
            parsed = _tangerine_money_value(value.get(key))
            if parsed is not None:
                return parsed
    return None


def _tangerine_balance_from_record(record: dict[str, Any]) -> float | None:
    preferred_paths = (
        ("balance",),
        ("current_balance",),
        ("currentBalance",),
        ("account_balance",),
        ("accountBalance",),
        ("posted_balance",),
        ("postedBalance",),
        ("ledger_balance",),
        ("ledgerBalance",),
        ("book_balance",),
        ("bookBalance",),
        ("balances", "current"),
        ("balances", "current_balance"),
        ("balances", "currentBalance"),
        ("balances", "balance"),
        ("balances", "posted"),
        ("balances", "book"),
        ("balances", "ledger"),
    )
    for path in preferred_paths:
        current = record
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current.get(key)
        parsed = _tangerine_money_value(current)
        if parsed is not None:
            return parsed

    stack = [(str(key), value) for key, value in record.items()]
    while stack:
        key_path, value = stack.pop()
        key_lower = key_path.lower()
        if isinstance(value, dict):
            stack.extend((f"{key_path}.{key}", child) for key, child in value.items())
            continue
        if "balance" not in key_lower:
            continue
        if any(token in key_lower for token in ("limit", "minimum", "payment")):
            continue
        parsed = _tangerine_money_value(value)
        if parsed is not None:
            return parsed
    return None


def _clean_tangerine_account_label(value: Any) -> str:
    label = " ".join(str(value or "").split())
    label = re.sub(r"\s+\*{2,}\d{4}\b", "", label)
    label = re.sub(r"\s+[xX]{2,}\d{4}\b", "", label)
    label = re.sub(r"®\*?", "", label).strip()
    return label


def _tangerine_account_from_source_record(record: dict[str, Any]) -> dict[str, Any] | None:
    parsed = _tangerine_pfm_account_record(record)
    if not parsed:
        return None

    balance = _tangerine_balance_from_record(record)
    if balance is None:
        return None

    description = _clean_tangerine_account_label(
        record.get("description") or record.get("type") or record.get("product_code") or ""
    )
    name = _clean_tangerine_account_label(
        record.get("nickname") or record.get("display_name") or description or "Tangerine Account"
    )
    account = {
        "name": name or description or "Tangerine Account",
        "balance": balance,
        "description": description or name or "Tangerine Account",
        "display_id": f"***{parsed['last4']}",
    }
    account["alternate_external_id"] = f"suffix:{parsed['last4']}"
    _set_tangerine_provider_account_id(account, parsed["identifier"])
    return account


def _tangerine_account_identity_values(raw: dict[str, Any]) -> list[str]:
    values: list[str] = []

    def visit(key_path: str, value: Any) -> None:
        key_lower = key_path.lower()
        is_account_identity = (
            ("account" in key_lower or "product" in key_lower)
            and "transaction" not in key_lower
            and any(token in key_lower for token in ("id", "identifier", "number", "hash"))
        )
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(f"{key_path}.{child_key}", child_value)
        elif isinstance(value, list):
            for item in value:
                visit(key_path, item)
        elif value is not None and is_account_identity:
            values.append(str(value))

    for key, value in raw.items():
        visit(str(key), value)
    return values


def _tangerine_provider_account_id_from_transactions(raw_txns: list[dict[str, Any]]) -> str | None:
    for raw in raw_txns:
        for value in _tangerine_account_identity_values(raw):
            text = _raw_tangerine_account_id(value)
            if _looks_like_tangerine_provider_account_id(text):
                return text
    return None


def _account_last4(acct: dict[str, Any]) -> str | None:
    for value in (
        acct.get("display_id"),
        acct.get("alternate_external_id"),
        acct.get("external_id"),
    ):
        if value:
            match = re.search(r"(\d{4})$", str(value))
            if match:
                return match.group(1)
    return None


def _is_tangerine_credit_card(acct: dict[str, Any]) -> bool:
    lower = f"{acct.get('name') or ''} {acct.get('description') or ''} {acct.get('account_type') or ''}".lower()
    return any(token in lower for token in ("credit card", "mastercard", "visa"))


def _tangerine_transaction_urls(
    acct: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
) -> list[str]:
    account_identifier = _raw_tangerine_account_id(
        acct.get("provider_account_id")
        or acct.get("api_account_id")
        or acct.get("external_id")
        or acct.get("account_id")
    )
    if not _looks_like_tangerine_provider_account_id(account_identifier):
        return []

    params = {
        "accountIdentifiers": account_identifier,
        "periodFrom": start_date,
        "periodTo": end_date,
    }
    if _is_tangerine_credit_card(acct):
        params["hideAuthorizedStatus"] = "true"
    return [f"{TANGERINE_TRANSACTIONS_URL}?{urlencode(params)}"]


def _tangerine_transaction_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    candidates = [
        payload.get("transactions"),
        payload.get("items"),
        payload.get("results"),
        payload.get("data"),
        (payload.get("_embedded") or {}).get("transactions") if isinstance(payload.get("_embedded"), dict) else None,
        (payload.get("embedded") or {}).get("transactions") if isinstance(payload.get("embedded"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _tangerine_transaction_items(candidate)
            if nested:
                return nested
    return []


def _tangerine_next_href(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    links = payload.get("links") or payload.get("_links") or {}
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue
            if str(link.get("rel") or "").lower() != "next":
                continue
            href = link.get("href")
            return str(href).strip() if href else None
        return None
    if isinstance(links, dict):
        next_link = links.get("next")
        if isinstance(next_link, str):
            return next_link
        if isinstance(next_link, dict):
            href = next_link.get("href")
            return str(href).strip() if href else None
    return None


def _tangerine_next_url(current_url: str, next_href: str | None) -> str | None:
    if not next_href:
        return None
    href = str(next_href).strip()
    if not href:
        return None
    if href.startswith("/web/rest/"):
        return f"https://secure.tangerine.ca{href}"
    if href.startswith("/pfm/"):
        return f"https://secure.tangerine.ca/web/rest{href}"
    if href.startswith("pfm/"):
        return f"https://secure.tangerine.ca/web/rest/{href}"
    return urljoin(current_url, href)


def _tangerine_query_date(url: str, key: str) -> date | None:
    values = urlparse(url).query
    for piece in values.split("&"):
        name, _, value = piece.partition("=")
        if name != key or not value:
            continue
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _tangerine_next_url_within_requested_window(first_url: str, next_url: str | None) -> bool:
    if not next_url:
        return False

    requested_period_from = _tangerine_query_date(first_url, "periodFrom")
    next_period_to = _tangerine_query_date(next_url, "periodTo")
    if requested_period_from and next_period_to and next_period_to < requested_period_from:
        return False
    return True


def _deduplicate_tangerine_raw_transactions(raw_transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for raw in raw_transactions or []:
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id")
        if raw_id is not None:
            key = f"id:{raw_id}"
        else:
            key = repr(sorted(raw.items()))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(raw)

    return deduped


def _tangerine_sync_window_entry(
    mode_per_account: dict[str, dict[str, Any]] | None,
    external_id: str | None,
    alternate_external_id: str | None = None,
) -> dict[str, Any]:
    if not mode_per_account:
        return {}
    entry = mode_per_account.get(str(external_id or ""))
    if entry is None and alternate_external_id:
        entry = mode_per_account.get(str(alternate_external_id))
    if isinstance(entry, dict):
        return entry
    return {}


def _tangerine_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    chunk_end = end_date
    while chunk_end >= start_date:
        chunk_start = max(
            start_date,
            chunk_end - timedelta(days=TANGERINE_BACKFILL_CHUNK_DAYS - 1),
        )
        windows.append((chunk_start, chunk_end))
        if chunk_start <= start_date:
            break
        chunk_end = chunk_start - timedelta(days=1)
    return windows


def _tangerine_pending_backfill_windows(
    sync_window: dict[str, Any],
    history_floor: date,
    history_end: date,
) -> tuple[list[tuple[date, date]], bool]:
    windows = pending_backfill_date_windows(sync_window)
    if windows:
        return windows, True
    return _tangerine_split_windows(history_floor, history_end), False


async def _record_tangerine_transaction_window(
    recorder: TransactionWindowRecorder | None,
    *,
    event: str,
    account: dict[str, Any],
    mode: str,
    start_date: date,
    end_date: date,
    transactions: list[dict[str, Any]] | None = None,
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
        _tangerine_log_event(
            "transaction window recorder failed",
            level="warning",
            account_external_id=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_tangerine_sanitize_log_text(str(exc), limit=140),
        )


def _tangerine_transaction_matches_account(raw: dict[str, Any], acct: dict[str, Any]) -> bool:
    last4 = _account_last4(acct)
    api_id = _raw_tangerine_account_id(
        acct.get("provider_account_id")
        or acct.get("api_account_id")
        or acct.get("external_id")
        or acct.get("account_id")
    )
    account_fragments = [str(value) for value in (api_id, last4) if value]
    if not account_fragments:
        return True

    account_values = _tangerine_account_identity_values(raw)
    if not account_values:
        return True
    return any(fragment in value for fragment in account_fragments for value in account_values)


def _tangerine_direct_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    transaction_token: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://secure.tangerine.ca",
        "Referer": "https://secure.tangerine.ca/",
        "User-Agent": _tangerine_user_agent(user_agent),
    }
    if transaction_token:
        headers["x-transaction-token"] = transaction_token
    return headers


async def _tangerine_direct_get(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    url: str,
    *,
    user_agent: str,
) -> dict[str, Any]:
    headers = _tangerine_direct_headers(
        storage_state,
        url,
        user_agent=user_agent,
        transaction_token=_tangerine_session_token(session_artifact),
    )
    async with SavedArtifactHttpClient(
        storage_state,
        session_artifact,
        user_agent=_tangerine_user_agent(user_agent),
        timeout=TANGERINE_DIRECT_TIMEOUT_SECONDS,
        seed_cookies=True,
    ) as replay:
        response = await replay.get(url, headers=headers)

    response_token = response.headers.get("x-transaction-token")
    if response_token:
        session_artifact["transaction_token"] = response_token

    return SavedArtifactHttpClient.response_payload(response, parse_json=_parse_tangerine_json_body)


def _tangerine_response_auth_signal(response: dict[str, Any]) -> str | None:
    status = int(response.get("status") or 0)
    if status in (401, 403):
        return f"http_{status}"
    payload = response.get("payload")
    status_code = _tangerine_payload_status_code(payload)
    if status_code == 401:
        return "payload_http_401"
    if isinstance(payload, dict):
        response_status = (
            payload.get("response_status")
            if isinstance(payload.get("response_status"), dict)
            else {}
        )
        status_text = " ".join(
            str(value or "")
            for value in (
                payload.get("status_text"),
                response_status.get("status_code"),
                response_status.get("status"),
                response_status.get("message"),
            )
        ).lower()
        if "no_session" in status_text:
            return "payload_no_session"
        if "unauthorized" in status_text:
            return "payload_unauthorized"
    return None


def _tangerine_response_server_error(response: dict[str, Any]) -> bool:
    return int(response.get("status") or 0) >= 500


async def _fetch_tangerine_json_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    user_id: int | None = None,
) -> Any | None:
    response = await _tangerine_direct_get(
        storage_state,
        session_artifact,
        url,
        user_agent=user_agent,
    )
    payload = response.get("payload")
    semantic_status_code = _tangerine_payload_status_code(payload)

    auth_signal = _tangerine_response_auth_signal(response)
    if auth_signal:
        _tangerine_log_event(
            "json request auth_required",
            user_id=user_id,
            level="warning",
            auth_signal=auth_signal,
            status_code=response.get("status"),
            semantic_status_code=semantic_status_code,
            url=_tangerine_sanitize_url_for_log(url),
            payload_type=type(payload).__name__,
            payload_keys=(sorted(payload)[:20] if isinstance(payload, dict) else []),
        )
        raise TangerineAuthRequired("Tangerine sign-in required")
    if _tangerine_response_server_error(response) or (
        semantic_status_code is not None and semantic_status_code >= 500
    ):
        raise TangerineNetworkError("Tangerine returned a server error during saved-session reuse.")
    if int(response.get("status") or 0) >= 400 or (
        semantic_status_code is not None and semantic_status_code >= 400
    ):
        _tangerine_log_event(
            "json request failed",
            user_id=user_id,
            level="warning",
            status_code=response.get("status"),
            semantic_status_code=semantic_status_code,
            url=_tangerine_sanitize_url_for_log(url),
            body=_tangerine_sanitize_log_text(response.get("text"), limit=220),
        )
        return None
    if payload is None:
        _tangerine_log_event(
            "json parse failed",
            user_id=user_id,
            level="warning",
            url=_tangerine_sanitize_url_for_log(url),
            body=_tangerine_sanitize_log_text(response.get("text"), limit=220),
        )
        return None
    if isinstance(payload, (dict, list)):
        return payload
    _tangerine_log_event(
        "json payload unsupported",
        user_id=user_id,
        level="warning",
        url=_tangerine_sanitize_url_for_log(url),
        payload_type=type(payload).__name__,
    )
    return None


async def _fetch_tangerine_account_detail_json_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    provider_account_id: str,
    *,
    user_agent: str,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    encoded_id = quote(provider_account_id, safe="")
    payload = await _fetch_tangerine_json_direct(
        storage_state,
        session_artifact,
        TANGERINE_ACCOUNT_DETAIL_URL_TEMPLATE.format(account_id=encoded_id),
        user_agent=user_agent,
        user_id=user_id,
    )
    return payload if isinstance(payload, dict) else None


async def _tangerine_accounts_from_source_payload_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    payload: dict[str, Any],
    *,
    user_agent: str,
    user_id: int | None = None,
) -> list[dict[str, Any]] | None:
    records = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return None

    accounts: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        account = _tangerine_account_from_source_record(record)
        if account is None:
            parsed = _tangerine_pfm_account_record(record)
            if not parsed:
                continue
            detail_payload = await _fetch_tangerine_account_detail_json_direct(
                storage_state,
                session_artifact,
                parsed["identifier"],
                user_agent=user_agent,
                user_id=user_id,
            )
            merged = {**record, **detail_payload} if isinstance(detail_payload, dict) else record
            account = _tangerine_account_from_source_record(merged)
        if account is not None:
            accounts.append(account)

    if not accounts:
        _tangerine_log_event(
            "accounts api payload unsupported",
            user_id=user_id,
            level="warning",
            account_records=len(records),
        )
        return None
    return accounts


async def _fetch_tangerine_accounts_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    *,
    user_agent: str,
    user_id: int | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    payload = await _fetch_tangerine_json_direct(
        storage_state,
        session_artifact,
        TANGERINE_ACCOUNTS_SOURCE_URL,
        user_agent=user_agent,
        user_id=user_id,
    )
    if not isinstance(payload, dict):
        return None, None
    accounts = await _tangerine_accounts_from_source_payload_direct(
        storage_state,
        session_artifact,
        payload,
        user_agent=user_agent,
        user_id=user_id,
    )
    return accounts, payload


async def _fetch_tangerine_paginated_transactions_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    first_url: str,
    *,
    user_agent: str,
    user_id: int | None = None,
) -> list[dict[str, Any]] | None:
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    url: str | None = first_url

    while url:
        if url in seen_urls:
            break
        seen_urls.add(url)

        payload = await _fetch_tangerine_json_direct(
            storage_state,
            session_artifact,
            url,
            user_agent=user_agent,
            user_id=user_id,
        )
        if payload is None:
            return None
        items = _tangerine_transaction_items(payload)
        collected.extend(items)

        next_href = _tangerine_next_href(payload)
        next_url = _tangerine_next_url(url, next_href)
        url = next_url if _tangerine_next_url_within_requested_window(first_url, next_url) else None

    return collected


async def _fetch_tangerine_transactions_for_window_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    acct: dict[str, Any],
    *,
    user_agent: str,
    user_id: int | None = None,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]] | None:
    start_date_str = start_date.isoformat()
    end_date_str = end_date.isoformat()
    urls = _tangerine_transaction_urls(
        acct,
        start_date=start_date_str,
        end_date=end_date_str,
    )
    if not urls:
        return []

    collected = None
    for url in urls:
        try:
            collected = await _fetch_tangerine_paginated_transactions_direct(
                storage_state,
                session_artifact,
                url,
                user_agent=user_agent,
                user_id=user_id,
            )
        except (TangerineAuthRequired, TangerineNetworkError):
            raise
        except Exception as exc:
            _tangerine_log_event(
                "transactions fetch failed",
                user_id=user_id,
                level="warning",
                account_external_id=acct.get("external_id"),
                period_from=start_date_str,
                period_to=end_date_str,
                exception_type=type(exc).__name__,
                message=_tangerine_sanitize_log_text(str(exc)),
            )
            collected = None
        if collected is not None:
            if not acct.get("provider_account_id"):
                provider_account_id = _tangerine_provider_account_id_from_transactions(collected)
                if provider_account_id:
                    _set_tangerine_provider_account_id(acct, provider_account_id)
            _tangerine_log_event(
                "transactions fetched",
                user_id=user_id,
                debug=True,
                account_external_id=acct.get("external_id"),
                period_from=start_date_str,
                period_to=end_date_str,
                count=len(collected),
            )
            break

    return collected


async def fetch_transactions_direct(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    accounts: list[dict[str, Any]],
    *,
    user_agent: str,
    user_id: int | None = None,
    mode_per_account: dict[str, dict[str, Any]] | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    results: dict[str, list[dict[str, Any]]] = {}
    succeeded_accounts: set[str] = set()

    for acct in accounts:
        external_id = acct.get("external_id")
        if not external_id:
            continue

        alternate_external_id = acct.get("alternate_external_id")
        sync_window = _tangerine_sync_window_entry(
            mode_per_account,
            external_id,
            alternate_external_id,
        )
        mode = sync_window.get("mode") or "backfill"
        account_key = external_id
        if mode != "backfill" and sync_window_date(sync_window, "start_date") is None:
            _tangerine_log_event(
                "transactions missing incremental window",
                user_id=user_id,
                level="warning",
                account_external_id=external_id,
                message="falling back to backfill because incremental sync window metadata is incomplete",
            )
            mode = "backfill"

        if mode == "backfill":
            history_end = sync_window_date(sync_window, "end_date") or date.today()
            history_floor = (
                sync_window_date(sync_window, "backfill_start_date")
                or (history_end - timedelta(days=TANGERINE_MAX_HISTORY_DAYS))
            )
            windows, planned_windows = _tangerine_pending_backfill_windows(
                sync_window,
                history_floor,
                history_end,
            )
            found_history = False
            consecutive_empty_chunks = 0
            collected_all: list[dict[str, Any]] = []
            account_fetch_succeeded = True

            for chunk_start, chunk_end in windows:
                await _record_tangerine_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=acct,
                    mode=mode,
                    start_date=chunk_start,
                    end_date=chunk_end,
                )
                try:
                    collected = await _fetch_tangerine_transactions_for_window_direct(
                        storage_state,
                        session_artifact,
                        acct,
                        user_agent=user_agent,
                        user_id=user_id,
                        start_date=chunk_start,
                        end_date=chunk_end,
                    )
                except Exception as exc:
                    await _record_tangerine_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=acct,
                        mode=mode,
                        start_date=chunk_start,
                        end_date=chunk_end,
                        error=str(exc),
                    )
                    raise
                if collected is None:
                    await _record_tangerine_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=acct,
                        mode=mode,
                        start_date=chunk_start,
                        end_date=chunk_end,
                        error="Tangerine transaction window failed",
                    )
                    account_fetch_succeeded = False
                    break

                filtered = [
                    raw for raw in collected
                    if _tangerine_transaction_matches_account(raw, acct)
                ]
                await _record_tangerine_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=acct,
                    mode=mode,
                    start_date=chunk_start,
                    end_date=chunk_end,
                    transactions=filtered,
                )
                if filtered:
                    found_history = True
                    consecutive_empty_chunks = 0
                    collected_all.extend(filtered)
                elif found_history and not planned_windows:
                    consecutive_empty_chunks += 1
                    if consecutive_empty_chunks >= TANGERINE_EMPTY_HISTORY_STOP_CHUNKS:
                        break

            if account_fetch_succeeded:
                succeeded_accounts.add(account_key)
            deduped = _deduplicate_tangerine_raw_transactions(collected_all)
            if deduped:
                results[account_key] = deduped
            continue

        incremental_end = sync_window_date(sync_window, "end_date") or date.today()
        incremental_start = sync_window_date(sync_window, "start_date") or incremental_end
        if incremental_start > incremental_end:
            incremental_start = incremental_end
        await _record_tangerine_transaction_window(
            transaction_window_recorder,
            event="started",
            account=acct,
            mode=mode,
            start_date=incremental_start,
            end_date=incremental_end,
        )
        try:
            collected = await _fetch_tangerine_transactions_for_window_direct(
                storage_state,
                session_artifact,
                acct,
                user_agent=user_agent,
                user_id=user_id,
                start_date=incremental_start,
                end_date=incremental_end,
            )
        except Exception as exc:
            await _record_tangerine_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=acct,
                mode=mode,
                start_date=incremental_start,
                end_date=incremental_end,
                error=str(exc),
            )
            raise
        if collected is not None:
            succeeded_accounts.add(account_key)
        else:
            await _record_tangerine_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=acct,
                mode=mode,
                start_date=incremental_start,
                end_date=incremental_end,
                error="Tangerine transaction window failed",
            )
        if collected:
            filtered = [
                raw for raw in collected
                if _tangerine_transaction_matches_account(raw, acct)
            ]
            if filtered:
                results[account_key] = _deduplicate_tangerine_raw_transactions(filtered)
            await _record_tangerine_transaction_window(
                transaction_window_recorder,
                event="completed",
                account=acct,
                mode=mode,
                start_date=incremental_start,
                end_date=incremental_end,
                transactions=filtered,
            )
        elif collected is not None:
            await _record_tangerine_transaction_window(
                transaction_window_recorder,
                event="completed",
                account=acct,
                mode=mode,
                start_date=incremental_start,
                end_date=incremental_end,
                transactions=[],
            )

    return results, succeeded_accounts


async def _sync_tangerine_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    *,
    user_id: int | None = None,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> dict[str, Any] | None:
    started_at = datetime.now(timezone.utc)
    user_agent = _tangerine_session_user_agent(session_artifact)

    _tangerine_log_event(
        "saved-session sync start",
        user_id=user_id,
        engine="direct",
        has_transaction_token=bool(_tangerine_session_token(session_artifact)),
        debug=True,
    )

    accounts, accounts_payload = await _fetch_tangerine_accounts_direct(
        storage_state,
        session_artifact,
        user_agent=user_agent,
        user_id=user_id,
    )
    if not accounts:
        _tangerine_log_event(
            "saved-session sync accounts unavailable",
            user_id=user_id,
            level="warning",
            engine="direct",
        )
        return {"status": "error", "message": "No Tangerine accounts returned"}

    extra = {"accounts_payload": accounts_payload} if isinstance(accounts_payload, dict) else None
    session_artifact.update(
        refresh_session_artifact(
            session_artifact,
            user_agent=user_agent,
            captured_at=started_at.isoformat(),
            extra=extra,
        )
    )

    raw_transactions: dict[str, list[dict[str, Any]]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()
    if sync_scope != "accounts":
        mode_map = await mode_resolver(accounts) if mode_resolver else {}
        raw_transactions, transaction_fetch_succeeded_accounts = await fetch_transactions_direct(
            storage_state,
            session_artifact,
            accounts,
            user_agent=user_agent,
            user_id=user_id,
            mode_per_account=mode_map,
            transaction_window_recorder=transaction_window_recorder,
        )

    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    _tangerine_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="direct",
        accounts=len(accounts),
        transaction_accounts=len(transaction_fetch_succeeded_accounts),
        transactions=sum(len(items or []) for items in raw_transactions.values()),
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
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    _tangerine_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_tangerine_session_user_agent,
        log_event=_tangerine_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact

    if not storage_state:
        return replay_session.missing_artifact_result()

    try:
        payload = await _sync_tangerine_with_saved_artifacts(
            storage_state,
            session_artifact_state,
            user_id=user_id,
            mode_resolver=mode_resolver,
            sync_scope=sync_scope,
            transaction_window_recorder=transaction_window_recorder,
        )
    except TangerineAuthRequired:
        _tangerine_log_event(
            "headless sync auth_required",
            user_id=user_id,
            engine="direct",
            message="saved-session direct reuse did not reach authenticated Tangerine state",
        )
        return scraper_auth_required()
    except TangerineNetworkError as exc:
        _tangerine_log_event(
            "headless sync network_error",
            user_id=user_id,
            engine="direct",
            level="warning",
            message=_tangerine_sanitize_log_text(str(exc)),
        )
        return scraper_network_error()
    except Exception as exc:
        if is_network_error(exc):
            _tangerine_log_event(
                "headless sync network_error",
                user_id=user_id,
                engine="direct",
                level="warning",
                exception_type=type(exc).__name__,
                message=_tangerine_sanitize_log_text(str(exc)),
            )
            return scraper_network_error()
        _tangerine_log_event(
            "headless sync failed",
            user_id=user_id,
            engine="direct",
            level="warning",
            exception_type=type(exc).__name__,
            message=_tangerine_sanitize_log_text(str(exc)),
        )
        return scraper_error(str(exc))

    if isinstance(payload, dict) and payload.get("message") == "No Tangerine accounts returned":
        return scraper_auth_required()
    if isinstance(payload, dict) and payload.get("status") == "ok":
        await replay_session.refresh_runtime_artifacts_async(storage_state)
    return replay_session.normalize_result(payload)
