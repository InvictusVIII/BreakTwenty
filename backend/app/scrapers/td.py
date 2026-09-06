from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.connectors.sync_modes import pending_backfill_date_windows, sync_window_date
from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    refresh_session_artifact,
    save_visible_auth_session_artifact_async,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_auth_required
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)

PROVIDER = "td"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.td")

TD_EASYWEB_SUMMARY_PAGE_URL = "https://easyweb.td.com/ui/ew/fs?fsType=PFS&kyc=Y"
TD_EASYWEB_ACCOUNT_PAGE_URL_TEMPLATE = "https://easyweb.td.com/ui/ew/da?accountKey={account_key}"
TD_EASYWEB_MANIFEST_URL = "https://easyweb.td.com/ui/assets/uapp-registry/mf.manifest.json"
TD_ACCOUNTS_SUMMARY_URL = "https://easyweb.td.com/ms/uainq/v1/accounts/summary"
TD_ACCOUNT_DETAILS_URL_TEMPLATE = "https://easyweb.td.com/ms/uainq/v1/accounts/{account_key}/details"
TD_TRANSACTIONS_URL_TEMPLATE = "https://easyweb.td.com/ms/uainq/v1/accounts/{account_key}/transactions"
TD_DIRECT_TIMEOUT_SECONDS = 30
TD_BACKFILL_CHUNK_DAYS = 365
TD_EMPTY_HISTORY_STOP_CHUNKS = 2
TD_MAX_HISTORY_DAYS = 3650
TD_ACCEPT_SECONDARY_LANGUAGE = "fr_CA"
TD_ORIGINATING_CHANNEL_NAME = "EWP"
TD_DEFAULT_APP_VERSIONS = {
    "uf-fs": "26.3.4",
    "uf-da": "26.2.1",
    "uu-accounts": "25.7.1",
}

TD_TRANSIENT_ERROR_KEYWORDS = (
    "temporarily unavailable",
    "technical difficulties",
    "service unavailable",
    "try again later",
    "try again soon",
    "something went wrong",
    "something's gone wrong",
    "500.generic",
)

ModeResolver = Callable[[list[dict[str, Any]]], Awaitable[dict[str, dict[str, str | int | None]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


async def save_session_artifact(user_id: int, artifact: dict[str, Any]) -> None:
    await save_visible_auth_session_artifact_async(user_id, PROVIDER, artifact)


def _td_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_TD_DEBUG",))


def _td_log_event(
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
        debug_enabled=_td_debug_logs_enabled(),
        **fields,
    )


def _td_sanitize_log_text(text: str | None, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit)


def _collect_td_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(_collect_td_strings(nested))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for nested in value:
            values.extend(_collect_td_strings(nested))
        return values
    return []


def _td_payload_text(payload: Any, fallback_text: str = "") -> str:
    values = [text.strip() for text in _collect_td_strings(payload) if text and text.strip()]
    if fallback_text:
        values.append(fallback_text.strip())
    return " ".join(values)


def _td_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("message", "errorMessage", "statusMessage", "description", "error_description", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    for value in _collect_td_strings(payload):
        text = str(value or "").strip()
        if text:
            return text
    return fallback


def _td_auth_url(url: str) -> bool:
    return "authentication.td.com" in str(url or "").lower()


def _looks_like_td_transient_error(payload: Any, fallback_text: str = "") -> bool:
    text = _td_payload_text(payload, fallback_text).lower()
    return any(keyword in text for keyword in TD_TRANSIENT_ERROR_KEYWORDS)


def _td_response_requires_auth(response: dict[str, Any]) -> bool:
    url = str(response.get("url") or "").lower()
    status = int(response.get("status") or 0)
    text = _td_payload_text(response.get("payload"), response.get("text", "")).lower()
    if _td_auth_url(url):
        return True
    if status in (401, 403):
        return True
    return "consumer=easyweb" in text or "easyweb login" in text


def _td_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _td_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _td_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _td_easyweb_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    app_id: str,
    app_version: str,
    user_agent: str,
    referrer: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Secondary-Language": TD_ACCEPT_SECONDARY_LANGUAGE,
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "MessageID": str(uuid.uuid4()),
        "Origin": "https://easyweb.td.com",
        "Originating-App-Name": f"RWUI-{app_id}",
        "Originating-App-Version-Num": app_version,
        "Originating-Channel-Name": TD_ORIGINATING_CHANNEL_NAME,
        "Pragma": "no-cache",
        "Referer": referrer,
        "Timestamp": _td_iso_now(),
        "TraceabilityID": str(uuid.uuid4()),
        "User-Agent": _td_user_agent(user_agent),
    }
    return headers


async def _td_direct_request(
    storage_state: dict[str, Any],
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout_seconds: int = TD_DIRECT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {}
    if body is not None:
        request_kwargs["json"] = body
    return await SavedArtifactHttpClient.request_once(
        storage_state,
        method,
        url,
        headers=headers,
        timeout=timeout_seconds,
        include_headers=True,
        seed_cookies=True,
        **request_kwargs,
    )


def _manifest_entry_version(payload: dict[str, Any], key: str, default: str) -> str:
    entry = payload.get(key)
    if not isinstance(entry, dict):
        return default

    version = entry.get("version")
    if isinstance(version, str) and version.strip():
        return version.strip()

    remote_entry = str(entry.get("remoteEntry") or "")
    if "?v=" in remote_entry:
        return remote_entry.split("?v=", 1)[1].split("&", 1)[0].strip() or default
    return default


async def _load_td_app_versions_direct(
    storage_state: dict[str, Any],
    *,
    user_agent: str,
) -> dict[str, str]:
    response = await _td_direct_request(
        storage_state,
        "GET",
        TD_EASYWEB_MANIFEST_URL,
        headers=_td_easyweb_headers(
            storage_state,
            TD_EASYWEB_MANIFEST_URL,
            app_id="uf-fs",
            app_version=TD_DEFAULT_APP_VERSIONS["uf-fs"],
            user_agent=user_agent,
            referrer=TD_EASYWEB_SUMMARY_PAGE_URL,
        ),
    )
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return dict(TD_DEFAULT_APP_VERSIONS)
    return {
        app_id: _manifest_entry_version(payload, app_id, default_version)
        for app_id, default_version in TD_DEFAULT_APP_VERSIONS.items()
    }


def _td_account_key(raw: dict[str, Any]) -> str | None:
    value = raw.get("accountKey")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _td_raw_provider_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _td_account_external_id(raw: dict[str, Any]) -> str | None:
    provider_account_id = _td_raw_provider_account_id(
        raw.get("provider_account_id")
        or raw.get("accountIdentifier")
    )
    if not provider_account_id:
        bank = str(raw.get("bankNum") or "").strip()
        branch = str(raw.get("branchNum") or "").strip()
        account_num = str(raw.get("accountNum") or raw.get("accountNumber") or "").strip()
        if bank and branch and account_num:
            provider_account_id = f"{bank}:{branch}:{account_num}"
    if provider_account_id:
        return f"account:{provider_account_id}"
    return raw.get("external_id")


def _td_alternate_external_ids(raw: dict[str, Any], external_id: str | None) -> tuple[str, ...]:
    values = [
        raw.get("accountKey"),
        raw.get("accountNum"),
        raw.get("accountNumber"),
        raw.get("accountIdentifier"),
        raw.get("summary_external_id"),
        raw.get("external_id") if raw.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _td_summary_accounts(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    financial_summaries = payload.get("financialSummaries")
    if not isinstance(financial_summaries, dict):
        return []

    accounts: list[dict[str, Any]] = []
    for summary_key in ("personalSummary", "businessSummary"):
        summary = financial_summaries.get(summary_key)
        if not isinstance(summary, dict):
            continue
        for profile_group in summary.get("profileGroups") or []:
            if not isinstance(profile_group, dict):
                continue
            for product_group in profile_group.get("productGroups") or []:
                if not isinstance(product_group, dict):
                    continue
                for account in product_group.get("accounts") or []:
                    if not isinstance(account, dict):
                        continue
                    record = dict(account)
                    record["summary_scope"] = summary_key
                    record["profile_group_display_name"] = profile_group.get("profileGroupDisplayName")
                    record["profile_group_id"] = profile_group.get("profileGroupId")
                    record["profile_group_type"] = profile_group.get("profileGroupType")
                    record["product_group_type"] = product_group.get("productGroupType")
                    record["external_id"] = _td_account_external_id(record)
                    accounts.append(record)
    return accounts


def _td_money_currency(value: Any, default: str = "CAD") -> str:
    if isinstance(value, dict):
        currency = value.get("currencyCd") or value.get("ccy") or value.get("currency")
        if currency:
            return str(currency).strip() or default
    return default


def _td_can_fetch_transactions(account: dict[str, Any]) -> bool:
    if not _td_account_key(account):
        return False

    detail_type = str(account.get("accountDetailType") or "").upper()
    if "ACTIVITY" in detail_type:
        return True

    product_group_type = str(account.get("product_group_type") or "").upper()
    return product_group_type in {"BANKING", "CREDIT", "LOAN_MORTGAGE"}


def _td_account_open_date(account: dict[str, Any]) -> date | None:
    opened_at = account.get("openDt")
    if not isinstance(opened_at, str):
        return None
    try:
        return date.fromisoformat(opened_at[:10])
    except ValueError:
        return None


def _td_apply_sync_window(account: dict[str, Any], sync_windows: dict[str, Any]) -> None:
    external_id = str(account.get("external_id") or "").strip()
    entry = sync_windows.get(external_id) if external_id else None
    if not isinstance(entry, dict):
        for alternate_external_id in _td_alternate_external_ids(account, external_id or None):
            candidate = sync_windows.get(alternate_external_id)
            if isinstance(candidate, dict):
                entry = candidate
                break
    account["sync_window"] = dict(entry) if isinstance(entry, dict) else {"mode": "backfill"}


def _td_transaction_window(account: dict[str, Any]) -> tuple[str, date, date]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    mode = str(sync_window.get("mode") or "backfill")
    end_date = sync_window_date(sync_window, "end_date") or date.today()
    opened_date = _td_account_open_date(account)

    if mode != "backfill":
        start_date = sync_window_date(sync_window, "start_date") or end_date
        if opened_date and opened_date > start_date:
            start_date = opened_date
    else:
        start_date = end_date - timedelta(days=TD_MAX_HISTORY_DAYS)
        if opened_date and opened_date > start_date:
            start_date = opened_date

    if start_date > end_date:
        start_date = end_date
    return mode, start_date, end_date


def _td_split_windows(start_date: date, end_date: date) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    current_end = end_date
    while current_end >= start_date:
        current_start = max(
            start_date,
            current_end - timedelta(days=TD_BACKFILL_CHUNK_DAYS - 1),
        )
        windows.append((current_start, current_end))
        if current_start <= start_date:
            break
        current_end = current_start - timedelta(days=1)
    return windows


def _td_pending_backfill_windows(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> tuple[list[tuple[date, date]], bool]:
    sync_window = account.get("sync_window") if isinstance(account.get("sync_window"), dict) else {}
    windows = pending_backfill_date_windows(
        sync_window,
        min_date=history_start,
        max_date=history_end,
    )
    if windows:
        return windows, True
    return _td_split_windows(history_start, history_end), False


def _td_bound_backfill_sync_window(
    account: dict[str, Any],
    history_start: date,
    history_end: date,
) -> None:
    sync_window = account.get("sync_window")
    if not isinstance(sync_window, dict):
        return
    if str(sync_window.get("mode") or "backfill") != "backfill":
        return
    account["sync_window"] = {
        **sync_window,
        "backfill_start_date": history_start.isoformat(),
        "backfill_end_date": history_end.isoformat(),
    }


async def _record_td_transaction_window(
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
        _td_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("external_id") or "",
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_td_sanitize_log_text(str(exc), limit=140),
        )


def _td_candidate_transaction_descriptions(account: dict[str, Any]) -> list[str | None]:
    product_group_type = str(account.get("product_group_type") or "").upper()
    account_text = " ".join(
        str(account.get(key) or "")
        for key in (
            "accountName",
            "accountDesc",
            "productCd",
            "planCd",
            "accountApplicationCd",
        )
    ).lower()

    candidates: list[str | None] = []
    if product_group_type == "BANKING":
        candidates.extend(["chq/sav_initLoad", None])
    elif product_group_type == "CREDIT":
        candidates.extend(["cc_initLoad", "credit_initLoad", "card_initLoad", None])
    elif product_group_type == "LOAN_MORTGAGE":
        if "mortgage" in account_text:
            candidates.extend(["mortgage_initLoad", "loan_initLoad", None])
        elif any(token in account_text for token in ("line of credit", "loc", "uloc")):
            candidates.extend(["loc_initLoad", "loan_initLoad", None])
        else:
            candidates.extend(["loan_initLoad", None])
    else:
        candidates.append(None)

    deduped: list[str | None] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _td_transactions_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None

    transaction_list = payload.get("transactionList")
    if isinstance(transaction_list, dict):
        for key in ("transactions", "postedTransactions", "posted"):
            candidate = transaction_list.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]

    for key in ("transactions", "postedTransactions", "posted"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]

    return None


async def _fetch_td_account_details_direct(
    storage_state: dict[str, Any],
    account_key: str,
    *,
    app_versions: dict[str, str],
    user_agent: str,
) -> dict[str, Any] | None:
    url = TD_ACCOUNT_DETAILS_URL_TEMPLATE.format(account_key=account_key)
    response = await _td_direct_request(
        storage_state,
        "GET",
        url,
        headers=_td_easyweb_headers(
            storage_state,
            url,
            app_id="uf-da",
            app_version=app_versions.get("uf-da", TD_DEFAULT_APP_VERSIONS["uf-da"]),
            user_agent=user_agent,
            referrer=TD_EASYWEB_ACCOUNT_PAGE_URL_TEMPLATE.format(account_key=account_key),
        ),
    )
    if _td_response_requires_auth(response):
        raise PermissionError("TD auth required")
    if response.get("status", 0) >= 400:
        if _looks_like_td_transient_error(response.get("payload"), response.get("text", "")):
            raise RuntimeError(_td_error_message(response.get("payload"), "TD account details unavailable"))
        return None

    payload = response.get("payload")
    if not isinstance(payload, dict):
        return None
    details = payload.get("accountDetails")
    return details if isinstance(details, dict) else payload


async def _fetch_td_transactions_for_account_direct(
    storage_state: dict[str, Any],
    account: dict[str, Any],
    *,
    app_versions: dict[str, str],
    user_agent: str,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[list[dict[str, Any]] | None, bool]:
    account_key = _td_account_key(account)
    if not account_key:
        return None, False

    url = TD_TRANSACTIONS_URL_TEMPLATE.format(account_key=account_key)
    referrer = TD_EASYWEB_ACCOUNT_PAGE_URL_TEMPLATE.format(account_key=account_key)
    version = app_versions.get("uf-da", TD_DEFAULT_APP_VERSIONS["uf-da"])
    account_currency = _td_money_currency(
        account.get("currentBalance") or account.get("balanceAmt"),
        default="CAD",
    )

    async def fetch_window(start_date: date, end_date: date) -> tuple[list[dict[str, Any]] | None, bool]:
        for description in _td_candidate_transaction_descriptions(account):
            body = {
                "startDt": start_date.isoformat(),
                "endDt": end_date.isoformat(),
            }
            if description:
                body["description"] = description

            response = await _td_direct_request(
                storage_state,
                "POST",
                url,
                headers=_td_easyweb_headers(
                    storage_state,
                    url,
                    app_id="uf-da",
                    app_version=version,
                    user_agent=user_agent,
                    referrer=referrer,
                ),
                body=body,
            )
            if _td_response_requires_auth(response):
                raise PermissionError("TD auth required")

            transactions = _td_transactions_from_payload(response.get("payload"))
            if transactions is not None:
                prepared: list[dict[str, Any]] = []
                for row in transactions:
                    record = dict(row)
                    record["_account_key"] = account_key
                    record["_account_currency"] = account_currency
                    prepared.append(record)
                return prepared, True

            if response.get("status", 0) >= 500 or _looks_like_td_transient_error(
                response.get("payload"),
                response.get("text", ""),
            ):
                _td_log_event(
                    "transaction fetch retry",
                    debug=True,
                    account_key=account_key,
                    description=description,
                    status=response.get("status"),
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                )
                continue

            if response.get("status", 0) in (400, 404):
                continue

        return None, False

    mode, history_start, history_end = _td_transaction_window(account)
    if mode != "backfill":
        await _record_td_transaction_window(
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
            await _record_td_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=history_start,
                end_date=history_end,
                error=str(exc),
            )
            raise
        await _record_td_transaction_window(
            transaction_window_recorder,
            event="completed" if succeeded else "failed",
            account=account,
            mode=mode,
            start_date=history_start,
            end_date=history_end,
            transactions=raw_transactions or [],
            error=None if succeeded else "TD transaction window failed",
        )
        return raw_transactions, succeeded

    windows, planned_windows = _td_pending_backfill_windows(
        account,
        history_start,
        history_end,
    )
    _td_bound_backfill_sync_window(account, history_start, history_end)
    seen_activity = False
    consecutive_empty_chunks = 0
    collected_all: list[dict[str, Any]] = []
    account_fetch_succeeded = True

    for current_start, current_end in windows:
        await _record_td_transaction_window(
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
            await _record_td_transaction_window(
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
            await _record_td_transaction_window(
                transaction_window_recorder,
                event="failed",
                account=account,
                mode=mode,
                start_date=current_start,
                end_date=current_end,
                error="TD transaction window failed",
            )
            account_fetch_succeeded = False
            break

        await _record_td_transaction_window(
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
            collected_all.extend(raw_transactions)
        elif seen_activity and not planned_windows:
            consecutive_empty_chunks += 1
            if consecutive_empty_chunks >= TD_EMPTY_HISTORY_STOP_CHUNKS:
                break

    return collected_all, account_fetch_succeeded


async def _sync_td_with_saved_artifacts(
    storage_state: dict[str, Any],
    session_artifact: dict[str, Any] | None,
    *,
    user_id: int | None = None,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
    replay_session: SavedArtifactReplaySession | None = None,
):
    started_at = datetime.now(timezone.utc)
    session_state = dict(session_artifact) if isinstance(session_artifact, dict) else {}
    user_agent = _td_session_user_agent(session_state)

    _td_log_event(
        "saved-session sync start",
        debug=True,
        user_id=user_id,
        has_user_agent=bool(session_state.get("user_agent")),
    )

    app_versions = await _load_td_app_versions_direct(storage_state, user_agent=user_agent)
    summary_response = await _td_direct_request(
        storage_state,
        "GET",
        TD_ACCOUNTS_SUMMARY_URL,
        headers=_td_easyweb_headers(
            storage_state,
            TD_ACCOUNTS_SUMMARY_URL,
            app_id="uf-fs",
            app_version=app_versions.get("uf-fs", TD_DEFAULT_APP_VERSIONS["uf-fs"]),
            user_agent=user_agent,
            referrer=TD_EASYWEB_SUMMARY_PAGE_URL,
        ),
    )
    if _td_response_requires_auth(summary_response):
        _td_log_event(
            "saved-session sync auth_required",
            user_id=user_id,
            engine="direct",
            message="accounts summary request returned auth required",
        )
        return None

    accounts = _td_summary_accounts(summary_response.get("payload"))
    if not accounts:
        _td_log_event(
            "saved-session sync accounts unavailable",
            user_id=user_id,
            level="warning",
            engine="direct",
            status=summary_response.get("status"),
        )
        return {"status": "error", "message": "No TD accounts returned"}

    session_state.update(refresh_session_artifact(session_state, user_agent=user_agent))

    raw_transactions: dict[str, list[dict[str, Any]]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()

    for account in accounts:
        account_key = _td_account_key(account)
        if not account_key:
            continue

        try:
            summary_external_id = account.get("external_id")
            details = await _fetch_td_account_details_direct(
                storage_state,
                account_key,
                app_versions=app_versions,
                user_agent=user_agent,
            )
            if details:
                account.update(details)
                account["external_id"] = _td_account_external_id(account)
                if summary_external_id and summary_external_id != account.get("external_id"):
                    account["summary_external_id"] = summary_external_id

            sync_windows = (
                await mode_resolver([account])
                if mode_resolver and sync_scope != "accounts"
                else {}
            )
            _td_apply_sync_window(account, sync_windows)
            if sync_scope == "accounts":
                continue
            if not _td_can_fetch_transactions(account):
                continue

            external_id = account.get("external_id")
            if not external_id:
                continue

            raw_txns, succeeded = await _fetch_td_transactions_for_account_direct(
                storage_state,
                account,
                app_versions=app_versions,
                user_agent=user_agent,
                transaction_window_recorder=transaction_window_recorder,
            )
            if raw_txns is not None:
                raw_transactions[external_id] = raw_txns
            if succeeded:
                transaction_fetch_succeeded_accounts.add(external_id)
            _td_log_event(
                "saved-session account fetched",
                debug=True,
                user_id=user_id,
                account_key=bool(account_key),
                details=bool(details),
                transactions=len(raw_txns or []),
                transaction_fetch_succeeded=bool(succeeded),
            )
        except PermissionError:
            _td_log_event(
                "saved-session sync auth_required",
                user_id=user_id,
                engine="direct",
                message="direct account request returned auth required",
            )
            return None
        except Exception as exc:
            _td_log_event(
                "saved-session account fetch failed",
                level="warning",
                user_id=user_id,
                account_key=bool(account_key),
                error=type(exc).__name__,
                message=_td_sanitize_log_text(str(exc), limit=180),
            )

    payload = {
        "status": "ok",
        "accounts": accounts,
        "transactions": raw_transactions,
        "transaction_fetch_succeeded_accounts": sorted(transaction_fetch_succeeded_accounts),
    }
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    _td_log_event(
        "saved-session sync success",
        user_id=user_id,
        engine="direct",
        accounts=len(accounts),
        transaction_accounts=len(transaction_fetch_succeeded_accounts),
        transactions=sum(len(items or []) for items in raw_transactions.values()),
        duration_ms=duration_ms,
    )
    if replay_session is not None:
        await replay_session.refresh_runtime_artifacts_async(storage_state, session_artifact=session_state)
    elif session_state and user_id is not None:
        await save_session_artifact(user_id, session_state)
    return payload


async def try_headless_sync(
    user_id: int,
    *,
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
):
    _td_log_event("headless sync start", user_id=user_id, engine="direct")
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_td_session_user_agent,
        log_event=_td_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact = replay_session.session_artifact
    if not storage_state:
        return replay_session.missing_artifact_result(stage="headless sync missing runtime_state")

    direct_payload = await _sync_td_with_saved_artifacts(
        storage_state,
        session_artifact,
        user_id=user_id,
        mode_resolver=mode_resolver,
        sync_scope=sync_scope,
        transaction_window_recorder=transaction_window_recorder,
        replay_session=replay_session,
    )
    if isinstance(direct_payload, dict) and direct_payload.get("status") == "ok":
        _td_log_event(
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
        _td_log_event(
            "headless sync result",
            user_id=user_id,
            engine="direct",
            level="warning",
            status=direct_payload.get("status"),
            accounts=len(direct_payload.get("accounts") or []),
            transaction_accounts=len(direct_payload.get("transaction_fetch_succeeded_accounts") or []),
            message=direct_payload.get("message"),
        )
        if direct_payload.get("message") == "No TD accounts returned":
            return scraper_auth_required()
        return replay_session.normalize_result(direct_payload)

    return replay_session.auth_required_result(
        log_message="saved-session direct reuse did not reach authenticated TD state"
    )


async def cleanup_pending(user_id: int):
    del user_id
    return None
