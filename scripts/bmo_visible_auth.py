#!/usr/bin/env python3
"""Desktop visible-auth runner for BMO."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from browser_timezone import DEFAULT_USER_TIMEZONE, normalize_browser_timezone
from desktop_browser_runtime import DesktopBrowserRuntimeError, ensure_brave_browser_runtime
import visible_auth_common as visible_auth

try:
    from patchright.async_api import async_playwright
except ImportError as exc:  # pragma: no cover - host environment only
    raise SystemExit(visible_auth.PATCHRIGHT_SETUP_MESSAGE) from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_AUTH_DIR = visible_auth.resolve_desktop_auth_dir(REPO_ROOT)
VISIBLE_AUTH_LOG_DIR = DESKTOP_AUTH_DIR / "logs" / "visible-auth"
BMO_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "bmo"
BMO_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "BMO")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "bmo").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()
BMO_ONLINE_BANKING_HAR_PATH = (
    Path(str(os.getenv("BREAKTWENTY_BMO_ONLINE_BANKING_HAR"))).expanduser()
    if os.getenv("BREAKTWENTY_BMO_ONLINE_BANKING_HAR")
    else None
)

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

BMO_ORIGIN = "https://www1.bmo.com"
BMO_ACCOUNTS_URL = f"{BMO_ORIGIN}/banking/digital/accounts"
BMO_LOGIN_URL = f"{BMO_ORIGIN}/banking/digital/login?lang=en"
BMO_VERIFY_CREDENTIAL_PATH = "/banking/services/signin/verifyCredential"
BMO_AUTHENTICATE_PATH = "/banking/services/signin/authenticate"
BMO_ENTITLEMENTS_PATH = "/banking/services/accounts/entitlements"
BMO_CDB_INIT_SESSION_PATH = "/api/cdb/contact-handler/session/initCDBSession"
BMO_CDB_ENTITLEMENTS_PATH = "/api/cdb/customer-access-entitlement/accounts/entitlements"
BMO_BANK_DETAILS_PATH = "/api/cdb/current-account/accountdetails/getBankAccountDetails"
BMO_BANK_DETAILS_URL = f"{BMO_ORIGIN}{BMO_BANK_DETAILS_PATH}"
BMO_SIGNON_OTP_PATH = "/banking/services/otp/signin/signOnOTP"
BMO_PAGE_URL_MARKERS = ("www1.bmo.com", "bmo.com")
BMO_USERNAME_SELECTORS = (
    'input[name="username-input"]',
    'fdc-input[name="username-input"] input',
    'input[aria-label="Enter your 16 digit card number or Login ID"]',
)
BMO_PASSWORD_SELECTORS = (
    'input[name="password-input"]',
    'fdc-input[name="password-input"] input',
    'input[aria-label="Enter your online banking password"]',
)
BMO_CREDENTIAL_CAPTURE_TIMEOUT_SECONDS = 1.0
BMO_CODE_SELECTORS = (
    "#otp-input",
    'input#otp-input[type="tel"]',
    "otp-enter-code input[type='tel']",
)
BMO_HAR_FILENAME_PREFIX = "bmo_visible_auth"
BMO_MAX_SANITIZED_HARS_PER_USER = 3
BMO_REDACTED_VALUE = "<redacted>"
BMO_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "callstatus",
        "categoryname",
        "description",
        "deviceType",
        "error",
        "errorcode",
        "groupheadtitle",
        "incompletebalance",
        "message",
        "productname",
        "responsemsg",
        "status",
        "type",
    }
)
BMO_HAR_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)
BMO_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1,C0005:1"
BMO_COOKIE_BANNER_SCRIPT = """
(() => {
    const isBmoHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host === "bmo.com" || host.endsWith(".bmo.com");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isBmoHost()) {
            return;
        }
        const acceptButton = document.querySelector("#onetrust-accept-btn-handler");
        if (acceptButton && !acceptButton.dataset.breaktwentyAccepted) {
            acceptButton.dataset.breaktwentyAccepted = "1";
            try {
                acceptButton.click();
            } catch (_err) {
            }
        }
        for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
                if (!(node instanceof HTMLElement)) {
                    continue;
                }
                node.style.setProperty("display", "none", "important");
                node.style.setProperty("visibility", "hidden", "important");
                node.style.setProperty("opacity", "0", "important");
                node.style.setProperty("pointer-events", "none", "important");
            }
        }
        if (document.documentElement instanceof HTMLElement) {
            document.documentElement.style.removeProperty("overflow");
        }
        if (document.body instanceof HTMLElement) {
            document.body.style.removeProperty("overflow");
        }
    };
    clearBanner();
})();
"""
BMO_WAIT_POLL_SECONDS = 0.35
BMO_STORAGE_STATE_TIMEOUT_SECONDS = 15
BMO_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
BMO_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
BMO_BROWSER_TRANSACTION_CAPTURE_TIMEOUT_SECONDS = 20
BMO_BROWSER_TRANSACTION_EVALUATE_TIMEOUT_SECONDS = 15
BMO_BACKFILL_CHUNK_DAYS = 365
BMO_MAX_HISTORY_DAYS = 3650
BMO_HISTORY_EDGE_TOLERANCE_DAYS = 7
BMO_BROWSER_TRANSACTION_FETCH_CONCURRENCY = 4
BMO_SYSTEM_ERROR_MESSAGE = (
    "BMO opened a system error page after verification. "
    f"Start BMO login again; {visible_auth.APP_BRAND_NAME} saved the failed secure-login capture for diagnostics."
)
HOST_BROWSER_CLOSED_MESSAGE = (
    "Login window was closed before secure login finished. "
    "Start sync again and keep the bank browser window open until the app says it is done."
)


async def log_support_event(**kwargs: Any) -> None:
    await VISIBLE_AUTH_CLIENT.log_support_event(**kwargs)


async def persist_visible_auth_artifact(*, artifact_type: str, payload: Any) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_artifact(
        artifact_type=artifact_type,
        payload=payload,
    )


async def persist_visible_auth_credentials(username: str, password: str) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_credentials(username, password)


def _bmo_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    filename = f"{BMO_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}.har"
    return BMO_DESKTOP_AUTH_RAW_HAR_DIR / filename


def _bmo_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    filename = f"{BMO_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}{scope_part}_{result}.har"
    return BMO_DIAGNOSTIC_DIR / filename


def _bmo_sanitized_har_candidates(user_id: int) -> list[Path]:
    if not BMO_DIAGNOSTIC_DIR.exists():
        return []
    return list(BMO_DIAGNOSTIC_DIR.glob(f"{BMO_HAR_FILENAME_PREFIX}_user-{user_id}_*.har"))


def _bmo_consent_cookie_payload() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    expiry = int(now.timestamp()) + (365 * 24 * 60 * 60)
    datestamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    consent_value = urlencode(
        [
            ("isGpcEnabled", "0"),
            ("datestamp", datestamp),
            ("version", "202602.1.0"),
            ("browserGpcFlag", "1"),
            ("isIABGlobal", "false"),
            ("hosts", ""),
            ("consentId", str(uuid.uuid4())),
            ("interactionCount", "1"),
            ("isAnonUser", "1"),
            ("prevHadToken", "0"),
            ("landingPath", "NotLandingPage"),
            ("groups", BMO_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".bmo.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".bmo.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "ot-consent-one-time",
            "value": "trigger",
            "domain": ".bmo.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]


async def _bmo_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_bmo_consent_cookie_payload())
    except Exception:
        pass


async def _bmo_clear_cookie_consent_noise(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    try:
        await raw_page.evaluate(BMO_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass


def _bmo_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not (host == "www1.bmo.com" or host.endswith(".bmo.com")):
        return False
    if "json" not in str(content_type or "").lower():
        return False
    if status and int(status) >= 500:
        return True
    return path in {
        BMO_VERIFY_CREDENTIAL_PATH.lower(),
        BMO_AUTHENTICATE_PATH.lower(),
        BMO_ENTITLEMENTS_PATH.lower(),
        BMO_BANK_DETAILS_PATH.lower(),
        BMO_SIGNON_OTP_PATH.lower(),
    }


def finalize_bmo_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=_bmo_sanitized_har_path(user_id, timestamp_slug, result),
        sanitized_paths_to_prune=_bmo_sanitized_har_candidates(user_id),
        keep=BMO_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_bmo_should_keep_response_body,
        safe_response_keys=BMO_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=BMO_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=BMO_REDACTED_VALUE,
    )


def _json_body(text: str | None) -> Any:
    normalized = (text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _request_body(post_data: str) -> Any:
    text = str(post_data or "").strip()
    if not text:
        return None
    if text[:1] in {"{", "["}:
        parsed = _json_body(text)
        if parsed is not None:
            return parsed
    parsed = parse_qs(text, keep_blank_values=True)
    if parsed:
        return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}
    return None


def _bmo_endpoint_key(url: str) -> str | None:
    path = (urlsplit(str(url or "")).path or "").lower()
    if path == BMO_VERIFY_CREDENTIAL_PATH.lower():
        return "verify_credential"
    if path == BMO_AUTHENTICATE_PATH.lower():
        return "authenticate"
    if path == BMO_ENTITLEMENTS_PATH.lower():
        return "entitlements"
    if path == BMO_CDB_INIT_SESSION_PATH.lower():
        return "init_cdb_session"
    if path == BMO_CDB_ENTITLEMENTS_PATH.lower():
        return "entitlements"
    if path == BMO_BANK_DETAILS_PATH.lower():
        return "bank_account_details"
    if path == BMO_SIGNON_OTP_PATH.lower():
        return "sign_on_otp"
    return None


def _is_bmo_storage_host(host: str) -> bool:
    normalized = str(host or "").lower()
    return normalized == "bmo.com" or normalized.endswith(".bmo.com")


def _is_bmo_consent_cookie_name(name: str) -> bool:
    normalized = str(name or "").lower()
    return "consent" in normalized or "optanon" in normalized or "onetrust" in normalized


def _collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for nested in value.values():
            values.extend(_collect_strings(nested))
        return values
    if isinstance(value, list):
        values: list[str] = []
        for nested in value:
            values.extend(_collect_strings(nested))
        return values
    return []


def _payload_text(payload: Any) -> str:
    return " ".join(text.strip() for text in _collect_strings(payload) if text and text.strip())


def _looks_like_invalid_credentials(payload: Any) -> bool:
    text = _payload_text(payload).lower()
    return any(
        keyword in text
        for keyword in (
            "invalid",
            "incorrect",
            "doesn't match our records",
            "does not match our records",
            "not recognized",
            "card disabled",
            "password lock",
            "wrong password",
        )
    )


def _extract_summary(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("categories"), list):
        return payload
    for response_key in ("AuthenticateRs", "InitCDBSessionRs"):
        body = ((payload.get(response_key) or {}).get("BodyRs") or {})
        summary = body.get("mySummary")
        if isinstance(summary, dict):
            return summary
    if isinstance(payload.get("mySummary"), dict):
        return payload["mySummary"]
    return None


def _extract_mfa_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for outer in payload.values():
        if not isinstance(outer, dict):
            continue
        for container_key in ("HdrRs", "BodyRs", "HdrRq", "BodyRq"):
            container = outer.get(container_key)
            if not isinstance(container, dict):
                continue
            for key in ("mfaDeviceToken", "mfa_device_token"):
                value = str(container.get(key) or "").strip()
                if value:
                    return value
    return ""


def _entitlements_account_total(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    body = ((payload.get("EntitlementsRs") or {}).get("BodyRs") or {})
    if not isinstance(body, dict):
        return 0
    return sum(
        len(body.get(key) or [])
        for key in ("bankAccounts", "creditCardAccounts", "loanAccounts", "investmentAccounts")
    )


def _extract_entitlements_body(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    body = ((payload.get("EntitlementsRs") or {}).get("BodyRs") or {})
    return body if isinstance(body, dict) else None


def _extract_bank_account_details_body(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    body = ((payload.get("GetBankAccountDetailsRs") or {}).get("BodyRs") or {})
    return body if isinstance(body, dict) else None


def _bank_account_detail_record(payload: Any, request_payload: Any = None) -> dict[str, Any] | None:
    body = _extract_bank_account_details_body(payload)
    if not isinstance(body, dict):
        return None
    details = body.get("bankAccountDetails")
    transactions = body.get("bankAccountTransactions")
    request_body = (
        (((request_payload or {}).get("GetBankAccountDetailsRq") or {}).get("BodyRq") or {})
        if isinstance(request_payload, dict)
        else {}
    )
    raw_account_index = request_body.get("accountIndex")
    return {
        "account_index": str(raw_account_index).strip() if raw_account_index is not None else None,
        "start_date": str(request_body.get("filterFromDate") or "").strip() or None,
        "end_date": str(request_body.get("filterToDate") or "").strip() or None,
        "details": details if isinstance(details, dict) else {},
        "transactions": [item for item in (transactions or []) if isinstance(item, dict)],
        "more_txns": str((details or {}).get("moreTxns") or "").upper() == "Y" if isinstance(details, dict) else False,
    }


def _extract_credentials_from_payload(payload: Any) -> tuple[str, str | None]:
    body = ((payload or {}).get("VerifyCredentialRq") or {}).get("BodyRq") if isinstance(payload, dict) else {}
    if not isinstance(body, dict):
        body = {}
    username = str(body.get("credential") or body.get("username") or body.get("loginId") or "").strip()
    password = ""
    for key, value in body.items():
        if "password" in str(key).lower() or str(key).lower() in {"passcode", "secret"}:
            password = str(value or "")
            break
    return username, password or None


def _capture_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = str(username or "").strip()
    if not normalized:
        return
    captured_credentials["username"] = normalized
    captured_credentials[f"_{source}_username_capture_count"] = str(
        int(captured_credentials.get(f"_{source}_username_capture_count") or 0) + 1
    )


def _capture_password(captured_credentials: dict[str, str], password: str, *, source: str) -> None:
    if not password or visible_auth._is_masked_secret(password):
        if password and visible_auth._is_masked_secret(password):
            captured_credentials[f"_{source}_masked_password_count"] = str(
                int(captured_credentials.get(f"_{source}_masked_password_count") or 0) + 1
            )
        return
    captured_credentials["password"] = str(password)
    captured_credentials[f"_{source}_password_capture_count"] = str(
        int(captured_credentials.get(f"_{source}_password_capture_count") or 0) + 1
    )


async def _capture_bmo_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    raw_page = getattr(page, "raw_page", page)
    try:
        values = await asyncio.wait_for(
            raw_page.evaluate(
                """
                ({ usernameSelectors, passwordSelectors }) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const valueFor = (selectors) => {
                        for (const selector of selectors) {
                            for (const el of document.querySelectorAll(selector)) {
                                if (isVisible(el) && typeof el.value === "string" && el.value) {
                                    return el.value;
                                }
                            }
                        }
                        return "";
                    };
                    return {
                        username: valueFor(usernameSelectors),
                        password: valueFor(passwordSelectors),
                    };
                }
                """,
                {
                    "usernameSelectors": list(BMO_USERNAME_SELECTORS),
                    "passwordSelectors": list(BMO_PASSWORD_SELECTORS),
                },
            ),
            timeout=BMO_CREDENTIAL_CAPTURE_TIMEOUT_SECONDS,
        )
    except Exception:
        return
    if not isinstance(values, dict):
        return
    _capture_username(captured_credentials, str(values.get("username") or ""), source="raw")
    _capture_password(captured_credentials, str(values.get("password") or ""), source="raw")


def _capture_bmo_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    if _bmo_endpoint_key(str(getattr(request, "url", "") or "")) != "verify_credential":
        return
    username, password = _extract_credentials_from_payload(_request_body(visible_auth.safe_request_post_data(request)))
    if username:
        _capture_username(captured_credentials, username, source="request")
    if password:
        _capture_password(captured_credentials, password, source="request")


async def _capture_bmo_request(request, auth_signals: dict[str, Any]) -> None:
    key = _bmo_endpoint_key(str(getattr(request, "url", "") or ""))
    try:
        headers = await request.all_headers()
    except Exception:
        headers = dict(getattr(request, "headers", {}) or {})
    request_payload = _request_body(visible_auth.safe_request_post_data(request))
    template = {
        "url": str(getattr(request, "url", "") or ""),
        "headers": headers if isinstance(headers, dict) else {},
        "payload": request_payload if isinstance(request_payload, dict) else {},
    }
    header_names = {str(name).lower() for name in template["headers"]}
    if key in {"init_cdb_session", "entitlements", "bank_account_details"} and {"authorization", "dpop"} <= header_names:
        auth_signals["cdb_request_template"] = template
    if key != "bank_account_details":
        return
    auth_signals["bank_account_detail_request_template"] = template


async def _capture_bmo_response(response, auth_signals: dict[str, Any]) -> None:
    key = _bmo_endpoint_key(str(getattr(response, "url", "") or ""))
    if not key:
        return
    try:
        text = await response.text()
    except Exception:
        text = ""
    payload = _json_body(text)
    if key == "verify_credential" and _looks_like_invalid_credentials(payload):
        auth_signals["invalid_credentials"] = True
    token = _extract_mfa_token(payload)
    if token:
        auth_signals["mfa_device_token"] = token
    summary = _extract_summary(payload)
    if summary:
        auth_signals["summary"] = summary
        auth_signals["authenticated"] = True
    entitlements = _extract_entitlements_body(payload)
    if key == "entitlements" and entitlements is not None:
        auth_signals["entitlements"] = entitlements
    if key == "entitlements" and _entitlements_account_total(payload) > 0:
        auth_signals["authenticated"] = True
    if key == "bank_account_details":
        request_payload = _request_body(visible_auth.safe_request_post_data(response.request))
        record = _bank_account_detail_record(payload, request_payload)
        if record:
            records = auth_signals.setdefault("bank_account_detail_responses", [])
            if isinstance(records, list):
                records.append(record)


def bind_bmo_page_watcher(page, captured_credentials: dict[str, str], auth_signals: dict[str, Any]) -> None:
    if getattr(page, "_bmo_visible_auth_watcher_installed", False):
        return
    setattr(page, "_bmo_visible_auth_watcher_installed", True)

    def on_request(request) -> None:
        key = _bmo_endpoint_key(str(getattr(request, "url", "") or ""))
        if key == "verify_credential":
            asyncio.create_task(_capture_bmo_raw_input_credentials(page, captured_credentials))
            _capture_bmo_credentials_from_request(captured_credentials, request)
        if key in {"init_cdb_session", "entitlements", "bank_account_details"}:
            asyncio.create_task(_capture_bmo_request(request, auth_signals))

    def on_response(response) -> None:
        if _bmo_endpoint_key(str(getattr(response, "url", "") or "")):
            asyncio.create_task(_capture_bmo_response(response, auth_signals))

    page.on("request", on_request)
    page.on("response", on_response)


async def _prepare_bmo_context_page(context, captured_credentials: dict[str, str], auth_signals: dict[str, Any]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_bmo_page_watcher(page, captured_credentials, auth_signals)
        for extra_page in pages[1:]:
            bind_bmo_page_watcher(extra_page, captured_credentials, auth_signals)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_bmo_page_watcher(page, captured_credentials, auth_signals)
    return page


async def get_bmo_page_state(page, auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    try:
        state = await raw_page.evaluate(
            """
            ({ usernameSelectors, passwordSelectors, codeSelectors }) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                };
                const visibleMatches = (selectors) => selectors.some((selector) =>
                    Array.from(document.querySelectorAll(selector)).some(isVisible)
                );
                const visibleEnabledMatches = (selector) => Array.from(document.querySelectorAll(selector))
                    .some((el) => isVisible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true");
                const buttonAriaMatches = (pattern) => Array.from(document.querySelectorAll("button, [role='button'], input[type='submit'], input[type='button']"))
                    .filter(isVisible)
                    .some((el) => String(el.getAttribute("aria-label") || el.getAttribute("name") || "").trim().toLowerCase() === pattern);
                const path = window.location.pathname || "";
                const usernameVisible = visibleMatches(usernameSelectors);
                const passwordVisible = visibleMatches(passwordSelectors);
                const codeVisible = visibleMatches(codeSelectors);
                const otpIntro = visibleEnabledMatches("otp-intro otp-button button");
                const otpDelivery = visibleEnabledMatches("otp-select-method otp-button button");
                const trustPrompt = visibleEnabledMatches("div.trust-device-card-content button, div.trust-device-card-content fdc-button button");
                const systemError = /\\/banking\\/digital\\/error\\//.test(path);
                const authenticatedPath = /\\/banking\\/digital\\/(accounts|account-details)/.test(path);
                const signOutVisible = buttonAriaMatches("sign out");
                return {
                    current_url: window.location.href,
                    username_visible: usernameVisible,
                    password_visible: passwordVisible,
                    code_visible: codeVisible,
                    otp_intro: otpIntro,
                    otp_delivery_options: otpDelivery,
                    trust_device_prompt: trustPrompt,
                    system_error_page: systemError,
                    authenticated:
                        !usernameVisible &&
                        !passwordVisible &&
                        !codeVisible &&
                        !systemError &&
                        (authenticatedPath || signOutVisible),
                };
            }
            """,
            {
                "usernameSelectors": list(BMO_USERNAME_SELECTORS),
                "passwordSelectors": list(BMO_PASSWORD_SELECTORS),
                "codeSelectors": list(BMO_CODE_SELECTORS),
            },
        )
    except Exception:
        state = {"current_url": visible_auth.safe_page_url(raw_page), "authenticated": False}
    if auth_signals.get("authenticated"):
        state["authenticated"] = True
    state["invalid_credentials"] = bool(auth_signals.get("invalid_credentials"))
    return state


def describe_bmo_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated BMO session detected."
    if state.get("invalid_credentials"):
        return "BMO showed a credential error. Correct the credentials in the secure browser and continue."
    if state.get("code_visible") or state.get("otp_intro") or state.get("otp_delivery_options") or state.get("trust_device_prompt"):
        return "Complete BMO verification in the secure browser."
    if state.get("username_visible") or state.get("password_visible"):
        return "Sign in to BMO in the secure browser."
    if state.get("system_error_page"):
        return "BMO opened an error page. Start BMO login again."
    return "Waiting for BMO secure login."


async def _fill_first_visible(page, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    await candidate.fill(value)
                    return True
            except Exception:
                continue
    return False


async def _maybe_prefill_bmo_credentials(
    page,
    *,
    state: dict[str, Any],
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: visible_auth.PopupInteractionLock | None,
    captured_credentials: dict[str, str],
) -> bool:
    if not credentials or prefill_state.get("done"):
        return False
    if (
        state.get("authenticated")
        or state.get("code_visible")
        or state.get("otp_intro")
        or state.get("otp_delivery_options")
        or state.get("trust_device_prompt")
        or state.get("invalid_credentials")
    ):
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        return False
    if not (state.get("username_visible") or state.get("password_visible")):
        return False
    username, password = credentials
    username_filled = await _fill_first_visible(page, BMO_USERNAME_SELECTORS, username)
    password_filled = await _fill_first_visible(page, BMO_PASSWORD_SELECTORS, password)
    if username_filled:
        _capture_username(captured_credentials, username, source="prefill")
    if password_filled:
        _capture_password(captured_credentials, password, source="prefill")
    if username_filled or password_filled:
        prefill_state["done"] = True
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        message = "Saved BMO credentials were prefilled in the secure browser. Review them and continue sign-in."
        visible_auth.print_status(message)
        await log_support_event(stage="credentials prefilled", message=message, last_output=message)
        return True
    return False


async def wait_for_authenticated_session(
    context,
    page,
    *,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    last_description = ""
    prefill_state: dict[str, Any] = {"done": False}
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_bmo_page(context, page, captured_credentials, auth_signals, popup_lock=popup_lock)
            await _bmo_clear_cookie_consent_noise(page)
            await _capture_bmo_raw_input_credentials(page, captured_credentials)
            try:
                state = await get_bmo_page_state(page, auth_signals)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_bmo_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                    captured_credentials=captured_credentials,
                )
            description = describe_bmo_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="BMO",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
                return page
            if state.get("system_error_page"):
                raise RuntimeError(BMO_SYSTEM_ERROR_MESSAGE)
            if (
                (state.get("invalid_credentials") or state.get("trust_device_prompt"))
                and popup_lock is not None
                and popup_lock.locked
            ):
                await popup_lock.set_locked(False)
            await asyncio.sleep(BMO_WAIT_POLL_SECONDS)
        raise TimeoutError("Timed out waiting for an authenticated BMO session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def get_viable_bmo_page(
    context,
    current_page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    *,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
):
    pages = [page for page in list(getattr(context, "pages", []) or []) if not visible_auth.is_page_closed(page)]
    for page in pages:
        bind_bmo_page_watcher(page, captured_credentials, auth_signals)
    if current_page is not None and not visible_auth.is_page_closed(getattr(current_page, "raw_page", current_page)):
        await visible_auth.bind_popup_lock_to_page(context, popup_lock, getattr(current_page, "raw_page", current_page))
        return current_page
    for page in pages:
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in BMO_PAGE_URL_MARKERS):
            await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)
            return popup_lock.page if popup_lock is not None and popup_lock.raw_page is page else page
    return await _prepare_bmo_context_page(context, captured_credentials, auth_signals)


def _bmo_request_uid() -> str:
    return f"REQ_{uuid.uuid4().hex[:16]}"


def _bmo_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "")


def _bmo_summary_bank_accounts(auth_signals: dict[str, Any]) -> list[dict[str, Any]]:
    summary = auth_signals.get("summary") if isinstance(auth_signals.get("summary"), dict) else {}
    accounts: list[dict[str, Any]] = []
    for category in summary.get("categories") or []:
        if not isinstance(category, dict):
            continue
        if str(category.get("categoryName") or "").upper() != "BA":
            continue
        for product in category.get("products") or []:
            if not isinstance(product, dict):
                continue
            if product.get("accountIndex") is None:
                continue
            accounts.append(product)
    return accounts


def _bmo_browser_history_windows(end_date: date | None = None) -> list[tuple[date, date]]:
    planned_end = end_date or datetime.now(timezone.utc).date()
    history_start = planned_end - timedelta(days=BMO_MAX_HISTORY_DAYS + BMO_HISTORY_EDGE_TOLERANCE_DAYS)
    windows: list[tuple[date, date]] = []
    current_end = planned_end
    while current_end >= history_start:
        current_start = max(history_start, current_end - timedelta(days=BMO_BACKFILL_CHUNK_DAYS - 1))
        windows.append((current_start, current_end))
        if current_start <= history_start:
            break
        current_end = current_start - timedelta(days=1)
    return windows


def _bmo_browser_fetch_headers(template: dict[str, Any], *, current_path: str) -> dict[str, str]:
    raw_headers = template.get("headers") if isinstance(template.get("headers"), dict) else {}
    forbidden = {
        "accept-encoding",
        "connection",
        "content-length",
        "cookie",
        "host",
        "origin",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "user-agent",
        "x-app-current-path",
    }
    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        normalized = str(name or "").strip()
        if not normalized or normalized.lower() in forbidden:
            continue
        headers[normalized] = str(value)
    headers["Accept"] = headers.get("Accept") or "application/json, text/plain, */*"
    headers["Content-Type"] = "application/json"
    headers["X-App-Current-Path"] = current_path
    return headers


def _bmo_template_header(template: dict[str, Any], name: str) -> str:
    raw_headers = template.get("headers") if isinstance(template.get("headers"), dict) else {}
    target = name.lower()
    for key, value in raw_headers.items():
        if str(key or "").lower() == target:
            return str(value or "").strip()
    return ""


def _bmo_template_header_request(template: dict[str, Any]) -> dict[str, Any]:
    payload = template.get("payload") if isinstance(template.get("payload"), dict) else {}
    for value in payload.values():
        if isinstance(value, dict) and isinstance(value.get("HdrRq"), dict):
            return value
    return {}


def _bmo_browser_request_body(
    template: dict[str, Any],
    *,
    account_index: str,
    start_date: date,
    end_date: date,
    user_agent: str,
    mfa_device_token: str | None,
) -> dict[str, Any]:
    request = _bmo_template_header_request(template)
    header = dict(request.get("HdrRq") or {}) if isinstance(request.get("HdrRq"), dict) else {}
    header.update(
        {
            "ver": str(header.get("ver") or "1.0"),
            "channelType": str(header.get("channelType") or "OLB"),
            "appName": str(header.get("appName") or "OLB"),
            "hostName": str(header.get("hostName") or "BDBN-HostName"),
            "clientDate": _bmo_now_iso(),
            "rqUID": _bmo_request_uid(),
            "clientSessionID": str(header.get("clientSessionID") or "session-id"),
            "userAgent": user_agent,
            "clientIP": str(header.get("clientIP") or "127.0.0.1"),
        }
    )
    if mfa_device_token:
        header["mfaDeviceToken"] = mfa_device_token
    return {
        "GetBankAccountDetailsRq": {
            "HdrRq": header,
            "BodyRq": {
                "accountIndex": account_index,
                "limitNoTxns": "1500",
                "filterFromDate": start_date.isoformat(),
                "filterToDate": end_date.isoformat(),
            },
        }
    }


def _bmo_account_detail_urls_from_online_banking_har() -> list[str]:
    har_path = BMO_ONLINE_BANKING_HAR_PATH
    if har_path is None or not har_path.is_file():
        return []
    try:
        har = json.loads(har_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for entry in ((har.get("log") or {}).get("entries") or []):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        if int(response.get("status") or 0) >= 400:
            continue
        headers = {
            str(header.get("name") or "").lower(): str(header.get("value") or "")
            for header in request.get("headers") or []
            if isinstance(header, dict)
        }
        candidates = [
            headers.get("referer") or "",
            headers.get("x-app-current-path") or "",
            str(request.get("url") or ""),
        ]
        for candidate in candidates:
            text = str(candidate or "")
            match = re.search(r"(/banking/digital/account-details/ba/[A-Za-z0-9._~-]+)", text)
            if not match:
                continue
            path = match.group(1)
            url = f"{BMO_ORIGIN}{path}?tab=overview"
            if url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


async def _bmo_raise_if_page_closed_or_system_error(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    if visible_auth.is_page_closed(raw_page):
        raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
    path = urlsplit(visible_auth.safe_page_url(raw_page)).path
    if re.search(r"/banking/digital/error/", path or ""):
        raise RuntimeError(BMO_SYSTEM_ERROR_MESSAGE)


async def capture_bmo_browser_transaction_history(page, auth_signals: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    handoff_sync_id = VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID or ""
    bank_accounts = _bmo_summary_bank_accounts(auth_signals)
    if not bank_accounts:
        return {
            "captured": False,
            "reason": "no_bank_accounts",
            "attempt_id": ATTEMPT_ID,
            "handoff_sync_id": handoff_sync_id,
        }

    await _bmo_raise_if_page_closed_or_system_error(page)
    template = auth_signals.get("bank_account_detail_request_template") or auth_signals.get("cdb_request_template")
    if not isinstance(template, dict) or not template.get("headers"):
        records = auth_signals.get("bank_account_detail_responses")
        return {
            "captured": False,
            "reason": "missing_cdb_request_template",
            "attempt_id": ATTEMPT_ID,
            "handoff_sync_id": handoff_sync_id,
            "windows": records if isinstance(records, list) else [],
        }

    mfa_device_token = str(auth_signals.get("mfa_device_token") or "").strip() or None
    raw_url = str(template.get("url") or BMO_BANK_DETAILS_URL)
    template_headers = template.get("headers") if isinstance(template.get("headers"), dict) else {}
    template_headers_lc = {str(key).lower(): str(value) for key, value in template_headers.items()}
    captured_current_path = str(template_headers_lc.get("x-app-current-path") or "").strip()
    captured_referrer = str(template_headers_lc.get("referer") or template_headers_lc.get("referrer") or "").strip()
    current_path = captured_current_path or urlsplit(captured_referrer).path or urlsplit(raw_url).path or BMO_BANK_DETAILS_PATH
    detail_page_path = urlsplit(visible_auth.safe_page_url(page)).path or "/banking/digital/accounts"
    if "/banking/digital/account-details/" in detail_page_path:
        current_path = detail_page_path
    referrer = (
        captured_referrer
        if captured_referrer.startswith(BMO_ORIGIN)
        else f"{BMO_ORIGIN}{current_path}" if current_path.startswith("/") else BMO_ORIGIN
    )
    headers = _bmo_browser_fetch_headers(template, current_path=current_path)
    headers = {
        key: value
        for key, value in headers.items()
        if str(key or "").lower() not in {"dpop", "x-bmo-mfa-device-token"}
    }
    if mfa_device_token:
        headers["x-bmo-mfa-device-token"] = mfa_device_token
    authorization_header = _bmo_template_header(template, "authorization")
    fetched_windows: list[dict[str, Any]] = []
    fetch_errors: list[dict[str, Any]] = []
    planned_windows = _bmo_browser_history_windows()
    expected_windows = 0

    requests: list[dict[str, Any]] = []
    for account in bank_accounts:
        raw_account_index = account.get("accountIndex")
        account_index = str(raw_account_index).strip() if raw_account_index is not None else ""
        if not account_index:
            continue
        expected_windows += len(planned_windows)
        for start_date, end_date in planned_windows:
            request_body = _bmo_browser_request_body(
                template,
                account_index=account_index,
                start_date=start_date,
                end_date=end_date,
                user_agent=user_agent,
                mfa_device_token=mfa_device_token,
            )
            requests.append(
                {
                    "account_index": account_index,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "body": request_body,
                }
            )

    try:
        results = await asyncio.wait_for(
            page.evaluate(
                """
                async ({ url, headers, requests, referrer, concurrency, authorizationHeader }) => {
                const base64Url = (value) => {
                    const bytes = typeof value === "string"
                        ? new TextEncoder().encode(value)
                        : value instanceof ArrayBuffer
                            ? new Uint8Array(value)
                            : value;
                    let binary = "";
                    for (const byte of bytes) binary += String.fromCharCode(byte);
                    return btoa(binary).replace(/=/g, "").replace(/\\+/g, "-").replace(/\\//g, "_");
                };
                const jsonBase64Url = (value) => base64Url(JSON.stringify(value));
                const randomId = () => {
                    if (crypto.randomUUID) return crypto.randomUUID();
                    const bytes = new Uint8Array(16);
                    crypto.getRandomValues(bytes);
                    bytes[6] = (bytes[6] & 0x0f) | 0x40;
                    bytes[8] = (bytes[8] & 0x3f) | 0x80;
                    const hex = Array.from(bytes).map((byte) => byte.toString(16).padStart(2, "0"));
                    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
                };
                const parseJwtPayload = (token) => {
                    try {
                        const part = String(token || "").split(".")[1] || "";
                        const padded = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
                        return JSON.parse(atob(padded));
                    } catch (_error) {
                        return {};
                    }
                };
                const openDpopDb = () => new Promise((resolve, reject) => {
                    const request = indexedDB.open("biometric-plugin", 1);
                    request.onerror = () => reject(request.error || new Error("indexedDB open failed"));
                    request.onsuccess = () => resolve(request.result);
                });
                const readDpopKeys = async () => {
                    const db = await openDpopDb();
                    return await new Promise((resolve, reject) => {
                        const tx = db.transaction("dpop-keys", "readonly");
                        const store = tx.objectStore("dpop-keys");
                        const request = store.getAll ? store.getAll() : null;
                        if (request) {
                            request.onsuccess = () => resolve(request.result || []);
                            request.onerror = () => reject(request.error || new Error("dpop getAll failed"));
                            return;
                        }
                        const values = [];
                        const cursor = store.openCursor();
                        cursor.onsuccess = () => {
                            const current = cursor.result;
                            if (!current) {
                                resolve(values);
                                return;
                            }
                            values.push(current.value);
                            current.continue();
                        };
                        cursor.onerror = () => reject(cursor.error || new Error("dpop cursor failed"));
                    });
                };
                const makeDpop = async ({ method, path, bodyText, authorization }) => {
                    const accessToken = String(authorization || "").replace(/^\\s*dpop\\s+/i, "").trim();
                    if (!accessToken) throw new Error("MissingAccessToken");
                    const tokenPayload = parseJwtPayload(accessToken);
                    const targetKid = tokenPayload && tokenPayload.cnf && tokenPayload.cnf.jkt ? String(tokenPayload.cnf.jkt) : "";
                    const keys = await readDpopKeys();
                    const keyRecord = keys.find((item) => item && item.privateKey && item.jwk && (!targetKid || item.jwk.kid === targetKid))
                        || keys.find((item) => item && item.privateKey && item.jwk);
                    if (!keyRecord) throw new Error("MissingDpopKey");
                    const athBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(accessToken));
                    const header = {
                        typ: "dpop+jwt",
                        alg: "PS256",
                        jwk: keyRecord.jwk,
                    };
                    const payload = {
                        htu: path,
                        htm: method.toUpperCase(),
                        jti: randomId(),
                        iat: Math.floor(Date.now() / 1000),
                        ath: base64Url(athBytes),
                    };
                    const signingInput = `${jsonBase64Url(header)}.${jsonBase64Url(payload)}`;
                    const signature = await crypto.subtle.sign(
                        { name: "RSA-PSS", saltLength: 32 },
                        keyRecord.privateKey,
                        new TextEncoder().encode(signingInput),
                    );
                    return `${signingInput}.${base64Url(signature)}`;
                };
                const results = new Array(requests.length);
                let cursor = 0;
                const worker = async () => {
                    while (cursor < requests.length) {
                        const index = cursor++;
                        const request = requests[index];
                        const bodyText = JSON.stringify(request.body);
                        try {
                            const requestHeaders = { ...headers };
                            requestHeaders.Authorization = requestHeaders.Authorization || requestHeaders.authorization || authorizationHeader;
                            requestHeaders["X-Request-ID"] = `REQ_${randomId().replace(/-/g, "").slice(0, 16)}`;
                            requestHeaders["X-Original-Request-Time"] = new Date().toUTCString();
                            requestHeaders.DPoP = await makeDpop({
                                method: "POST",
                                path: "/api/cdb/current-account/accountdetails/getBankAccountDetails",
                                bodyText,
                                authorization: requestHeaders.Authorization,
                            });
                            const response = await fetch(url, {
                                method: "POST",
                                credentials: "include",
                                cache: "no-store",
                                referrer,
                                headers: requestHeaders,
                                body: bodyText,
                            });
                            results[index] = {
                                account_index: request.account_index,
                                start_date: request.start_date,
                                end_date: request.end_date,
                                status: response.status,
                                ok: response.ok,
                                text: await response.text(),
                            };
                        } catch (error) {
                            results[index] = {
                                account_index: request.account_index,
                                start_date: request.start_date,
                                end_date: request.end_date,
                                error: error && error.name ? String(error.name) : "FetchError",
                            };
                        }
                    }
                };
                const workers = Array.from(
                    { length: Math.max(1, Math.min(concurrency || 1, requests.length || 1)) },
                    worker,
                );
                await Promise.all(workers);
                return results;
            }
                """,
                {
                    "url": BMO_BANK_DETAILS_URL,
                    "headers": headers,
                    "requests": requests,
                    "referrer": referrer,
                    "concurrency": BMO_BROWSER_TRANSACTION_FETCH_CONCURRENCY,
                    "authorizationHeader": authorization_header,
                },
            ),
            timeout=BMO_BROWSER_TRANSACTION_EVALUATE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if visible_auth.is_browser_closed_error(exc) or visible_auth.is_page_closed(page):
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
        system_error_after_fetch = False
        try:
            await _bmo_raise_if_page_closed_or_system_error(page)
        except RuntimeError as page_exc:
            if str(page_exc) == HOST_BROWSER_CLOSED_MESSAGE:
                raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from page_exc
            system_error_after_fetch = True
        results = []
        error_name = "BMOSystemErrorPage" if system_error_after_fetch else type(exc).__name__
        for request in requests:
            fetch_errors.append(
                {
                    "account_index": request.get("account_index"),
                    "start_date": request.get("start_date"),
                    "end_date": request.get("end_date"),
                    "error": error_name,
                }
            )

    request_bodies = {
        (request.get("account_index"), request.get("start_date"), request.get("end_date")): request.get("body")
        for request in requests
    }
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        if result.get("error"):
            fetch_errors.append(
                {
                    "account_index": result.get("account_index"),
                    "start_date": result.get("start_date"),
                    "end_date": result.get("end_date"),
                    "error": result.get("error"),
                }
            )
            continue
        request_body = request_bodies.get(
            (result.get("account_index"), result.get("start_date"), result.get("end_date")),
            {},
        )
        payload = _json_body(result.get("text") if isinstance(result, dict) else "")
        record = _bank_account_detail_record(payload, request_body)
        if record:
            record["status"] = int(result.get("status") or 0) if isinstance(result, dict) else 0
            record["ok"] = bool(result.get("ok")) if isinstance(result, dict) else False
            fetched_windows.append(record)
        else:
            fetch_errors.append(
                {
                    "account_index": result.get("account_index"),
                    "start_date": result.get("start_date"),
                    "end_date": result.get("end_date"),
                    "status": int(result.get("status") or 0) if isinstance(result, dict) else 0,
                }
            )

    observed = auth_signals.get("bank_account_detail_responses")
    if isinstance(observed, list):
        observed_windows = [item for item in observed if isinstance(item, dict)]
    else:
        observed_windows = []
    complete_capture = bool(fetched_windows) and (
        not expected_windows or len(fetched_windows) >= expected_windows
    ) and not any(bool(window.get("more_txns")) for window in fetched_windows)
    return {
        "captured": complete_capture,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "attempt_id": ATTEMPT_ID,
        "handoff_sync_id": handoff_sync_id,
        "windows": fetched_windows,
        "observed_windows": observed_windows[:5],
        "expected_windows": expected_windows,
        "errors": fetch_errors[:10],
    }


def _bmo_origin_from_url(url: str | None) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and (host == "www1.bmo.com" or host.endswith(".bmo.com")):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


async def _collect_bmo_page_web_storage(page) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    try:
        payload = await raw_page.evaluate(
            """
            () => ({
                href: window.location.href,
                localStorage: Object.fromEntries(Object.entries(window.localStorage || {})),
                sessionStorage: Object.fromEntries(Object.entries(window.sessionStorage || {})),
            })
            """
        )
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _bmo_storage_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return []
    items: list[dict[str, str]] = []
    for name, stored_value in value.items():
        key = str(name or "").strip()
        if key:
            items.append({"name": key, "value": str(stored_value)})
    return items


async def collect_bmo_browser_storage_snapshot(context, page=None) -> dict[str, Any]:
    pages = list(getattr(context, "pages", []) or [])
    if page is not None:
        raw_page = getattr(page, "raw_page", page)
        pages = [raw_page, *[candidate for candidate in pages if candidate is not raw_page]]
    origins: list[dict[str, Any]] = []
    seen_origins: set[str] = set()
    for candidate_page in pages:
        if visible_auth.is_page_closed(candidate_page):
            continue
        payload = await _collect_bmo_page_web_storage(candidate_page)
        origin = _bmo_origin_from_url(str(payload.get("href") or ""))
        if not origin or origin in seen_origins:
            continue
        local_storage = _bmo_storage_items(payload.get("localStorage"))
        session_storage = _bmo_storage_items(payload.get("sessionStorage"))
        if not local_storage and not session_storage:
            continue
        entry: dict[str, Any] = {"origin": origin}
        if local_storage:
            entry["localStorage"] = local_storage
        if session_storage:
            entry["sessionStorage"] = session_storage
        origins.append(entry)
        seen_origins.add(origin)
    return {"origins": origins} if origins else {}


def _merge_bmo_browser_storage_snapshot(
    storage_state: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("origins"), list):
        return storage_state
    origins = storage_state.setdefault("origins", [])
    if not isinstance(origins, list):
        origins = []
        storage_state["origins"] = origins
    by_origin = {
        str(origin.get("origin") or ""): origin
        for origin in origins
        if isinstance(origin, dict) and origin.get("origin")
    }
    for snapshot_origin in snapshot.get("origins") or []:
        if not isinstance(snapshot_origin, dict):
            continue
        origin_url = str(snapshot_origin.get("origin") or "").strip()
        if not origin_url:
            continue
        target = by_origin.get(origin_url)
        if target is None:
            target = {"origin": origin_url}
            origins.append(target)
            by_origin[origin_url] = target
        for key in ("localStorage", "sessionStorage"):
            items = snapshot_origin.get(key)
            if isinstance(items, list):
                target[key] = [dict(item) for item in items if isinstance(item, dict)]
    return storage_state


def storage_state_has_bmo_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").lower()
        if _is_bmo_storage_host(domain) and not _is_bmo_consent_cookie_name(name):
            return True
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if not _is_bmo_storage_host(host):
            continue
        local_storage = origin.get("localStorage") or []
        if any(
            isinstance(item, dict) and not _is_bmo_consent_cookie_name(str(item.get("name") or ""))
            for item in local_storage
        ):
            return True
        session_storage = origin.get("sessionStorage") or []
        if any(isinstance(item, dict) and str(item.get("name") or "").strip() for item in session_storage):
            return True
        if origin.get("indexedDB"):
            return True
    return False


def sanitize_bmo_bootstrap_storage_state(storage_state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(storage_state, dict):
        return None
    sanitized = dict(storage_state)
    safe_cookies: list[dict[str, Any]] = []
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").lower()
        if _is_bmo_storage_host(domain):
            if _is_bmo_consent_cookie_name(name):
                safe_cookies.append(dict(cookie))
            continue
        safe_cookies.append(dict(cookie))
    safe_origins: list[dict[str, Any]] = []
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if _is_bmo_storage_host(host):
            continue
        safe_origins.append(dict(origin))
    sanitized["cookies"] = safe_cookies
    sanitized["origins"] = safe_origins
    if not safe_cookies and not safe_origins:
        return None
    return sanitized


async def collect_storage_state(context) -> dict[str, Any]:
    storage_state = await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_bmo_material,
        origin_from_url=_bmo_origin_from_url,
        storage_state_timeout_seconds=BMO_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=BMO_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=BMO_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )
    snapshot = await collect_bmo_browser_storage_snapshot(context)
    return _merge_bmo_browser_storage_snapshot(storage_state, snapshot)


def build_bmo_session_artifact(auth_signals: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    handoff_sync_id = VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID or ""
    return {
        "captured_at": now,
        "authenticated_at": now,
        "attempt_id": ATTEMPT_ID,
        "handoff_sync_id": handoff_sync_id,
        "mfa_device_token": str(auth_signals.get("mfa_device_token") or "").strip() or None,
        "summary": auth_signals.get("summary") if isinstance(auth_signals.get("summary"), dict) else {},
        "entitlements": auth_signals.get("entitlements") if isinstance(auth_signals.get("entitlements"), dict) else {},
        "browser_transaction_history": {
            "captured": False,
            "status": "pending",
            "attempt_id": ATTEMPT_ID,
            "handoff_sync_id": handoff_sync_id,
            "started_at": now,
        },
        "user_agent": user_agent,
    }


def collect_bmo_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if username and password and not visible_auth._is_masked_secret(password):
        return username, password
    return None


async def open_bmo_login_page(page) -> None:
    await page.goto(BMO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)


async def run() -> None:
    if PROVIDER != "bmo":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for BMO visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    auth_signals: dict[str, Any] = {"authenticated": False, "invalid_credentials": False}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        if storage_state_has_bmo_material(loaded_storage_state):
            bootstrap_storage_state = sanitize_bmo_bootstrap_storage_state(loaded_storage_state)
            if bootstrap_storage_state is None:
                bootstrap_storage_slot = ""
        else:
            bootstrap_storage_slot = ""
        bootstrap_session_artifact, bootstrap_session_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="session",
            slots=("quarantine", "active"),
        )

    har_timestamp_slug = visible_auth.har_timestamp_slug() if VISIBLE_AUTH_RECORD_HAR else ""
    raw_har_path = _bmo_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening BMO secure login browser.")
    visible_auth.print_status("Opening BMO secure login browser.")

    async with async_playwright() as playwright:
        browser = None
        context = None
        browser_runtime: dict[str, Any] = {}
        browser_timezone = normalize_browser_timezone(VISIBLE_AUTH_TIMEZONE)
        launch_mode = "isolated_context_fresh"
        try:
            try:
                browser_runtime = ensure_brave_browser_runtime()
            except DesktopBrowserRuntimeError as exc:
                raise RuntimeError(str(exc)) from exc

            har_capture_message = (
                "BMO visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "BMO visible-auth HAR capture is disabled."
            )
            await log_support_event(
                stage="har capture",
                debug=True,
                message=har_capture_message,
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )

            try:
                launch_kwargs = visible_auth.managed_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {"timezone_id": browser_timezone}
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    BMO_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _bmo_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="BMO",
                    )
                ) from exc

            runtime_description = browser_runtime.get("runtime") or "unknown"
            runtime_path = browser_runtime.get("executable_path") or "patchright_default"
            runtime_mode = browser_runtime.get("mode") or "unknown"
            runtime_version = browser_runtime.get("version") or ""
            runtime_platform = browser_runtime.get("platform") or ""
            runtime_installed_now = bool(browser_runtime.get("installed_now"))
            await log_support_event(
                stage="browser runtime",
                debug=True,
                message="BMO visible-auth browser runtime selected.",
                details={
                    "browser_runtime": runtime_description,
                    "runtime_mode": runtime_mode,
                    "runtime_version": runtime_version,
                    "runtime_platform": runtime_platform,
                    "installed_now": runtime_installed_now,
                    "executable_path": runtime_path,
                },
            )
            await log_support_event(
                stage="browser context",
                debug=True,
                message="BMO visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_storage_sanitized": not VISIBLE_AUTH_ADD_FLOW,
                    "locale_policy": "browser_default",
                    "timezone_id": browser_timezone,
                    "har_capture_enabled": VISIBLE_AUTH_RECORD_HAR,
                    "browser_runtime": runtime_description,
                    "runtime_mode": runtime_mode,
                    "runtime_version": runtime_version,
                    "runtime_platform": runtime_platform,
                    "installed_now": runtime_installed_now,
                    "executable_path": runtime_path,
                },
            )

            popup_lock: visible_auth.PopupInteractionLock | None = None

            def on_new_page(new_page) -> None:
                bind_bmo_page_watcher(new_page, captured_credentials, auth_signals)
                if popup_lock is None:
                    return

                async def bind_locked_page() -> None:
                    try:
                        should_lock = popup_lock.locked
                        await popup_lock.bind_page(new_page, await context.new_cdp_session(new_page))
                        if should_lock:
                            await popup_lock.set_locked(True)
                    except Exception:
                        return

                asyncio.create_task(bind_locked_page())

            context.on("page", on_new_page)
            page = await _prepare_bmo_context_page(context, captured_credentials, auth_signals)
            await log_support_event(
                stage="credential capture",
                debug=True,
                message="BMO visible-auth credential capture configured.",
                details={
                    "browser_init_scripts_enabled": False,
                    "credential_capture_strategy": "bounded_post_load_input_and_structured_request_capture",
                    "input_capture_timeout_seconds": BMO_CREDENTIAL_CAPTURE_TIMEOUT_SECONDS,
                },
            )
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = visible_auth.PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_bmo_login_page(page)
            page = await wait_for_authenticated_session(
                context,
                page,
                captured_credentials=captured_credentials,
                auth_signals=auth_signals,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="BMO",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            await asyncio.sleep(1.5)
            active_target = getattr(page, "raw_page", page)
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_bmo_material": storage_state_has_bmo_material(storage_state),
            }
            if not storage_state_has_bmo_material(storage_state):
                raise RuntimeError("BMO storage state could not be captured for future syncs.")

            session_artifact = build_bmo_session_artifact(auth_signals, user_agent=user_agent)
            session_artifact_details = {
                "has_mfa_device_token": bool(session_artifact.get("mfa_device_token")),
                "has_summary": bool(session_artifact.get("summary")),
                "has_entitlements": bool(session_artifact.get("entitlements")),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            captured_pair = collect_bmo_credentials(captured_credentials)
            credential_capture_details = {
                "raw_username_capture_count": int(captured_credentials.get("_raw_username_capture_count") or 0),
                "request_username_capture_count": int(captured_credentials.get("_request_username_capture_count") or 0),
                "raw_password_capture_count": int(captured_credentials.get("_raw_password_capture_count") or 0),
                "request_password_capture_count": int(captured_credentials.get("_request_password_capture_count") or 0),
                "prefill_username_capture_count": int(captured_credentials.get("_prefill_username_capture_count") or 0),
                "prefill_password_capture_count": int(captured_credentials.get("_prefill_password_capture_count") or 0),
                "username_captured": bool(captured_credentials.get("username")),
                "password_captured": bool(captured_credentials.get("password")),
            }
            if captured_pair is None:
                raise RuntimeError("BMO credentials could not be captured from the secure login flow. Start BMO login again.")
            username, password = captured_pair

            auth_message = "Authenticated BMO session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="BMO visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="BMO visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="BMO visible-auth credential capture summary.",
                details=credential_capture_details,
            )
            storage_result = await persist_visible_auth_artifact(
                artifact_type="storage_state",
                payload=storage_state,
            )
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured BMO credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)

            try:
                browser_history = await capture_bmo_browser_transaction_history(
                    page,
                    auth_signals,
                    user_agent=user_agent,
                )
                session_artifact["browser_transaction_history"] = browser_history
                await log_support_event(
                    stage="transaction_capture",
                    result="succeeded" if browser_history.get("captured") else "failed",
                    level="info" if browser_history.get("captured") else "warning",
                    debug=True,
                    message="BMO browser transaction capture finished.",
                    details={
                        "captured": bool(browser_history.get("captured")),
                        "windows": len(browser_history.get("windows") or []),
                        "expected_windows": browser_history.get("expected_windows"),
                        "observed_windows": len(browser_history.get("observed_windows") or []),
                        "errors": len(browser_history.get("errors") or []),
                        "reason": browser_history.get("reason"),
                        "har_route_fallbacks": len(_bmo_account_detail_urls_from_online_banking_har()),
                    },
                )
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc) or str(exc) == HOST_BROWSER_CLOSED_MESSAGE:
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                session_artifact["browser_transaction_history"] = {
                    "captured": False,
                    "status": "failed",
                    "attempt_id": ATTEMPT_ID,
                    "handoff_sync_id": VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID or "",
                    "error": type(exc).__name__,
                }
                await log_support_event(
                    stage="transaction_capture",
                    result="failed",
                    level="warning",
                    debug=True,
                    message="BMO browser transaction capture failed.",
                    details={"error": type(exc).__name__, "message": str(exc)[:240]},
                )

            session_result = await persist_visible_auth_artifact(
                artifact_type="session",
                payload=session_artifact,
            )
            storage_message = "BMO secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            success_message = f"BMO secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
            visible_auth.print_status(success_message)
            await log_support_event(stage="sync handoff", message=success_message, last_output=success_message)

            result_payload = visible_auth.build_visible_auth_handoff_result(
                provider=PROVIDER,
                attempt_id=ATTEMPT_ID,
                sync_id=VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID or "",
                storage_result=storage_result,
                session_result=session_result,
                credentials_result=credentials_result,
            )
            visible_auth.print_visible_auth_result(result_payload)
            run_result = "succeeded"
        except Exception as exc:
            message = (
                HOST_BROWSER_CLOSED_MESSAGE
                if visible_auth.is_browser_closed_error(exc) or str(exc) == HOST_BROWSER_CLOSED_MESSAGE
                else str(exc)
            )
            await log_support_event(
                stage="sync result",
                result="failed",
                level="error",
                message=message,
                last_output=message,
            )
            visible_auth.print_status(message)
            raise
        finally:
            await visible_auth.close_visible_auth_context_quietly(context)
            if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_bmo_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized BMO HAR capture.",
                            details={"har_result": run_result, "path": str(sanitized_har_path)},
                        )
                except Exception as exc:
                    await log_support_event(
                        stage="har capture",
                        level="warning",
                        debug=True,
                        message="Could not finalize the sanitized BMO HAR capture.",
                        details={"har_result": run_result, "error": str(exc)},
                    )
            await visible_auth.close_visible_auth_context_quietly(None, browser)


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
        install_signal_handlers=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
