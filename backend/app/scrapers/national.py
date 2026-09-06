from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    load_saved_artifact_replay_session_async,
    parse_json_body,
    save_visible_auth_session_artifact_async,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_error
from app.scrapers.scraper_logging import (
    EMAIL_LOG_REDACTION_PATTERN,
    JWT_LOG_REDACTION_PATTERN,
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)

PROVIDER = "national"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.national")

NATIONAL_GRAPHQL_URL = "https://digitalretail.apis.bnc.ca/sbip/graphql"
NATIONAL_BACKFILL_CHUNK_DAYS = 365
NATIONAL_MAX_HISTORY_DAYS = 3650
NATIONAL_DIRECT_TIMEOUT_SECONDS = 30
NATIONAL_ACCOUNTS_WITH_PROFILE_OPERATION = "OP20ac0191105d4e1c9431d4ee47e753b0"
NATIONAL_DETAILED_TRANSACTIONS_OPERATION = "OPbba3ce1cb8f44bec99877c8e7c36cbaa"
NATIONAL_GRAPHQL_REPLAY_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-language",
        "cache-control",
        "content-type",
        "origin",
        "pragma",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-gpc",
        "session_id",
        "x-disable-legacy",
        "x-user-screen-resolution",
    }
)
JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, dict[str, str | int | None]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


class NationalGraphQLRequestError(RuntimeError):
    def __init__(self, message: str, *, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class NationalGraphQLHttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"National Bank GraphQL request failed with HTTP {status_code}")
        self.status_code = status_code


async def save_session_artifact(user_id: int, artifact) -> None:
    await save_visible_auth_session_artifact_async(user_id, PROVIDER, artifact)


def _national_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _national_clean_header_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text or len(text) > 1000:
        return ""
    return text


def _national_clean_authorization_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "<redacted>" in text or "\n" in text or "\r" in text or len(text) > 5000:
        return ""
    return text


def _national_authorization_access_token(authorization: Any) -> str | None:
    text = _national_clean_authorization_value(authorization)
    if not text:
        return None
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    return _national_any_access_token(text)


def _national_any_access_token(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    candidate = _normalize_access_token(text, allow_opaque=True)
    if candidate:
        return candidate
    if text and text != "<redacted>" and not re.search(r"\s", text):
        return text
    return None


def _national_session_graphql_headers(session_artifact: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(session_artifact, dict):
        return {}
    raw_headers = session_artifact.get("graphql_headers")
    if not isinstance(raw_headers, dict):
        profile = session_artifact.get("graphql_request_profile")
        raw_headers = profile.get("headers") if isinstance(profile, dict) else {}
    if not isinstance(raw_headers, dict):
        raw_headers = {}
    headers: dict[str, str] = {}
    for name in NATIONAL_GRAPHQL_REPLAY_HEADER_NAMES:
        value = _national_clean_header_value(raw_headers.get(name))
        if value:
            headers[name] = value
    session_id = _national_clean_header_value(session_artifact.get("session_id")) or headers.get("session_id", "")
    if session_id:
        headers["session_id"] = session_id
    return headers


def _national_session_id(session_artifact: dict[str, Any] | None) -> str:
    return _national_session_graphql_headers(session_artifact).get("session_id", "")


def _national_session_authorization_header(
    session_artifact: dict[str, Any] | None,
    *,
    access_token: str,
) -> str:
    if not isinstance(session_artifact, dict):
        return ""
    captured = _national_clean_authorization_value(session_artifact.get("graphql_authorization"))
    if captured:
        return captured
    if not access_token or not session_artifact.get("graphql_uses_authorization"):
        return ""
    scheme = str(session_artifact.get("graphql_authorization_scheme") or "").strip().lower()
    return f"Bearer {access_token}" if scheme == "bearer" else access_token


def _national_graphql_replay_headers(
    session_artifact: dict[str, Any] | None,
    *,
    access_token: str,
) -> dict[str, str]:
    captured = _national_session_graphql_headers(session_artifact)
    headers = dict(captured)
    headers["content-type"] = "application/json"
    headers["idempotency-key"] = str(uuid.uuid4())
    authorization = _national_session_authorization_header(session_artifact, access_token=access_token)
    if authorization:
        headers["authorization"] = authorization
    return {name: value for name, value in headers.items() if value}


def _national_clean_graphql_operation_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "<redacted>" or len(text) > 240:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_:-]+", text):
        return ""
    return text


def _national_session_operation_name(
    session_artifact: dict[str, Any] | None,
    key: str,
    fallback: str,
) -> str:
    if isinstance(session_artifact, dict):
        captured = _national_clean_graphql_operation_name(session_artifact.get(key))
        if captured:
            return captured
    return fallback


def _national_session_accounts_request_body(session_artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(session_artifact, dict):
        return None
    body = session_artifact.get("accounts_graphql_request_body")
    if not isinstance(body, dict):
        return None
    if "variables" in body:
        return None
    if "query" not in body and "operationName" not in body:
        return None
    return dict(body)


def _national_replay_material_missing(
    storage_state: dict[str, Any] | None,
    session_artifact: dict[str, Any] | None,
) -> dict[str, bool]:
    access_token = _national_access_token_any(storage_state, session_artifact)
    return {
        "access_token": not bool(access_token),
        "session_id": not bool(_national_session_id(session_artifact)),
        "graphql_authorization": not bool(
            _national_session_authorization_header(session_artifact, access_token=access_token or "")
        ),
        "accounts_graphql_request_body": not bool(_national_session_accounts_request_body(session_artifact)),
    }


def _national_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_NATIONAL_DEBUG",))


def _national_log_event(
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
        debug_enabled=_national_debug_logs_enabled(),
        **fields,
    )


def _national_sanitize_log_text(text: str | None, *, limit: int = 220) -> str:
    return sanitize_scraper_log_text(
        text,
        limit=limit,
        redactions=(
            (JWT_LOG_REDACTION_PATTERN, "<jwt>", 0),
            (EMAIL_LOG_REDACTION_PATTERN, "<email>", re.IGNORECASE),
        ),
    )


def _national_jwt_expiry(token: str | None) -> int | None:
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


def _national_token_is_fresh(token: str | None, *, skew_seconds: int = 60) -> bool:
    if not token:
        return False
    expires_at = _national_jwt_expiry(token)
    if expires_at is None:
        return len(str(token).strip()) > 40
    return expires_at > int(time.time()) + skew_seconds


def _national_token_is_replayable(token: str | None, *, skew_seconds: int = 60) -> bool:
    text = str(token or "").strip()
    if not text or text == "<redacted>" or re.search(r"\s", text):
        return False
    expires_at = _national_jwt_expiry(text)
    if expires_at is None:
        return True
    return expires_at > int(time.time()) + skew_seconds


def _national_token_expires_at_iso(token: str | None) -> str | None:
    expires_at = _national_jwt_expiry(token)
    if not expires_at:
        return None
    return datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_json_candidate(text: str | None) -> Any:
    normalized = str(text or "").strip()
    if not normalized or normalized[:1] not in {"{", "["}:
        return None
    return parse_json_body(normalized)


def _is_access_token_key(key: Any) -> bool:
    normalized = str(key or "").lower()
    return ("access" in normalized and "token" in normalized) or normalized in {
        "authorization",
        "x-authorization",
    }


def _normalize_access_token(value: Any, *, allow_opaque: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower().startswith("bearer "):
        text = text.split(None, 1)[1].strip()
    match = JWT_RE.search(text)
    if match:
        return match.group(0)
    parsed = _parse_json_candidate(text)
    if parsed is not None:
        return _extract_access_token_candidate(parsed)
    if allow_opaque and len(text) > 40 and not re.search(r"\s", text):
        return text
    return None


def _extract_access_token_candidate(value: Any, *, allow_opaque: bool = False) -> str | None:
    if isinstance(value, str):
        return _normalize_access_token(value, allow_opaque=allow_opaque)
    if isinstance(value, dict):
        priority_items = sorted(
            value.items(),
            key=lambda item: 0 if _is_access_token_key(item[0]) else 1,
        )
        for key, nested in priority_items:
            if _is_access_token_key(key):
                candidate = _extract_access_token_candidate(nested, allow_opaque=True)
                if candidate:
                    return candidate
        for nested in value.values():
            candidate = _extract_access_token_candidate(nested)
            if candidate:
                return candidate
        return None
    if isinstance(value, list):
        for nested in value:
            candidate = _extract_access_token_candidate(nested)
            if candidate:
                return candidate
    return None


def _extract_storage_item_access_token(item: dict[str, Any]) -> str | None:
    return _extract_access_token_candidate(
        item.get("value"),
        allow_opaque=_is_access_token_key(item.get("name") or item.get("key")),
    )


def _extract_session_artifact_access_token(session_artifact: dict[str, Any] | None) -> str | None:
    if not isinstance(session_artifact, dict):
        return None
    token = _national_authorization_access_token(session_artifact.get("graphql_authorization"))
    if _national_token_is_replayable(token):
        return token
    token = _national_any_access_token(session_artifact.get("graphql_access_token"))
    if _national_token_is_replayable(token):
        return token
    token = _normalize_access_token(session_artifact.get("access_token"), allow_opaque=True)
    if _national_token_is_fresh(token):
        return token
    raw_token = session_artifact.get("access_token")
    if isinstance(raw_token, str):
        raw_token = raw_token.strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token.split(None, 1)[1].strip()
        if _national_token_is_replayable(raw_token):
            return raw_token
    candidate = _extract_access_token_candidate(session_artifact.get("browser_storage"))
    return candidate if _national_token_is_fresh(candidate) else None


def _extract_storage_state_access_token(storage_state: dict[str, Any] | None) -> str | None:
    if not isinstance(storage_state, dict):
        return None
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if not (host == "app.bnc.ca" or host.endswith(".bnc.ca")):
            continue
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            candidate = _extract_storage_item_access_token(item)
            if _national_token_is_fresh(candidate):
                return candidate
        indexed_db = origin.get("indexedDB")
        candidate = _extract_access_token_candidate(indexed_db)
        if _national_token_is_fresh(candidate):
            return candidate
    return None


def _national_access_token(
    storage_state: dict[str, Any] | None,
    session_artifact: dict[str, Any] | None,
) -> str | None:
    return (
        _extract_session_artifact_access_token(session_artifact)
        or _extract_storage_state_access_token(storage_state)
    )


def _extract_session_artifact_access_token_any(
    session_artifact: dict[str, Any] | None,
) -> str | None:
    if not isinstance(session_artifact, dict):
        return None
    token = _national_authorization_access_token(session_artifact.get("graphql_authorization"))
    if token:
        return token
    token = _national_any_access_token(session_artifact.get("graphql_access_token"))
    if token:
        return token
    token = _normalize_access_token(session_artifact.get("access_token"), allow_opaque=True)
    if token:
        return token
    raw_token = session_artifact.get("access_token")
    if isinstance(raw_token, str):
        raw_token = raw_token.strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token.split(None, 1)[1].strip()
        if raw_token:
            return raw_token
    return _extract_access_token_candidate(session_artifact.get("browser_storage"))


def _extract_storage_state_access_token_any(storage_state: dict[str, Any] | None) -> str | None:
    if not isinstance(storage_state, dict):
        return None
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if not (host == "app.bnc.ca" or host.endswith(".bnc.ca")):
            continue
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            candidate = _extract_storage_item_access_token(item)
            if candidate:
                return candidate
        indexed_db = origin.get("indexedDB")
        candidate = _extract_access_token_candidate(indexed_db)
        if candidate:
            return candidate
    return None


def _national_access_token_any(
    storage_state: dict[str, Any] | None,
    session_artifact: dict[str, Any] | None,
) -> str | None:
    return (
        _extract_session_artifact_access_token_any(session_artifact)
        or _extract_storage_state_access_token_any(storage_state)
    )


def _national_response_requires_auth(status: int, url: str, payload: Any) -> bool:
    if status in {401, 403}:
        return True
    host = (urlsplit(str(url or "")).hostname or "").lower()
    path = (urlsplit(str(url or "")).path or "").lower()
    if host == "connexion.bnc.ca" or "/login" in path or "/moduleauth" in path:
        return True
    text = _national_sanitize_log_text(json.dumps(payload, default=str), limit=700).lower()
    return any(
        token in text
        for token in (
            "unauthorized",
            "forbidden",
            "not authenticated",
            "access token",
            "token expired",
            "session expired",
            "login required",
        )
    )


def _national_graphql_error_requires_auth(errors: list[Any]) -> bool:
    text = _national_sanitize_log_text(json.dumps(errors, default=str), limit=700).lower()
    return any(
        token in text
        for token in (
            "unauthorized",
            "forbidden",
            "not authenticated",
            "access token",
            "token expired",
            "session expired",
            "login required",
        )
    )


async def _national_graphql_request(
    replay: SavedArtifactHttpClient,
    *,
    operation_name: str,
    variables: dict[str, Any] | None,
    access_token: str,
    session_artifact: dict[str, Any] | None,
    request_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if request_body is None:
        request_body = {"operationName": operation_name}
        if variables is not None:
            request_body["variables"] = variables
    else:
        request_body = dict(request_body)
    response = await replay.post(
        NATIONAL_GRAPHQL_URL,
        headers=_national_graphql_replay_headers(session_artifact, access_token=access_token),
        json=request_body,
        include_cookie_header=False,
        refresh_cookies=False,
    )
    payload = parse_json_body(response.text)
    status = int(response.status_code)
    if _national_response_requires_auth(status, str(response.url), payload):
        raise PermissionError("National Bank auth required")
    if status >= 400:
        raise NationalGraphQLHttpError(status)
    if not isinstance(payload, dict):
        raise RuntimeError("National Bank GraphQL returned an invalid response")
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        if _national_graphql_error_requires_auth(errors):
            raise PermissionError("National Bank auth required")
        raise NationalGraphQLRequestError(
            _national_sanitize_log_text(json.dumps(errors, default=str), limit=240)
            or "National Bank GraphQL request returned errors",
            errors=errors,
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("National Bank GraphQL returned no data")
    return data


def _national_provider_account_id(raw: dict[str, Any]) -> str | None:
    for key in ("key", "guid", "accountNumber", "referenceId", "primaryCardReferenceId"):
        value = raw.get(key)
        if value:
            return str(value).strip()
    return None


def _national_account_external_id(raw: dict[str, Any]) -> str | None:
    value = raw.get("external_id")
    if value:
        return str(value).strip()
    provider_account_id = _national_provider_account_id(raw)
    if provider_account_id:
        return f"account:{provider_account_id}"
    return None


def _national_flatten_accounts(accounts: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        record = dict(account)
        sub_accounts = record.pop("subAccounts", None)
        record["provider_account_id"] = _national_provider_account_id(record)
        record["external_id"] = _national_account_external_id(record)
        flattened.append(record)
        for sub_account in sub_accounts or []:
            if not isinstance(sub_account, dict):
                continue
            child = dict(sub_account)
            child["_parent_key"] = record.get("key")
            child["provider_account_id"] = _national_provider_account_id(child)
            child["external_id"] = _national_account_external_id(child)
            flattened.append(child)
    return flattened


async def _national_fetch_accounts(
    replay: SavedArtifactHttpClient,
    *,
    access_token: str,
    session_artifact: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    data = await _national_graphql_request(
        replay,
        operation_name=NATIONAL_ACCOUNTS_WITH_PROFILE_OPERATION,
        variables=None,
        access_token=access_token,
        session_artifact=session_artifact,
        request_body=_national_session_accounts_request_body(session_artifact),
    )
    payload = data.get("accountsWithProductProfile")
    if not isinstance(payload, dict):
        return []
    accounts = _national_flatten_accounts(payload.get("items") or [])
    status_code = ((payload.get("responseDetails") or {}).get("statusCode")) if isinstance(payload.get("responseDetails"), dict) else None
    for account in accounts:
        account["_accounts_status_code"] = status_code
        account["_product_profile"] = payload.get("productProfile") if isinstance(payload.get("productProfile"), dict) else {}
    return accounts


def _national_apply_sync_window(account: dict[str, Any], sync_windows: dict[str, Any]) -> None:
    external_id = str(account.get("external_id") or "").strip()
    raw_window = sync_windows.get(external_id) if external_id else None
    if isinstance(raw_window, dict):
        account["sync_window"] = dict(raw_window)
    elif isinstance(raw_window, str):
        account["sync_window"] = {"mode": raw_window}
    else:
        account["sync_window"] = {"mode": "backfill"}


def _national_account_window(account: dict[str, Any]) -> tuple[str, date | None, date]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    mode = str(sync_window.get("mode") or "backfill")
    end_date = sync_window_date(sync_window, "end_date") or date.today()
    start_date = sync_window_date(sync_window, "start_date")
    if mode != "backfill" and start_date is None:
        start_date = end_date
    if start_date and start_date > end_date:
        start_date = end_date
    return mode, start_date, end_date


def _national_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(start_date, current_end - timedelta(days=NATIONAL_BACKFILL_CHUNK_DAYS - 1))
        windows.append((current_start, current_end))
        if current_start <= start_date:
            break
        current_end = current_start - timedelta(days=1)
    return windows


def _national_pending_backfill_windows(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> list[tuple[date, date]]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    windows = pending_backfill_date_windows(sync_window)
    return windows or _national_split_windows(history_start, history_end)


async def _record_national_transaction_window(
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
        _national_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_national_sanitize_log_text(str(exc), limit=140),
        )


def _national_transaction_input_candidates(
    account_key: str,
    *,
    start_date: date,
    end_date: date,
    include_undated: bool,
) -> list[dict[str, Any]]:
    browser_range = {
        "queryParams": {
            "sorting": [
                {
                    "fieldName": "effectiveDate",
                    "ascending": False,
                }
            ]
        },
        "fromDate": start_date.isoformat(),
        "toDate": end_date.isoformat(),
    }
    scoped_browser_range = {
        "accountKey": account_key,
        **browser_range,
    }
    period = {
        "name": "PERSONALIZED",
        "dateFrom": start_date.isoformat(),
        "dateTo": end_date.isoformat(),
    }
    dated = [
        browser_range,
        scoped_browser_range,
        {
            "accountKey": account_key,
            "dateFrom": start_date.isoformat(),
            "dateTo": end_date.isoformat(),
        },
        {
            "accountKey": account_key,
            "fromDate": start_date.isoformat(),
            "toDate": end_date.isoformat(),
        },
        {
            "accountKey": account_key,
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        },
        {
            "accountKey": account_key,
            "period": period,
            "category": "ALL",
        },
        {
            "accountKey": account_key,
            "filter": {
                "period": period,
                "category": "ALL",
            },
        },
        {
            "accountKey": account_key,
            "transactionFilter": {
                "period": period,
                "category": "ALL",
            },
        },
    ]
    if include_undated:
        dated.append({"accountKey": account_key})
    return dated


async def _national_fetch_transactions_window(
    replay: SavedArtifactHttpClient,
    *,
    account: dict[str, Any],
    access_token: str,
    session_artifact: dict[str, Any] | None,
    start_date: date,
    end_date: date,
    include_undated: bool,
    user_id: int | None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    account_key = str(account.get("key") or "").strip()
    if not account_key:
        return [], False, False

    last_schema_error = ""
    for request_input in _national_transaction_input_candidates(
        account_key,
        start_date=start_date,
        end_date=end_date,
        include_undated=include_undated,
    ):
        try:
            operation_name = _national_session_operation_name(
                session_artifact,
                "transactions_graphql_operation_name",
                NATIONAL_DETAILED_TRANSACTIONS_OPERATION,
            )
            data = await _national_graphql_request(
                replay,
                operation_name=operation_name,
                variables={"transactionsRequestInput": request_input},
                access_token=access_token,
                session_artifact=session_artifact,
            )
        except NationalGraphQLRequestError as exc:
            last_schema_error = _national_sanitize_log_text(str(exc), limit=180)
            continue
        except NationalGraphQLHttpError as exc:
            if exc.status_code in {400, 422}:
                last_schema_error = _national_sanitize_log_text(str(exc), limit=180)
                continue
            raise

        payload = data.get("detailedTransactions")
        if not isinstance(payload, dict):
            return [], False, False
        rows = []
        request_has_account_key = bool(request_input.get("accountKey"))
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            row_account_key = str(item.get("accountKey") or "").strip()
            if row_account_key and row_account_key != account_key:
                continue
            if not row_account_key and not request_has_account_key:
                continue
            rows.append(dict(item))
        for row in rows:
            row["_account_external_id"] = account.get("external_id")
            row["_account_currency"] = account.get("currency") or account.get("primaryCurrency") or "CAD"
        undated = set(request_input.keys()) == {"accountKey"}
        return rows, True, undated

    if last_schema_error:
        _national_log_event(
            "transaction request shape rejected",
            level="warning",
            debug=True,
            user_id=user_id,
            account_key=bool(account_key),
            message=last_schema_error,
        )
    return [], False, False


async def _national_fetch_account_transactions(
    replay: SavedArtifactHttpClient,
    account: dict[str, Any],
    *,
    access_token: str,
    session_artifact: dict[str, Any] | None,
    user_id: int | None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if not account.get("key"):
        return [], False

    mode, planned_start, planned_end = _national_account_window(account)
    if mode != "backfill":
        start_date = planned_start or planned_end
        await _record_national_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=start_date,
            end_date=planned_end,
        )
        try:
            rows, succeeded, _undated = await _national_fetch_transactions_window(
                replay,
                account=account,
                access_token=access_token,
                session_artifact=session_artifact,
                start_date=start_date,
                end_date=planned_end,
                include_undated=True,
                user_id=user_id,
            )
        except Exception as exc:
            await _record_national_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=start_date,
                end_date=planned_end,
                error=str(exc),
            )
            raise
        await _record_national_transaction_window(
            transaction_window_recorder,
            event="completed" if succeeded else "failed",
            account=account,
            mode=mode,
            start_date=start_date,
            end_date=planned_end,
            transactions=rows,
            error=None if succeeded else "National Bank transaction window failed",
        )
        return rows, succeeded

    history_floor = planned_end - timedelta(days=NATIONAL_MAX_HISTORY_DAYS)
    collected: list[dict[str, Any]] = []

    for current_start, current_end in _national_pending_backfill_windows(
        account,
        history_floor,
        planned_end,
    ):
        await _record_national_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
        )
        try:
            rows, succeeded, undated = await _national_fetch_transactions_window(
                replay,
                account=account,
                access_token=access_token,
                session_artifact=session_artifact,
                start_date=current_start,
                end_date=current_end,
                include_undated=not collected,
                user_id=user_id,
            )
        except Exception as exc:
            await _record_national_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error=str(exc),
            )
            raise
        if not succeeded:
            await _record_national_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error="National Bank transaction window failed",
            )
            return collected, False
        collected.extend(rows)
        await _record_national_transaction_window(
            transaction_window_recorder,
            event="completed",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
            transactions=rows,
        )
        if undated:
            return collected, True

    return collected, True


async def _sync_national_with_saved_artifacts(
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
    user_agent = _national_session_user_agent(session_state)
    access_token = (
        _national_access_token(storage_state, session_state)
        or _national_access_token_any(storage_state, session_state)
    )
    session_id = _national_session_id(session_state)
    missing_replay_material = _national_replay_material_missing(storage_state, session_state)

    _national_log_event(
        "saved-session sync start",
        user_id=user_id,
        engine="direct",
        debug=True,
        has_access_token=bool(access_token),
        has_session_id=bool(session_id),
        has_graphql_headers=bool(_national_session_graphql_headers(session_state)),
        has_graphql_authorization=not missing_replay_material["graphql_authorization"],
        has_accounts_graphql_request_body=not missing_replay_material["accounts_graphql_request_body"],
    )
    if any(missing_replay_material.values()):
        _national_log_event(
            "saved-session sync auth_required",
            level="warning",
            user_id=user_id,
            engine="direct",
            message="saved National Bank artifact is missing GraphQL replay auth material",
            missing_replay_material=sorted(key for key, missing in missing_replay_material.items() if missing),
        )
        return None

    async with SavedArtifactHttpClient(
        storage_state,
        session_state,
        user_agent=user_agent,
        timeout=NATIONAL_DIRECT_TIMEOUT_SECONDS,
        follow_redirects=True,
        seed_cookies=False,
    ) as replay:
        try:
            accounts = await _national_fetch_accounts(
                replay,
                access_token=access_token,
                session_artifact=session_state,
            )
        except PermissionError:
            _national_log_event(
                "saved-session sync auth_required",
                level="warning",
                user_id=user_id,
                engine="direct",
                message="accounts GraphQL request returned auth required",
            )
            return None
        if not accounts:
            return {"status": "error", "message": "No National Bank accounts returned"}

        sync_windows = await mode_resolver(accounts) if mode_resolver and sync_scope != "accounts" else {}
        for account in accounts:
            _national_apply_sync_window(account, sync_windows)

        raw_transactions: dict[str, list[dict[str, Any]]] = {}
        transaction_fetch_succeeded_accounts: set[str] = set()
        transaction_fetch_failed_accounts: set[str] = set()
        if sync_scope != "accounts":
            for account in accounts:
                external_id = str(account.get("external_id") or "").strip()
                if not external_id:
                    continue
                try:
                    transactions, succeeded = await _national_fetch_account_transactions(
                        replay,
                        account,
                        access_token=access_token,
                        session_artifact=session_state,
                        user_id=user_id,
                        transaction_window_recorder=transaction_window_recorder,
                    )
                except PermissionError:
                    _national_log_event(
                        "saved-session sync auth_required",
                        level="warning",
                        user_id=user_id,
                        engine="direct",
                        message="transactions GraphQL request returned auth required",
                    )
                    return None
                except Exception as exc:
                    _national_log_event(
                        "transaction fetch failed",
                        level="warning",
                        user_id=user_id,
                        account_type=account.get("type"),
                        error=type(exc).__name__,
                        message=_national_sanitize_log_text(str(exc), limit=180),
                    )
                    raw_transactions[external_id] = []
                    transaction_fetch_failed_accounts.add(external_id)
                    continue
                raw_transactions[external_id] = transactions
                if succeeded:
                    transaction_fetch_succeeded_accounts.add(external_id)
                else:
                    transaction_fetch_failed_accounts.add(external_id)

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        transaction_count = sum(len(items or []) for items in raw_transactions.values())
        if accounts and not transaction_fetch_succeeded_accounts and transaction_fetch_failed_accounts:
            message = "National Bank transaction fetch failed before any accounts could be synced."
            _national_log_event(
                "saved-session sync transaction fetch failed",
                level="warning",
                user_id=user_id,
                engine="direct",
                accounts=len(accounts),
                failed_transaction_accounts=len(transaction_fetch_failed_accounts),
                transactions=transaction_count,
                duration_ms=duration_ms,
                message=message,
            )
            return {
                "status": "error",
                "message": message,
                "accounts": accounts,
                "transactions": raw_transactions,
                "transaction_fetch_succeeded_accounts": [],
                "transaction_fetch_failed_accounts": sorted(transaction_fetch_failed_accounts),
            }

        session_extra = {
            "access_token": access_token,
            "token_expires_at": _national_token_expires_at_iso(access_token),
            "session_id": session_id,
            "graphql_headers": _national_session_graphql_headers(session_state),
            "graphql_uses_authorization": bool(session_state.get("graphql_uses_authorization")),
            "graphql_authorization_scheme": session_state.get("graphql_authorization_scheme"),
            "browser_profile": {"user_agent": user_agent},
        }
        graphql_authorization = _national_clean_authorization_value(session_state.get("graphql_authorization"))
        if graphql_authorization:
            session_extra["graphql_authorization"] = graphql_authorization
        accounts_request_body = _national_session_accounts_request_body(session_state)
        if accounts_request_body:
            session_extra["accounts_graphql_request_body"] = accounts_request_body
        transactions_operation_name = _national_clean_graphql_operation_name(
            session_state.get("transactions_graphql_operation_name")
        )
        if transactions_operation_name:
            session_extra["transactions_graphql_operation_name"] = transactions_operation_name
        session_payload = replay.refreshed_session_artifact(extra=session_extra)
        if replay_session is not None:
            await replay_session.refresh_runtime_artifacts_async(storage_state, session_artifact=session_payload)
        elif user_id is not None:
            await save_session_artifact(
                user_id,
                session_payload,
            )

    _national_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="direct",
        accounts=len(accounts),
        transaction_accounts=len(transaction_fetch_succeeded_accounts),
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
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    _national_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_national_session_user_agent,
        log_event=_national_log_event,
    )
    if not isinstance(replay_session.storage_state, dict):
        return replay_session.missing_artifact_result()

    try:
        payload = await _sync_national_with_saved_artifacts(
            replay_session.storage_state,
            replay_session.session_artifact,
            user_id=user_id,
            mode_resolver=mode_resolver,
            sync_scope=sync_scope,
            transaction_window_recorder=transaction_window_recorder,
            replay_session=replay_session,
        )
    except Exception as exc:
        if replay_session.exception_is_network_error(exc):
            raise
        _national_log_event(
            "headless sync failed",
            level="warning",
            user_id=user_id,
            error=type(exc).__name__,
            message=_national_sanitize_log_text(str(exc), limit=180),
        )
        return scraper_error(_national_sanitize_log_text(str(exc), limit=180))
    if payload is None:
        return replay_session.auth_required_result(log_message="saved National Bank artifacts did not validate")
    return replay_session.normalize_result(payload)
