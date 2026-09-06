from __future__ import annotations

import asyncio
import calendar
import httpx
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from app.connectors.sync_modes import (
    DEFAULT_BACKFILL_MONTHS,
    pending_backfill_date_windows,
    sync_window_date,
)
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    default_scraper_user_agent,
    refresh_session_artifact,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_auth_required, scraper_network_error
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)
from app.services.sync_utils import is_network_error

PROVIDER = "scotiabank"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.scotiabank")

SCOTIA_ACCOUNTS_URL = "https://secure.scotiabank.com/accounts?lng=en"
SCOTIA_SUMMARY_URL = "https://secure.scotiabank.com/api/accounts/summary?isAccountSummaryPage=true"
SCOTIA_TXN_URL = "https://secure.scotiabank.com/api/transactions/transaction-history"
SCOTIA_DIRECT_TIMEOUT_SECONDS = 30
SCOTIA_SUMMARY_TIMEOUT_SECONDS = 5.0
SCOTIA_SUMMARY_AUTH_PROBE_TIMEOUT_SECONDS = 8.0
SCOTIA_TRANSACTION_TIMEOUT_SECONDS = 20
SCOTIA_DIRECT_RETRY_ATTEMPTS = 3
SCOTIA_SUMMARY_RETRY_ATTEMPTS = 1
SCOTIA_DIRECT_RETRY_BACKOFF_SECONDS = (0.35, 1.0)
SCOTIA_DIRECT_REQUIRED_AUTH_COOKIE_NAMES = frozenset({"session-id"})
SCOTIA_DIRECT_CSRF_COOKIE_NAMES = frozenset({"_csrf", "_csrfauth"})

ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


class ScotiaAuthRequired(PermissionError):
    pass


def _scotia_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_SCOTIABANK_DEBUG",))


def _scotia_log_event(
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
        debug_enabled=_scotia_debug_logs_enabled(),
        **fields,
    )


def _scotia_sanitize_log_text(text: Any, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit)


def _scotia_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _scotia_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _scotia_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _scotia_api_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-CA,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": SCOTIA_ACCOUNTS_URL,
        "User-Agent": _scotia_user_agent(user_agent),
    }
    return headers


def _scotia_endpoint_label(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return "unknown"
    return f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path or "unknown"


async def _scotia_direct_get(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    timeout_seconds: float = SCOTIA_DIRECT_TIMEOUT_SECONDS,
    retry_attempts: int = SCOTIA_DIRECT_RETRY_ATTEMPTS,
) -> dict[str, Any]:
    headers = _scotia_api_headers(storage_state, url, user_agent=user_agent)
    attempts = max(int(retry_attempts or 1), 1)
    for attempt in range(1, attempts + 1):
        try:
            return await SavedArtifactHttpClient.request_once(
                storage_state,
                "GET",
                url,
                user_agent=_scotia_user_agent(user_agent),
                headers=headers,
                timeout=timeout_seconds,
                seed_cookies=True,
            )
        except Exception as exc:
            if not is_network_error(exc) or attempt >= attempts:
                raise
            _scotia_log_event(
                "direct get retry",
                level="warning",
                endpoint=_scotia_endpoint_label(url),
                attempt=attempt,
                max_attempts=attempts,
                exception_type=type(exc).__name__,
                message=_scotia_sanitize_log_text(str(exc) or type(exc).__name__),
            )
            backoff_index = min(attempt - 1, len(SCOTIA_DIRECT_RETRY_BACKOFF_SECONDS) - 1)
            await asyncio.sleep(SCOTIA_DIRECT_RETRY_BACKOFF_SECONDS[backoff_index])
    raise RuntimeError("unreachable Scotia direct GET retry state")


def _scotia_response_auth_signal(response: dict[str, Any]) -> str | None:
    status = int(response.get("status") or 0)
    if status in (401, 403):
        return f"http_{status}"
    try:
        parsed = urlparse(str(response.get("url") or ""))
    except ValueError:
        return None
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "auth.scotiaonline.scotiabank.com" in host:
        return "authentication_host_redirect"
    if "login" in path:
        return "login_path_redirect"
    return None


def _scotia_response_requires_auth(response: dict[str, Any]) -> bool:
    return _scotia_response_auth_signal(response) is not None


def _scotia_direct_auth_cookie_names(storage_state: dict[str, Any] | None) -> set[str]:
    if not isinstance(storage_state, dict):
        return set()
    names: set[str] = set()
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "scotiabank.com" not in domain:
            continue
        name = str(cookie.get("name") or "").strip().lower()
        if name:
            names.add(name)
    return names


def _scotia_storage_state_has_direct_auth_material(storage_state: dict[str, Any] | None) -> bool:
    cookie_names = _scotia_direct_auth_cookie_names(storage_state)
    return (
        SCOTIA_DIRECT_REQUIRED_AUTH_COOKIE_NAMES.issubset(cookie_names)
        and bool(cookie_names & SCOTIA_DIRECT_CSRF_COOKIE_NAMES)
    )


def _scotia_products_from_summary_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    products = ((payload.get("data") or {}).get("products")) if isinstance(payload.get("data"), dict) else None
    return [dict(product) for product in products if isinstance(product, dict)] if isinstance(products, list) else []


async def _fetch_scotia_summary_direct(
    storage_state: dict[str, Any],
    *,
    user_agent: str,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    response = await _scotia_direct_get(
        storage_state,
        SCOTIA_SUMMARY_URL,
        user_agent=user_agent,
        timeout_seconds=SCOTIA_SUMMARY_TIMEOUT_SECONDS,
        retry_attempts=SCOTIA_SUMMARY_RETRY_ATTEMPTS,
    )
    auth_signal = _scotia_response_auth_signal(response)
    if auth_signal:
        try:
            response_url = urlparse(str(response.get("url") or ""))
            response_host = response_url.netloc.lower() or None
            response_path = response_url.path or None
        except ValueError:
            response_host = None
            response_path = None
        _scotia_log_event(
            "accounts api auth_required",
            user_id=user_id,
            level="warning",
            auth_signal=auth_signal,
            status_code=response.get("status"),
            response_host=response_host,
            response_path=response_path,
            payload_type=type(response.get("payload")).__name__,
        )
        raise ScotiaAuthRequired("Scotiabank sign-in required")
    if int(response.get("status") or 0) >= 400:
        _scotia_log_event(
            "accounts api failed",
            user_id=user_id,
            level="warning",
            status_code=response.get("status"),
            url=response.get("url"),
            body=_scotia_sanitize_log_text(response.get("text"), limit=180),
        )
        return None
    payload = response.get("payload")
    return payload if isinstance(payload, dict) else None


async def _scotia_auth_entry_reachable_after_timeout(
    *,
    user_agent: str,
    user_id: int | None = None,
) -> bool:
    try:
        response = await SavedArtifactHttpClient.request_once(
            {},
            "GET",
            SCOTIA_SUMMARY_URL,
            user_agent=_scotia_user_agent(user_agent),
            headers=_scotia_api_headers({}, SCOTIA_SUMMARY_URL, user_agent=user_agent),
            timeout=SCOTIA_SUMMARY_AUTH_PROBE_TIMEOUT_SECONDS,
            seed_cookies=False,
        )
    except Exception as exc:
        _scotia_log_event(
            "summary timeout auth probe failed",
            user_id=user_id,
            engine="direct",
            level="warning",
            exception_type=type(exc).__name__,
            message=_scotia_sanitize_log_text(str(exc) or type(exc).__name__),
        )
        return False

    parsed_url = urlparse(str(response.get("url") or ""))
    auth_entry_reachable = _scotia_response_requires_auth(response)
    _scotia_log_event(
        "summary timeout auth probe result",
        user_id=user_id,
        engine="direct",
        level="info" if auth_entry_reachable else "warning",
        status_code=response.get("status"),
        response_host=parsed_url.netloc,
        response_path=parsed_url.path,
        auth_entry_reachable=auth_entry_reachable,
    )
    return auth_entry_reachable


def _scotia_account_type(
    product_category: str,
    asset_liability_type: str,
    description: str,
) -> tuple[str, bool]:
    product_category_upper = str(product_category or "").upper()
    asset_liability_type_upper = str(asset_liability_type or "").upper()
    name = str(description or "").lower()

    if product_category_upper == "CREDITCARDS":
        return "credit_card", True
    if product_category_upper == "BORROWING":
        if "mortgage" in name:
            return "mortgage", True
        return "loc", True

    if "tfsa" in name:
        return "tfsa", asset_liability_type_upper == "LIABILITY"
    if "fhsa" in name:
        return "fhsa", asset_liability_type_upper == "LIABILITY"
    if "rrsp" in name:
        return "rrsp", asset_liability_type_upper == "LIABILITY"
    if "resp" in name:
        return "resp", asset_liability_type_upper == "LIABILITY"
    if "savings" in name:
        return "savings", asset_liability_type_upper == "LIABILITY"
    if "chequing" in name or "checking" in name:
        return "chequing", asset_liability_type_upper == "LIABILITY"
    if "mortgage" in name:
        return "mortgage", True
    if "line of credit" in name or re.search(r"\bloc\b", name):
        return "loc", True
    if "credit" in name or "visa" in name or "amex" in name or "mastercard" in name:
        return "credit_card", True

    return "chequing", asset_liability_type_upper == "LIABILITY"


def accounts_from_summary_payload(
    payload: Any,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]] | None:
    products = _scotia_products_from_summary_payload(payload)
    accounts: list[dict[str, Any]] = []
    for product in products:
        key = product.get("key") or ""
        if not key:
            continue
        display_id = str(product.get("displayId") or "")
        description = str(product.get("description") or "Account")
        product_category = str(product.get("productCategory") or "")
        asset_liability_type = str(product.get("assetLiabilityType") or "")
        account_type, is_liability = _scotia_account_type(
            product_category,
            asset_liability_type,
            description,
        )

        balances = product.get("primaryBalances") if isinstance(product.get("primaryBalances"), list) else []
        primary_balance = balances[0] if balances and isinstance(balances[0], dict) else {}
        balance = _scotia_float(primary_balance.get("amount") if primary_balance else 0)
        currency = primary_balance.get("currencyCode") if primary_balance else "CAD"

        name = f"{description} *{display_id}" if display_id else description
        accounts.append(
            {
                "name": name,
                "account_type": account_type,
                "is_liability": is_liability,
                "balance": balance,
                "currency": currency or "CAD",
                "external_id": f"scotiabank:{key}",
                "encrypted_id": key,
                "product_category": product_category,
                "display_id": display_id,
            }
        )

    if accounts:
        _scotia_log_event("accounts fetch success", user_id=user_id, count=len(accounts))
        return accounts

    _scotia_log_event("accounts fetch empty", user_id=user_id, level="warning")
    return None


def _scotia_month_floor(months: int) -> date:
    months = max(int(months or 1), 1)
    today = date.today()
    start_month = today.month - (months - 1)
    start_year = today.year
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return date(start_year, start_month, 1)


def _scotia_month_windows(months: int) -> list[tuple[str, str]]:
    return _scotia_date_windows(_scotia_month_floor(months), date.today())


def _scotia_date_windows(start_date: date, end_date: date) -> list[tuple[str, str]]:
    if start_date > end_date:
        start_date = end_date

    windows: list[tuple[str, str]] = []
    current = start_date
    while current <= end_date:
        last_day_num = calendar.monthrange(current.year, current.month)[1]
        month_end = date(current.year, current.month, last_day_num)
        window_end = min(month_end, end_date)
        windows.append((current.isoformat(), window_end.isoformat()))
        current = window_end + timedelta(days=1)
    return windows


def _scotia_sync_window_entry(
    sync_window_per_account: dict[str, Any] | None,
    external_id: str | None,
) -> dict[str, Any]:
    if not sync_window_per_account or not external_id:
        return {}
    entry = sync_window_per_account.get(str(external_id))
    return entry if isinstance(entry, dict) else {}


def _scotia_pending_backfill_windows(entry: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (start.isoformat(), end.isoformat())
        for start, end in pending_backfill_date_windows(entry)
    ]


async def _record_scotia_transaction_window(
    recorder: TransactionWindowRecorder | None,
    *,
    event: str,
    account: dict[str, Any],
    mode: str,
    start_date: str,
    end_date: str,
    transactions: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> None:
    if not recorder:
        return
    await recorder(
        {
            "event": event,
            "account": account,
            "mode": mode,
            "start_date": start_date,
            "end_date": end_date,
            "transactions": transactions or [],
            "error": error,
        }
    )


async def fetch_transactions_direct(
    storage_state: dict[str, Any],
    accounts: list[dict[str, Any]],
    *,
    user_agent: str,
    sync_window_per_account: dict[str, Any] | None = None,
    user_id: int | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], set[str], set[str], set[str]]:
    sync_windows = sync_window_per_account or {}
    results: dict[str, list[dict[str, Any]]] = {}
    succeeded_accounts: set[str] = set()
    failed_accounts: set[str] = set()
    network_failed_accounts: set[str] = set()

    for account in accounts:
        encrypted_id = account.get("encrypted_id")
        external_id = account.get("external_id")
        if not encrypted_id or not external_id:
            continue

        sync_window = _scotia_sync_window_entry(sync_windows, external_id)
        mode = sync_window.get("mode") or "backfill"
        if mode == "backfill":
            windows = _scotia_pending_backfill_windows(sync_window) or _scotia_month_windows(DEFAULT_BACKFILL_MONTHS)
        else:
            end_date = sync_window_date(sync_window, "end_date") or date.today()
            start_date = sync_window_date(sync_window, "start_date") or end_date
            history_floor = _scotia_month_floor(DEFAULT_BACKFILL_MONTHS)
            if start_date < history_floor:
                start_date = history_floor
            windows = _scotia_date_windows(start_date, end_date)

        _scotia_log_event(
            "transactions fetch start",
            user_id=user_id,
            debug=True,
            account=external_id,
            mode=mode,
            window_count=len(windows),
            from_date=windows[0][0] if windows else None,
            to_date=windows[-1][1] if windows else None,
        )

        collected: list[dict[str, Any]] = []
        account_fetch_succeeded = True
        account_network_failed = False
        for from_date, to_date in windows:
            url = (
                f"{SCOTIA_TXN_URL}/{encrypted_id}"
                f"?accountType=CREDITCARDS&fromDate={from_date}&toDate={to_date}"
            )
            await _record_scotia_transaction_window(
                transaction_window_recorder,
                event="started",
                account=account,
                mode=mode,
                start_date=from_date,
                end_date=to_date,
            )
            try:
                response = await _scotia_direct_get(
                    storage_state,
                    url,
                    user_agent=user_agent,
                    timeout_seconds=SCOTIA_TRANSACTION_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                account_fetch_succeeded = False
                account_network_failed = account_network_failed or is_network_error(exc)
                await _record_scotia_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=from_date,
                    end_date=to_date,
                    error=str(exc) or type(exc).__name__,
                )
                _scotia_log_event(
                    "transactions fetch exception",
                    user_id=user_id,
                    level="warning",
                    account=external_id,
                    from_date=from_date,
                    to_date=to_date,
                    exception_type=type(exc).__name__,
                    message=_scotia_sanitize_log_text(str(exc) or type(exc).__name__),
                )
                continue

            if _scotia_response_requires_auth(response):
                await _record_scotia_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=from_date,
                    end_date=to_date,
                    error="Scotiabank sign-in required",
                )
                raise ScotiaAuthRequired("Scotiabank sign-in required")
            if int(response.get("status") or 0) >= 400:
                account_fetch_succeeded = False
                await _record_scotia_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=from_date,
                    end_date=to_date,
                    error=f"Scotiabank transaction response status {response.get('status')}",
                )
                _scotia_log_event(
                    "transactions fetch failed",
                    user_id=user_id,
                    level="warning",
                    account=external_id,
                    from_date=from_date,
                    to_date=to_date,
                    status_code=response.get("status"),
                )
                continue

            txn_result = response.get("payload")
            if txn_result is None:
                account_fetch_succeeded = False
                await _record_scotia_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=from_date,
                    end_date=to_date,
                    error="Scotiabank transaction response was empty.",
                )
                _scotia_log_event(
                    "transactions fetch empty payload",
                    user_id=user_id,
                    debug=True,
                    account=external_id,
                    from_date=from_date,
                    to_date=to_date,
                )
                continue

            data = txn_result.get("data") if isinstance(txn_result, dict) else None
            settled = data if isinstance(data, list) else data.get("settled") if isinstance(data, dict) else None
            if not isinstance(settled, list):
                account_fetch_succeeded = False
                await _record_scotia_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=from_date,
                    end_date=to_date,
                    error="Scotiabank transaction response was missing settled transactions.",
                )
                _scotia_log_event(
                    "transactions fetch invalid payload",
                    user_id=user_id,
                    level="warning",
                    account=external_id,
                    from_date=from_date,
                    to_date=to_date,
                )
                continue

            if settled:
                collected.extend(settled)
            await _record_scotia_transaction_window(
                transaction_window_recorder,
                event="completed",
                account=account,
                mode=mode,
                start_date=from_date,
                end_date=to_date,
                transactions=settled,
            )

        if account_fetch_succeeded:
            succeeded_accounts.add(external_id)
        else:
            failed_accounts.add(external_id)
            if account_network_failed:
                network_failed_accounts.add(external_id)
        if collected:
            results[external_id] = collected

        _scotia_log_event(
            "transactions fetch complete",
            user_id=user_id,
            debug=True,
            account=external_id,
            succeeded=account_fetch_succeeded,
            count=len(collected),
        )

    return results, succeeded_accounts, failed_accounts, network_failed_accounts


async def _sync_scotia_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any],
    *,
    user_id: int | None = None,
    sync_scope: str = "full",
    mode_resolver: ModeResolver | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> dict[str, Any] | None:
    started_at = datetime.now(timezone.utc)
    user_agent = _scotia_session_user_agent(session_artifact)
    prefetched_payload = session_artifact.get("accounts_payload") if isinstance(session_artifact, dict) else None
    prefetched_account_count = (
        len(_scotia_products_from_summary_payload(prefetched_payload))
        if isinstance(prefetched_payload, dict)
        else 0
    )

    _scotia_log_event(
        "saved-session sync start",
        user_id=user_id,
        engine="direct",
        prefetched_accounts=prefetched_account_count,
        debug=True,
    )

    summary_payload = await _fetch_scotia_summary_direct(
        storage_state,
        user_agent=user_agent,
        user_id=user_id,
    )
    accounts = accounts_from_summary_payload(summary_payload, user_id=user_id)
    if isinstance(session_artifact, dict) and isinstance(summary_payload, dict):
        session_artifact["accounts_payload"] = summary_payload

    if not accounts:
        _scotia_log_event(
            "saved-session sync accounts unavailable",
            user_id=user_id,
            level="warning",
            engine="direct",
        )
        return None

    session_artifact.update(
        refresh_session_artifact(
            session_artifact,
            user_agent=user_agent,
            captured_at=started_at.isoformat(),
        )
    )

    if sync_scope == "accounts":
        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        _scotia_log_event(
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

    sync_windows = await mode_resolver(accounts) if mode_resolver else {}
    (
        raw_transactions,
        transaction_fetch_succeeded_accounts,
        transaction_fetch_failed_accounts,
        transaction_fetch_network_failed_accounts,
    ) = await fetch_transactions_direct(
        storage_state,
        accounts,
        user_agent=user_agent,
        sync_window_per_account=sync_windows,
        user_id=user_id,
        transaction_window_recorder=transaction_window_recorder,
    )

    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    transaction_count = sum(len(items or []) for items in raw_transactions.values())
    if accounts and not transaction_fetch_succeeded_accounts and transaction_fetch_failed_accounts:
        status = "network_error" if transaction_fetch_network_failed_accounts else "error"
        message = (
            "Connection failed"
            if status == "network_error"
            else "Scotiabank transaction fetch failed before any accounts could be synced."
        )
        _scotia_log_event(
            "saved-session sync transaction fetch failed",
            user_id=user_id,
            engine="direct",
            level="warning",
            accounts=len(accounts),
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
            "transaction_fetch_succeeded_accounts": [],
            "transaction_fetch_failed_accounts": sorted(transaction_fetch_failed_accounts),
        }

    _scotia_log_event(
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
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    _scotia_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = SavedArtifactReplaySession(
        PROVIDER,
        user_id,
        visible_auth_attempt_namespace=VISIBLE_AUTH_ATTEMPT_NAMESPACE,
        user_agent_resolver=_scotia_session_user_agent,
        log_event=_scotia_log_event,
    )
    await replay_session.load_async()
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact

    if not storage_state:
        return replay_session.missing_artifact_result()
    if not _scotia_storage_state_has_direct_auth_material(storage_state):
        cookie_names = _scotia_direct_auth_cookie_names(storage_state)
        _scotia_log_event(
            "headless sync auth_required",
            user_id=user_id,
            engine="direct",
            level="warning",
            message="saved-session storage_state is missing Scotiabank direct auth cookies",
            missing_auth_cookies=sorted(SCOTIA_DIRECT_REQUIRED_AUTH_COOKIE_NAMES - cookie_names),
            has_csrf_cookie=bool(cookie_names & SCOTIA_DIRECT_CSRF_COOKIE_NAMES),
        )
        return scraper_auth_required()

    try:
        payload = await _sync_scotia_with_saved_artifacts(
            storage_state,
            session_artifact_state,
            user_id=user_id,
            sync_scope=sync_scope,
            mode_resolver=mode_resolver,
            transaction_window_recorder=transaction_window_recorder,
        )
    except ScotiaAuthRequired:
        _scotia_log_event(
            "headless sync auth_required",
            user_id=user_id,
            engine="direct",
            message="saved-session direct reuse did not reach authenticated Scotiabank state",
        )
        return scraper_auth_required()
    except Exception as exc:
        if isinstance(exc, httpx.TimeoutException) and await _scotia_auth_entry_reachable_after_timeout(
            user_agent=_scotia_session_user_agent(session_artifact_state),
            user_id=user_id,
        ):
            _scotia_log_event(
                "headless sync auth_required",
                user_id=user_id,
                engine="direct",
                message="saved-session direct reuse timed out while Scotiabank auth entry was reachable",
            )
            return scraper_auth_required()
        if is_network_error(exc):
            _scotia_log_event(
                "headless sync network_error",
                user_id=user_id,
                engine="direct",
                level="warning",
                exception_type=type(exc).__name__,
                message=_scotia_sanitize_log_text(str(exc) or type(exc).__name__),
            )
            return scraper_network_error()
        _scotia_log_event(
            "headless sync failed",
            user_id=user_id,
            engine="direct",
            level="warning",
            exception_type=type(exc).__name__,
            message=_scotia_sanitize_log_text(str(exc) or type(exc).__name__),
        )
        raise

    if isinstance(payload, dict) and payload.get("status") == "ok":
        await replay_session.refresh_runtime_artifacts_async(storage_state)
    return replay_session.normalize_result(payload)
