from __future__ import annotations

import logging
import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.scrapers.browser_session import (
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    scraper_session_user_agent,
    storage_state_cookie_header,
)
from app.scrapers.results import scraper_auth_required, scraper_network_error
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    scraper_debug_enabled,
)
from app.services.sync_utils import is_network_error

PROVIDER = "amex"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
AMEX_DASHBOARD_URL = "https://global.americanexpress.com/dashboard"
AMEX_ORIGIN = "https://global.americanexpress.com"
AMEX_FUNCTIONS_ORIGIN = "https://functions.americanexpress.com"
AMEX_READ_ACCOUNT_ACTIVITY_URL = f"{AMEX_FUNCTIONS_ORIGIN}/ReadAccountActivity.web.v1"
AMEX_ACTIVITY_PAGE_LIMIT = 100
AMEX_ACTIVITY_VIEW_RECENT = "RECENT"
AMEX_ACTIVITY_VIEW_BY_YEAR = "VIEW_BY_YEAR"
AMEX_BACKFILL_CHUNK_DAYS = 365
AMEX_MAX_HISTORY_DAYS = 366 * 100
AMEX_DIRECT_TIMEOUT_SECONDS = 30
AMEX_DIRECT_ENGINE = "direct"

logger = logging.getLogger("breaktwenty.scrapers.amex")

ModeResolver = Callable[[list[dict]], Awaitable[dict[str, dict[str, Any]]]]
TransactionWindowRecorder = Callable[[dict[str, Any]], Awaitable[None]]


class AmexAuthRequired(PermissionError):
    def __init__(
        self,
        message: str = "American Express sign-in required",
        *,
        stage: str | None = None,
        status: int | None = None,
        url: str | None = None,
        signal: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.url = url
        self.signal = signal


def _amex_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=("BREAKTWENTY_AMEX_DEBUG",))


def _amex_log_event(
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
        debug_enabled=_amex_debug_logs_enabled(),
        **fields,
    )


def _clean_amex_name(value: str) -> str:
    return re.sub(r"®", "", str(value or "")).strip()


def _amex_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _amex_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _amex_parse_sync_state_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _amex_sync_state_mode(
    mode_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str | None,
    alternate_external_id: str | None = None,
) -> str:
    if not mode_per_account:
        return "backfill"
    for key in (external_id, alternate_external_id):
        if not key:
            continue
        value = mode_per_account.get(key)
        if isinstance(value, dict):
            mode = str(value.get("mode") or "").strip().lower()
            if mode:
                return mode
        elif isinstance(value, str) and value.strip():
            return value.strip().lower()
    return "incremental"


def _amex_sync_state_date(
    mode_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str | None,
    field: str,
    alternate_external_id: str | None = None,
) -> date | None:
    if not mode_per_account:
        return None
    for key in (external_id, alternate_external_id):
        if not key:
            continue
        value = mode_per_account.get(key)
        if not isinstance(value, dict):
            continue
        parsed = _amex_parse_sync_state_date(value.get(field))
        if parsed:
            return parsed
    return None


def _amex_sync_state_entry(
    mode_per_account: dict[str, str | dict[str, Any]] | None,
    external_id: str | None,
    alternate_external_id: str | None = None,
) -> dict[str, Any]:
    if not mode_per_account:
        return {}
    for key in (external_id, alternate_external_id):
        if not key:
            continue
        value = mode_per_account.get(key)
        if isinstance(value, dict):
            return value
    return {}


async def _record_amex_transaction_window(
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
        _amex_log_event(
            "transaction window recorder failed",
            level="warning",
            account=account.get("name"),
            mode=mode,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            event=event,
            error=_amex_sanitize_log_text(str(exc)),
        )


def _amex_sanitize_log_text(text: Any, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit, collapse_first=True)


def _amex_statement_url_query_values(url: str | None) -> dict[str, str]:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return {}
    values = parse_qs(parsed.query, keep_blank_values=True)
    return {
        key: str(value[0]).strip()
        for key, value in values.items()
        if value and str(value[0]).strip()
    }


def _amex_direct_headers(
    storage_state: dict[str, Any],
    url: str,
    *,
    user_agent: str,
    accept: str,
    referer: str | None = None,
    origin: str | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "Accept-Language": "en-CA,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": _amex_user_agent(user_agent),
    }
    cookie_header = storage_state_cookie_header(storage_state, url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    if referer:
        headers["Referer"] = referer
    if origin:
        headers["Origin"] = origin
    return headers


def _amex_auth_required_signal(status: int, url: str, body_text: str | None = None) -> str | None:
    if status in (401, 403):
        return f"http_{status}"
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return None
    path = parsed.path.lower()
    if "login" in path:
        return "url_login_path"
    if "two-step" in path:
        return "url_two_step_path"
    lowered_body = (body_text or "").lower()
    if "elilouserid" in lowered_body:
        return "body_elilouserid"
    if "elilopassword" in lowered_body:
        return "body_elilopassword"
    return None


def _amex_account_token_from_row(row: dict[str, Any]) -> str | None:
    details = row.get("extendedTransactionDetails")
    if isinstance(details, dict):
        value = details.get("accToken")
        if value:
            token = str(value).strip()
            if token.startswith("account:"):
                token = token.split(":", 1)[1].strip()
            return token or None
    value = row.get("accToken") or row.get("accountToken")
    token = str(value).strip() if value else ""
    if token.startswith("account:"):
        token = token.split(":", 1)[1].strip()
    return token or None


def _update_account_identity_from_rows(account: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    provider_account_id = account.get("provider_account_id")
    for row in rows:
        provider_account_id = provider_account_id or _amex_account_token_from_row(row)
        if provider_account_id:
            break
    if not provider_account_id:
        return
    account["provider_account_id"] = provider_account_id
    account["alternate_external_id"] = account.get("alternate_external_id") or account.get("external_id")
    account["external_id"] = f"account:{provider_account_id}"


def _amex_activity_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    activity_data = payload.get("activityData")
    if not isinstance(activity_data, dict):
        return []
    rows: list[dict[str, Any]] = []
    for group in activity_data.get("data") or []:
        if isinstance(group, dict):
            rows.extend(row for row in group.get("transactions") or [] if isinstance(row, dict))
    return rows


def _amex_activity_years(payload: dict[str, Any]) -> list[int]:
    member = payload.get("member")
    raw_years = member.get("years") if isinstance(member, dict) else []
    years: list[int] = []
    for value in raw_years if isinstance(raw_years, list) else []:
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        if 1900 <= year <= 2100:
            years.append(year)
    return sorted(set(years), reverse=True)


def _amex_activity_start_date(payload: dict[str, Any]) -> date | None:
    member = payload.get("member")
    if not isinstance(member, dict):
        return None
    return _amex_parse_sync_state_date(member.get("startDateForSearch"))


def _amex_activity_account_token(account: dict[str, Any]) -> str:
    token = str(
        account.get("provider_account_id")
        or account.get("acc_token")
        or account.get("external_id")
        or ""
    ).strip()
    if token.startswith("account:"):
        token = token.split(":", 1)[1].strip()
    return token


async def _fetch_account_activity_view(
    client: httpx.AsyncClient,
    storage_state: dict[str, Any],
    *,
    user_agent: str,
    account: dict[str, Any],
    view: str,
    year: int | None = None,
    user_id: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    account_token = _amex_activity_account_token(account)
    if not account_token:
        raise RuntimeError("American Express activity request is missing an account token")

    headers = _amex_direct_headers(
        storage_state,
        AMEX_READ_ACCOUNT_ACTIVITY_URL,
        user_agent=user_agent,
        accept="application/json, text/plain, */*",
        referer=AMEX_DASHBOARD_URL,
        origin=AMEX_ORIGIN,
    )
    headers["CE-Source"] = "WEB"
    offset = 1
    collected_rows: list[dict[str, Any]] = []
    latest_payload: dict[str, Any] = {}
    page_count = 0
    reported_total = 0

    while True:
        page_count += 1
        body: dict[str, Any] = {
            "accountToken": account_token,
            "axplocale": "en-CA",
            "view": view,
            "transactionFilters": {
                "limit": AMEX_ACTIVITY_PAGE_LIMIT,
                "offset": offset,
            },
        }
        if year is not None:
            body["year"] = int(year)
        response = await client.post(
            AMEX_READ_ACCOUNT_ACTIVITY_URL,
            json=body,
            headers=headers,
        )
        signal = _amex_auth_required_signal(response.status_code, str(response.url), response.text)
        if signal is not None:
            raise AmexAuthRequired(
                stage="account_activity",
                status=response.status_code,
                url=str(response.url),
                signal=signal,
            )
        if not response.is_success:
            raise RuntimeError(
                f"American Express account activity failed ({response.status_code}): {response.url}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("American Express account activity returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("American Express account activity returned an unexpected payload")

        latest_payload = payload
        activity_data = payload.get("activityData")
        if not isinstance(activity_data, dict):
            raise RuntimeError("American Express account activity omitted activityData")
        raw_groups = activity_data.get("data")
        if not isinstance(raw_groups, list):
            raise RuntimeError("American Express account activity returned invalid activityData.data")
        for group in raw_groups:
            if not isinstance(group, dict) or not isinstance(group.get("transactions"), list):
                raise RuntimeError(
                    "American Express account activity returned invalid transaction groups"
                )
        if "totalTransactionCount" not in activity_data:
            raise RuntimeError("American Express account activity omitted its transaction total")
        try:
            total = int(activity_data.get("totalTransactionCount") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "American Express account activity returned an invalid transaction total"
            ) from exc
        if total < 0:
            raise RuntimeError("American Express account activity returned a negative transaction total")
        reported_total = max(reported_total, total)
        rows = _amex_activity_rows(payload)
        collected_rows.extend(rows)
        if not rows:
            if reported_total > len(collected_rows):
                raise RuntimeError(
                    "American Express account activity pagination stopped before the reported total"
                )
            break
        if (
            (reported_total > 0 and len(collected_rows) >= reported_total)
            or (reported_total == 0 and len(rows) < AMEX_ACTIVITY_PAGE_LIMIT)
        ):
            break
        offset += AMEX_ACTIVITY_PAGE_LIMIT

    _amex_log_event(
        "account activity fetched",
        user_id=user_id,
        view=view,
        year=year,
        pages=page_count,
        reported_total=reported_total,
        rows=len(collected_rows),
    )
    return latest_payload, collected_rows


async def fetch_transactions(
    client: httpx.AsyncClient,
    storage_state: dict[str, Any],
    accounts: list[dict],
    *,
    user_agent: str,
    user_id: int,
    mode_per_account: dict[str, str | dict[str, Any]] | None = None,
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> tuple[dict[str, list[dict]], set[str]]:
    results: dict[str, list[dict]] = {}
    succeeded_accounts: set[str] = set()

    for account in accounts:
        sync_window = _amex_sync_state_entry(
            mode_per_account,
            account.get("external_id"),
            account.get("alternate_external_id"),
        )
        account["sync_window"] = sync_window
        mode = _amex_sync_state_mode(
            mode_per_account,
            account.get("external_id"),
            account.get("alternate_external_id"),
        )
        latest_persisted_date = _amex_sync_state_date(
            mode_per_account,
            account.get("external_id"),
            "latest_persisted_date",
            account.get("alternate_external_id"),
        )
        end_date = (
            _amex_sync_state_date(
                mode_per_account,
                account.get("external_id"),
                "end_date",
                account.get("alternate_external_id"),
            )
            or datetime.now(timezone.utc).date()
        )
        start_date = _amex_sync_state_date(
            mode_per_account,
            account.get("external_id"),
            "start_date",
            account.get("alternate_external_id"),
        )
        backfill_start_date = _amex_sync_state_date(
            mode_per_account,
            account.get("external_id"),
            "backfill_start_date",
            account.get("alternate_external_id"),
        )

        _amex_log_event(
            "transactions account start",
            user_id=user_id,
            debug=True,
            account=account.get("name"),
            mode=mode,
            start_date=start_date.isoformat() if start_date else None,
            end_date=end_date.isoformat(),
            latest_persisted_date=latest_persisted_date.isoformat() if latest_persisted_date else None,
        )
        collected_rows: list[dict[str, Any]] = []
        account_fetch_succeeded = True
        failure_recorded = False
        coverage_start = start_date or (
            backfill_start_date if mode == "backfill" else end_date
        ) or end_date

        try:
            activity_payload, recent_rows = await _fetch_account_activity_view(
                client,
                storage_state,
                user_agent=user_agent,
                account=account,
                view=AMEX_ACTIVITY_VIEW_RECENT,
                user_id=user_id,
            )
            available_years = _amex_activity_years(activity_payload)
            if mode == "backfill":
                provider_start_date = _amex_activity_start_date(activity_payload)
                if provider_start_date is None:
                    raise RuntimeError(
                        "American Express did not provide the earliest searchable activity date"
                    )
                coverage_start = provider_start_date
                sync_window["backfill_start_date"] = coverage_start.isoformat()
                sync_window["backfill_end_date"] = end_date.isoformat()
            if coverage_start > end_date:
                coverage_start = end_date
            selected_years = [
                year
                for year in available_years
                if coverage_start.year <= year <= end_date.year
            ]

            if mode == "backfill":
                await _record_amex_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=account,
                    mode=mode,
                    start_date=coverage_start,
                    end_date=end_date,
                )
                try:
                    backfill_years = list(range(end_date.year, coverage_start.year - 1, -1))
                    for year in backfill_years:
                        _payload, rows = await _fetch_account_activity_view(
                            client,
                            storage_state,
                            user_agent=user_agent,
                            account=account,
                            view=AMEX_ACTIVITY_VIEW_BY_YEAR,
                            year=year,
                            user_id=user_id,
                        )
                        if year == end_date.year and recent_rows and not rows:
                            raise RuntimeError(
                                "American Express yearly activity was empty despite recent activity"
                            )
                        collected_rows.extend(rows)
                    if not collected_rows and (recent_rows or available_years):
                        raise RuntimeError(
                            "American Express yearly backfill was empty despite advertised activity"
                        )
                except Exception as exc:
                    await _record_amex_transaction_window(
                        transaction_window_recorder,
                        event="failed",
                        account=account,
                        mode=mode,
                        start_date=coverage_start,
                        end_date=end_date,
                        error=str(exc),
                    )
                    failure_recorded = True
                    raise
                await _record_amex_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=account,
                    mode=mode,
                    start_date=coverage_start,
                    end_date=end_date,
                    transactions=collected_rows,
                )
            else:
                await _record_amex_transaction_window(
                    transaction_window_recorder,
                    event="started",
                    account=account,
                    mode=mode,
                    start_date=coverage_start,
                    end_date=end_date,
                )
                if selected_years:
                    for year in selected_years:
                        _payload, rows = await _fetch_account_activity_view(
                            client,
                            storage_state,
                            user_agent=user_agent,
                            account=account,
                            view=AMEX_ACTIVITY_VIEW_BY_YEAR,
                            year=year,
                            user_id=user_id,
                        )
                        collected_rows.extend(rows)
                else:
                    collected_rows.extend(recent_rows)
                await _record_amex_transaction_window(
                    transaction_window_recorder,
                    event="completed",
                    account=account,
                    mode=mode,
                    start_date=coverage_start,
                    end_date=end_date,
                    transactions=collected_rows,
                )
        except Exception as exc:
            if isinstance(exc, AmexAuthRequired):
                raise
            if not failure_recorded:
                failed_start = coverage_start
                if failed_start > end_date:
                    failed_start = end_date
                await _record_amex_transaction_window(
                    transaction_window_recorder,
                    event="failed",
                    account=account,
                    mode=mode,
                    start_date=failed_start,
                    end_date=end_date,
                    error=str(exc),
                )
            _amex_log_event(
                "transactions backfill exception"
                if mode == "backfill" and not latest_persisted_date
                else "transactions incremental exception",
                user_id=user_id,
                level="warning",
                account=account.get("name"),
                message=_amex_sanitize_log_text(str(exc)),
            )
            account_fetch_succeeded = False

        deduped_rows: list[dict[str, Any]] = []
        seen_tx_ids: set[str] = set()
        for row in collected_rows:
            tx_id = (
                row.get("identifier")
                or row.get("uniqueReferenceNumber")
                or row.get("transactionId")
                or row.get("referenceNumber")
            )
            tx_key = str(tx_id) if tx_id else None
            if tx_key and tx_key in seen_tx_ids:
                continue
            if tx_key:
                seen_tx_ids.add(tx_key)
            deduped_rows.append(row)

        _update_account_identity_from_rows(account, deduped_rows)
        account_key = account.get("external_id") or account["name"]

        if account_fetch_succeeded:
            succeeded_accounts.add(account_key)
        if deduped_rows:
            results[account_key] = deduped_rows
        _amex_log_event(
            "transactions account complete",
            user_id=user_id,
            debug=True,
            account=account.get("name"),
            rows=len(deduped_rows),
            succeeded=account_fetch_succeeded,
        )

    return results, succeeded_accounts


def _amex_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _amex_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _amex_path(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _amex_values(value: Any) -> list[Any]:
    return list(value.values()) if isinstance(value, dict) else []


def _amex_first_string(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _amex_key(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _amex_parse_json(value: str | None) -> Any:
    normalized = (value or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _amex_serialized_initial_state(raw_html: str | None) -> Any:
    if not raw_html:
        return None
    decoded_html = html.unescape(str(raw_html))
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*("(?:\\.|[^"\\])*")', decoded_html, re.S)
    if match:
        return _amex_parse_json(match.group(1))

    script_match = re.search(
        r'<script[^>]+id=["\']initial-state["\'][^>]*>(.*?)</script>',
        decoded_html,
        re.I | re.S,
    )
    if not script_match:
        return None
    script_content = script_match.group(1).strip()
    nested_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*("(?:\\.|[^"\\])*")', script_content, re.S)
    if nested_match:
        return _amex_parse_json(nested_match.group(1))
    return _amex_parse_json(script_content) or script_content


def _amex_statement_data_from_html(raw_html: str | None) -> dict[str, Any] | None:
    if not raw_html:
        return None
    marker_index = str(raw_html).find("var statementData")
    if marker_index < 0:
        return None
    start_index = str(raw_html).find("{", marker_index)
    if start_index < 0:
        return None
    try:
        value, _end_index = json.JSONDecoder().raw_decode(str(raw_html)[start_index:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _amex_decode_transit(value: Any) -> Any:
    if isinstance(value, list):
        tag = value[0] if value and isinstance(value[0], str) else None
        if tag == "^ ":
            return {
                str(_amex_decode_transit(value[index])): _amex_decode_transit(value[index + 1])
                for index in range(1, len(value) - 1, 2)
            }
        if len(value) == 2 and tag in {"~#iM", "~#cmap"} and isinstance(value[1], list):
            pairs = value[1]
            return {
                str(_amex_decode_transit(pairs[index])): _amex_decode_transit(pairs[index + 1])
                for index in range(0, len(pairs) - 1, 2)
            }
        if len(value) == 2 and tag == "~#iL" and isinstance(value[1], list):
            return [_amex_decode_transit(nested) for nested in value[1]]
        if len(value) == 2 and tag and tag.startswith("~#"):
            return _amex_decode_transit(value[1])
        return [_amex_decode_transit(nested) for nested in value]
    if isinstance(value, dict):
        return {str(key): _amex_decode_transit(nested) for key, nested in value.items()}
    if value == "~#nil":
        return None
    if value == "~#true":
        return True
    if value == "~#false":
        return False
    if isinstance(value, str) and value.startswith("~:"):
        return value[2:]
    return value


def _amex_money(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {"amount": None, "currency": None}
    if isinstance(value, (int, float)):
        return {"amount": float(value), "currency": None}
    if isinstance(value, str):
        try:
            return {"amount": float(value.replace("$", "").replace(",", "")), "currency": None}
        except ValueError:
            return {"amount": None, "currency": None}
    if not isinstance(value, dict):
        return {"amount": None, "currency": None}
    raw_amount = value.get("amount", value.get("value"))
    try:
        amount = float(str(raw_amount or "").replace("$", "").replace(",", ""))
    except ValueError:
        amount = None
    return {
        "amount": amount,
        "currency": value.get("currency") or value.get("currencyCode") or value.get("isoCurrencyCode"),
    }


def _amex_first_money(*values: Any) -> dict[str, Any]:
    for value in values:
        parsed = _amex_money(value)
        if parsed.get("amount") is not None:
            return parsed
    return {"amount": None, "currency": None}


def _amex_raw_accounts_from_dashboard_state(raw_state: Any) -> list[dict[str, Any]]:
    state_input = _amex_parse_json(raw_state) if isinstance(raw_state, str) else raw_state
    state = _amex_decode_transit(state_input)
    modules = _amex_dict(_amex_path(state, "modules"))
    root = _amex_dict(modules.get("axp-myca-root"))
    navigation = _amex_dict(modules.get("axp-consumer-navigation"))
    product_sources = [
        source
        for source in (
            _amex_path(root, "products", "details", "types", "CARD_PRODUCT"),
            _amex_path(navigation, "products", "details", "types", "CARD_PRODUCT"),
        )
        if isinstance(source, dict)
    ]

    member_product_items = [
        *_amex_values(_amex_path(root, "resources", "memberProductsV2", "items")),
        *_amex_values(_amex_path(navigation, "resources", "memberProductsV2", "items")),
    ]
    for item in member_product_items:
        item_accounts = _amex_list(_amex_path(item, "accounts"))
        if not item_accounts:
            continue
        products_list: dict[str, Any] = {}
        products_order: list[str] = []
        for index, account in enumerate(item_accounts):
            if not isinstance(account, dict):
                continue
            token = _amex_first_string(
                account.get("account_token"),
                account.get("accountToken"),
                account.get("legacy_account_token"),
                account.get("opaqueAccountId"),
                account.get("account_key"),
                account.get("accountKey"),
            ) or f"resource-account-{index}"
            products_list[token] = account
            products_order.append(token)
        product_sources.append({"productsList": products_list, "productsOrder": products_order})

    products_list: dict[str, Any] = {}
    products_order: list[str] = []
    for source in product_sources:
        source_list = _amex_dict(source.get("productsList"))
        source_order = source.get("productsOrder") if isinstance(source.get("productsOrder"), list) else list(source_list)
        for token in source_order:
            key = _amex_key(token)
            if not key:
                continue
            if key not in products_list and key in source_list:
                products_list[key] = source_list[key]
            if key not in products_order:
                products_order.append(key)
        for token, details in source_list.items():
            key = _amex_key(token)
            if not key:
                continue
            products_list.setdefault(key, details)
            if key not in products_order:
                products_order.append(key)

    vitals_entries = [
        *_amex_values(_amex_path(root, "procedures", "procedureCaches", "readCardAccountVitalsV1")),
        *_amex_values(_amex_path(navigation, "procedures", "procedureCaches", "readCardAccountVitalsV1")),
        *_amex_values(_amex_path(root, "resources", "cardAccountVitalsV1", "items")),
    ]
    vitals_by_token: dict[str, dict[str, Any]] = {}
    for vitals in vitals_entries:
        if not isinstance(vitals, dict):
            continue
        tokens: set[str] = set()
        for txn in _amex_list(vitals.get("transactions")):
            if not isinstance(txn, dict):
                continue
            for token in (
                txn.get("accountToken"),
                _amex_path(txn, "extendedTransactionDetails", "accToken"),
            ):
                if token:
                    tokens.add(str(token))
        for loyalty in _amex_list(vitals.get("loyaltyAccounts")):
            if not isinstance(loyalty, dict):
                continue
            if loyalty.get("accountToken"):
                tokens.add(str(loyalty.get("accountToken")))
            for relationship in _amex_list(loyalty.get("relationships")):
                if isinstance(relationship, dict) and relationship.get("accountToken"):
                    tokens.add(str(relationship.get("accountToken")))
        if not tokens and len(products_order) == 1:
            tokens.add(products_order[0])
        for token in tokens:
            vitals_by_token[token] = vitals

    raw_accounts: list[dict[str, Any]] = []
    for index, token in enumerate(products_order):
        product_details = _amex_dict(products_list.get(token))
        account = _amex_dict(product_details.get("account"))
        product = _amex_dict(product_details.get("product"))
        provider_account_id = _amex_first_string(
            product_details.get("account_token"),
            product_details.get("accountToken"),
            product_details.get("legacy_account_token"),
            product_details.get("opaqueAccountId"),
            token,
        )
        display_number = _amex_first_string(
            account.get("display_account_number"),
            account.get("displayAccountNumber"),
            product_details.get("display_account_number"),
            product_details.get("displayAccountNumber"),
        )
        vitals = _amex_dict(vitals_by_token.get(provider_account_id) or vitals_by_token.get(str(token)))
        balance_details = _amex_dict(_amex_path(vitals, "balanceCreditDetails", "balanceDetails"))
        balances = _amex_list(vitals.get("balances"))
        first_balance = _amex_dict(balances[0]) if balances else {}
        balance = _amex_first_money(
            _amex_path(balance_details, "totalBalanceData", "totalBalance"),
            first_balance.get("statementBalanceAmount"),
            first_balance.get("remainingStatementBalanceAmount"),
            first_balance.get("lastStatementBalanceAmount"),
        )
        name = _amex_first_string(
            product.get("description"),
            product.get("name"),
            product_details.get("product_name"),
            product_details.get("name"),
        )
        raw_account = {
            "name": name or None,
            "provider_account_id": provider_account_id or None,
            "acc_token": provider_account_id or None,
            "account_key": _amex_first_string(
                product_details.get("account_key"),
                product_details.get("accountKey"),
                product_details.get("accountKeyValue"),
            ) or None,
            "bp_index": product_details.get("sorted_index", product_details.get("sortedIndex", index)),
            "sorted_index": product_details.get("sorted_index", product_details.get("sortedIndex", index)),
            "display_account_number": display_number or None,
            "last4": display_number[-4:] if display_number else None,
            "last5": display_number[-5:] if len(display_number) >= 5 else None,
            "balance": balance.get("amount"),
            "currency": balance.get("currency") or "CAD",
        }
        if raw_account["name"] and (
            raw_account["provider_account_id"] or raw_account["account_key"] or raw_account["last4"]
        ):
            raw_accounts.append(raw_account)
    return raw_accounts


def _amex_raw_accounts_from_statement_data(
    statement_data: dict[str, Any] | None,
    *,
    statement_url: str | None = None,
) -> list[dict[str, Any]]:
    data = _amex_dict((statement_data or {}).get("data"))
    card_member = _amex_dict(_amex_path(data, "cardMemberInfo", "CARD_MEMBER"))
    cards = _amex_list(card_member.get("cards"))
    query_values = _amex_statement_url_query_values(statement_url)
    balance_info = _amex_dict(data.get("defaultBalanceInfo"))
    raw_accounts: list[dict[str, Any]] = []

    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        product = _amex_dict(card.get("productId"))
        provider_account_id = _amex_first_string(
            card.get("accountToken"),
            card.get("accToken"),
            card.get("account_token"),
        )
        display_number = _amex_first_string(
            card.get("obfuscatedAccountNumber"),
            card.get("lastFiveDigits"),
        )
        account_key = _amex_first_string(
            card.get("cardKeyData"),
            card.get("accountKey"),
            card.get("account_key"),
            query_values.get("account_key"),
        )
        bp_index = card.get("sortedIndex", query_values.get("BPIndex", index))
        balance = _amex_first_money(
            balance_info.get("totalBalance"),
            balance_info.get("totalBalanceCharge"),
            balance_info.get("balanceDue"),
            balance_info.get("lastStmtBalTot"),
        )
        name = _amex_first_string(
            product.get("cardProductDesc"),
            product.get("name"),
            product.get("description"),
        )
        raw_account = {
            "name": name or None,
            "provider_account_id": provider_account_id or None,
            "acc_token": provider_account_id or None,
            "account_key": account_key or None,
            "bp_index": bp_index,
            "sorted_index": bp_index,
            "display_account_number": display_number or None,
            "last4": display_number[-4:] if display_number else None,
            "last5": str(card.get("lastFiveDigits") or "").strip() or (display_number[-5:] if len(display_number) >= 5 else None),
            "balance": balance.get("amount"),
            "currency": balance.get("currency") or balance_info.get("currencyCode") or "CAD",
        }
        if raw_account["name"] and (
            raw_account["provider_account_id"] or raw_account["account_key"] or raw_account["last4"]
        ):
            raw_accounts.append(raw_account)
    return raw_accounts


def _amex_accounts_from_raw_accounts(raw_accounts: list[Any]) -> list[dict[str, Any]]:
    def raw_account_quality(raw_account: dict[str, Any]) -> int:
        provider_account_id = str(
            raw_account.get("provider_account_id")
            or raw_account.get("acc_token")
            or ""
        ).strip()
        account_key = str(raw_account.get("account_key") or "").strip()
        score = 0
        if raw_account.get("balance") is not None:
            score += 100
        if provider_account_id:
            score += 20
        if account_key:
            score += 20
        if provider_account_id and account_key and provider_account_id != account_key:
            score += 20
        if raw_account.get("last5"):
            score += 5
        if raw_account.get("last4"):
            score += 3
        return score

    accounts: list[dict[str, Any]] = []
    alias_to_account_index: dict[str, int] = {}
    sorted_raw_accounts = sorted(
        (
            (index, raw_account)
            for index, raw_account in enumerate(raw_accounts)
            if isinstance(raw_account, dict)
        ),
        key=lambda item: raw_account_quality(item[1]),
        reverse=True,
    )
    for index, raw_account in sorted_raw_accounts:
        name = _clean_amex_name(str(raw_account.get("name") or ""))
        if not name:
            continue
        provider_account_id = str(
            raw_account.get("provider_account_id")
            or raw_account.get("acc_token")
            or ""
        ).strip()
        account_key = str(raw_account.get("account_key") or "").strip()
        last4 = str(raw_account.get("last4") or "").strip() or None
        last5 = str(raw_account.get("last5") or "").strip()
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in (
                    f"account_key:{account_key}" if account_key else None,
                    f"last5:{last5}" if last5 else None,
                    f"name_last4:{name.lower()}:{last4}" if last4 else None,
                    f"provider:{provider_account_id}" if provider_account_id else None,
                    f"name_index:{name.lower()}:{index}" if not (account_key or last5 or last4 or provider_account_id) else None,
                )
                if alias
            )
        )
        existing_index = next(
            (alias_to_account_index[alias] for alias in aliases if alias in alias_to_account_index),
            None,
        )
        if existing_index is not None:
            existing = accounts[existing_index]
            if account_key and not existing.get("account_key"):
                existing["account_key"] = account_key
            if provider_account_id and not existing.get("provider_account_id"):
                existing["provider_account_id"] = provider_account_id
                existing["acc_token"] = provider_account_id
                existing["external_id"] = f"account:{provider_account_id}"
            if last4 and not existing.get("last4"):
                existing["last4"] = last4
                existing["alternate_external_id"] = f"suffix:{last4}"
            if last5 and not existing.get("last5"):
                existing["last5"] = last5
            for alias in aliases:
                alias_to_account_index.setdefault(alias, existing_index)
            continue

        try:
            balance = float(raw_account.get("balance") or 0)
        except (TypeError, ValueError):
            balance = 0.0

        account = {
            "name": name,
            "balance": balance,
            "currency": str(raw_account.get("currency") or "CAD"),
            "bp_index": raw_account.get("bp_index", index),
            "sorted_index": raw_account.get("sorted_index", index),
        }
        if provider_account_id:
            account["provider_account_id"] = provider_account_id
            account["acc_token"] = provider_account_id
            account["external_id"] = f"account:{provider_account_id}"
        if account_key:
            account["account_key"] = account_key
        if last4:
            account["last4"] = last4
            account["alternate_external_id"] = f"suffix:{last4}"
            account.setdefault("external_id", f"suffix:{last4}")
        if last5:
            account["last5"] = last5
        accounts.append(account)
        account_index = len(accounts) - 1
        for alias in aliases:
            alias_to_account_index[alias] = account_index

    accounts.sort(key=lambda account: account.get("sorted_index", account.get("bp_index", 9999)))
    return accounts


def _amex_accounts_from_dashboard_html(raw_html: str | None) -> list[dict[str, Any]]:
    raw_accounts: list[dict[str, Any]] = []
    statement_data = _amex_statement_data_from_html(raw_html)
    if statement_data is not None:
        raw_accounts.extend(_amex_raw_accounts_from_statement_data(statement_data))
    raw_state = _amex_serialized_initial_state(raw_html)
    if raw_state is not None:
        raw_accounts.extend(_amex_raw_accounts_from_dashboard_state(raw_state))
    if not raw_accounts:
        return []
    return _amex_accounts_from_raw_accounts(raw_accounts)


async def _fetch_amex_dashboard_html_direct(
    client: httpx.AsyncClient,
    storage_state: dict[str, Any],
    *,
    user_agent: str,
    user_id: int,
) -> str | None:
    response = await client.get(
        AMEX_DASHBOARD_URL,
        headers=_amex_direct_headers(
            storage_state,
            AMEX_DASHBOARD_URL,
            user_agent=user_agent,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            referer="https://www.americanexpress.com/",
        ),
    )
    signal = _amex_auth_required_signal(response.status_code, str(response.url), response.text)
    if signal is not None:
        raise AmexAuthRequired(
            stage="dashboard_fetch",
            status=response.status_code,
            url=str(response.url),
            signal=signal,
        )
    if response.status_code >= 400:
        _amex_log_event(
            "dashboard direct fetch failed",
            user_id=user_id,
            level="warning",
            status=response.status_code,
            url=str(response.url),
            body=_amex_sanitize_log_text(response.text, limit=180),
        )
        return None
    return response.text


async def _build_authenticated_payload_direct(
    user_id: int,
    client: httpx.AsyncClient,
    storage_state: dict[str, Any],
    *,
    session_artifact: dict[str, Any],
    mode_resolver: ModeResolver | None = None,
    sync_scope: str = "full",
    transaction_window_recorder: TransactionWindowRecorder | None = None,
) -> dict[str, Any] | None:
    user_agent = _amex_session_user_agent(session_artifact)
    dashboard_html = await _fetch_amex_dashboard_html_direct(
        client,
        storage_state,
        user_agent=user_agent,
        user_id=user_id,
    )
    accounts = _amex_accounts_from_dashboard_html(dashboard_html)
    if not accounts:
        _amex_log_event(
            "authenticated payload missing accounts",
            user_id=user_id,
            level="warning",
            engine=AMEX_DIRECT_ENGINE,
            has_dashboard_response_initial_state=bool(dashboard_html and "window.__INITIAL_STATE__" in dashboard_html),
        )
        return None

    raw_transactions: dict[str, list[dict]] = {}
    transaction_fetch_succeeded_accounts: set[str] = set()
    if sync_scope != "accounts":
        mode_per_account = await mode_resolver(accounts) if mode_resolver else None
        raw_transactions, transaction_fetch_succeeded_accounts = await fetch_transactions(
            client,
            storage_state,
            accounts,
            user_agent=user_agent,
            user_id=user_id,
            mode_per_account=mode_per_account,
            transaction_window_recorder=transaction_window_recorder,
        )
    _amex_log_event(
        "authenticated payload built",
        user_id=user_id,
        accounts=len(accounts),
        transaction_accounts=len(raw_transactions),
        succeeded_transaction_accounts=len(transaction_fetch_succeeded_accounts),
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
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_amex_session_user_agent,
        engine=AMEX_DIRECT_ENGINE,
        log_event=_amex_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact
    if not isinstance(storage_state, dict):
        return replay_session.missing_artifact_result()

    _amex_log_event("headless sync start", user_id=user_id, engine=AMEX_DIRECT_ENGINE)
    try:
        async with replay_session.http_client(
            timeout=AMEX_DIRECT_TIMEOUT_SECONDS,
            follow_redirects=True,
            seed_cookies=True,
        ) as replay:
            payload = await _build_authenticated_payload_direct(
                user_id,
                replay.client,
                storage_state,
                session_artifact=session_artifact_state,
                mode_resolver=mode_resolver,
                sync_scope=sync_scope,
                transaction_window_recorder=transaction_window_recorder,
            )
    except AmexAuthRequired as exc:
        storage_cookies = (
            len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0
        )
        _amex_log_event(
            "headless sync auth_required",
            user_id=user_id,
            engine=AMEX_DIRECT_ENGINE,
            message="saved-session direct reuse did not reach authenticated American Express state",
            auth_stage=exc.stage,
            auth_signal=exc.signal,
            auth_status=exc.status,
            auth_url=exc.url,
            storage_cookies=storage_cookies,
        )
        return scraper_auth_required()
    except Exception as exc:
        if is_network_error(exc):
            _amex_log_event(
                "headless sync network_error",
                user_id=user_id,
                engine=AMEX_DIRECT_ENGINE,
                level="warning",
                exception_type=type(exc).__name__,
                message=_amex_sanitize_log_text(str(exc) or type(exc).__name__),
            )
            return scraper_network_error()
        _amex_log_event(
            "headless sync failed",
            user_id=user_id,
            engine=AMEX_DIRECT_ENGINE,
            level="warning",
            exception_type=type(exc).__name__,
            message=_amex_sanitize_log_text(str(exc)),
        )
        raise

    if payload:
        await replay_session.refresh_runtime_artifacts_async(storage_state)
        _amex_log_event("headless sync success", user_id=user_id, engine=AMEX_DIRECT_ENGINE)
    else:
        _amex_log_event("headless sync empty payload", user_id=user_id, engine=AMEX_DIRECT_ENGINE, level="warning")
    return replay_session.normalize_result(payload)
