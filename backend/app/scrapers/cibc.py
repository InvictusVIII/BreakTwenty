from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    parse_json_body,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_auth_required
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)
from app.services.replay_diagnostics import ReplayDiagnosticsCapture, begin_replay_diagnostics
from app.services.runtime_state import (
    get_runtime_state,
    pop_runtime_state,
)

PROVIDER = "cibc"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
CIBC_DIAGNOSTIC_PROVIDER_DIR = "CIBC"
logger = logging.getLogger("breaktwenty.scrapers.cibc")

CIBC_ORIGIN = "https://www.cibconline.cibc.com"
CIBC_APP_SHELL_URL = f"{CIBC_ORIGIN}/ebm-resources/public/banking/cibc/client/web/index.html"
CIBC_ACCOUNTS_URL = f"{CIBC_ORIGIN}/ebm-resources/online-banking/accounts/client/index.html"
CIBC_ACCOUNTS_API_URL = f"{CIBC_ORIGIN}/ebm-ai/api/v2/json/accounts"
CIBC_ACCOUNT_DETAILS_API_URL_TEMPLATE = f"{CIBC_ORIGIN}/ebm-ai/api/v1/json/accountDetails/{{account_id}}"
CIBC_TRANSACTIONS_API_URL = f"{CIBC_ORIGIN}/ebm-ai/api/v1/json/transactions"
CIBC_TRANSACTION_LIMIT = 150
CIBC_BACKFILL_CHUNK_DAYS = 365
CIBC_EMPTY_HISTORY_STOP_CHUNKS = 2
CIBC_FALLBACK_MAX_HISTORY_DAYS = 366 * 8

ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


def _cibc_visible_auth_attempt_id(user_id: int | None) -> str:
    if user_id is None:
        return ""
    attempt_state = get_runtime_state(VISIBLE_AUTH_ATTEMPT_NAMESPACE, user_id)
    if not isinstance(attempt_state, dict):
        return ""
    return str(attempt_state.get("attempt_id") or "").strip()


def _extract_cibc_auth_token_from_value(value: Any, *, depth: int = 0) -> str:
    if value is None or depth > 6:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("ebkpce."):
            return text
        if text.startswith("{") or text.startswith("["):
            parsed = _parse_cibc_json_body(text)
            if parsed is not None:
                return _extract_cibc_auth_token_from_value(parsed, depth=depth + 1)
        return ""
    if isinstance(value, dict):
        for key in (
            "authenticationToken",
            "authToken",
            "sessionToken",
            "x-auth-token",
            "X-Auth-Token",
            "token",
        ):
            candidate = _extract_cibc_auth_token_from_value(value.get(key), depth=depth + 1)
            if candidate:
                return candidate
        for nested in value.values():
            candidate = _extract_cibc_auth_token_from_value(nested, depth=depth + 1)
            if candidate:
                return candidate
        return ""
    if isinstance(value, list):
        for nested in value:
            candidate = _extract_cibc_auth_token_from_value(nested, depth=depth + 1)
            if candidate:
                return candidate
    return ""


def _extract_cibc_device_id_from_value(value: Any, *, current_key: str = "", depth: int = 0) -> str:
    if value is None or depth > 6:
        return ""
    key_lower = str(current_key or "").strip().lower()
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            parsed = _parse_cibc_json_body(text)
            if parsed is not None:
                return _extract_cibc_device_id_from_value(parsed, current_key=current_key, depth=depth + 1)
        if key_lower and ("deviceid" in key_lower or "device-id" in key_lower):
            return text
        return ""
    if isinstance(value, dict):
        preferred_keys = [
            key
            for key in value.keys()
            if "deviceid" in str(key).strip().lower() or "device-id" in str(key).strip().lower()
        ]
        for key in preferred_keys:
            candidate = _extract_cibc_device_id_from_value(value.get(key), current_key=str(key), depth=depth + 1)
            if candidate:
                return candidate
        for key, nested in value.items():
            candidate = _extract_cibc_device_id_from_value(nested, current_key=str(key), depth=depth + 1)
            if candidate:
                return candidate
        return ""
    if isinstance(value, list):
        for nested in value:
            candidate = _extract_cibc_device_id_from_value(nested, current_key=current_key, depth=depth + 1)
            if candidate:
                return candidate
    return ""


def _cibc_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _cibc_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _extract_cibc_storage_state_device_id(storage_state: dict[str, Any] | None) -> str:
    if not isinstance(storage_state, dict):
        return ""
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip().lower()
        value = str(cookie.get("value") or "").strip()
        if value and name.startswith("ab.storage.deviceid."):
            return value
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            device_id = _extract_cibc_device_id_from_value(
                entry.get("value"),
                current_key=str(entry.get("name") or ""),
            )
            if device_id:
                return device_id
    return ""


def _extract_cibc_storage_state_auth_token(storage_state: dict[str, Any] | None) -> str:
    if not isinstance(storage_state, dict):
        return ""
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            token = _extract_cibc_auth_token_from_value(entry.get("value"))
            if token:
                return token
    return ""


def _resolve_cibc_auth_token(
    storage_state: dict[str, Any] | None,
    session_artifact: dict[str, Any] | None,
) -> str:
    if isinstance(session_artifact, dict):
        auth_token = str(session_artifact.get("auth_token") or "").strip()
        if auth_token:
            return auth_token
    return _extract_cibc_storage_state_auth_token(storage_state)


def _resolve_cibc_device_id(
    storage_state: dict[str, Any] | None,
    session_artifact: dict[str, Any] | None,
) -> str:
    if isinstance(session_artifact, dict):
        device_id = str(session_artifact.get("device_id") or "").strip()
        if device_id:
            return device_id
    return _extract_cibc_storage_state_device_id(storage_state)


def _cibc_direct_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    api_profile: str,
    user_agent: str | None,
    auth_token: str | None,
    device_id: str | None,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json" if api_profile == "bootstrap" else "application/vnd.api+json",
        "Accept-Language": "en",
        "Brand": "cibc",
        "Cache-Control": "no-cache",
        "Client-Type": "DEFAULT_WEB" if api_profile == "bootstrap" else "default_web",
        "Pragma": "no-cache",
        "Referer": CIBC_ACCOUNTS_URL if api_profile == "bootstrap" else CIBC_APP_SHELL_URL,
        "User-Agent": _cibc_user_agent(user_agent),
    }
    if api_profile != "bootstrap":
        headers["Content-Type"] = "application/vnd.api+json"
        headers["Origin"] = CIBC_ORIGIN
        headers["X-Requested-With"] = "XMLHttpRequest"
    if auth_token:
        headers["X-Auth-Token"] = auth_token
    if api_profile != "bootstrap" and device_id:
        headers["X-Device-Id"] = device_id
    return headers


async def _cibc_direct_request(
    storage_state: dict[str, Any],
    method: str,
    url: str,
    *,
    api_profile: str = "app",
    auth_token: str | None = None,
    device_id: str | None = None,
    user_agent: str | None = None,
    params: dict[str, str] | None = None,
    json_body: Any | None = None,
) -> dict[str, Any]:
    headers = _cibc_direct_headers(
        storage_state,
        url,
        api_profile=api_profile,
        user_agent=user_agent,
        auth_token=auth_token,
        device_id=device_id,
    )
    return await SavedArtifactHttpClient.request_once(
        storage_state,
        method,
        url,
        user_agent=_cibc_user_agent(user_agent),
        headers=headers,
        timeout=30,
        seed_cookies=True,
        parse_json=_parse_cibc_json_body,
        params=params,
        json=json_body,
    )


def _parse_cibc_json_body(body_text: str | None):
    return parse_json_body(body_text)


def _cibc_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_CIBC_DEBUG",))


def _cibc_log_event(stage: str, *, level: str = "info", debug: bool = False, **fields: Any) -> None:
    log_scraper_event(
        logger,
        provider=PROVIDER,
        stage=stage,
        level=level,
        debug=debug,
        debug_enabled=_cibc_debug_logs_enabled(),
        **fields,
    )


def _cibc_sanitize_log_text(text: str | None, *, limit: int = 220) -> str:
    return sanitize_scraper_log_text(
        text,
        limit=limit,
        redactions=(
            (r"ebkpce\.[A-Za-z0-9._-]+", "<token>", 0),
            (
                r'(["\']?(?:password|cardNumber|card_number|verificationCode|otvc|token|authToken|authenticationToken|authorization)["\']?\s*[:=]\s*)["\'][^"\']+["\']',
                r"\1<redacted>",
                re.IGNORECASE,
            ),
        ),
    )


def _cibc_response_requires_auth(response: dict[str, Any]) -> bool:
    status = int(response.get("status") or 0)
    text = str(response.get("text") or "").lower()
    if _cibc_response_is_policy_violation(response):
        return False
    if status in (401, 403):
        return True
    if status in (0, 302):
        return True
    return "sign on to cibc" in text or "unauthorized" in text


def _cibc_response_is_policy_violation(response: dict[str, Any]) -> bool:
    if int(response.get("status") or 0) != 403:
        return False
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return False
    problems = payload.get("problems")
    if not isinstance(problems, list):
        return False
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        details = problem.get("details") if isinstance(problem.get("details"), dict) else {}
        if str(problem.get("code") or "") == "0001" and str(details.get("reason") or "") == "policy-violation":
            return True
    return False


def _cibc_transaction_like(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {str(key).lower() for key in value}
    has_date = bool(keys & {"date", "transactiondate", "posteddate", "effectivedate", "bookingdate", "bookingdatetime"})
    has_amount = bool(
        keys
        & {
            "amount",
            "transactionamount",
            "withdrawalamt",
            "withdrawalamount",
            "debit",
            "debitsortvalue",
            "debitamount",
            "depositamt",
            "depositamount",
            "credit",
            "creditsortvalue",
            "creditamount",
        }
    )
    has_description = bool(keys & {"description", "transactiondescription", "descriptionline1", "descriptionline2", "memo", "details", "name", "merchantname"})
    return has_date and (has_amount or has_description)


def _cibc_find_transaction_list(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        dict_items = [item for item in value if isinstance(item, dict)]
        if dict_items and any(_cibc_transaction_like(item) for item in dict_items):
            return dict_items
        for item in value:
            nested = _cibc_find_transaction_list(item)
            if nested is not None:
                return nested
    if isinstance(value, dict):
        for key in ("transactions", "items", "results", "data", "records"):
            nested = value.get(key)
            if isinstance(nested, list):
                found = _cibc_find_transaction_list(nested)
                if found is not None:
                    return found
        for nested in value.values():
            found = _cibc_find_transaction_list(nested)
            if found is not None:
                return found
    return None


def _cibc_transactions_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, dict):
        problems = payload.get("problems")
        if isinstance(problems, list) and any(str(problem.get("code") or "") == "0121" for problem in problems if isinstance(problem, dict)):
            return []
    return _cibc_find_transaction_list(payload)


def _cibc_can_fetch_transactions(account: dict[str, Any]) -> bool:
    account_id = account.get("id") or account.get("accountId") or account.get("provider_account_id")
    if not account_id:
        return False
    raw_capabilities = account.get("capabilities") or []
    if isinstance(raw_capabilities, str):
        raw_capabilities = [raw_capabilities]
    capabilities = {str(value).upper() for value in raw_capabilities}
    if capabilities and not ({"VIEW_TRANSACTIONS", "VIEW_TRANSACTIONS_MONTHLY", "DOWNLOAD_TRANSACTIONS"} & capabilities):
        return False
    return True


def _cibc_account_open_date(account: dict[str, Any]) -> date | None:
    candidates: list[Any] = [
        account.get("openDate"),
        account.get("openedDate"),
        account.get("accountOpeningDate"),
        account.get("openingDate"),
    ]
    details = account.get("details")
    if isinstance(details, dict):
        candidates.extend(
            (
                details.get("openDate"),
                details.get("openedDate"),
                details.get("accountOpeningDate"),
                details.get("openingDate"),
            )
        )

    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            continue
    return None


def _cibc_apply_sync_window(account: dict[str, Any], sync_windows: dict[str, Any]) -> None:
    external_id = str(account.get("external_id") or "").strip()
    entry = sync_windows.get(external_id) if external_id else None
    account["sync_window"] = dict(entry) if isinstance(entry, dict) else {"mode": "backfill"}


def _cibc_transaction_window(account: dict[str, Any]) -> tuple[str, date, date]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    mode = str(sync_window.get("mode") or "backfill")
    end_date = sync_window_date(sync_window, "end_date") or date.today()
    opened_date = _cibc_account_open_date(account)

    if mode != "backfill":
        start_date = sync_window_date(sync_window, "start_date") or end_date
        if opened_date and opened_date > start_date:
            start_date = opened_date
    else:
        start_date = end_date - timedelta(days=CIBC_FALLBACK_MAX_HISTORY_DAYS)
        if opened_date and opened_date > start_date:
            start_date = opened_date

    if start_date > end_date:
        start_date = end_date
    return mode, start_date, end_date


def _cibc_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(
            start_date,
            current_end - timedelta(days=CIBC_BACKFILL_CHUNK_DAYS - 1),
        )
        windows.append((current_start, current_end))
        if current_start <= start_date:
            break
        current_end = current_start - timedelta(days=1)
    return windows


def _cibc_pending_backfill_windows(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> tuple[list[tuple[date, date]], bool]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    windows = pending_backfill_date_windows(sync_window)
    if windows:
        return windows, True
    return _cibc_split_windows(history_start, history_end), False


async def _record_cibc_transaction_window(
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
        _cibc_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_cibc_sanitize_log_text(str(exc), limit=140),
        )


def _cibc_accounts_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        data = payload.get("data")
        accounts = data if isinstance(data, list) else []

    result: list[dict[str, Any]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        record = dict(account)
        account_id = record.get("id") or record.get("accountId")
        if account_id:
            record["provider_account_id"] = str(account_id)
            record["external_id"] = f"account:{account_id}"
        result.append(record)
    return result


def _cibc_record_accounts_fetch(
    diagnostics: ReplayDiagnosticsCapture | None,
    *,
    stage: str,
    response: dict[str, Any],
    api_profile: str,
) -> None:
    if not diagnostics:
        return
    payload = response.get("payload")
    diagnostics.record_http_fetch(
        stage=stage,
        method="GET",
        url=CIBC_ACCOUNTS_API_URL,
        status=response.get("status"),
        payload=payload,
        records=_cibc_accounts_from_payload(payload),
        record_source="accounts",
        window={"api_profile": api_profile},
    )


async def _fetch_cibc_account_details_direct(
    storage_state: dict[str, Any],
    account: dict[str, Any],
    *,
    auth_token: str | None,
    device_id: str | None,
    user_agent: str | None,
    diagnostics: ReplayDiagnosticsCapture | None = None,
) -> dict[str, Any] | None:
    account_id = account.get("id") or account.get("accountId") or account.get("provider_account_id")
    if not account_id:
        return None
    url = CIBC_ACCOUNT_DETAILS_API_URL_TEMPLATE.format(account_id=account_id)
    response = await _cibc_direct_request(
        storage_state,
        "GET",
        url,
        auth_token=auth_token,
        device_id=device_id,
        user_agent=user_agent,
    )
    payload = response.get("payload")
    details_payload = payload.get("accountDetails") if isinstance(payload, dict) else None
    if diagnostics:
        diagnostics.record_http_fetch(
            stage="account.details",
            method="GET",
            url=url,
            status=response.get("status"),
            payload=payload,
            records=[details_payload] if isinstance(details_payload, dict) else [],
            record_source="accountDetails",
            account_ref=account_id,
        )
    if _cibc_response_requires_auth(response):
        raise PermissionError("CIBC auth required")
    if int(response.get("status") or 0) >= 400:
        return None
    if not isinstance(payload, dict):
        return None
    details = details_payload
    return details if isinstance(details, dict) else None


async def _fetch_cibc_transactions_for_account_direct(
    storage_state: dict[str, Any],
    account: dict[str, Any],
    *,
    auth_token: str | None,
    device_id: str | None,
    user_agent: str | None,
    diagnostics: ReplayDiagnosticsCapture | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[list[dict[str, Any]] | None, bool]:
    account_id = account.get("id") or account.get("accountId") or account.get("provider_account_id")
    if not account_id:
        return None, False

    async def fetch_window(start_date: date, end_date: date) -> tuple[list[dict[str, Any]] | None, bool]:
        offset = 0
        collected: list[dict[str, Any]] = []
        while True:
            query = {
                "accountId": str(account_id),
                "filterBy": "range",
                "fromDate": start_date.isoformat(),
                "interaction": "",
                "lastFilterBy": "range",
                "limit": str(CIBC_TRANSACTION_LIMIT),
                "lowerLimitAmount": "",
                "offset": str(offset),
                "sortAsc": "true",
                "sortByField": "date",
                "toDate": end_date.isoformat(),
                "transactionLocation": "",
                "transactionType": "",
                "upperLimitAmount": "",
            }
            response = await _cibc_direct_request(
                storage_state,
                "GET",
                CIBC_TRANSACTIONS_API_URL,
                auth_token=auth_token,
                device_id=device_id,
                user_agent=user_agent,
                params=query,
            )
            status = int(response.get("status") or 0)
            payload = response.get("payload")
            transactions = _cibc_transactions_from_payload(payload)
            if diagnostics:
                diagnostics.record_http_fetch(
                    stage="transactions.window",
                    method="GET",
                    url=CIBC_TRANSACTIONS_API_URL,
                    status=status,
                    params=query,
                    payload=payload,
                    records=transactions if isinstance(transactions, list) else [],
                    record_source="transactions",
                    account_ref=account_id,
                    window={
                        "fromDate": start_date.isoformat(),
                        "toDate": end_date.isoformat(),
                        "offset": offset,
                        "limit": CIBC_TRANSACTION_LIMIT,
                    },
                )
            if _cibc_response_requires_auth(response):
                raise PermissionError("CIBC auth required")

            if transactions is None:
                if status == 204:
                    transactions = []
                elif status >= 500:
                    return None, False
                elif status in (400, 404, 422):
                    transactions = []
                else:
                    return None, False

            for row in transactions:
                record = dict(row)
                record["_account_currency"] = account.get("currency") or "CAD"
                collected.append(record)

            if len(transactions) < CIBC_TRANSACTION_LIMIT:
                break
            offset += CIBC_TRANSACTION_LIMIT
        return collected, True

    mode, history_start, history_end = _cibc_transaction_window(account)
    if mode != "backfill":
        await _record_cibc_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=history_start,
            end_date=history_end,
        )
        try:
            raw_transactions, succeeded = await fetch_window(history_start, history_end)
        except Exception as exc:
            await _record_cibc_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=history_start,
                end_date=history_end,
                error=str(exc),
            )
            raise
        await _record_cibc_transaction_window(
            transaction_window_recorder,
            event="completed" if succeeded else "failed",
            account=account,
            mode=mode,
            start_date=history_start,
            end_date=history_end,
            transactions=raw_transactions or [],
            error=None if succeeded else "CIBC transaction window failed",
        )
        return raw_transactions, succeeded

    windows, planned_windows = _cibc_pending_backfill_windows(
        account,
        history_start,
        history_end,
    )
    seen_activity = False
    consecutive_empty_chunks = 0
    collected: list[dict[str, Any]] = []
    account_fetch_succeeded = True

    for current_start, current_end in windows:
        await _record_cibc_transaction_window(
            transaction_window_recorder,
            event="started",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
        )
        try:
            raw_transactions, chunk_succeeded = await fetch_window(current_start, current_end)
        except Exception as exc:
            await _record_cibc_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error=str(exc),
            )
            raise
        if not chunk_succeeded:
            await _record_cibc_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error="CIBC transaction window failed",
            )
            account_fetch_succeeded = False
            break

        await _record_cibc_transaction_window(
            transaction_window_recorder,
            event="completed",
            account=account,
            mode=mode,
            start_date=current_start,
            end_date=current_end,
            transactions=raw_transactions or [],
        )
        if raw_transactions:
            seen_activity = True
            consecutive_empty_chunks = 0
            collected.extend(raw_transactions)
        elif seen_activity and not planned_windows:
            consecutive_empty_chunks += 1
            if consecutive_empty_chunks >= CIBC_EMPTY_HISTORY_STOP_CHUNKS:
                break

    return collected, account_fetch_succeeded


async def _sync_cibc_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any] | None,
    *,
    user_id: int | None = None,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    started_at = datetime.now(timezone.utc)
    user_agent = _cibc_session_user_agent(session_artifact)
    auth_token = _resolve_cibc_auth_token(storage_state, session_artifact)
    device_id = _resolve_cibc_device_id(storage_state, session_artifact)
    accounts: list[dict[str, Any]] = []
    diagnostics = begin_replay_diagnostics(
        PROVIDER,
        user_id,
        attempt_id=_cibc_visible_auth_attempt_id(user_id),
        provider_dir=CIBC_DIAGNOSTIC_PROVIDER_DIR,
    )

    def finish_replay_diagnostics(result: str) -> None:
        if not diagnostics:
            return
        path = diagnostics.finish(result)
        _cibc_log_event(
            "phase2 replay diagnostics",
            debug=True,
            user_id=user_id,
            sync_id=diagnostics.sync_id,
            attempt_id=diagnostics.attempt_id,
            result=result,
            path=str(path),
            display_path=path.name,
            phase1_har_path=str(diagnostics.phase1_har_path or ""),
        )

    _cibc_log_event(
        "saved-session sync start",
        debug=True,
        user_id=user_id,
        has_auth_token=bool(auth_token),
        has_device_id=bool(device_id),
    )

    accounts_response = await _cibc_direct_request(
        storage_state,
        "GET",
        CIBC_ACCOUNTS_API_URL,
        api_profile="bootstrap",
        auth_token=auth_token or None,
        device_id=device_id or None,
        user_agent=user_agent,
    )
    _cibc_record_accounts_fetch(
        diagnostics,
        stage="accounts.list.bootstrap",
        response=accounts_response,
        api_profile="bootstrap",
    )
    if _cibc_response_requires_auth(accounts_response):
        _cibc_log_event(
            "saved-session sync auth_required",
            user_id=user_id,
            engine="direct",
            message="bootstrap accounts request returned auth required",
        )
        finish_replay_diagnostics("auth_required")
        return None
    accounts = _cibc_accounts_from_payload(accounts_response.get("payload"))

    if not accounts:
        accounts_response = await _cibc_direct_request(
            storage_state,
            "GET",
            CIBC_ACCOUNTS_API_URL,
            api_profile="app",
            auth_token=auth_token or None,
            device_id=device_id or None,
            user_agent=user_agent,
        )
        _cibc_record_accounts_fetch(
            diagnostics,
            stage="accounts.list.app",
            response=accounts_response,
            api_profile="app",
        )
        if _cibc_response_requires_auth(accounts_response):
            _cibc_log_event(
                "saved-session sync auth_required",
                user_id=user_id,
                engine="direct",
                message="app accounts request returned auth required",
            )
            finish_replay_diagnostics("auth_required")
            return None
        accounts = _cibc_accounts_from_payload(accounts_response.get("payload"))

    if not accounts:
        _cibc_log_event(
            "saved-session sync accounts unavailable",
            user_id=user_id,
            level="warning",
            engine="direct",
        )
        finish_replay_diagnostics("error")
        return {"status": "error", "message": "No CIBC accounts returned"}

    if isinstance(session_artifact, dict):
        if auth_token and not session_artifact.get("auth_token"):
            session_artifact["auth_token"] = auth_token
        if device_id and not session_artifact.get("device_id"):
            session_artifact["device_id"] = device_id
        session_artifact.pop("accounts_payload", None)

    sync_windows = await mode_resolver(accounts) if mode_resolver and sync_scope != "accounts" else {}
    raw_transactions: dict[str, list[dict[str, Any]]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()

    for account in accounts:
        external_id = account.get("external_id")
        try:
            details = await _fetch_cibc_account_details_direct(
                storage_state,
                account,
                auth_token=auth_token or None,
                device_id=device_id or None,
                user_agent=user_agent,
                diagnostics=diagnostics,
            )
            if details:
                account["details"] = details.get("details") if isinstance(details.get("details"), dict) else details
                if details.get("accountId") and not account.get("provider_account_id"):
                    account["provider_account_id"] = details.get("accountId")
                    account["external_id"] = f"account:{details.get('accountId')}"
                    external_id = account["external_id"]

            _cibc_apply_sync_window(account, sync_windows)
            if sync_scope == "accounts":
                continue
            if not external_id or not _cibc_can_fetch_transactions(account):
                continue

            raw_txns, succeeded = await _fetch_cibc_transactions_for_account_direct(
                storage_state,
                account,
                auth_token=auth_token or None,
                device_id=device_id or None,
                user_agent=user_agent,
                diagnostics=diagnostics,
                transaction_window_recorder=transaction_window_recorder,
            )
            if raw_txns is not None:
                raw_transactions[external_id] = raw_txns
            if succeeded:
                transaction_fetch_succeeded_accounts.add(external_id)
            _cibc_log_event(
                "saved-session account fetched",
                debug=True,
                user_id=user_id,
                account_id=bool(account.get("id")),
                details=bool(account.get("details")),
                transactions=len(raw_txns or []),
                transaction_fetch_succeeded=bool(succeeded),
            )
        except PermissionError:
            _cibc_log_event(
                "saved-session sync auth_required",
                user_id=user_id,
                engine="direct",
                message="direct account request returned auth required",
            )
            finish_replay_diagnostics("auth_required")
            return None
        except Exception as exc:
            _cibc_log_event(
                "saved-session account fetch failed",
                level="warning",
                user_id=user_id,
                account_id=bool(account.get("id")),
                error=type(exc).__name__,
                message=_cibc_sanitize_log_text(str(exc), limit=180),
            )

    payload = {
        "status": "ok",
        "accounts": accounts,
        "transactions": raw_transactions,
        "transaction_fetch_succeeded_accounts": sorted(transaction_fetch_succeeded_accounts),
    }
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    _cibc_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="direct",
        accounts=len(accounts),
        transaction_accounts=len(transaction_fetch_succeeded_accounts),
        transactions=sum(len(items or []) for items in raw_transactions.values()),
        duration_ms=duration_ms,
    )
    finish_replay_diagnostics("succeeded")
    return payload


async def try_headless_sync(
    user_id: int,
    *,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    _cibc_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_cibc_session_user_agent,
        log_event=_cibc_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact
    if not storage_state:
        return replay_session.missing_artifact_result(stage="headless sync missing runtime_state")

    direct_payload = await _sync_cibc_with_saved_artifacts(
        storage_state,
        session_artifact_state,
        user_id=user_id,
        mode_resolver=mode_resolver,
        sync_scope=sync_scope,
        transaction_window_recorder=transaction_window_recorder,
    )
    if isinstance(direct_payload, dict) and direct_payload.get("status") == "ok":
        await replay_session.refresh_runtime_artifacts_async(storage_state)
        _cibc_log_event(
            "headless sync result",
            user_id=user_id,
            engine="direct",
            status=direct_payload.get("status"),
            accounts=len(direct_payload.get("accounts") or []),
            transaction_accounts=len(direct_payload.get("transaction_fetch_succeeded_accounts") or []),
            message=direct_payload.get("message"),
        )
        return replay_session.normalize_result(direct_payload)
    if isinstance(direct_payload, dict):
        _cibc_log_event(
            "headless sync result",
            user_id=user_id,
            engine="direct",
            level="warning",
            status=direct_payload.get("status"),
            accounts=len(direct_payload.get("accounts") or []),
            transaction_accounts=len(direct_payload.get("transaction_fetch_succeeded_accounts") or []),
            message=direct_payload.get("message"),
        )
        if direct_payload.get("message") == "No CIBC accounts returned":
            return scraper_auth_required()
        return replay_session.normalize_result(direct_payload)

    return replay_session.auth_required_result(
        log_message="saved-session direct reuse did not reach authenticated CIBC state"
    )


async def _close_cibc_session(session_state) -> None:
    if not session_state:
        return

    context = session_state.get("context") if isinstance(session_state, dict) else None
    browser = session_state.get("browser") if isinstance(session_state, dict) else None
    playwright = session_state.get("playwright") if isinstance(session_state, dict) else None

    if context:
        try:
            await context.close()
        except Exception:
            pass
    if browser:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright:
        try:
            await playwright.stop()
        except Exception:
            pass


async def cleanup_pending(user_id: int):
    pending_session = pop_runtime_state(PROVIDER, user_id)
    if not pending_session:
        return
    await _close_cibc_session(pending_session)
