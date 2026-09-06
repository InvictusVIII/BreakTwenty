from __future__ import annotations

import logging
import re
import uuid
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.connectors.sync_context import get_connector_sync_context
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    client_cookie_value,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    parse_json_body,
    save_visible_auth_session_artifact_async,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_error
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)
from app.services.runtime_state import get_runtime_state

PROVIDER = "bmo"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.bmo")

BMO_ORIGIN = "https://www1.bmo.com"
BMO_ACCOUNTS_URL = f"{BMO_ORIGIN}/banking/digital/accounts"
BMO_INIT_ISAM_SESSION_URL = f"{BMO_ORIGIN}/banking/services/csgcb/session/initISAMSession"
BMO_ENTITLEMENTS_URL = f"{BMO_ORIGIN}/api/cdb/customer-access-entitlement/accounts/entitlements"
BMO_BANK_DETAILS_URL = f"{BMO_ORIGIN}/api/cdb/current-account/accountdetails/getBankAccountDetails"
BMO_TRANSACTION_HISTORY_FLOW_TYPES_URL = (
    f"{BMO_ORIGIN}/api/cdb/session-dialogue/cdb-endpoint-flow-control/transaction-history-flow-types"
)
BMO_POST_HANDOFF_CAPTURE_WAIT_SECONDS = 120

BMO_HOST_NAME = "BDBN-HostName"
BMO_CLIENT_IP = "127.0.0.1"
BMO_CLIENT_SESSION_ID = "session-id"
BMO_UI_SESSION_ID = "0.0.1"
BMO_BACKFILL_CHUNK_DAYS = 365
BMO_MAX_HISTORY_DAYS = 3650
BMO_HISTORY_EDGE_TOLERANCE_DAYS = 7
BMO_DIRECT_TIMEOUT_SECONDS = 30
BMO_SECRET_LOG_REDACTIONS = (
    (
        r'(["\']?(?:password|credential|token|mfaDeviceToken|mfaDevicePrint|XSRF)["\']?\s*[:=]\s*)["\'][^"\']+["\']',
        r"\1<redacted>",
        re.IGNORECASE,
    ),
)
ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, str | dict[str, Any]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


async def save_session_artifact(user_id: int, artifact) -> None:
    await save_visible_auth_session_artifact_async(user_id, PROVIDER, artifact)


def _bmo_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _bmo_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _bmo_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "")


def _bmo_request_uid() -> str:
    return f"REQ_{uuid.uuid4().hex[:16]}"


def _bmo_http_date() -> str:
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _bmo_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_BMO_DEBUG",))


def _bmo_log_event(
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
        debug_enabled=_bmo_debug_logs_enabled(),
        **fields,
    )


def _bmo_sanitize_log_text(text: str | None, *, limit: int = 220) -> str:
    return sanitize_scraper_log_text(
        text,
        limit=limit,
        redactions=BMO_SECRET_LOG_REDACTIONS,
        collapse_first=True,
    )


def _bmo_parse_json_body(body_text: str | None) -> Any:
    return parse_json_body(body_text)


def _bmo_collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(_bmo_collect_strings(nested))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for nested in value:
            values.extend(_bmo_collect_strings(nested))
        return values
    return []


def _bmo_extract_call_status(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for outer in payload.values():
        if not isinstance(outer, dict):
            continue
        header = outer.get("HdrRs")
        if isinstance(header, dict) and header.get("callStatus"):
            return str(header.get("callStatus"))
    return None


def _bmo_extract_response_message(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("responsemsg", "message", "errorMessage", "detail", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for outer in payload.values():
            if not isinstance(outer, dict):
                continue
            body = outer.get("BodyRs")
            if not isinstance(body, dict):
                continue
            for key in ("message", "responsemsg", "errorMessage", "detail", "description"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for text in _bmo_collect_strings(payload):
        normalized = text.strip()
        if normalized:
            return normalized
    return None


def _bmo_extract_mfa_token(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for outer in payload.values():
        if not isinstance(outer, dict):
            continue
        for container_key in ("HdrRs", "BodyRs", "HdrRq", "BodyRq"):
            container = outer.get(container_key)
            if not isinstance(container, dict):
                continue
            for key in ("mfaDeviceToken", "mfa_device_token"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _bmo_extract_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("categories"), list):
        return payload
    authenticate_body = ((payload.get("AuthenticateRs") or {}).get("BodyRs") or {})
    summary = authenticate_body.get("mySummary")
    if isinstance(summary, dict):
        return summary
    if isinstance(payload.get("mySummary"), dict):
        return payload["mySummary"]
    return None


def _bmo_entitlements_account_total(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    body = ((payload.get("EntitlementsRs") or {}).get("BodyRs") or {})
    if not isinstance(body, dict):
        return 0
    return sum(
        len(body.get(key) or [])
        for key in ("bankAccounts", "creditCardAccounts", "loanAccounts", "investmentAccounts")
    )


def _bmo_response_requires_auth(response: dict[str, Any]) -> bool:
    status = int(response.get("status") or 0)
    url = str(response.get("url") or "").lower()
    if status in (401, 403):
        return True
    if "/banking/digital/login" in url or "/banking/digital/error/session" in url:
        return True
    payload = response.get("payload")
    message = (_bmo_extract_response_message(payload) or "").strip().lower()
    return "session has timed out" in message or "please sign in again" in message


def _bmo_manual_entitlements_requires_reauth(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    if _bmo_response_requires_auth(response):
        return True
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return False
    call_status = (_bmo_extract_call_status(payload) or "").strip().lower()
    message = (_bmo_extract_response_message(payload) or "").strip().lower()
    return call_status == "error" and _bmo_entitlements_account_total(payload) == 0 and (
        not message or message == "error"
    )


def _bmo_response_log_summary(payload: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    call_status = _bmo_extract_call_status(payload)
    if call_status:
        fields["call_status"] = call_status
    message = _bmo_extract_response_message(payload)
    if message:
        fields["message"] = _bmo_sanitize_log_text(message, limit=120)
    return fields


def _bmo_current_path_from_url(url: str | None) -> str:
    text = str(url or "").strip()
    if not text:
        return "/banking/digital/accounts"
    parsed = urlsplit(text)
    if parsed.scheme and parsed.netloc:
        return parsed.path or "/"
    if text.startswith("/"):
        return text
    return f"/{text.lstrip('/')}"


def _bmo_service_headers(
    client: httpx.AsyncClient,
    *,
    current_path: str | None,
    user_agent: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en;q=0.6",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": BMO_ORIGIN,
        "Pragma": "no-cache",
        "User-Agent": _bmo_user_agent(user_agent),
        "X-App-Current-Path": _bmo_current_path_from_url(current_path or BMO_ACCOUNTS_URL),
        "X-App-Version": BMO_CLIENT_SESSION_ID,
        "X-ChannelType": "OLB",
        "X-Original-Request-Time": _bmo_http_date(),
        "X-Request-ID": _bmo_request_uid(),
        "X-UI-Session-ID": BMO_UI_SESSION_ID,
        "x_channeltype": "OLB",
    }
    xsrf_token = client_cookie_value(client, "XSRF-TOKEN", host="www1.bmo.com")
    if xsrf_token:
        headers["X-XSRF-TOKEN"] = xsrf_token
    return headers


def _bmo_request_header(mfa_device_token: str | None, *, user_agent: str) -> dict[str, Any]:
    return {
        "ver": "1.0",
        "channelType": "OLB",
        "appName": "OLB",
        "hostName": BMO_HOST_NAME,
        "clientDate": _bmo_now_iso(),
        "rqUID": _bmo_request_uid(),
        "clientSessionID": BMO_CLIENT_SESSION_ID,
        "userAgent": _bmo_user_agent(user_agent),
        "clientIP": BMO_CLIENT_IP,
        "mfaDeviceToken": mfa_device_token or "",
    }


def _bmo_service_request(
    request_key: str,
    body: dict[str, Any],
    *,
    mfa_device_token: str | None,
    user_agent: str,
) -> dict[str, Any]:
    return {
        request_key: {
            "HdrRq": _bmo_request_header(mfa_device_token, user_agent=user_agent),
            "BodyRq": body,
        }
    }


async def _bmo_direct_request(
    client: httpx.AsyncClient,
    url: str,
    *,
    request_key: str,
    body: dict[str, Any],
    current_path: str | None,
    referrer: str,
    mfa_device_token: str | None,
    user_agent: str,
) -> dict[str, Any]:
    headers = {
        **_bmo_service_headers(
            client,
            current_path=current_path,
            user_agent=user_agent,
        ),
        "Referer": referrer,
    }
    if mfa_device_token and urlsplit(url).path.startswith("/api/cdb/"):
        headers["x-bmo-mfa-device-token"] = mfa_device_token

    response = await client.post(
        url,
        headers=headers,
        json=_bmo_service_request(
            request_key,
            body,
            mfa_device_token=mfa_device_token,
            user_agent=user_agent,
        ),
    )
    payload = _bmo_parse_json_body(response.text)
    result = {
        "ok": response.is_success,
        "status": int(response.status_code),
        "url": str(response.url),
        "payload": payload,
    }
    token = _bmo_extract_mfa_token(payload)
    if token:
        client.cookies.set("PMData", token, domain="bmo.com", path="/")
    log_fields = _bmo_response_log_summary(payload)
    if response.status_code >= 400:
        content_type = response.headers.get("content-type")
        if content_type:
            log_fields["content_type"] = _bmo_sanitize_log_text(content_type, limit=80)
        if payload is None and response.text:
            log_fields["body_preview"] = _bmo_sanitize_log_text(response.text, limit=180)
    _bmo_log_event(
        "direct request",
        debug=True,
        endpoint=request_key,
        status=response.status_code,
        **log_fields,
    )
    return result


def _bmo_request_failure_message(response: dict[str, Any], fallback: str) -> str:
    message = _bmo_extract_response_message(response.get("payload")) or fallback
    status = int(response.get("status") or 0)
    if status:
        return f"{message} (HTTP {status})"
    return message


async def _bmo_direct_bootstrap(
    client: httpx.AsyncClient,
    *,
    mfa_device_token: str | None,
    user_agent: str,
    user_id: int | None,
) -> dict[str, Any] | None:
    init_isam_response = await _bmo_direct_request(
        client,
        BMO_INIT_ISAM_SESSION_URL,
        request_key="InitISAMSessionRq",
        body={},
        current_path=BMO_ACCOUNTS_URL,
        referrer=BMO_ACCOUNTS_URL,
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
    )
    if _bmo_response_requires_auth(init_isam_response):
        return None

    entitlements_response = await _bmo_direct_request(
        client,
        BMO_ENTITLEMENTS_URL,
        request_key="EntitlementsRq",
        body={},
        current_path=BMO_ACCOUNTS_URL,
        referrer=BMO_ACCOUNTS_URL,
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
    )
    if _bmo_manual_entitlements_requires_reauth(entitlements_response):
        _bmo_log_event(
            "saved-session sync auth_required",
            user_id=user_id,
            engine="direct",
            message="entitlements request returned the empty auth-expired response",
        )
        return None
    if _bmo_response_requires_auth(entitlements_response):
        return None
    if int(entitlements_response.get("status") or 0) >= 400:
        raise RuntimeError(_bmo_request_failure_message(entitlements_response, "BMO entitlements request failed"))

    payload = entitlements_response.get("payload")
    body = ((payload.get("EntitlementsRs") or {}).get("BodyRs") or {}) if isinstance(payload, dict) else {}
    return body if isinstance(body, dict) else {}


async def _bmo_transaction_history_preflight(
    client: httpx.AsyncClient,
    *,
    mfa_device_token: str | None,
    user_agent: str,
    user_id: int | None,
) -> bool:
    headers = {
        **_bmo_service_headers(
            client,
            current_path=BMO_ACCOUNTS_URL,
            user_agent=user_agent,
        ),
        "Referer": BMO_ACCOUNTS_URL,
    }
    if mfa_device_token:
        headers["x-bmo-mfa-device-token"] = mfa_device_token

    response = await client.get(BMO_TRANSACTION_HISTORY_FLOW_TYPES_URL, headers=headers)
    payload = _bmo_parse_json_body(response.text)
    result = {
        "ok": response.is_success,
        "status": int(response.status_code),
        "url": str(response.url),
        "payload": payload,
    }
    log_fields = _bmo_response_log_summary(payload)
    if response.status_code >= 400:
        content_type = response.headers.get("content-type")
        if content_type:
            log_fields["content_type"] = _bmo_sanitize_log_text(content_type, limit=80)
        if payload is None and response.text:
            log_fields["body_preview"] = _bmo_sanitize_log_text(response.text, limit=180)
    _bmo_log_event(
        "transaction history preflight",
        debug=True,
        user_id=user_id,
        status=response.status_code,
        **log_fields,
    )
    return not _bmo_response_requires_auth(result) and int(response.status_code) < 400


def _bmo_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _bmo_provider_account_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("provider_account_id")
    if isinstance(value, str) and value.strip():
        return value.strip()

    category = str(raw.get("category_name") or raw.get("categoryName") or "account").lower()
    digits = _bmo_digits(raw.get("accountNumber"))
    if digits:
        return f"{category}:{digits}"

    account_index = raw.get("accountIndex")
    if account_index is not None:
        return f"{category}:index:{account_index}"

    name = re.sub(
        r"[^a-z0-9]+",
        "-",
        str(raw.get("ocifAccountName") or raw.get("productName") or "").lower(),
    ).strip("-")
    if name:
        return f"{category}:{name}"
    return None


def _bmo_account_external_id(raw: dict[str, Any]) -> str | None:
    provider_account_id = _bmo_provider_account_id(raw)
    if provider_account_id:
        return f"account:{provider_account_id}"
    value = raw.get("external_id")
    return str(value).strip() if value else None


def _bmo_mask_account_number(value: Any) -> str | None:
    digits = _bmo_digits(value)
    if not digits:
        return None
    return f"***{digits[-4:]}" if len(digits) > 4 else "***"


def _bmo_summary_accounts(summary: dict[str, Any], *, fresh_summary: bool) -> list[dict[str, Any]]:
    categories = summary.get("categories") or []
    accounts: list[dict[str, Any]] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        for product in category.get("products") or []:
            if not isinstance(product, dict):
                continue
            record = dict(product)
            record["category_name"] = category.get("categoryName")
            record["group_head_title"] = category.get("groupHeadTitle")
            record["summary_is_fresh"] = fresh_summary
            record["balance_is_fresh"] = fresh_summary
            record["provider_account_id"] = _bmo_provider_account_id(record)
            record["external_id"] = _bmo_account_external_id(record)
            accounts.append(record)
    return accounts


def _bmo_accounts_from_entitlements(
    entitlements: dict[str, Any],
    *,
    fresh_summary: bool,
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    for index, item in enumerate(entitlements.get("bankAccounts") or []):
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record.setdefault("category_name", "BA")
        record.setdefault("group_head_title", "Bank Accounts")
        record.setdefault("accountType", "BANK_ACCOUNT")
        record.setdefault("productName", "Bank Account")
        record.setdefault(
            "ocifAccountName",
            f"BMO Bank Account {_bmo_mask_account_number(record.get('accountNumber')) or ''}".strip(),
        )
        record.setdefault("currency", "CAD")
        record.setdefault("accountIndex", index)
        record["summary_is_fresh"] = fresh_summary
        record["balance_is_fresh"] = False
        record["provider_account_id"] = _bmo_provider_account_id(record)
        record["external_id"] = _bmo_account_external_id(record)
        accounts.append(record)
    return accounts


def _bmo_summary_from_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    bank_products = [dict(account) for account in accounts if str(account.get("category_name") or "").upper() == "BA"]
    total = 0.0
    currency = "CAD"
    for account in bank_products:
        amount = account.get("accountBalance") or account.get("availableAmount")
        try:
            total += float(str(amount).replace(",", ""))
        except (TypeError, ValueError):
            pass
        currency = str(account.get("currency") or currency or "CAD")
    return {
        "categories": [
            {
                "categoryName": "BA",
                "groupHeadTitle": "Bank Accounts",
                "groupTotal": [{"summaryBalance": f"{total:.2f}", "currency": currency, "incompleteBalance": "N"}],
                "products": bank_products,
            }
        ]
    }


def _bmo_augment_accounts_with_entitlements(
    accounts: list[dict[str, Any]],
    entitlements: dict[str, Any],
) -> None:
    entitlement_by_digits = {
        _bmo_digits(item.get("accountNumber")): item
        for item in (entitlements.get("bankAccounts") or [])
        if isinstance(item, dict) and _bmo_digits(item.get("accountNumber"))
    }
    for account in accounts:
        digits = _bmo_digits(account.get("accountNumber"))
        entitlement = entitlement_by_digits.get(digits)
        if entitlement:
            account["entitlements"] = entitlement
            for key, value in entitlement.items():
                account.setdefault(key, value)

def _bmo_apply_sync_window(account: dict[str, Any], sync_windows: dict[str, Any]) -> None:
    external_id = str(account.get("external_id") or "").strip()
    raw_window = sync_windows.get(external_id) if external_id else None
    if isinstance(raw_window, dict):
        account["sync_window"] = dict(raw_window)
    elif isinstance(raw_window, str):
        account["sync_window"] = {"mode": raw_window}
    else:
        account["sync_window"] = {"mode": "backfill"}


def _bmo_account_window(account: dict[str, Any]) -> tuple[str, date | None, date]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    mode = str(sync_window.get("mode") or "backfill")
    end_date = sync_window_date(sync_window, "end_date") or date.today()
    start_date = sync_window_date(sync_window, "start_date")
    if mode != "backfill" and start_date is None:
        start_date = end_date
    if start_date and start_date > end_date:
        start_date = end_date
    return mode, start_date, end_date


def _bmo_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(start_date, current_end - timedelta(days=BMO_BACKFILL_CHUNK_DAYS - 1))
        windows.append((current_start, current_end))
        if current_start <= start_date:
            break
        current_end = current_start - timedelta(days=1)
    return windows


def _bmo_pending_backfill_windows(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> list[tuple[date, date]]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    windows = pending_backfill_date_windows(sync_window)
    windows = windows or _bmo_split_windows(history_start, history_end)
    return sorted(windows, key=lambda item: (item[1], item[0]), reverse=True)


def _bmo_value_to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _bmo_transaction_date(row: dict[str, Any]) -> date | None:
    for key in (
        "txnDate",
        "transactionDate",
        "postedDate",
        "transactionDt",
        "postedDt",
        "effectiveDate",
        "bookingDate",
        "date",
    ):
        parsed = _bmo_value_to_date(row.get(key))
        if parsed:
            return parsed
    return None


def _bmo_browser_history_windows_for_account(
    browser_history: dict[str, Any] | None,
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(browser_history, dict):
        return []
    raw_index = account.get("accountIndex")
    if raw_index is None:
        return []
    account_index = str(raw_index)
    windows: list[dict[str, Any]] = []
    for window in browser_history.get("windows") or []:
        if not isinstance(window, dict):
            continue
        raw_window_index = window.get("account_index")
        window_index = str(raw_window_index).strip() if raw_window_index is not None else ""
        if window_index != account_index:
            continue
        start_date = _bmo_value_to_date(window.get("start_date"))
        end_date = _bmo_value_to_date(window.get("end_date"))
        if start_date is None or end_date is None:
            continue
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        normalized = dict(window)
        normalized["_start_date"] = start_date
        normalized["_end_date"] = end_date
        windows.append(normalized)
    return sorted(windows, key=lambda item: (item["_start_date"], item["_end_date"]))


def _bmo_browser_windows_cover_range(
    windows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> bool:
    coverage_cursor = start_date
    for window in windows:
        if bool(window.get("more_txns")):
            continue
        window_start = window.get("_start_date")
        window_end = window.get("_end_date")
        if not isinstance(window_start, date) or not isinstance(window_end, date):
            continue
        if window_end < coverage_cursor:
            continue
        if window_start > coverage_cursor:
            return False
        coverage_cursor = window_end + timedelta(days=1)
        if coverage_cursor > end_date:
            return True
    return coverage_cursor > end_date


def _bmo_browser_missing_range_is_history_edge(
    windows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> bool:
    dated_windows = [
        window
        for window in windows
        if isinstance(window.get("_start_date"), date)
        and isinstance(window.get("_end_date"), date)
        and not bool(window.get("more_txns"))
    ]
    if not dated_windows:
        return False
    earliest_start = min(window["_start_date"] for window in dated_windows)
    if end_date >= earliest_start:
        return False
    gap_days = (earliest_start - start_date).days
    return 0 < gap_days <= BMO_HISTORY_EDGE_TOLERANCE_DAYS


def _bmo_browser_window_transactions(
    windows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for window in windows:
        window_start = window.get("_start_date")
        window_end = window.get("_end_date")
        if not isinstance(window_start, date) or not isinstance(window_end, date):
            continue
        if window_end < start_date or window_start > end_date:
            continue
        for row in window.get("transactions") or []:
            if not isinstance(row, dict):
                continue
            txn_date = _bmo_transaction_date(row)
            if txn_date is None or txn_date < start_date or txn_date > end_date:
                continue
            key = str(
                row.get("transactionId")
                or row.get("txnId")
                or row.get("id")
                or row.get("referenceNumber")
                or row.get("referenceId")
                or row.get("sequenceNumber")
                or row.get("cimgRef")
                or f"{txn_date.isoformat()}:{row.get('txnAmount')}:{row.get('descr')}"
            )
            if key in seen:
                continue
            seen.add(key)
            collected.append(dict(row))
    return collected


def _bmo_latest_browser_account_details(
    browser_history: dict[str, Any] | None,
    account: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(browser_history, dict):
        return None

    windows: list[dict[str, Any]] = []
    for key in ("windows", "observed_windows"):
        raw_windows = browser_history.get(key)
        if not isinstance(raw_windows, list):
            continue
        windows.extend(_bmo_browser_history_windows_for_account({"windows": raw_windows}, account))

    for window in sorted(windows, key=lambda item: item["_end_date"], reverse=True):
        details = window.get("details")
        if isinstance(details, dict) and details:
            return dict(details)
    return None


async def _bmo_fetch_bank_account_history_from_browser_capture(
    browser_history: dict[str, Any],
    account: dict[str, Any],
    *,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, bool]:
    windows = _bmo_browser_history_windows_for_account(browser_history, account)
    if not windows:
        return None, None, False

    mode, planned_start, planned_end = _bmo_account_window(account)
    latest_details = next(
        (window.get("details") for window in sorted(windows, key=lambda item: item["_end_date"], reverse=True) if isinstance(window.get("details"), dict)),
        None,
    )
    collected: list[dict[str, Any]] = []

    if mode != "backfill":
        requested_windows = [(planned_start or planned_end, planned_end)]
    else:
        history_floor = planned_end - timedelta(days=BMO_MAX_HISTORY_DAYS)
        requested_windows = _bmo_pending_backfill_windows(account, history_floor, planned_end)

    for current_start, current_end in requested_windows:
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
        )
        if not _bmo_browser_windows_cover_range(windows, current_start, current_end):
            if _bmo_browser_missing_range_is_history_edge(windows, current_start, current_end):
                await _record_bmo_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=account,
                    mode=mode,
                    start_date=current_start,
                    end_date=current_end,
                    transactions=[],
                )
                continue
            message = "BMO browser transaction capture did not include this date range"
            await _record_bmo_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error=message,
            )
            raise RuntimeError(message)
        transactions = _bmo_browser_window_transactions(windows, current_start, current_end)
        for row in transactions:
            row["_account_external_id"] = account.get("external_id")
            row["_account_currency"] = account.get("currency") or (latest_details or {}).get("currency") or "CAD"
        collected.extend(transactions)
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="completed",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
            transactions=transactions,
        )

    return latest_details if isinstance(latest_details, dict) else None, collected, True


async def _record_bmo_transaction_window(
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
        _bmo_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_bmo_sanitize_log_text(str(exc), limit=140),
        )


async def _bmo_fetch_bank_account_window_direct(
    client: httpx.AsyncClient,
    *,
    account: dict[str, Any],
    start_date: date,
    end_date: date,
    mfa_device_token: str | None,
    user_agent: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    raw_account_index = account.get("accountIndex")
    account_index = "" if raw_account_index is None else str(raw_account_index)
    detail_path = f"/banking/digital/account-details/ba/{account_index}"
    response = await _bmo_direct_request(
        client,
        BMO_BANK_DETAILS_URL,
        request_key="GetBankAccountDetailsRq",
        body={
            "accountIndex": account_index,
            "limitNoTxns": "1500",
            "filterFromDate": start_date.isoformat(),
            "filterToDate": end_date.isoformat(),
        },
        current_path=detail_path,
        referrer=f"{BMO_ORIGIN}{detail_path}",
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
    )
    if _bmo_response_requires_auth(response):
        raise PermissionError("BMO auth required")

    payload = response.get("payload")
    call_status = _bmo_extract_call_status(payload)
    if int(response.get("status") or 0) >= 400 or (call_status and call_status.lower() != "success"):
        _bmo_log_event(
            "bank details rejected",
            level="warning",
            account=_bmo_mask_account_number(account.get("accountNumber")),
            account_index=account_index or None,
            from_date=start_date.isoformat(),
            to_date=end_date.isoformat(),
            **_bmo_response_log_summary(payload),
        )
        raise RuntimeError(_bmo_extract_response_message(payload) or "BMO bank account details request failed")

    body = ((payload.get("GetBankAccountDetailsRs") or {}).get("BodyRs") or {}) if isinstance(payload, dict) else {}
    details = body.get("bankAccountDetails")
    transactions = [item for item in (body.get("bankAccountTransactions") or []) if isinstance(item, dict)]
    more_txns = str((details or {}).get("moreTxns") or "").upper() == "Y"
    return details if isinstance(details, dict) else None, transactions, more_txns


async def _bmo_fetch_bank_account_resolved_window_direct(
    client: httpx.AsyncClient,
    *,
    account: dict[str, Any],
    start_date: date,
    end_date: date,
    mfa_device_token: str | None,
    user_agent: str,
    user_id: int | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    details, transactions, more_txns = await _bmo_fetch_bank_account_window_direct(
        client,
        account=account,
        start_date=start_date,
        end_date=end_date,
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
    )
    if not more_txns:
        return details, transactions

    if start_date >= end_date:
        _bmo_log_event(
            "transaction window truncated",
            user_id=user_id,
            account=_bmo_mask_account_number(account.get("accountNumber")),
            account_index=account.get("accountIndex"),
            from_date=start_date.isoformat(),
            to_date=end_date.isoformat(),
            transactions=len(transactions),
        )
        return details, transactions

    split_at = start_date + timedelta(days=(end_date - start_date).days // 2)
    newer_start = split_at + timedelta(days=1)
    _bmo_log_event(
        "transaction window split",
        user_id=user_id,
        account=_bmo_mask_account_number(account.get("accountNumber")),
        account_index=account.get("accountIndex"),
        from_date=start_date.isoformat(),
        to_date=end_date.isoformat(),
        split_date=split_at.isoformat(),
        transactions=len(transactions),
    )
    older_details, older_transactions = await _bmo_fetch_bank_account_resolved_window_direct(
        client,
        account=account,
        start_date=start_date,
        end_date=split_at,
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
        user_id=user_id,
    )
    newer_details, newer_transactions = await _bmo_fetch_bank_account_resolved_window_direct(
        client,
        account=account,
        start_date=newer_start,
        end_date=end_date,
        mfa_device_token=mfa_device_token,
        user_agent=user_agent,
        user_id=user_id,
    )
    return details or newer_details or older_details, [*newer_transactions, *older_transactions]


async def _bmo_fetch_bank_account_history_direct(
    client: httpx.AsyncClient,
    account: dict[str, Any],
    *,
    mfa_device_token: str | None,
    user_agent: str,
    user_id: int | None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, bool]:
    if account.get("accountIndex") is None:
        return None, None, False

    mode, planned_start, planned_end = _bmo_account_window(account)
    if mode != "backfill":
        start_date = planned_start or planned_end
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=start_date,
            end_date=planned_end,
        )
        try:
            details, transactions = await _bmo_fetch_bank_account_resolved_window_direct(
                client,
                account=account,
                start_date=start_date,
                end_date=planned_end,
                mfa_device_token=mfa_device_token,
                user_agent=user_agent,
                user_id=user_id,
            )
        except Exception as exc:
            await _record_bmo_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=start_date,
                end_date=planned_end,
                error=str(exc),
            )
            raise
        for row in transactions:
            row["_account_external_id"] = account.get("external_id")
            row["_account_currency"] = account.get("currency") or (details or {}).get("currency") or "CAD"
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="completed",
            account=account,
            mode=mode,
            start_date=start_date,
            end_date=planned_end,
            transactions=transactions,
        )
        return details, transactions, True

    history_floor = planned_end - timedelta(days=BMO_MAX_HISTORY_DAYS)
    latest_details: dict[str, Any] | None = None
    collected: list[dict[str, Any]] = []
    pending_windows = _bmo_pending_backfill_windows(
        account,
        history_floor,
        planned_end,
    )

    if pending_windows:
        _bmo_log_event(
            "bank account history window plan",
            debug=True,
            user_id=user_id,
            account=_bmo_mask_account_number(account.get("accountNumber")),
            account_index=account.get("accountIndex"),
            windows=len(pending_windows),
            first_start=pending_windows[0][0].isoformat(),
            first_end=pending_windows[0][1].isoformat(),
            last_start=pending_windows[-1][0].isoformat(),
            last_end=pending_windows[-1][1].isoformat(),
        )

    for current_start, current_end in pending_windows:
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
        )
        try:
            details, transactions = await _bmo_fetch_bank_account_resolved_window_direct(
                client,
                account=account,
                start_date=current_start,
                end_date=current_end,
                mfa_device_token=mfa_device_token,
                user_agent=user_agent,
                user_id=user_id,
            )
        except Exception as exc:
            await _record_bmo_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error=str(exc),
            )
            raise
        if latest_details is None and details:
            latest_details = details

        for row in transactions:
            record = dict(row)
            record["_account_external_id"] = account.get("external_id")
            record["_account_currency"] = account.get("currency") or (details or {}).get("currency") or "CAD"
            collected.append(record)
        await _record_bmo_transaction_window(
            transaction_window_recorder,
            event="completed",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
            transactions=transactions,
        )

    return latest_details, collected, True


def _bmo_bank_accounts_from_summary(accounts: list[dict[str, Any]]) -> int:
    return sum(1 for account in accounts if str(account.get("category_name") or "").upper() == "BA")


def _bmo_browser_transaction_history_ready(value: Any) -> bool:
    if not isinstance(value, dict) or not value.get("captured"):
        return False
    windows = value.get("windows")
    if not isinstance(windows, list) or not windows:
        return False
    try:
        expected_windows = int(value.get("expected_windows") or 0)
    except (TypeError, ValueError):
        expected_windows = 0
    return bool(
        expected_windows > 0
        and len(windows) >= expected_windows
    )


def _bmo_current_sync_id() -> str:
    context = get_connector_sync_context()
    return str(context.sync_id or "").strip() if context else ""


def _bmo_current_visible_auth_attempt_id(user_id: int | None) -> str:
    if user_id is None:
        return ""
    attempt_state = get_runtime_state(VISIBLE_AUTH_ATTEMPT_NAMESPACE, user_id)
    if not isinstance(attempt_state, dict):
        return ""
    return str(attempt_state.get("attempt_id") or "").strip()


def _bmo_browser_history_handoff_sync_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("handoff_sync_id") or value.get("sync_id") or "").strip()


def _bmo_visible_auth_attempt_handoff(session_state: dict[str, Any], *, user_id: int | None) -> bool:
    current_attempt_id = _bmo_current_visible_auth_attempt_id(user_id)
    if not current_attempt_id:
        return False
    browser_history = (
        session_state.get("browser_transaction_history")
        if isinstance(session_state.get("browser_transaction_history"), dict)
        else {}
    )
    artifact_attempt_id = str(
        session_state.get("attempt_id") or browser_history.get("attempt_id") or ""
    ).strip()
    return not artifact_attempt_id or artifact_attempt_id == current_attempt_id


def _bmo_browser_history_current_handoff(value: Any) -> bool:
    current_sync_id = _bmo_current_sync_id()
    if not current_sync_id:
        return False
    return _bmo_browser_history_handoff_sync_id(value) == current_sync_id


def _bmo_browser_transaction_history_pending(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and not value.get("captured")
        and str(value.get("status") or "").strip().lower() == "pending"
    )


async def _bmo_wait_for_post_handoff_browser_transaction_history(
    replay_session: SavedArtifactReplaySession | None,
    *,
    session_state: dict[str, Any],
    user_id: int | None,
) -> dict[str, Any] | None:
    current = session_state.get("browser_transaction_history")
    if _bmo_browser_transaction_history_ready(current):
        return current
    if replay_session is None or not _bmo_browser_transaction_history_pending(current):
        return current if isinstance(current, dict) else None

    _bmo_log_event(
        "waiting for post-handoff browser transaction capture",
        debug=True,
        user_id=user_id,
        wait_seconds=BMO_POST_HANDOFF_CAPTURE_WAIT_SECONDS,
    )
    deadline = asyncio.get_running_loop().time() + BMO_POST_HANDOFF_CAPTURE_WAIT_SECONDS
    poll_count = 0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        poll_count += 1
        try:
            await replay_session.load_async()
        except Exception as exc:
            _bmo_log_event(
                "post-handoff browser transaction capture poll failed",
                level="warning",
                debug=True,
                user_id=user_id,
                error=type(exc).__name__,
            )
            continue
        refreshed = replay_session.session_artifact
        if isinstance(refreshed, dict) and refreshed:
            session_state.clear()
            session_state.update(refreshed)
        current = session_state.get("browser_transaction_history")
        if _bmo_browser_transaction_history_ready(current):
            _bmo_log_event(
                "post-handoff browser transaction capture ready",
                debug=True,
                user_id=user_id,
                polls=poll_count,
                windows=len(current.get("windows") or []),
            )
            return current
        if isinstance(current, dict) and str(current.get("status") or "").strip().lower() == "failed":
            _bmo_log_event(
                "post-handoff browser transaction capture failed",
                level="warning",
                debug=True,
                user_id=user_id,
                polls=poll_count,
                error=current.get("error"),
            )
            return current
    _bmo_log_event(
        "post-handoff browser transaction capture timed out",
        level="warning",
        debug=True,
        user_id=user_id,
        wait_seconds=BMO_POST_HANDOFF_CAPTURE_WAIT_SECONDS,
    )
    return current if isinstance(current, dict) else None


async def _sync_bmo_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any] | None,
    *,
    user_id: int | None = None,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
    replay_session: SavedArtifactReplaySession | None = None,
) -> dict[str, Any] | None:
    started_at = datetime.now(timezone.utc)
    session_state = dict(session_artifact) if isinstance(session_artifact, dict) else {}
    user_agent = _bmo_session_user_agent(session_state)
    mfa_device_token = str(session_state.get("mfa_device_token") or "").strip() or None
    summary = _bmo_extract_summary(session_state.get("summary")) or {}
    captured_entitlements = session_state.get("entitlements") if isinstance(session_state.get("entitlements"), dict) else {}
    browser_transaction_history = (
        session_state.get("browser_transaction_history")
        if isinstance(session_state.get("browser_transaction_history"), dict)
        else None
    )
    visible_auth_attempt_handoff = _bmo_visible_auth_attempt_handoff(session_state, user_id=user_id)
    browser_history_current_handoff = _bmo_browser_history_current_handoff(browser_transaction_history)
    use_browser_transaction_history = bool(
        sync_scope != "accounts"
        and browser_history_current_handoff
        and _bmo_browser_transaction_history_ready(browser_transaction_history)
    )

    _bmo_log_event(
        "saved-session sync start",
        user_id=user_id,
        engine="browser_capture" if use_browser_transaction_history else "direct",
        debug=True,
        has_mfa_device_token=bool(mfa_device_token),
        has_summary=bool(summary),
        has_entitlements=bool(captured_entitlements),
        visible_auth_attempt_handoff=visible_auth_attempt_handoff,
        browser_history_current_handoff=browser_history_current_handoff,
        browser_history_handoff_sync_id=_bmo_browser_history_handoff_sync_id(browser_transaction_history),
        browser_transaction_windows=len((browser_transaction_history or {}).get("windows") or []),
    )

    if (
        sync_scope != "accounts"
        and not use_browser_transaction_history
        and browser_history_current_handoff
    ):
        browser_transaction_history = await _bmo_wait_for_post_handoff_browser_transaction_history(
            replay_session,
            session_state=session_state,
            user_id=user_id,
        )
        browser_history_current_handoff = _bmo_browser_history_current_handoff(browser_transaction_history)
        use_browser_transaction_history = bool(
            browser_history_current_handoff
            and _bmo_browser_transaction_history_ready(browser_transaction_history)
        )
        if use_browser_transaction_history:
            mfa_device_token = str(session_state.get("mfa_device_token") or "").strip() or None
            summary = _bmo_extract_summary(session_state.get("summary")) or summary
            captured_entitlements = (
                session_state.get("entitlements")
                if isinstance(session_state.get("entitlements"), dict)
                else captured_entitlements
            )

    async with SavedArtifactHttpClient(
        storage_state,
        session_state,
        user_agent=user_agent,
        timeout=BMO_DIRECT_TIMEOUT_SECONDS,
        follow_redirects=True,
        seed_cookies=True,
    ) as replay:
        client = replay.client
        entitlements = captured_entitlements
        defer_direct_bootstrap_for_background_browser = bool(
            sync_scope != "accounts"
            and entitlements
            and not use_browser_transaction_history
        )
        if use_browser_transaction_history:
            _bmo_log_event(
                "transaction sync using captured browser transaction history",
                user_id=user_id,
                accounts=sum(
                    len(entitlements.get(key) or [])
                    for key in ("bankAccounts", "creditCardAccounts", "loanAccounts", "investmentAccounts")
                ) if isinstance(entitlements, dict) else 0,
                windows=len((browser_transaction_history or {}).get("windows") or []),
            )
        elif entitlements and defer_direct_bootstrap_for_background_browser:
            _bmo_log_event(
                "sync using captured entitlements",
                debug=True,
                user_id=user_id,
                accounts=sum(
                    len(entitlements.get(key) or [])
                    for key in ("bankAccounts", "creditCardAccounts", "loanAccounts", "investmentAccounts")
                ),
            )
        else:
            refreshed_entitlements = await _bmo_direct_bootstrap(
                client,
                mfa_device_token=mfa_device_token,
                user_agent=user_agent,
                user_id=user_id,
            )
            if refreshed_entitlements is None:
                if entitlements and sync_scope == "accounts" and visible_auth_attempt_handoff:
                    _bmo_log_event(
                        "account sync using captured entitlements after replay validation failed",
                        level="warning",
                        user_id=user_id,
                        accounts=sum(
                            len(entitlements.get(key) or [])
                            for key in ("bankAccounts", "creditCardAccounts", "loanAccounts", "investmentAccounts")
                        ),
                    )
                else:
                    return None
            else:
                entitlements = refreshed_entitlements or entitlements

        accounts = _bmo_summary_accounts(summary, fresh_summary=bool(summary))
        summary_seeded_accounts = bool(accounts)
        if not accounts:
            accounts = _bmo_accounts_from_entitlements(entitlements, fresh_summary=False)
            if accounts:
                _bmo_log_event(
                    "sync using entitlements seed",
                    debug=True,
                    user_id=user_id,
                    accounts=len(accounts),
                )
        if not accounts:
            return {"status": "error", "message": "No BMO accounts returned"}

        _bmo_augment_accounts_with_entitlements(accounts, entitlements)
        sync_windows = await mode_resolver(accounts) if mode_resolver and sync_scope != "accounts" else {}
        for account in accounts:
            _bmo_apply_sync_window(account, sync_windows)

        if sync_scope != "accounts" and not use_browser_transaction_history:
            if defer_direct_bootstrap_for_background_browser:
                refreshed_entitlements = await _bmo_direct_bootstrap(
                    client,
                    mfa_device_token=mfa_device_token,
                    user_agent=user_agent,
                    user_id=user_id,
                )
                if refreshed_entitlements is None:
                    return None
                entitlements = refreshed_entitlements or entitlements
                _bmo_augment_accounts_with_entitlements(accounts, entitlements)
            preflight_ok = await _bmo_transaction_history_preflight(
                client,
                mfa_device_token=mfa_device_token,
                user_agent=user_agent,
                user_id=user_id,
            )
            if not preflight_ok:
                return None

        raw_transactions: dict[str, list[dict[str, Any]]] = {}
        transaction_fetch_succeeded_accounts: set[str] = set()
        transaction_fetch_failed_accounts: dict[str, str] = {}
        transaction_eligible_accounts: set[str] = set()

        for account in accounts:
            category_name = str(account.get("category_name") or "").upper()
            external_id = account.get("external_id")
            if category_name != "BA" or account.get("accountIndex") is None or not external_id:
                if not summary:
                    account["balance_is_fresh"] = False
                continue

            transaction_eligible_accounts.add(str(external_id))
            if sync_scope == "accounts":
                if summary:
                    account["balance_is_fresh"] = True
                    continue
                browser_details = _bmo_latest_browser_account_details(browser_transaction_history, account)
                if browser_details:
                    account.update(browser_details)
                    account["balance_is_fresh"] = True
                    continue
                try:
                    today = date.today()
                    details, _transactions, _more_txns = await _bmo_fetch_bank_account_window_direct(
                        client,
                        account=account,
                        start_date=today,
                        end_date=today,
                        mfa_device_token=mfa_device_token,
                        user_agent=user_agent,
                    )
                    if details:
                        account.update(details)
                        account["balance_is_fresh"] = True
                except PermissionError:
                    if entitlements:
                        account["balance_is_fresh"] = False
                        _bmo_log_event(
                            "bank account balance refresh auth_required",
                            level="warning",
                            user_id=user_id,
                            account=_bmo_mask_account_number(account.get("accountNumber")),
                            account_index=account.get("accountIndex"),
                            message="using captured BMO entitlements without fresh balance",
                        )
                        continue
                    return None
                except Exception as exc:
                    account["balance_is_fresh"] = bool(summary)
                    _bmo_log_event(
                        "bank account balance refresh failed",
                        level="warning",
                        user_id=user_id,
                        account=_bmo_mask_account_number(account.get("accountNumber")),
                        account_index=account.get("accountIndex"),
                        error=type(exc).__name__,
                        message=_bmo_sanitize_log_text(str(exc), limit=180),
                    )
                continue

            try:
                if use_browser_transaction_history and browser_transaction_history is not None:
                    details, transactions, succeeded = await _bmo_fetch_bank_account_history_from_browser_capture(
                        browser_transaction_history,
                        account,
                        transaction_window_recorder=transaction_window_recorder,
                    )
                else:
                    details, transactions, succeeded = await _bmo_fetch_bank_account_history_direct(
                        client,
                        account,
                        mfa_device_token=mfa_device_token,
                        user_agent=user_agent,
                        user_id=user_id,
                        transaction_window_recorder=transaction_window_recorder,
                    )
                if details:
                    account.update(details)
                    account["balance_is_fresh"] = True
                if transactions is not None:
                    raw_transactions[external_id] = transactions
                if succeeded:
                    transaction_fetch_succeeded_accounts.add(external_id)
            except PermissionError:
                return None
            except Exception as exc:
                account["balance_is_fresh"] = bool(summary)
                transaction_fetch_failed_accounts[str(external_id)] = str(exc)
                _bmo_log_event(
                    "bank account fetch failed",
                    level="warning",
                    user_id=user_id,
                    account=_bmo_mask_account_number(account.get("accountNumber")),
                    account_index=account.get("accountIndex"),
                    error=type(exc).__name__,
                    message=_bmo_sanitize_log_text(str(exc), limit=180),
                )

    if not summary_seeded_accounts:
        session_state["summary"] = _bmo_summary_from_accounts(accounts)
    elif summary:
        session_state["summary"] = summary
    refreshed_extra = {
        "summary": session_state.get("summary") or {},
        "entitlements": entitlements or {},
    }
    if isinstance(session_state.get("browser_transaction_history"), dict):
        refreshed_extra["browser_transaction_history"] = session_state["browser_transaction_history"]
    session_state = replay.refreshed_session_artifact(extra=refreshed_extra)
    if replay_session is not None:
        await replay_session.refresh_runtime_artifacts_async(storage_state, session_artifact=session_state)
    elif user_id is not None:
        await save_session_artifact(user_id, session_state)

    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    if sync_scope != "accounts" and transaction_eligible_accounts and not transaction_fetch_succeeded_accounts:
        first_error = next(iter(transaction_fetch_failed_accounts.values()), None)
        message = first_error or "BMO transaction fetch failed"
        _bmo_log_event(
            "saved-session transaction sync failed",
            level="warning",
            user_id=user_id,
            engine="browser_capture" if use_browser_transaction_history else "direct",
            eligible_accounts=len(transaction_eligible_accounts),
            failed_accounts=len(transaction_fetch_failed_accounts),
            transactions=sum(len(items or []) for items in raw_transactions.values()),
            duration_ms=duration_ms,
            message=_bmo_sanitize_log_text(message, limit=180),
        )
        return {
            "status": "error",
            "message": message,
            "accounts": accounts,
            "transactions": raw_transactions,
            "transaction_fetch_succeeded_accounts": sorted(transaction_fetch_succeeded_accounts),
        }

    _bmo_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="browser_capture" if use_browser_transaction_history else "direct",
        accounts=len(accounts),
        bank_accounts=_bmo_bank_accounts_from_summary(accounts),
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
    _bmo_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_bmo_session_user_agent,
        log_event=_bmo_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact = replay_session.session_artifact
    if not isinstance(storage_state, dict):
        return replay_session.missing_artifact_result()

    try:
        payload = await _sync_bmo_with_saved_artifacts(
            storage_state,
            session_artifact,
            user_id=user_id,
            mode_resolver=mode_resolver,
            sync_scope=sync_scope,
            transaction_window_recorder=transaction_window_recorder,
            replay_session=replay_session,
        )
    except Exception as exc:
        message = str(exc).lower()
        if any(
            token in message
            for token in (
                "timeout",
                "connect",
                "dns",
                "network",
                "unreachable",
                "resolve",
                "name resolution",
                "name or service not known",
                "ssl",
                "connection refused",
            )
        ):
            raise
        _bmo_log_event(
            "headless sync failed",
            level="warning",
            user_id=user_id,
            error=type(exc).__name__,
            message=_bmo_sanitize_log_text(str(exc), limit=180),
        )
        return scraper_error(_bmo_sanitize_log_text(str(exc), limit=180))
    if payload is None:
        return replay_session.auth_required_result(log_message="saved BMO artifacts did not validate")
    return replay_session.normalize_result(payload)
