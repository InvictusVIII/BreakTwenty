from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    parse_json_body,
    scraper_session_user_agent,
)
from app.scrapers.scraper_logging import (
    EMAIL_LOG_REDACTION_PATTERN,
    JWT_LOG_REDACTION_PATTERN,
    log_scraper_event,
    sanitize_scraper_log_text,
    sanitize_scraper_log_url,
    scraper_debug_enabled,
)
from app.services.connection_auth_storage import SCRAPER_CREDENTIALS_ARTIFACT_KIND
from app.services.runtime_state import get_runtime_state, pop_runtime_state
from app.services.visible_auth_attempt_storage import load_visible_auth_attempt_artifact_async

PROVIDER = "eqbank"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.eqbank")

EQBANK_ORIGIN = "https://secure.eqbank.ca"
EQBANK_LOGIN_DETAILS_URL = "https://api.eqbank.ca/auth/v3/login-details"
EQBANK_PARTY_PROFILES_URL = "https://web-api.eqbank.ca/web/v1.1/party/profiles"
EQBANK_CUSTOMERS_URL = "https://web-api.eqbank.ca/web/v1.1/v3/customers"
EQBANK_ACCOUNTS_DASHBOARD_URL = "https://web-api.eqbank.ca/web/v1.1/accounts/v2/accounts/dashboard"
EQBANK_ACCOUNTS_URL = "https://web-api.eqbank.ca/web/v1.1/accounts/v2/accounts"
EQBANK_TRANSACTIONS_URL_TEMPLATE = "https://web-api.eqbank.ca/web/v1.1/accounts/{account_number}/transactions"
EQBANK_BACKFILL_CHUNK_DAYS = 365
EQBANK_MAX_BACKFILL_DAYS = 3650
JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
EQBANK_TRACE_SAFE_QUERY_KEYS = {"fromBookingDate", "toBookingDate"}

ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, dict[str, str | int | None]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


def _visible_auth_attempt_id(user_id: int) -> str:
    attempt_state = get_runtime_state(VISIBLE_AUTH_ATTEMPT_NAMESPACE, user_id)
    if not isinstance(attempt_state, dict):
        return ""
    return str(attempt_state.get("attempt_id") or "").strip()


async def _visible_auth_attempt_username(user_id: int) -> str:
    attempt_id = _visible_auth_attempt_id(user_id)
    if not attempt_id:
        return ""
    payload = await load_visible_auth_attempt_artifact_async(
        user_id,
        PROVIDER,
        attempt_id,
        SCRAPER_CREDENTIALS_ARTIFACT_KIND,
    )
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("username") or "").strip()


def _eqbank_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_EQBANK_DEBUG",))


def _eqbank_log_event(
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
        debug_enabled=_eqbank_debug_logs_enabled(),
        **fields,
    )


def _eqbank_sanitize_log_text(text: str | None, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(
        text,
        limit=limit,
        redactions=(
            (JWT_LOG_REDACTION_PATTERN, "<jwt>", 0),
            (EMAIL_LOG_REDACTION_PATTERN, "<email>", re.IGNORECASE),
        ),
    )


def _eqbank_sanitize_url_for_log(url: str | None) -> str:
    return sanitize_scraper_log_url(url, safe_query_keys=EQBANK_TRACE_SAFE_QUERY_KEYS)


def _eqbank_mask_account_number(account_number: str | None) -> str | None:
    digits = re.sub(r"\D+", "", str(account_number or ""))
    if not digits:
        return None
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def _eqbank_jwt_expiry(token: str | None) -> int | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    value = data.get("exp")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _eqbank_token_is_fresh(token: str | None, *, skew_seconds: int = 60) -> bool:
    expires_at = _eqbank_jwt_expiry(token)
    return bool(token and expires_at and expires_at > int(time.time()) + skew_seconds)


def _extract_token_candidate(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        match = JWT_RE.search(text)
        if match:
            return match.group(0)
        if text.startswith("{") or text.startswith("["):
            parsed = parse_json_body(text)
            if parsed is not None:
                return _extract_token_candidate(parsed)
        return None
    if isinstance(value, dict):
        for key, nested in value.items():
            if "token" in str(key).lower():
                candidate = _extract_token_candidate(nested)
                if candidate:
                    return candidate
        for nested in value.values():
            candidate = _extract_token_candidate(nested)
            if candidate:
                return candidate
        return None
    if isinstance(value, list):
        for nested in value:
            candidate = _extract_token_candidate(nested)
            if candidate:
                return candidate
    return None


def _extract_session_artifact_access_token(session_artifact: dict[str, Any] | None) -> str | None:
    if not isinstance(session_artifact, dict):
        return None
    token = session_artifact.get("access_token")
    return token if _eqbank_token_is_fresh(token) else None


def _extract_session_artifact_access_token_any(session_artifact: dict[str, Any] | None) -> str | None:
    if not isinstance(session_artifact, dict):
        return None
    token = session_artifact.get("access_token")
    return token if isinstance(token, str) and token.strip() else None


def _extract_storage_state_access_token(storage_state: dict[str, Any] | None) -> str | None:
    if not isinstance(storage_state, dict):
        return None
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            candidate = _extract_token_candidate(entry.get("value"))
            if _eqbank_token_is_fresh(candidate):
                return candidate
    return None


def _extract_storage_state_access_token_any(storage_state: dict[str, Any] | None) -> str | None:
    if not isinstance(storage_state, dict):
        return None
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            candidate = _extract_token_candidate(entry.get("value"))
            if candidate:
                return candidate
    return None


def _eqbank_browser_profile(session_artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(session_artifact, dict):
        return {}
    profile = session_artifact.get("browser_profile")
    if isinstance(profile, dict):
        return profile
    return {}


def _eqbank_user_agent(session_artifact: dict[str, Any] | None = None) -> str:
    artifact_user_agent = scraper_session_user_agent(session_artifact, "")
    if artifact_user_agent:
        return artifact_user_agent
    return default_scraper_user_agent(_eqbank_browser_profile(session_artifact).get("user_agent"))


def _eqbank_set_last_error(error_state: dict[str, Any] | None, message: str) -> None:
    if not isinstance(error_state, dict):
        return
    if message:
        error_state["last_error"] = message
    else:
        error_state.pop("last_error", None)


def _eqbank_last_error(error_state: dict[str, Any] | None, fallback: str) -> str:
    if isinstance(error_state, dict):
        message = str(error_state.get("last_error") or "").strip()
        if message:
            return message
    return fallback


def _eqbank_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    access_token: str | None = None,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    json_body: Any | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-CA",
        "Cache-Control": "no-cache",
        "channel": "WEB",
        "correlationid": str(uuid.uuid4()),
        "Origin": EQBANK_ORIGIN,
        "Pragma": "no-cache",
        "Referer": f"{EQBANK_ORIGIN}/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": _eqbank_user_agent(session_artifact),
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if email:
        headers["email"] = email
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    return headers


async def _eqbank_request(
    storage_state: dict[str, Any],
    method: str,
    url: str,
    *,
    access_token: str | None = None,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    params: dict[str, str] | None = None,
    json_body: Any | None = None,
) -> httpx.Response:
    headers = _eqbank_headers(
        storage_state,
        url,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        json_body=json_body,
    )
    async with SavedArtifactHttpClient(
        storage_state,
        session_artifact,
        user_agent=_eqbank_user_agent(session_artifact),
        timeout=30,
        seed_cookies=True,
    ) as replay:
        return await replay.request(method, url, headers=headers, params=params, json=json_body)


def _eqbank_json_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return parse_json_body(response.text)


def _eqbank_text_error_snippet(text: str | None, *, limit: int = 500) -> str:
    snippet = str(text or "").strip()
    if not snippet:
        return ""
    return _eqbank_sanitize_log_text(snippet, limit=limit)


def _eqbank_request_url(url: str, params: dict[str, str] | None = None) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


async def _resolve_eqbank_access_token(
    storage_state: dict[str, Any],
    *,
    session_artifact: dict[str, Any] | None = None,
) -> str | None:
    session_token = _extract_session_artifact_access_token(session_artifact)
    if session_token:
        return session_token
    storage_token = _extract_storage_state_access_token(storage_state)
    if storage_token:
        return storage_token
    stale_session_token = _extract_session_artifact_access_token_any(session_artifact)
    if stale_session_token:
        return stale_session_token
    stale_storage_token = _extract_storage_state_access_token_any(storage_state)
    if stale_storage_token:
        return stale_storage_token
    return None


async def _eqbank_fetch_payload(
    storage_state: dict[str, Any],
    method: str,
    url: str,
    *,
    access_token: str,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    params: dict[str, str] | None = None,
    json_body: Any | None = None,
    error_state: dict[str, Any] | None = None,
    error_label: str,
) -> Any | None:
    response = await _eqbank_request(
        storage_state,
        method,
        url,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        params=params,
        json_body=json_body,
    )
    if response.status_code in (401, 403):
        message = f"EQ Bank {error_label} returned unauthorized after authentication"
        _eqbank_set_last_error(error_state, message)
        _eqbank_log_event(
            "saved-session sync auth_required",
            level="warning",
            url=_eqbank_sanitize_url_for_log(_eqbank_request_url(url, params)),
            has_email=bool(email),
            message=message,
        )
        return None
    if response.status_code >= 400:
        message = f"EQ Bank {error_label} request failed ({response.status_code})"
        _eqbank_set_last_error(error_state, message)
        _eqbank_log_event(
            f"{error_label} request failed",
            level="warning",
            status=response.status_code,
            url=_eqbank_sanitize_url_for_log(_eqbank_request_url(url, params)),
            body=_eqbank_text_error_snippet(response.text) or "<empty>",
        )
        raise RuntimeError(message)

    _eqbank_log_event(
        f"{error_label} ok",
        debug=True,
        url=_eqbank_sanitize_url_for_log(_eqbank_request_url(url, params)),
        status=response.status_code,
    )
    return _eqbank_json_payload(response)


def _eqbank_relationship_index(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    profile_groups = []
    data = payload.get("data")
    if isinstance(data, dict):
        profile_groups.append(data.get("customerProfiles"))
    profile_groups.append(payload.get("customerProfiles"))

    fallback = None
    for profiles in profile_groups:
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            relationship_index = profile.get("relationshipIndex")
            if relationship_index is None:
                continue
            index_text = str(relationship_index).strip()
            if not index_text:
                continue
            if str(profile.get("relationship") or "").upper() == "SELF":
                return index_text
            if fallback is None:
                fallback = index_text
    return fallback


async def _bootstrap_eqbank_profile_context(
    storage_state: dict[str, Any],
    access_token: str,
    *,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    error_state: dict[str, Any] | None = None,
) -> bool | None:
    login_details = await _eqbank_fetch_payload(
        storage_state,
        "GET",
        EQBANK_LOGIN_DETAILS_URL,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        error_state=error_state,
        error_label="login-details",
    )
    if login_details is None:
        return None

    relationship_index = _eqbank_relationship_index(login_details)
    if not relationship_index:
        message = "EQ Bank login-details response did not include a usable relationship profile"
        _eqbank_set_last_error(error_state, message)
        _eqbank_log_event("profile bootstrap missing relationship", level="warning", message=message)
        return False

    profiles_payload = await _eqbank_fetch_payload(
        storage_state,
        "POST",
        EQBANK_PARTY_PROFILES_URL,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        params={"relationshipIndex": relationship_index},
        json_body={},
        error_state=error_state,
        error_label="profile bootstrap",
    )
    if profiles_payload is None:
        return None

    customers_payload = await _eqbank_fetch_payload(
        storage_state,
        "GET",
        EQBANK_CUSTOMERS_URL,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        error_state=error_state,
        error_label="customer bootstrap",
    )
    if customers_payload is None:
        return None

    return True


def _eqbank_account_payload(raw_account: dict[str, Any]) -> dict[str, Any]:
    provider_account_id = str(
        raw_account.get("accountId")
        or raw_account.get("arrangementId")
        or raw_account.get("accountNumber")
        or ""
    ).strip()
    account_number = str(raw_account.get("accountNumber") or "").strip()

    payload = dict(raw_account)
    payload["provider_account_id"] = provider_account_id or None
    payload["account_number"] = account_number or None
    payload["external_id"] = f"account:{provider_account_id}" if provider_account_id else (account_number or None)
    payload["alternate_external_id"] = account_number or None
    payload["name"] = str(raw_account.get("accountName") or raw_account.get("productType") or "EQ Bank Account").strip()
    return payload


def _eqbank_accounts_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        return [account for account in payload if isinstance(account, dict)]
    if not isinstance(payload, dict):
        return None
    for key in ("accounts", "Accounts"):
        accounts = payload.get(key)
        if isinstance(accounts, list):
            return [account for account in accounts if isinstance(account, dict)]
    data = payload.get("Data") or payload.get("data")
    if isinstance(data, dict):
        for key in ("accounts", "Accounts"):
            accounts = data.get(key)
            if isinstance(accounts, list):
                return [account for account in accounts if isinstance(account, dict)]
    return None


async def _fetch_eqbank_accounts(
    storage_state: dict[str, Any],
    access_token: str,
    *,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    error_state: dict[str, Any] | None = None,
    bootstrap_context: bool = True,
) -> list[dict[str, Any]] | None:
    _eqbank_log_event("accounts fetch start", transport="direct", bootstrap_context=bootstrap_context)
    if bootstrap_context:
        bootstrap_ready = await _bootstrap_eqbank_profile_context(
            storage_state,
            access_token,
            session_artifact=session_artifact,
            email=email,
            error_state=error_state,
        )
        if bootstrap_ready is not True:
            return None

    last_status_code = None
    for url in (EQBANK_ACCOUNTS_DASHBOARD_URL, EQBANK_ACCOUNTS_URL):
        response = await _eqbank_request(
            storage_state,
            "GET",
            url,
            access_token=access_token,
            session_artifact=session_artifact,
            email=email,
        )
        last_status_code = response.status_code
        if response.status_code in (401, 403):
            _eqbank_set_last_error(error_state, "EQ Bank accounts request returned unauthorized after authentication")
            return None
        if response.status_code >= 400:
            _eqbank_log_event(
                "accounts request failed",
                level="warning",
                status=response.status_code,
                url=_eqbank_sanitize_url_for_log(url),
                body=_eqbank_text_error_snippet(response.text) or "<empty>",
            )
            continue
        accounts = _eqbank_accounts_from_payload(_eqbank_json_payload(response))
        if accounts is not None:
            _eqbank_log_event("accounts fetch success", transport="direct", url=_eqbank_sanitize_url_for_log(url), count=len(accounts))
            return [_eqbank_account_payload(account) for account in accounts]
    raise RuntimeError(f"EQ Bank accounts request failed ({last_status_code or 0})")


def _parse_eqbank_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if "T" in text:
            return date.fromisoformat(text.split("T", 1)[0])
        return date.fromisoformat(text)
    except ValueError:
        return None


def _eqbank_tx_identity(raw_txn: dict[str, Any]) -> str:
    return str(
        raw_txn.get("TransactionId")
        or raw_txn.get("transactionId")
        or json.dumps(raw_txn, sort_keys=True)
    )


def _dedupe_eqbank_transactions(raw_transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for txn in raw_transactions:
        key = _eqbank_tx_identity(txn)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(txn)
    return deduped


def _eqbank_truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _eqbank_apply_sync_window(account: dict[str, Any], sync_windows: dict[str, Any]) -> None:
    external_id = str(account.get("external_id") or "").strip()
    account_number = str(account.get("account_number") or "").strip()
    entry = sync_windows.get(external_id) if external_id else None
    if not isinstance(entry, dict) and account_number:
        entry = sync_windows.get(account_number)
    account["sync_window"] = dict(entry) if isinstance(entry, dict) else {"mode": "backfill"}


def _eqbank_transaction_window(account: dict[str, Any]) -> tuple[str, date, date]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    mode = str(sync_window.get("mode") or "backfill")
    end_date = sync_window_date(sync_window, "end_date") or date.today()
    opening_date = _parse_eqbank_date(account.get("accountOpeningDate"))

    if mode != "backfill":
        start_date = sync_window_date(sync_window, "start_date") or end_date
        if opening_date and opening_date > start_date:
            start_date = opening_date
    else:
        start_date = opening_date or (end_date - timedelta(days=EQBANK_MAX_BACKFILL_DAYS))

    if start_date > end_date:
        start_date = end_date
    return mode, start_date, end_date


def _eqbank_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor_end = end_date
    while cursor_end >= start_date:
        cursor_start = max(start_date, cursor_end - timedelta(days=EQBANK_BACKFILL_CHUNK_DAYS - 1))
        windows.append((cursor_start, cursor_end))
        if cursor_start <= start_date:
            break
        cursor_end = cursor_start - timedelta(days=1)
    return windows


def _eqbank_pending_backfill_windows(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> list[tuple[date, date]]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    windows = pending_backfill_date_windows(sync_window)
    return windows or _eqbank_split_windows(history_start, history_end)


async def _record_eqbank_transaction_window(
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
        _eqbank_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_eqbank_sanitize_log_text(str(exc), limit=140),
        )


async def _fetch_eqbank_transaction_window(
    storage_state: dict[str, Any],
    access_token: str,
    account_number: str,
    start_date: date,
    end_date: date,
    *,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
) -> list[dict[str, Any]] | None:
    url = EQBANK_TRANSACTIONS_URL_TEMPLATE.format(account_number=account_number)
    params = {
        "fromBookingDate": start_date.isoformat(),
        "toBookingDate": end_date.isoformat(),
    }
    _eqbank_log_event(
        "transactions window start",
        account=_eqbank_mask_account_number(account_number),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        transport="direct",
    )
    response = await _eqbank_request(
        storage_state,
        "GET",
        url,
        access_token=access_token,
        session_artifact=session_artifact,
        email=email,
        params=params,
    )
    if response.status_code in (401, 403):
        return None
    if response.status_code >= 400:
        _eqbank_log_event(
            "transactions request failed",
            level="warning",
            status=response.status_code,
            account=_eqbank_mask_account_number(account_number),
            body=_eqbank_text_error_snippet(response.text) or "<empty>",
        )
        raise RuntimeError(f"EQ Bank transactions request failed ({response.status_code})")

    payload = _eqbank_json_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("EQ Bank transactions response was not JSON")

    data = payload.get("Data") or {}
    raw_transactions = data.get("Transaction") or []
    if not isinstance(raw_transactions, list):
        raw_transactions = []

    meta = payload.get("Meta") or {}
    list_end = _eqbank_truthy(meta.get("ListEnd"), default=True)
    _eqbank_log_event(
        "transactions window response",
        account=_eqbank_mask_account_number(account_number),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        count=len(raw_transactions),
        list_end=list_end,
    )
    if list_end or start_date >= end_date:
        return raw_transactions

    day_span = (end_date - start_date).days
    if day_span <= 1:
        _eqbank_log_event(
            "transactions window still truncated",
            level="warning",
            account=_eqbank_mask_account_number(account_number),
            start=start_date.isoformat(),
        )
        return raw_transactions

    midpoint = start_date + timedelta(days=day_span // 2)
    _eqbank_log_event(
        "transactions window split",
        account=_eqbank_mask_account_number(account_number),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        midpoint=midpoint.isoformat(),
    )
    left = await _fetch_eqbank_transaction_window(
        storage_state,
        access_token,
        account_number,
        start_date,
        midpoint,
        session_artifact=session_artifact,
        email=email,
    )
    if left is None:
        return None

    right_start = midpoint + timedelta(days=1)
    if right_start > end_date:
        return _dedupe_eqbank_transactions(left)

    right = await _fetch_eqbank_transaction_window(
        storage_state,
        access_token,
        account_number,
        right_start,
        end_date,
        session_artifact=session_artifact,
        email=email,
    )
    if right is None:
        return None
    return _dedupe_eqbank_transactions(left + right)


async def fetch_transactions(
    storage_state: dict[str, Any],
    access_token: str,
    accounts: list[dict[str, Any]],
    *,
    sync_windows: dict[str, Any] | None = None,
    session_artifact: dict[str, Any] | None = None,
    email: str | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]] | None:
    sync_windows = sync_windows or {}
    transactions: dict[str, list[dict[str, Any]]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()

    for account in accounts:
        external_id = account.get("external_id")
        account_number = account.get("account_number")
        if not external_id or not account_number:
            continue

        _eqbank_apply_sync_window(account, sync_windows)
        mode, history_start, history_end = _eqbank_transaction_window(account)
        collected: list[dict[str, Any]] = []
        _eqbank_log_event(
            "transactions account start",
            account=_eqbank_mask_account_number(account_number),
            external_id=external_id,
            mode=mode,
            start=history_start.isoformat(),
            end=history_end.isoformat(),
        )

        if mode != "backfill":
            await _record_eqbank_transaction_window(
                transaction_window_recorder,
                event="started",
                account=account,
                mode=mode,
                start_date=history_start,
                end_date=history_end,
            )
            try:
                window_transactions = await _fetch_eqbank_transaction_window(
                    storage_state,
                    access_token,
                    account_number,
                    history_start,
                    history_end,
                    session_artifact=session_artifact,
                    email=email,
                )
            except Exception as exc:
                await _record_eqbank_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=history_start,
                    end_date=history_end,
                    error=str(exc),
                )
                raise
            if window_transactions is None:
                await _record_eqbank_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=history_start,
                    end_date=history_end,
                    error="EQ Bank transaction window failed",
                )
                return None
            collected.extend(window_transactions)
            await _record_eqbank_transaction_window(
                transaction_window_recorder,
                event="completed",
                account=account,
                mode=mode,
                start_date=history_start,
                end_date=history_end,
                transactions=window_transactions,
            )
        else:
            for cursor_start, cursor_end in _eqbank_pending_backfill_windows(
                account,
                history_start,
                history_end,
            ):
                await _record_eqbank_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=account,
                    mode=mode,
                    start_date=cursor_start,
                    end_date=cursor_end,
                )
                try:
                    window_transactions = await _fetch_eqbank_transaction_window(
                        storage_state,
                        access_token,
                        account_number,
                        cursor_start,
                        cursor_end,
                        session_artifact=session_artifact,
                        email=email,
                    )
                except Exception as exc:
                    await _record_eqbank_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=account,
                        mode=mode,
                        start_date=cursor_start,
                        end_date=cursor_end,
                        error=str(exc),
                    )
                    raise
                if window_transactions is None:
                    await _record_eqbank_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=account,
                        mode=mode,
                        start_date=cursor_start,
                        end_date=cursor_end,
                        error="EQ Bank transaction window failed",
                    )
                    return None
                collected.extend(window_transactions)
                await _record_eqbank_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=account,
                    mode=mode,
                    start_date=cursor_start,
                    end_date=cursor_end,
                    transactions=window_transactions,
                )

        transactions[external_id] = _dedupe_eqbank_transactions(collected)
        transaction_fetch_succeeded_accounts.add(external_id)
        _eqbank_log_event(
            "transactions account complete",
            account=_eqbank_mask_account_number(account_number),
            external_id=external_id,
            count=len(transactions[external_id]),
            mode=mode,
        )

    return transactions, transaction_fetch_succeeded_accounts


async def _sync_eqbank_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any] | None,
    *,
    user_id: int | None = None,
    error_state: dict[str, Any] | None = None,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
    username: str | None = None,
):
    started_at = datetime.now(timezone.utc)
    email = str(username or "").strip() or None
    error_target = error_state if isinstance(error_state, dict) else session_artifact
    _eqbank_set_last_error(error_target, "")
    _eqbank_log_event(
        "saved-session sync start",
        user_id=user_id,
        transport="direct",
        has_storage_state=bool(storage_state),
        has_session_artifact=bool(session_artifact),
        has_email=bool(email),
    )
    access_token = await _resolve_eqbank_access_token(storage_state, session_artifact=session_artifact)
    if not access_token:
        message = "No fresh EQ bearer token was available for saved-session reuse"
        _eqbank_set_last_error(error_target, message)
        _eqbank_log_event("saved-session sync auth_required", level="warning", user_id=user_id, message=message)
        return None
    if isinstance(session_artifact, dict):
        session_artifact["access_token"] = access_token

    accounts = await _fetch_eqbank_accounts(
        storage_state,
        access_token,
        session_artifact=session_artifact,
        email=email,
        error_state=error_target,
    )
    if accounts is None:
        message = _eqbank_last_error(error_target, "EQ accounts request returned unauthorized after authentication")
        _eqbank_set_last_error(error_target, message)
        _eqbank_log_event("saved-session sync auth_required", level="warning", user_id=user_id, message=message)
        return None

    raw_transactions: dict[str, list[dict[str, Any]]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()
    if sync_scope != "accounts":
        sync_windows = await mode_resolver(accounts) if mode_resolver else {}
        transaction_result = await fetch_transactions(
            storage_state,
            access_token,
            accounts,
            sync_windows=sync_windows,
            session_artifact=session_artifact,
            email=email,
            transaction_window_recorder=transaction_window_recorder,
        )
        if transaction_result is None:
            message = _eqbank_last_error(error_target, "EQ transactions request returned unauthorized after authentication")
            _eqbank_set_last_error(error_target, message)
            _eqbank_log_event("saved-session sync auth_required", level="warning", user_id=user_id, message=message)
            return None

        raw_transactions, transaction_fetch_succeeded_accounts = transaction_result
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    _eqbank_log_event(
        "saved-session sync success",
        user_id=user_id,
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
    username: str | None = None,
):
    _eqbank_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_eqbank_user_agent,
        log_event=_eqbank_log_event,
    )
    storage_state = replay_session.storage_state
    if not storage_state:
        return replay_session.missing_artifact_result(stage="headless sync missing runtime_state")

    session_artifact_state = replay_session.session_artifact
    effective_username = (
        await _visible_auth_attempt_username(user_id)
        or str(username or "").strip()
        or None
    )

    payload = await _sync_eqbank_with_saved_artifacts(
        storage_state,
        session_artifact_state,
        user_id=user_id,
        mode_resolver=mode_resolver,
        sync_scope=sync_scope,
        transaction_window_recorder=transaction_window_recorder,
        username=effective_username,
    )
    if isinstance(payload, dict) and payload.get("status") == "ok":
        access_token = _extract_session_artifact_access_token(session_artifact_state)
        if access_token:
            session_artifact_state["access_token"] = access_token
        await replay_session.refresh_runtime_artifacts_async(storage_state)
        _eqbank_log_event(
            "headless sync result",
            user_id=user_id,
            engine="direct",
            status=payload.get("status"),
            accounts=len(payload.get("accounts") or []),
            transaction_accounts=len(payload.get("transaction_fetch_succeeded_accounts") or []),
        )
        return replay_session.normalize_result(payload)
    if isinstance(payload, dict):
        _eqbank_log_event(
            "headless sync result",
            user_id=user_id,
            engine="direct",
            level="warning",
            status=payload.get("status"),
            message=payload.get("message"),
        )
        return replay_session.normalize_result(payload)

    return replay_session.auth_required_result(
        log_message="saved-session direct reuse did not reach authenticated EQ Bank state"
    )


async def cleanup_pending(user_id: int) -> None:
    pop_runtime_state(PROVIDER, user_id, None)
