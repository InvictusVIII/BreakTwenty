#!/usr/bin/env python3
"""Desktop visible-auth runner for National Bank."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
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
NATIONAL_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "national"
NATIONAL_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "National")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "national").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

NATIONAL_LOGIN_URL = "https://app.bnc.ca/?lang=en"
NATIONAL_USERNAME_SELECTORS = (
    "#identity",
    'input[name="identity"]',
    'input[type="email"]',
)
NATIONAL_PASSWORD_SELECTORS = (
    "#password",
    'input[name="password"]',
    'input[type="password"]',
)
NATIONAL_CODE_SELECTORS = (
    "#code",
    'input[name="code"][data-test="code"]',
    'input[name="code"]',
)
NATIONAL_MFA_CHOICE_SELECTORS = (
    "#multiFactorAuthChoice-common",
    "#send-mfa-code__choice-sms",
    "#send-mfa-code__choice-email",
)
NATIONAL_UNSUPPORTED_BROWSER_PATHS = ("/unsupportedbrowser", "/browser-not-supported")
NATIONAL_PAGE_URL_MARKERS = ("connexion.bnc.ca", "app.bnc.ca", "api.bnc.ca", "digitalretail.apis.bnc.ca")
NATIONAL_BOOTSTRAP_AUTH_COOKIE_NAMES = frozenset(
    {
        "idx",
        "sid",
        "xids",
    }
)
NATIONAL_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES = frozenset(
    {
        "aphishi-lws_at",
    }
)
NATIONAL_HAR_FILENAME_PREFIX = "national_visible_auth"
NATIONAL_MAX_SANITIZED_HARS_PER_USER = 3
NATIONAL_HAR_URL_FILTER = re.compile(
    r"^https://(?:(?:api|digitalretail\.apis|infosec\.apis)\.bnc\.ca/"
    r"|connexion\.bnc\.ca/(?!static/|favicons/|.*\.(?:css|js|mjs|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf)(?:$|[?#]))"
    r"|app\.bnc\.ca/(?!assets/|favicons/|.*\.(?:css|js|mjs|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf)(?:$|[?#])))",
    re.IGNORECASE,
)
NATIONAL_REDACTED_VALUE = "<redacted>"
NATIONAL_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "error",
        "error_description",
        "errorcode",
        "errorCode",
        "errorMessage",
        "factorType",
        "code",
        "message",
        "reason",
        "status",
        "statusCode",
        "state",
        "type",
        "validationErrors",
    }
)
NATIONAL_HAR_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
        "session_id",
    }
)
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
NATIONAL_GRAPHQL_SESSION_HEADER_TIMEOUT_SECONDS = 5.0
JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
NATIONAL_WAIT_POLL_SECONDS = 0.35
NATIONAL_PAGE_REPLACEMENT_RETRY_SECONDS = 3.0
NATIONAL_PAGE_REPLACEMENT_POLL_SECONDS = 0.15
NATIONAL_STORAGE_STATE_TIMEOUT_SECONDS = 15
NATIONAL_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
NATIONAL_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
NATIONAL_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1"
NATIONAL_DIDOMI_NOTICE_URL_PATTERN = "**/content/dam/tools/cmp/notice.js*"
NATIONAL_COOKIE_BANNER_SCRIPT = """
(() => {
    const isNationalHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host === "bnc.ca" || host.endsWith(".bnc.ca") || host === "nbc.ca" || host.endsWith(".nbc.ca");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
        "#didomi-host",
        "[id^='didomi']",
        "[class*='didomi']",
        "[data-testid*='didomi']",
        "[id*='consent'][role='dialog']",
        "[class*='consent'][role='dialog']",
        "[aria-modal='true'][id*='didomi']",
        "#conversation-launcher",
        "[id*='conversation-launcher']",
        "iframe[id*='conversation']",
        "iframe[src*='conversation']",
        "iframe[src*='privacy-center']",
        "iframe[src*='didomi']",
    ];
    const clearBanner = () => {
        if (!isNationalHost()) {
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
    if (!window.__breaktwentyNationalConsentObserver) {
        window.__breaktwentyNationalConsentObserver = new MutationObserver(clearBanner);
        window.__breaktwentyNationalConsentObserver.observe(document.documentElement || document.body, {
            childList: true,
            subtree: true,
        });
    }
})();
"""
HOST_BROWSER_CLOSED_MESSAGE = (
    "Login window was closed before secure login finished. "
    "Start sync again and keep the bank browser window open until the app says it is done."
)


def _national_chrome_compat_identity(browser_runtime: dict[str, Any]) -> dict[str, Any]:
    chromium_major = str(browser_runtime.get("chromium_major") or "").strip()
    if not re.fullmatch(r"\d+", chromium_major):
        raise RuntimeError("National Bank could not verify the managed Chromium version.")
    chromium_full_version = f"{chromium_major}.0.0.0"
    brands = [
        {"brand": "Not;A=Brand", "version": "8"},
        {"brand": "Chromium", "version": chromium_major},
        {"brand": "Google Chrome", "version": chromium_major},
    ]
    full_version_list = [
        {"brand": "Not;A=Brand", "version": "8.0.0.0"},
        {"brand": "Chromium", "version": chromium_full_version},
        {"brand": "Google Chrome", "version": chromium_full_version},
    ]
    return {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_full_version} Safari/537.36"
        ),
        "metadata": {
            "brands": brands,
            "fullVersionList": full_version_list,
            "mobile": False,
            "model": "",
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "x86",
            "bitness": "64",
            "wow64": False,
        },
    }


async def log_support_event(**kwargs: Any) -> None:
    await VISIBLE_AUTH_CLIENT.log_support_event(**kwargs)


async def persist_visible_auth_artifact(*, artifact_type: str, payload: Any) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_artifact(
        artifact_type=artifact_type,
        payload=payload,
    )


async def persist_visible_auth_credentials(username: str, password: str) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_credentials(username, password)


def _national_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    filename = f"{NATIONAL_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}.har"
    return NATIONAL_DESKTOP_AUTH_RAW_HAR_DIR / filename


def _national_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    filename = f"{NATIONAL_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}{scope_part}_{result}.har"
    return NATIONAL_DIAGNOSTIC_DIR / filename


def _national_sanitized_har_candidates(user_id: int) -> list[Path]:
    if not NATIONAL_DIAGNOSTIC_DIR.exists():
        return []
    return list(NATIONAL_DIAGNOSTIC_DIR.glob(f"{NATIONAL_HAR_FILENAME_PREFIX}_user-{user_id}_*.har"))


def _national_consent_cookie_payload() -> list[dict[str, Any]]:
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
            ("groups", NATIONAL_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    cookies: list[dict[str, Any]] = []
    for domain in (".bnc.ca", ".nbc.ca"):
        cookies.extend(
            [
                {
                    "name": "OptanonConsent",
                    "value": consent_value,
                    "domain": domain,
                    "path": "/",
                    "expires": expiry,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                },
                {
                    "name": "OptanonAlertBoxClosed",
                    "value": datestamp,
                    "domain": domain,
                    "path": "/",
                    "expires": expiry,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                },
                {
                    "name": "ot-consent-one-time",
                    "value": "trigger",
                    "domain": domain,
                    "path": "/",
                    "expires": expiry,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
    return cookies


async def _national_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_national_consent_cookie_payload())
    except Exception:
        pass


async def _national_clear_cookie_consent_noise(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    try:
        await raw_page.evaluate(NATIONAL_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass


async def _national_notice_route_handler(route) -> None:
    try:
        response = await route.fetch()
        body = await response.text()
        patched = body.replace(
            'const isDidomiNoticeEnabled = didomiNoticeEnabledAttribute? "true" === didomiNoticeEnabledAttribute:true;',
            "const isDidomiNoticeEnabled = false;",
        )
        if patched == body:
            patched = re.sub(
                r"const\s+isDidomiNoticeEnabled\s*=\s*didomiNoticeEnabledAttribute\s*\?\s*"
                r'"true"\s*===\s*didomiNoticeEnabledAttribute\s*:\s*true\s*;',
                "const isDidomiNoticeEnabled = false;",
                body,
            )
        patched = patched.replace(
            "notice: {\n            enable: true\n        }",
            "notice: {\n            enable: false\n        }",
        )
        await route.fulfill(response=response, body=patched, content_type="application/javascript")
    except Exception:
        try:
            await route.continue_()
        except Exception:
            pass


def _national_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "json" not in str(content_type or "").lower():
        return False
    if status and int(status) >= 500:
        return True
    if host == "connexion.bnc.ca" and path == "/validate":
        return True
    if host == "api.bnc.ca" and "/api/v1/authn" in path:
        return True
    if host == "digitalretail.apis.bnc.ca" and path.endswith("/graphql"):
        return True
    return False


def finalize_national_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=_national_sanitized_har_path(user_id, timestamp_slug, result),
        sanitized_paths_to_prune=_national_sanitized_har_candidates(user_id),
        keep=NATIONAL_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_national_should_keep_response_body,
        safe_response_keys=NATIONAL_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=NATIONAL_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=NATIONAL_REDACTED_VALUE,
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


def _national_endpoint_key(url: str) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host == "connexion.bnc.ca" and path == "/validate":
        return "credential_submit"
    if host == "api.bnc.ca" and "/api/v1/authn" in path and "/factors/" not in path:
        return "authn"
    if host == "api.bnc.ca" and "/api/v1/authn/factors/" in path:
        return "mfa_verify"
    if host == "api.bnc.ca" and "sessioncookieredirect" in path:
        return "session_redirect"
    if host == "api.bnc.ca" and "/oauth2/" in path and path.endswith("/v1/token"):
        return "oauth_token"
    if host == "digitalretail.apis.bnc.ca" and path.endswith("/graphql"):
        return "graphql"
    return None


def _national_api_browser_validation_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in (parsed.path or "").split("/") if part]
    return host == "api.bnc.ca" and path_parts and path_parts[0].lower() != "bnc"


def _is_national_storage_host(host: str) -> bool:
    normalized = str(host or "").strip().lower().lstrip(".")
    return normalized == "bnc.ca" or normalized.endswith(".bnc.ca") or normalized == "nbc.ca" or normalized.endswith(".nbc.ca")


def _is_national_consent_cookie_name(name: str) -> bool:
    normalized = str(name or "").lower()
    return any(token in normalized for token in ("consent", "optanon", "onetrust", "didomi", "notice"))


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


def _national_sanitize_log_text(text: str | None, *, limit: int = 220) -> str:
    sanitized = JWT_RE.sub("<jwt>", str(text or ""))
    sanitized = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "<email>", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\b\d{5,}\b", "<number>", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    if len(sanitized) <= limit:
        return sanitized
    return f"{sanitized[:limit].rstrip()}..."


def _national_sanitized_payload_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    summary: dict[str, Any] = {"payload_keys": sorted(str(key) for key in payload.keys())[:20]}
    for key in NATIONAL_HAR_SAFE_RESPONSE_KEYS:
        if key in payload:
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                summary[str(key)] = value
    text = _payload_text(payload)
    if text:
        summary["message_preview"] = JWT_RE.sub("<jwt>", re.sub(r"\s+", " ", text).strip())[:240]
    return summary


def _looks_like_invalid_credentials(payload: Any) -> bool:
    text = _payload_text(payload).lower()
    return any(
        keyword in text
        for keyword in (
            "invalid",
            "incorrect",
            "not recognized",
            "not found",
            "login failed",
            "authentication failed",
        )
    )


def _looks_like_browser_validation_rejection(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    error_code = str(payload.get("errorCode") or payload.get("errorcode") or "").strip().upper()
    if error_code == "AK000001":
        return True
    return "bm validation failed" in _payload_text(payload).lower()


def _extract_credentials_from_payload(payload: Any) -> tuple[str, str | None]:
    username = ""
    password = ""

    def walk(value: Any, key_hint: str = "") -> None:
        nonlocal username, password
        key_lower = key_hint.lower()
        if isinstance(value, dict):
            for key, nested in value.items():
                walk(nested, str(key))
            return
        if isinstance(value, list):
            for nested in value:
                walk(nested, key_hint)
            return
        text = str(value or "").strip()
        if not text:
            return
        if not username and key_lower in {"identity", "username", "user_name", "login", "loginid", "email"}:
            username = text
        if not password and "password" in key_lower:
            password = text

    walk(payload)
    return username, password or None


def _capture_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = str(username or "").strip()
    if not normalized or visible_auth.username_looks_like_placeholder(normalized):
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


async def _capture_national_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    raw_page = getattr(page, "raw_page", page)
    try:
        values = await raw_page.evaluate(
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
                "usernameSelectors": list(NATIONAL_USERNAME_SELECTORS),
                "passwordSelectors": list(NATIONAL_PASSWORD_SELECTORS),
            },
        )
    except Exception:
        return
    if not isinstance(values, dict):
        return
    _capture_username(captured_credentials, str(values.get("username") or ""), source="raw")
    _capture_password(captured_credentials, str(values.get("password") or ""), source="raw")


def _capture_national_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    key = _national_endpoint_key(str(getattr(request, "url", "") or ""))
    if key not in {"credential_submit", "authn"}:
        return
    username, password = _extract_credentials_from_payload(_request_body(visible_auth.safe_request_post_data(request)))
    if username:
        _capture_username(captured_credentials, username, source="request")
    if password:
        _capture_password(captured_credentials, password, source="request")


def _national_clean_header_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text or len(text) > 1000:
        return ""
    return text


def _national_clean_authorization_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text or NATIONAL_REDACTED_VALUE in text or "\n" in text or "\r" in text or len(text) > 5000:
        return ""
    return text


def _national_clean_graphql_operation_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == NATIONAL_REDACTED_VALUE or len(text) > 240:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_:-]+", text):
        return ""
    return text


def _national_graphql_operation_name_from_body(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    operation_name = _national_clean_graphql_operation_name(body.get("operationName"))
    if operation_name:
        return operation_name
    meta = body.get("meta")
    if isinstance(meta, dict):
        return _national_clean_graphql_operation_name(meta.get("operationName"))
    return ""


def _national_graphql_request_body_from_request(request) -> dict[str, Any] | None:
    body = _request_body(visible_auth.safe_request_post_data(request))
    return body if isinstance(body, dict) else None


def _national_accounts_request_body(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    if "variables" in body:
        return None
    if "query" not in body and "operationName" not in body:
        return None
    return dict(body)


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
    if text and text != NATIONAL_REDACTED_VALUE and not re.search(r"\s", text):
        return text
    return None


async def _capture_national_graphql_request_headers(request, auth_signals: dict[str, Any]) -> None:
    if _national_endpoint_key(str(getattr(request, "url", "") or "")) != "graphql":
        return
    method = str(getattr(request, "method", "") or "").upper()
    if method and method != "POST":
        return
    headers = await visible_auth.request_headers(request)
    replay_headers = auth_signals.get("graphql_headers")
    captured: dict[str, str] = dict(replay_headers) if isinstance(replay_headers, dict) else {}
    for name in NATIONAL_GRAPHQL_REPLAY_HEADER_NAMES:
        value = _national_clean_header_value(headers.get(name))
        if value:
            captured[name] = value
    session_id = _national_clean_header_value(captured.get("session_id"))
    if session_id:
        auth_signals["session_id"] = session_id
    authorization = _national_clean_authorization_value(headers.get("authorization"))
    if authorization:
        auth_signals["graphql_uses_authorization"] = True
        auth_signals["graphql_authorization_scheme"] = (
            "bearer" if authorization.lower().startswith("bearer ") else "raw"
        )
        auth_signals["graphql_authorization"] = authorization
        token = _national_authorization_access_token(authorization)
        if token:
            auth_signals["graphql_access_token"] = token
            auth_signals["access_token"] = token
    if captured:
        auth_signals["graphql_headers"] = captured
    body = _national_graphql_request_body_from_request(request)
    operation_name = _national_graphql_operation_name_from_body(body)
    variables = body.get("variables") if isinstance(body, dict) else None
    if operation_name and isinstance(variables, dict) and "transactionsRequestInput" in variables:
        auth_signals["transactions_graphql_operation_name"] = operation_name


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


def _national_token_expires_at_iso(token: str | None) -> str | None:
    expires_at = _national_jwt_expiry(token)
    if not expires_at:
        return None
    return datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


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
    parsed = _json_body(text)
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


async def _collect_page_web_storage(page) -> dict[str, Any]:
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


def _national_storage_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return []
    items: list[dict[str, str]] = []
    for name, stored_value in value.items():
        key = str(name or "").strip()
        if not key:
            continue
        items.append({"name": key, "value": str(stored_value)})
    return items


async def collect_national_browser_storage_snapshot(context, page) -> dict[str, Any]:
    pages = list(getattr(context, "pages", []) or [])
    if page is not None:
        raw_page = getattr(page, "raw_page", page)
        pages = [raw_page, *[candidate_page for candidate_page in pages if candidate_page is not raw_page]]
    origins: list[dict[str, Any]] = []
    seen_origins: set[str] = set()
    for candidate_page in pages:
        if visible_auth.is_page_closed(candidate_page):
            continue
        payload = await _collect_page_web_storage(candidate_page)
        href = str(payload.get("href") or "")
        origin = _national_origin_from_url(href)
        if not origin or origin in seen_origins:
            continue
        session_storage = _national_storage_items(payload.get("sessionStorage"))
        local_storage = _national_storage_items(payload.get("localStorage"))
        if not session_storage and not local_storage:
            continue
        entry: dict[str, Any] = {"origin": origin}
        if local_storage:
            entry["localStorage"] = local_storage
        if session_storage:
            entry["sessionStorage"] = session_storage
        origins.append(entry)
        seen_origins.add(origin)
    return {"origins": origins} if origins else {}


def _extract_storage_state_access_token(storage_state: dict[str, Any] | None) -> str | None:
    if not isinstance(storage_state, dict):
        return None
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if not _is_national_storage_host(host):
            continue
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            candidate = _extract_storage_item_access_token(item)
            if candidate:
                return candidate
        candidate = _extract_access_token_candidate(origin.get("indexedDB"))
        if candidate:
            return candidate
    return None


def _national_graphql_data_keys(payload: Any) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    data = payload.get("data")
    if not isinstance(data, dict):
        return set()
    return {str(key) for key in data.keys()}


async def collect_national_access_token(context, page, storage_state: dict[str, Any], auth_signals: dict[str, Any]) -> str | None:
    if auth_signals.get("invalid_credentials"):
        return None
    candidate = _national_any_access_token(auth_signals.get("graphql_access_token"))
    if candidate:
        return candidate
    candidate = _national_any_access_token(auth_signals.get("access_token"))
    if candidate:
        return candidate
    candidate = _extract_storage_state_access_token(storage_state)
    if candidate:
        return candidate
    pages = list(getattr(context, "pages", []) or [])
    if page is not None:
        raw_page = getattr(page, "raw_page", page)
        pages = [raw_page, *[candidate_page for candidate_page in pages if candidate_page is not raw_page]]
    for candidate_page in pages:
        if visible_auth.is_page_closed(candidate_page):
            continue
        payload = await _collect_page_web_storage(candidate_page)
        candidate = _extract_access_token_candidate(payload)
        if candidate:
            return candidate
    return None


async def _capture_national_response(response, auth_signals: dict[str, Any]) -> None:
    response_url = str(getattr(response, "url", "") or "")
    status = 0
    try:
        status = int(getattr(response, "status", 0) or 0)
    except Exception:
        status = 0
    try:
        request_method = str(getattr(getattr(response, "request", None), "method", "") or "").upper()
    except Exception:
        request_method = ""
    if _national_api_browser_validation_url(response_url):
        auth_signals["api_browser_validation_seen"] = True
        if request_method == "POST" and status in {200, 201, 204}:
            auth_signals["api_browser_validation_ready"] = True
            auth_signals.pop("api_browser_validation_failed", None)
            if not auth_signals.get("_api_browser_validation_ready_logged"):
                auth_signals["_api_browser_validation_ready_logged"] = True
                await log_support_event(
                    stage="browser validation",
                    debug=True,
                    message="National Bank browser validation sensor completed before sign-in.",
                    details={"status": status},
                )
    key = _national_endpoint_key(response_url)
    if not key:
        return
    try:
        text = await response.text()
    except Exception:
        text = ""
    payload = _json_body(text)
    rejected = key in {"credential_submit", "authn"} and (
        status in {400, 401, 403} or _looks_like_invalid_credentials(payload)
    )
    if rejected:
        if _looks_like_browser_validation_rejection(payload):
            auth_signals["browser_validation_failed"] = True
            auth_signals["authenticated"] = False
            auth_signals.pop("access_token", None)
            await log_support_event(
                stage="browser validation rejection",
                level="warning",
                message="National Bank rejected browser validation.",
                details={
                    "endpoint": key,
                    "status": status,
                    **_national_sanitized_payload_summary(payload),
                },
            )
            return
        auth_signals["invalid_credentials"] = True
        auth_signals["authenticated"] = False
        auth_signals.pop("access_token", None)
        await log_support_event(
            stage="provider rejection",
            level="warning",
            message="National Bank returned a sign-in rejection.",
            details={
                "endpoint": key,
                "status": status,
                **_national_sanitized_payload_summary(payload),
            },
        )
        return
    if status >= 400:
        return
    if key in {"credential_submit", "authn", "mfa_verify"}:
        payload_status = str(payload.get("status") if isinstance(payload, dict) else "").upper()
        if payload_status and payload_status != "SUCCESS":
            auth_signals["authenticated"] = False
            return
    if key == "graphql" and status < 400:
        request = getattr(response, "request", None)
        if request is not None:
            await _capture_national_graphql_request_headers(request, auth_signals)
        data_keys = _national_graphql_data_keys(payload)
        if "accountsWithProductProfile" in data_keys:
            auth_signals["accounts_graphql_ready"] = True
            auth_signals["authenticated"] = True
            request_body = _national_accounts_request_body(
                _national_graphql_request_body_from_request(request) if request is not None else None
            )
            if request_body:
                auth_signals["accounts_graphql_request_body"] = request_body
    candidate = _extract_access_token_candidate(payload)
    if candidate:
        if not auth_signals.get("graphql_access_token"):
            auth_signals["access_token"] = candidate
        if key == "oauth_token":
            auth_signals["authenticated"] = True


def bind_national_page_watcher(page, captured_credentials: dict[str, str], auth_signals: dict[str, Any]) -> None:
    if getattr(page, "_national_visible_auth_watcher_installed", False):
        return
    setattr(page, "_national_visible_auth_watcher_installed", True)

    def on_request(request) -> None:
        url = str(getattr(request, "url", "") or "")
        key = _national_endpoint_key(url)
        if key == "graphql":
            asyncio.create_task(_capture_national_graphql_request_headers(request, auth_signals))
        if key in {"credential_submit", "authn"}:
            asyncio.create_task(_capture_national_raw_input_credentials(page, captured_credentials))
            _capture_national_credentials_from_request(captured_credentials, request)

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        if _national_endpoint_key(url) or _national_api_browser_validation_url(url):
            asyncio.create_task(_capture_national_response(response, auth_signals))

    def on_request_failed(request) -> None:
        url = str(getattr(request, "url", "") or "")
        method = str(getattr(request, "method", "") or "").upper()
        if _national_api_browser_validation_url(url) and method == "POST" and not auth_signals.get("api_browser_validation_ready"):
            auth_signals["api_browser_validation_seen"] = True
            auth_signals["api_browser_validation_failed"] = True

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


async def _apply_national_user_agent_override(context, page, chrome_identity: dict[str, Any], *, phase: str) -> bool:
    raw_page = getattr(page, "raw_page", page)
    if getattr(raw_page, "_national_user_agent_override_applied", False):
        return True
    try:
        cdp_session = await context.new_cdp_session(raw_page)
        await cdp_session.send(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": chrome_identity["user_agent"],
                "platform": "Linux x86_64",
                "userAgentMetadata": chrome_identity["metadata"],
            },
        )
        setattr(raw_page, "_national_user_agent_override_cdp_session", cdp_session)
        setattr(raw_page, "_national_user_agent_override_applied", True)
        return True
    except Exception as exc:
        await log_support_event(
            stage="user agent override",
            level="warning",
            debug=True,
            message="National Bank managed Chromium user-agent override could not be applied.",
            details={"phase": phase, "error": _national_sanitize_log_text(str(exc), limit=180)},
        )
        return False


async def _prepare_national_context_page(
    context,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    chrome_identity: dict[str, Any],
):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        await _apply_national_user_agent_override(context, page, chrome_identity, phase="initial_existing_page")
        bind_national_page_watcher(page, captured_credentials, auth_signals)
        for extra_page in pages[1:]:
            await _apply_national_user_agent_override(context, extra_page, chrome_identity, phase="extra_existing_page")
            bind_national_page_watcher(extra_page, captured_credentials, auth_signals)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    await _apply_national_user_agent_override(context, page, chrome_identity, phase="initial_new_page")
    bind_national_page_watcher(page, captured_credentials, auth_signals)
    return page


async def get_national_page_state(page, auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    try:
        state = await raw_page.evaluate(
            """
            ({ usernameSelectors, passwordSelectors, codeSelectors, mfaChoiceSelectors }) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                };
                const visibleMatches = (selectors) => selectors.some((selector) =>
                    Array.from(document.querySelectorAll(selector)).some(isVisible)
                );
                const buttonAriaMatches = (pattern) => Array.from(document.querySelectorAll("button, [role='button'], input[type='submit'], input[type='button']"))
                    .filter(isVisible)
                    .some((el) => String(el.getAttribute("aria-label") || el.getAttribute("name") || "").trim().toLowerCase() === pattern);
                const host = window.location.hostname || "";
                const path = window.location.pathname || "";
                const usernameVisible = visibleMatches(usernameSelectors);
                const passwordVisible = visibleMatches(passwordSelectors);
                const codeVisible = visibleMatches(codeSelectors);
                const mfaChoiceVisible = visibleMatches(mfaChoiceSelectors);
                const providerErrorVisible = Boolean(
                    Array.from(document.querySelectorAll("[data-test='error-message'][role='alert'], [role='alert'][data-test*='error'], [id*='error'][role='alert']"))
                        .some(isVisible)
                );
                const appShellVisible = Boolean(
                    document.querySelector("#dashboard_section_accounts, [id^='accounts-table_accountId_'], form#filter-form, #filter-form__display")
                );
                const authenticatedPath = host === "app.bnc.ca" && /\\/(dashboard|accounts)/.test(path);
                const signOutVisible = buttonAriaMatches("sign out");
                return {
                    current_url: window.location.href,
                    username_visible: usernameVisible,
                    password_visible: passwordVisible,
                    code_visible: codeVisible,
                    mfa_choice_visible: mfaChoiceVisible,
                    provider_error_visible: providerErrorVisible,
                    authenticated_path: authenticatedPath,
                    app_shell_visible: appShellVisible,
                    sign_out_visible: signOutVisible,
                };
            }
            """,
            {
                "usernameSelectors": list(NATIONAL_USERNAME_SELECTORS),
                "passwordSelectors": list(NATIONAL_PASSWORD_SELECTORS),
                "codeSelectors": list(NATIONAL_CODE_SELECTORS),
                "mfaChoiceSelectors": list(NATIONAL_MFA_CHOICE_SELECTORS),
            },
        )
    except Exception:
        state = {"current_url": visible_auth.safe_page_url(raw_page), "authenticated": False}
    invalid_credentials = bool(auth_signals.get("invalid_credentials") or state.get("provider_error_visible"))
    browser_validation_sensor_failed = bool(
        auth_signals.get("api_browser_validation_failed")
        and not auth_signals.get("api_browser_validation_ready")
    )
    browser_validation_pending = bool(
        state.get("username_visible")
        and state.get("password_visible")
        and auth_signals.get("api_browser_validation_seen")
        and not auth_signals.get("api_browser_validation_ready")
        and not browser_validation_sensor_failed
    )
    if auth_signals.get("browser_validation_failed"):
        if state.get("username_visible") or state.get("password_visible") or state.get("code_visible") or state.get("mfa_choice_visible"):
            auth_signals.pop("browser_validation_failed", None)
        else:
            state["browser_validation_failed"] = True
        state["authenticated"] = False
    state["browser_validation_sensor_failed"] = browser_validation_sensor_failed
    state["browser_validation_pending"] = browser_validation_pending
    state["authenticated"] = bool(
        auth_signals.get("authenticated")
        and auth_signals.get("accounts_graphql_ready")
        and not invalid_credentials
        and not state.get("browser_validation_failed")
        and not state.get("browser_validation_sensor_failed")
        and not state.get("code_visible")
        and not state.get("mfa_choice_visible")
    )
    state["invalid_credentials"] = invalid_credentials
    if invalid_credentials:
        state["authenticated"] = False
    return state


def describe_national_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated National Bank session detected."
    if state.get("browser_validation_sensor_failed"):
        return "National Bank browser validation did not finish. Close this window and start sync again."
    if state.get("browser_validation_failed"):
        return "National Bank rejected browser validation. Close this window and start sync again."
    if state.get("invalid_credentials"):
        return "National Bank showed a sign-in error. Review the fields in the secure browser and continue."
    if state.get("code_visible") or state.get("mfa_choice_visible"):
        return "Complete National Bank verification in the secure browser."
    if state.get("browser_validation_pending"):
        return "Waiting for National Bank browser validation."
    if state.get("username_visible") or state.get("password_visible"):
        return "Sign in to National Bank in the secure browser."
    if state.get("authenticated_path") or state.get("app_shell_visible") or state.get("sign_out_visible"):
        return "Waiting for National Bank account data."
    return "Waiting for National Bank secure login."


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


async def _maybe_prefill_national_credentials(
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
    if state.get("authenticated") or state.get("code_visible") or state.get("mfa_choice_visible") or state.get("invalid_credentials"):
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        return False
    if not (state.get("username_visible") or state.get("password_visible")):
        return False
    username, password = credentials
    username_filled = await _fill_first_visible(page, NATIONAL_USERNAME_SELECTORS, username)
    password_filled = await _fill_first_visible(page, NATIONAL_PASSWORD_SELECTORS, password)
    if username_filled:
        _capture_username(captured_credentials, username, source="prefill")
    if password_filled:
        _capture_password(captured_credentials, password, source="prefill")
    if username_filled or password_filled:
        prefill_state["done"] = True
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        message = "Saved National Bank credentials were prefilled in the secure browser. Review them and continue sign-in."
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
    chrome_identity: dict[str, Any],
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    last_description = ""
    prefill_state: dict[str, Any] = {"done": False}
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_national_page(
                context,
                page,
                captured_credentials,
                auth_signals,
                chrome_identity,
                popup_lock=popup_lock,
            )
            await _national_clear_cookie_consent_noise(page)
            await _maybe_continue_past_unsupported_browser(page)
            await _capture_national_raw_input_credentials(page, captured_credentials)
            try:
                state = await get_national_page_state(page, auth_signals)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_national_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                    captured_credentials=captured_credentials,
                )
            if (
                (state.get("authenticated") or state.get("authenticated_path") or state.get("app_shell_visible") or state.get("sign_out_visible"))
                and not (state.get("code_visible") or state.get("mfa_choice_visible") or state.get("invalid_credentials"))
                and not state.get("browser_validation_failed")
                and not state.get("browser_validation_sensor_failed")
            ):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="National Bank",
                    log_support_event=log_support_event,
                    reason="app_shell",
                )
            description = describe_national_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                return page
            if (
                (
                    state.get("invalid_credentials")
                    or state.get("browser_validation_failed")
                    or state.get("code_visible")
                    or state.get("mfa_choice_visible")
                )
                and popup_lock is not None
                and popup_lock.locked
            ):
                await popup_lock.set_locked(False)
            await asyncio.sleep(NATIONAL_WAIT_POLL_SECONDS)
        raise TimeoutError("Timed out waiting for an authenticated National Bank session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def get_viable_national_page(
    context,
    current_page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    chrome_identity: dict[str, Any],
    *,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
):
    deadline = asyncio.get_running_loop().time() + NATIONAL_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    while True:
        try:
            pages = [page for page in list(getattr(context, "pages", []) or []) if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"National Bank browser context is no longer available: {exc}") from exc
        for page in pages:
            await _apply_national_user_agent_override(context, page, chrome_identity, phase="viable_page_scan")
            bind_national_page_watcher(page, captured_credentials, auth_signals)
        national_pages = [
            page
            for page in pages
            if any(marker in visible_auth.safe_page_url(page).lower() for marker in NATIONAL_PAGE_URL_MARKERS)
        ]
        if national_pages:
            replacement = national_pages[-1]
            if current_raw_page is replacement:
                return current_page
            await visible_auth.bind_popup_lock_to_page(context, popup_lock, replacement)
            return popup_lock.page if popup_lock is not None and popup_lock.raw_page is replacement else replacement
        if current_raw_page is not None and current_raw_page in pages:
            current_url = visible_auth.safe_page_url(current_raw_page).strip().lower()
            if current_url and current_url != "about:blank":
                await visible_auth.bind_popup_lock_to_page(context, popup_lock, current_raw_page)
                return current_page
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(NATIONAL_PAGE_REPLACEMENT_POLL_SECONDS)


def _national_origin_from_url(url: str | None) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and _is_national_storage_host(host):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def storage_state_has_national_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").lower()
        if _is_national_storage_host(domain) and not _is_national_consent_cookie_name(name):
            return True
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if not _is_national_storage_host(host):
            continue
        local_storage = origin.get("localStorage") or []
        if any(
            isinstance(item, dict) and not _is_national_consent_cookie_name(str(item.get("name") or ""))
            for item in local_storage
        ):
            return True
        if origin.get("indexedDB"):
            return True
    return False


def sanitize_national_bootstrap_storage_state(
    storage_state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
        "kept_national_cookies": 0,
        "kept_national_origins": 0,
    }
    if not isinstance(storage_state, dict):
        return None, details
    try:
        sanitized = json.loads(json.dumps(storage_state))
    except Exception:
        sanitized = dict(storage_state)

    cookies = sanitized.get("cookies") if isinstance(sanitized.get("cookies"), list) else []
    kept_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").strip().lower()
        if _is_national_storage_host(domain) and name in NATIONAL_BOOTSTRAP_AUTH_COOKIE_NAMES:
            details["removed_auth_cookies"] += 1
            continue
        if _is_national_storage_host(domain):
            details["kept_national_cookies"] += 1
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    origins = sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []
    kept_origins: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if _is_national_storage_host(host):
            local_storage = origin.get("localStorage") if isinstance(origin.get("localStorage"), list) else []
            kept_local_storage: list[dict[str, Any]] = []
            for item in local_storage:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip().lower()
                if name in NATIONAL_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES:
                    details["removed_auth_local_storage"] += 1
                    continue
                kept_local_storage.append(item)
            origin["localStorage"] = kept_local_storage
            details["kept_national_origins"] += 1
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not storage_state_has_national_material(sanitized):
        return None, details
    return sanitized, details


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_national_material,
        origin_from_url=_national_origin_from_url,
        storage_state_timeout_seconds=NATIONAL_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=NATIONAL_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=NATIONAL_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def build_national_session_artifact(*, access_token: str, user_agent: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifact = {
        "captured_at": now,
        "authenticated_at": now,
        "access_token": access_token,
        "token_expires_at": _national_token_expires_at_iso(access_token),
        "user_agent": user_agent,
        "browser_profile": {"user_agent": user_agent},
    }
    return artifact


def add_national_graphql_session_metadata(artifact: dict[str, Any], auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_headers = auth_signals.get("graphql_headers")
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for name in NATIONAL_GRAPHQL_REPLAY_HEADER_NAMES:
            value = _national_clean_header_value(raw_headers.get(name))
            if value:
                headers[name] = value
    session_id = _national_clean_header_value(auth_signals.get("session_id")) or headers.get("session_id", "")
    if session_id:
        headers["session_id"] = session_id
        artifact["session_id"] = session_id
    if headers:
        artifact["graphql_headers"] = headers
        artifact["graphql_request_profile"] = {
            "method": "POST",
            "url": "https://digitalretail.apis.bnc.ca/sbip/graphql",
            "headers": headers,
        }
    if auth_signals.get("graphql_uses_authorization"):
        artifact["graphql_uses_authorization"] = True
        scheme = str(auth_signals.get("graphql_authorization_scheme") or "").strip().lower()
        if scheme in {"bearer", "raw"}:
            artifact["graphql_authorization_scheme"] = scheme
    authorization = _national_clean_authorization_value(auth_signals.get("graphql_authorization"))
    if authorization:
        artifact["graphql_authorization"] = authorization
    graphql_access_token = _national_authorization_access_token(authorization)
    if graphql_access_token:
        artifact["graphql_access_token"] = graphql_access_token
    accounts_request_body = _national_accounts_request_body(auth_signals.get("accounts_graphql_request_body"))
    if accounts_request_body:
        artifact["accounts_graphql_request_body"] = accounts_request_body
    transactions_operation_name = _national_clean_graphql_operation_name(
        auth_signals.get("transactions_graphql_operation_name")
    )
    if transactions_operation_name:
        artifact["transactions_graphql_operation_name"] = transactions_operation_name
    return artifact


def add_national_browser_storage_snapshot(artifact: dict[str, Any], browser_storage: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(browser_storage, dict):
        return artifact
    origins = browser_storage.get("origins")
    if isinstance(origins, list) and origins:
        artifact["browser_storage"] = {"origins": origins}
    return artifact


def _national_session_authorization_header(
    session_artifact: dict[str, Any],
    *,
    access_token: str,
) -> str:
    captured = _national_clean_authorization_value(session_artifact.get("graphql_authorization"))
    if captured:
        return captured
    if not access_token or not session_artifact.get("graphql_uses_authorization"):
        return ""
    scheme = str(session_artifact.get("graphql_authorization_scheme") or "").strip().lower()
    return f"Bearer {access_token}" if scheme == "bearer" else access_token


def _national_replay_material_missing(session_artifact: dict[str, Any]) -> dict[str, bool]:
    access_token = (
        _national_any_access_token(session_artifact.get("access_token"))
        or _national_any_access_token(session_artifact.get("graphql_access_token"))
        or _national_authorization_access_token(session_artifact.get("graphql_authorization"))
    )
    return {
        "access_token": not bool(access_token),
        "session_id": not bool(_national_clean_header_value(session_artifact.get("session_id"))),
        "graphql_authorization": not bool(
            _national_session_authorization_header(session_artifact, access_token=access_token or "")
        ),
        "accounts_graphql_request_body": not bool(
            _national_accounts_request_body(session_artifact.get("accounts_graphql_request_body"))
        ),
    }


async def wait_for_national_graphql_session_metadata(auth_signals: dict[str, Any]) -> None:
    if _national_clean_header_value(auth_signals.get("session_id")):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + NATIONAL_GRAPHQL_SESSION_HEADER_TIMEOUT_SECONDS
    while loop.time() < deadline:
        if _national_clean_header_value(auth_signals.get("session_id")):
            return
        await asyncio.sleep(0.1)


def collect_national_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if username and password and not visible_auth._is_masked_secret(password):
        return username, password
    return None


async def open_national_login_page(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    await raw_page.goto(NATIONAL_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await raw_page.wait_for_timeout(750)
    await _national_clear_cookie_consent_noise(page)
    await _maybe_continue_past_unsupported_browser(page)
    await _national_clear_cookie_consent_noise(page)


def _national_is_unsupported_browser_url(current_url: str | None) -> bool:
    parsed = urlsplit(str(current_url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return host == "connexion.bnc.ca" and any(marker in path for marker in NATIONAL_UNSUPPORTED_BROWSER_PATHS)


async def _maybe_continue_past_unsupported_browser(page) -> bool:
    raw_page = getattr(page, "raw_page", page)
    current_url = visible_auth.safe_page_url(raw_page)
    if not _national_is_unsupported_browser_url(current_url):
        return False
    clicked = False
    try:
        clicked = bool(
            await raw_page.evaluate(
                """
                () => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const buttons = Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit']"))
                        .filter(isVisible)
                        .filter((el) => !el.disabled && el.getAttribute("aria-disabled") !== "true");
                    const button = buttons[buttons.length - 1];
                    if (!button) {
                        return false;
                    }
                    button.click();
                    return true;
                }
                """
            )
        )
    except Exception:
        clicked = False
    await log_support_event(
        stage="unsupported browser gate",
        message=(
            "National Bank unsupported-browser gate was continued."
            if clicked
            else "National Bank unsupported-browser gate was detected but could not be continued automatically."
        ),
        details={"continued": clicked},
    )
    if not clicked:
        return False
    try:
        await raw_page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        await raw_page.wait_for_timeout(1000)
    except Exception:
        pass
    return True


async def run() -> None:
    if PROVIDER != "national":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for National Bank visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    auth_signals: dict[str, Any] = {"authenticated": False, "invalid_credentials": False}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
        "kept_national_cookies": 0,
        "kept_national_origins": 0,
    }
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        if storage_state_has_national_material(loaded_storage_state):
            bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_national_bootstrap_storage_state(
                loaded_storage_state
            )
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
    raw_har_path = _national_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening National Bank secure login browser.")
    visible_auth.print_status("Opening National Bank secure login browser.")

    async with async_playwright() as playwright:
        browser = None
        context = None
        browser_runtime: dict[str, Any] = {}
        chrome_identity: dict[str, Any] = {}
        browser_timezone = normalize_browser_timezone(VISIBLE_AUTH_TIMEZONE)
        launch_mode = "isolated_context_fresh"
        context_kwargs: dict[str, Any] = {}
        try:
            try:
                browser_runtime = ensure_brave_browser_runtime()
                chrome_identity = _national_chrome_compat_identity(browser_runtime)
            except DesktopBrowserRuntimeError as exc:
                raise RuntimeError(str(exc)) from exc

            har_capture_message = (
                "National Bank visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "National Bank visible-auth HAR capture is disabled."
            )
            await log_support_event(
                stage="har capture",
                debug=True,
                message=har_capture_message,
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )

            try:
                launch_kwargs = visible_auth.managed_browser_launch_kwargs(browser_runtime)
                context_kwargs = {
                    "timezone_id": browser_timezone,
                    "user_agent": chrome_identity["user_agent"],
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    NATIONAL_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_url_filter": NATIONAL_HAR_URL_FILTER,
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _national_preseed_cookie_consent(context)
                await context.route(NATIONAL_DIDOMI_NOTICE_URL_PATTERN, _national_notice_route_handler)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="National Bank",
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
                message="National Bank visible-auth browser runtime selected.",
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
                message="National Bank visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_storage_sanitized": not VISIBLE_AUTH_ADD_FLOW,
                    "bootstrap_auth_cookies_removed": bootstrap_storage_sanitize_details["removed_auth_cookies"],
                    "bootstrap_auth_local_storage_removed": bootstrap_storage_sanitize_details[
                        "removed_auth_local_storage"
                    ],
                    "bootstrap_national_cookies_kept": bootstrap_storage_sanitize_details["kept_national_cookies"],
                    "bootstrap_national_origins_kept": bootstrap_storage_sanitize_details["kept_national_origins"],
                    "managed_chromium_user_agent_override": True,
                    "consent_notice_route_patched": True,
                    "locale_policy": "browser_default",
                    "entry_url": NATIONAL_LOGIN_URL,
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
                bind_national_page_watcher(new_page, captured_credentials, auth_signals)

                async def install_page_hooks() -> None:
                    try:
                        await _apply_national_user_agent_override(context, new_page, chrome_identity, phase="new_page")
                    except Exception:
                        return

                asyncio.create_task(install_page_hooks())
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
            page = await _prepare_national_context_page(
                context,
                captured_credentials,
                auth_signals,
                chrome_identity,
            )
            await log_support_event(
                stage="credential capture",
                debug=True,
                message="National Bank visible-auth credential capture configured.",
                details={
                    "browser_init_scripts_enabled": False,
                    "credential_capture_strategy": "raw_input_request_fallback_no_credential_init_script",
                },
            )
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = visible_auth.PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_national_login_page(page)
            page = await wait_for_authenticated_session(
                context,
                page,
                captured_credentials=captured_credentials,
                auth_signals=auth_signals,
                chrome_identity=chrome_identity,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="National Bank",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            await asyncio.sleep(1.5)
            await wait_for_national_graphql_session_metadata(auth_signals)
            active_target = getattr(page, "raw_page", page)
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            access_token = await collect_national_access_token(context, page, storage_state, auth_signals)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_national_material": storage_state_has_national_material(storage_state),
                "has_access_token": bool(access_token),
            }
            if not storage_state_has_national_material(storage_state):
                raise RuntimeError("National Bank storage state could not be captured for future syncs.")
            if not access_token:
                raise RuntimeError("National Bank session token could not be captured from the secure login flow. Start National Bank login again.")

            browser_storage = await collect_national_browser_storage_snapshot(context, page)
            session_artifact = build_national_session_artifact(access_token=access_token, user_agent=user_agent)
            session_artifact = add_national_graphql_session_metadata(session_artifact, auth_signals)
            session_artifact = add_national_browser_storage_snapshot(session_artifact, browser_storage)
            missing_replay_material = _national_replay_material_missing(session_artifact)
            if any(missing_replay_material.values()):
                missing_fields = ", ".join(
                    sorted(key for key, missing in missing_replay_material.items() if missing)
                )
                raise RuntimeError(
                    "National Bank GraphQL replay material could not be captured "
                    f"({missing_fields}). Start National Bank login again."
                )
            session_artifact_details = {
                "has_access_token": bool(session_artifact.get("access_token")),
                "has_token_expiry": bool(session_artifact.get("token_expires_at")),
                "has_session_id": bool(session_artifact.get("session_id")),
                "has_graphql_headers": bool(session_artifact.get("graphql_headers")),
                "has_graphql_request_profile": bool(session_artifact.get("graphql_request_profile")),
                "has_graphql_authorization": bool(session_artifact.get("graphql_authorization")),
                "has_accounts_graphql_request_body": bool(session_artifact.get("accounts_graphql_request_body")),
                "has_transactions_graphql_operation_name": bool(session_artifact.get("transactions_graphql_operation_name")),
                "browser_storage_origins": len(browser_storage.get("origins") or []) if isinstance(browser_storage, dict) else 0,
                "graphql_uses_authorization": bool(session_artifact.get("graphql_uses_authorization")),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            captured_pair = collect_national_credentials(captured_credentials)
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
                raise RuntimeError("National Bank credentials could not be captured from the secure login flow. Start National Bank login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="National Bank",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated National Bank session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="National Bank visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="National Bank visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="National Bank visible-auth credential capture summary.",
                details=credential_capture_details,
            )
            storage_result = await persist_visible_auth_artifact(
                artifact_type="storage_state",
                payload=storage_state,
            )
            session_result = await persist_visible_auth_artifact(
                artifact_type="session",
                payload=session_artifact,
            )
            storage_message = "National Bank secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured National Bank credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"National Bank secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
            await visible_auth.close_visible_auth_context_quietly(context, browser)
            context = None
            browser = None
            if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_national_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized National Bank HAR capture.",
                            details={"har_result": run_result, "path": str(sanitized_har_path)},
                        )
                except Exception as exc:
                    await log_support_event(
                        stage="har capture",
                        level="warning",
                        debug=True,
                        message="Could not finalize the sanitized National Bank HAR capture.",
                        details={"har_result": run_result, "error": str(exc)},
                    )


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
        install_signal_handlers=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
