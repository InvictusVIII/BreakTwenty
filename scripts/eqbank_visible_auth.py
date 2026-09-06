#!/usr/bin/env python3
"""Desktop visible-auth runner for EQ Bank."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
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
EQBANK_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "eqbank"
EQBANK_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "EQBank")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "eqbank").strip().lower()
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

EQBANK_ORIGIN = "https://secure.eqbank.ca"
EQBANK_LOGIN_URL = f"{EQBANK_ORIGIN}/"
EQBANK_AUTH_CALLBACK_URL = f"{EQBANK_ORIGIN}/auth-callback"
EQBANK_ACCESS_TOKEN_PATH = "/auth/v3/access-token"
EQBANK_LOGIN_DETAILS_PATH = "/auth/v3/login-details"
EQBANK_ACCOUNTS_PATH_MARKER = "/web/v1.1/accounts/v2/accounts"
EQBANK_PAGE_URL_MARKERS = ("eqbank.ca",)
EQBANK_USERNAME_SELECTORS = (
    "#username",
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
)
EQBANK_PASSWORD_SELECTORS = (
    "#password",
    'input[name="password"]',
    'input[type="password"]',
)
EQBANK_CODE_SELECTORS = (
    "#code",
    'input[name="code"]',
    'input[autocomplete="one-time-code"]',
)
EQBANK_PUBLIC_LOGIN_SELECTORS = (
    "a[href*='auth.eqbank.ca']",
)
EQBANK_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1"
EQBANK_COOKIE_BANNER_SCRIPT = """
(() => {
    const isEqbankHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("eqbank.ca");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isEqbankHost()) {
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
            document.body.style.removeProperty("position");
        }
    };
    clearBanner();
    window.setTimeout(clearBanner, 250);
    window.setTimeout(clearBanner, 1000);
})();
"""
EQBANK_HAR_FILENAME_PREFIX = "eqbank_visible_auth"
EQBANK_MAX_SANITIZED_HARS_PER_USER = 3
EQBANK_REDACTED_VALUE = "<redacted>"
EQBANK_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "accounts",
        "accountType",
        "availableBalance",
        "balanceType",
        "code",
        "currency",
        "currentBalance",
        "error",
        "message",
        "productType",
        "relationship",
        "status",
        "type",
    }
)
EQBANK_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
EQBANK_WAIT_POLL_SECONDS = 0.35
EQBANK_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
EQBANK_PAGE_REPLACEMENT_RETRY_SECONDS = 5
EQBANK_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
EQBANK_STORAGE_STATE_TIMEOUT_SECONDS = 15
EQBANK_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
EQBANK_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
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


def _eqbank_consent_cookie_payload() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    expiry = int(now.timestamp()) + (365 * 24 * 60 * 60)
    datestamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    consent_value = urlencode(
        [
            ("isGpcEnabled", "0"),
            ("datestamp", datestamp),
            ("version", "202502.1.0"),
            ("browserGpcFlag", "1"),
            ("isIABGlobal", "false"),
            ("hosts", ""),
            ("consentId", str(uuid.uuid4())),
            ("interactionCount", "1"),
            ("isAnonUser", "1"),
            ("prevHadToken", "0"),
            ("landingPath", "NotLandingPage"),
            ("groups", EQBANK_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".eqbank.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".eqbank.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "ot-consent-one-time",
            "value": "trigger",
            "domain": ".eqbank.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]


async def _eqbank_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_eqbank_consent_cookie_payload())
    except Exception:
        pass


async def _dismiss_eqbank_cookie_banner(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    try:
        await raw_page.evaluate(EQBANK_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass


def _eqbank_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    filename = f"{EQBANK_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}.har"
    return EQBANK_DESKTOP_AUTH_RAW_HAR_DIR / filename


def _eqbank_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    filename = f"{EQBANK_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}{scope_part}_{result}.har"
    return EQBANK_DIAGNOSTIC_DIR / filename


def _eqbank_sanitized_har_candidates(user_id: int) -> list[Path]:
    if not EQBANK_DIAGNOSTIC_DIR.exists():
        return []
    return list(EQBANK_DIAGNOSTIC_DIR.glob(f"{EQBANK_HAR_FILENAME_PREFIX}_user-{user_id}_*.har"))


def _eqbank_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if not (host == "api.eqbank.ca" or host == "web-api.eqbank.ca" or host.endswith(".eqbank.ca")):
        return False
    if "json" not in str(content_type or "").lower():
        return False
    if status and int(status) >= 500:
        return True
    return path in {
        EQBANK_ACCESS_TOKEN_PATH,
        EQBANK_LOGIN_DETAILS_PATH,
    } or path.startswith(EQBANK_ACCOUNTS_PATH_MARKER)


def finalize_eqbank_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=_eqbank_sanitized_har_path(user_id, timestamp_slug, result),
        sanitized_paths_to_prune=_eqbank_sanitized_har_candidates(user_id),
        keep=EQBANK_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_eqbank_should_keep_response_body,
        safe_response_keys=EQBANK_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=EQBANK_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=EQBANK_REDACTED_VALUE,
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
        parsed = _json_body(text)
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


def _extract_storage_state_access_token(storage_state: dict[str, Any] | None) -> str:
    if not isinstance(storage_state, dict):
        return ""
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            candidate = _extract_token_candidate(entry.get("value"))
            if _eqbank_token_is_fresh(candidate):
                return str(candidate)
    return ""


def _capture_token(auth_signals: dict[str, Any], token: str | None) -> None:
    if _eqbank_token_is_fresh(token):
        auth_signals["access_token"] = str(token)


def _eqbank_endpoint_key(url: str) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host == "api.eqbank.ca" and path == EQBANK_ACCESS_TOKEN_PATH:
        return "access_token"
    if host == "api.eqbank.ca" and path == EQBANK_LOGIN_DETAILS_PATH:
        return "login_details"
    if host == "web-api.eqbank.ca" and path.startswith(EQBANK_ACCOUNTS_PATH_MARKER):
        return "accounts"
    return None


def _find_credential_value(value: Any, preferred_keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_lower = str(key).strip().lower()
            if any(preferred in key_lower for preferred in preferred_keys):
                if isinstance(nested, (str, int)):
                    text = str(nested).strip()
                    if text:
                        return text
            candidate = _find_credential_value(nested, preferred_keys)
            if candidate:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = _find_credential_value(nested, preferred_keys)
            if candidate:
                return candidate
    return ""


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
        return
    captured_credentials["password"] = str(password)
    captured_credentials[f"_{source}_password_capture_count"] = str(
        int(captured_credentials.get(f"_{source}_password_capture_count") or 0) + 1
    )


async def _capture_eqbank_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    raw_page = getattr(page, "raw_page", page)
    frames = list(getattr(raw_page, "frames", []) or [])
    if not frames:
        frames = [raw_page]
    for frame in frames:
        try:
            values = await frame.evaluate(
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
                    "usernameSelectors": list(EQBANK_USERNAME_SELECTORS),
                    "passwordSelectors": list(EQBANK_PASSWORD_SELECTORS),
                },
            )
        except Exception:
            continue
        if not isinstance(values, dict):
            continue
        _capture_username(captured_credentials, str(values.get("username") or ""), source="raw")
        _capture_password(captured_credentials, str(values.get("password") or ""), source="raw")


def _capture_eqbank_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    method = str(getattr(request, "method", "") or "").upper()
    if method != "POST":
        return
    parsed = urlsplit(str(getattr(request, "url", "") or ""))
    host = (parsed.hostname or "").lower()
    if not (host == "auth.eqbank.ca" or host.endswith(".eqbank.ca")):
        return
    payload = _request_body(visible_auth.safe_request_post_data(request))
    if payload is None:
        return
    username = _find_credential_value(payload, ("username", "email", "identifier", "login"))
    password = _find_credential_value(payload, ("password",))
    if username:
        _capture_username(captured_credentials, username, source="request")
    if password:
        _capture_password(captured_credentials, password, source="request")


async def _capture_eqbank_request_headers(request, auth_signals: dict[str, Any]) -> None:
    try:
        headers = await visible_auth.request_headers(request)
    except Exception:
        headers = {}
    authorization = str(headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        _capture_token(auth_signals, authorization.split(" ", 1)[1].strip())


async def _capture_eqbank_response(response, auth_signals: dict[str, Any]) -> None:
    key = _eqbank_endpoint_key(str(getattr(response, "url", "") or ""))
    if not key:
        return
    status = int(getattr(response, "status", 0) or 0)
    if key == "access_token":
        try:
            text = await response.text()
        except Exception:
            text = ""
        _capture_token(auth_signals, _extract_token_candidate(_json_body(text) or text))
    if status in (200, 201, 204) and key == "accounts":
        auth_signals["authenticated"] = True


def bind_eqbank_page_watcher(page, captured_credentials: dict[str, str], auth_signals: dict[str, Any]) -> None:
    if getattr(page, "_eqbank_visible_auth_watcher_installed", False):
        return
    setattr(page, "_eqbank_visible_auth_watcher_installed", True)

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                _schedule_eqbank_popup_lock(
                    page,
                    force=bool(auth_signals.get("manual_prefill_pending_password")),
                )
        except Exception:
            return

    def on_request(request) -> None:
        asyncio.create_task(_capture_eqbank_raw_input_credentials(page, captured_credentials))
        _capture_eqbank_credentials_from_request(captured_credentials, request)
        asyncio.create_task(_capture_eqbank_request_headers(request, auth_signals))

    def on_response(response) -> None:
        asyncio.create_task(_capture_eqbank_response(response, auth_signals))

    page.on("framenavigated", on_frame_navigated)
    page.on("request", on_request)
    page.on("response", on_response)


def _schedule_eqbank_popup_lock(page, *, force: bool = False) -> None:
    popup_lock = getattr(page, "_eqbank_popup_lock", None)
    if popup_lock is None or (not popup_lock.locked and not force):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def lock_page() -> None:
        try:
            await popup_lock.set_locked(True)
        except Exception:
            return

    loop.create_task(lock_page())


async def _prepare_eqbank_context_page(context, captured_credentials: dict[str, str], auth_signals: dict[str, Any]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_eqbank_page_watcher(page, captured_credentials, auth_signals)
        for extra_page in pages[1:]:
            bind_eqbank_page_watcher(extra_page, captured_credentials, auth_signals)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_eqbank_page_watcher(page, captured_credentials, auth_signals)
    return page


def _eqbank_is_auth_callback_url(current_url: str | None) -> bool:
    parsed = urlsplit(str(current_url or ""))
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == EQBANK_AUTH_CALLBACK_URL


def _eqbank_is_authenticated_url(current_url: str | None) -> bool:
    parsed = urlsplit(str(current_url or ""))
    host = (parsed.hostname or "").lower()
    return host == "secure.eqbank.ca" and not _eqbank_is_auth_callback_url(current_url)


async def get_eqbank_page_state(page, auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    frames = list(getattr(raw_page, "frames", []) or [])
    if not frames:
        frames = [raw_page]
    combined = {
        "current_url": visible_auth.safe_page_url(raw_page),
        "username_visible": False,
        "password_visible": False,
        "code_visible": False,
    }
    for frame in frames:
        try:
            state = await frame.evaluate(
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
                    return {
                        current_url: window.location.href,
                        username_visible: visibleMatches(usernameSelectors),
                        password_visible: visibleMatches(passwordSelectors),
                        code_visible: visibleMatches(codeSelectors),
                    };
                }
                """,
                {
                    "usernameSelectors": list(EQBANK_USERNAME_SELECTORS),
                    "passwordSelectors": list(EQBANK_PASSWORD_SELECTORS),
                    "codeSelectors": list(EQBANK_CODE_SELECTORS),
                },
            )
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        combined["current_url"] = str(state.get("current_url") or combined["current_url"])
        combined["username_visible"] = combined["username_visible"] or bool(state.get("username_visible"))
        combined["password_visible"] = combined["password_visible"] or bool(state.get("password_visible"))
        combined["code_visible"] = combined["code_visible"] or bool(state.get("code_visible"))

    current_url = str(combined.get("current_url") or "")
    token_present = _eqbank_token_is_fresh(str(auth_signals.get("access_token") or ""))
    combined["auth_callback"] = _eqbank_is_auth_callback_url(current_url)
    combined["authenticated"] = bool(auth_signals.get("authenticated")) or (
        token_present
        and _eqbank_is_authenticated_url(current_url)
        and not combined["username_visible"]
        and not combined["password_visible"]
        and not combined["code_visible"]
    )
    return combined


def describe_eqbank_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated EQ Bank session detected."
    if state.get("code_visible"):
        return "Complete EQ Bank verification in the secure browser."
    if state.get("username_visible") or state.get("password_visible"):
        return "Sign in to EQ Bank in the secure browser."
    if state.get("auth_callback"):
        return "Finishing EQ Bank secure login."
    return "Waiting for EQ Bank secure login."


async def _fill_first_visible(page, selectors: tuple[str, ...], value: str) -> bool:
    raw_page = getattr(page, "raw_page", page)
    for selector in selectors:
        try:
            locator = raw_page.locator(selector)
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


async def _first_visible_input_value(page, selectors: tuple[str, ...]) -> str:
    raw_page = getattr(page, "raw_page", page)
    for selector in selectors:
        try:
            locator = raw_page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                value = await candidate.input_value(timeout=250)
            except Exception:
                continue
            return str(value or "")
    return ""


async def _maybe_prefill_eqbank_credentials(
    page,
    *,
    state: dict[str, Any],
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: visible_auth.PopupInteractionLock | None,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
) -> bool:
    if not credentials:
        return False
    if state.get("authenticated") or state.get("code_visible"):
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        auth_signals["manual_prefill_complete"] = True
        auth_signals["manual_prefill_pending_password"] = False
        return False
    username, password = credentials
    username_value = await _first_visible_input_value(page, EQBANK_USERNAME_SELECTORS) if state.get("username_visible") else ""
    password_value = await _first_visible_input_value(page, EQBANK_PASSWORD_SELECTORS) if state.get("password_visible") else ""
    needs_username_prefill = bool(state.get("username_visible") and username and not username_value)
    needs_password_prefill = bool(state.get("password_visible") and password and not password_value)

    if not (needs_username_prefill or needs_password_prefill):
        if state.get("username_visible") and username_value:
            prefill_state["username_prefilled"] = True
            if password and not prefill_state.get("password_prefilled"):
                auth_signals["manual_prefill_pending_password"] = True
        if state.get("password_visible") and password_value:
            prefill_state["password_prefilled"] = True
            auth_signals["manual_prefill_complete"] = True
            auth_signals["manual_prefill_pending_password"] = False
        if popup_lock is not None and popup_lock.locked:
            started_at = float(prefill_state.setdefault("lock_started_at", asyncio.get_running_loop().time()))
            should_release = (
                state.get("username_visible")
                or state.get("password_visible")
                or (asyncio.get_running_loop().time() - started_at) >= EQBANK_CREDENTIAL_LOCK_GRACE_SECONDS
            )
            if should_release:
                prefill_state.pop("lock_started_at", None)
                await popup_lock.set_locked(False)
        return False

    if popup_lock is not None:
        await popup_lock.set_locked(True)
        prefill_state["lock_started_at"] = asyncio.get_running_loop().time()

    prefills_applied = False
    try:
        if needs_username_prefill:
            username_filled = await _fill_first_visible(page, EQBANK_USERNAME_SELECTORS, username)
            if username_filled:
                _capture_username(captured_credentials, username, source="prefill")
                prefill_state["username_prefilled"] = True
                auth_signals["manual_prefill_pending_password"] = bool(password) and not bool(
                    prefill_state.get("password_prefilled")
                )
                prefills_applied = True
        if needs_password_prefill:
            password_filled = await _fill_first_visible(page, EQBANK_PASSWORD_SELECTORS, password)
            if password_filled:
                _capture_password(captured_credentials, password, source="prefill")
                prefill_state["password_prefilled"] = True
                auth_signals["manual_prefill_complete"] = True
                auth_signals["manual_prefill_pending_password"] = False
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved EQ Bank credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
        return prefills_applied
    finally:
        prefill_state.pop("lock_started_at", None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


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
    prefill_state: dict[str, Any] = {"announced": False}
    callback_seen_at: float | None = None
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_eqbank_page(context, page, captured_credentials, auth_signals, popup_lock=popup_lock)
            await _dismiss_eqbank_cookie_banner(page)
            await _capture_eqbank_raw_input_credentials(page, captured_credentials)
            try:
                state = await get_eqbank_page_state(page, auth_signals)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_eqbank_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                    captured_credentials=captured_credentials,
                    auth_signals=auth_signals,
                )
            description = describe_eqbank_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="EQ Bank",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
                return page
            if state.get("auth_callback") and _eqbank_token_is_fresh(str(auth_signals.get("access_token") or "")):
                now = asyncio.get_running_loop().time()
                if callback_seen_at is None:
                    callback_seen_at = now
                elif now - callback_seen_at > 3:
                    try:
                        await getattr(page, "raw_page", page).goto(EQBANK_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
            await asyncio.sleep(EQBANK_WAIT_POLL_SECONDS)
        raise TimeoutError("Timed out waiting for an authenticated EQ Bank session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def get_viable_eqbank_page(
    context,
    current_page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    *,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
):
    deadline = asyncio.get_running_loop().time() + EQBANK_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    pages = []
    while True:
        try:
            pages = [page for page in list(getattr(context, "pages", []) or []) if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"EQ Bank browser context is no longer available: {exc}") from exc
        for page in pages:
            bind_eqbank_page_watcher(page, captured_credentials, auth_signals)
        eqbank_pages = [
            page
            for page in pages
            if any(marker in visible_auth.safe_page_url(page).lower() for marker in EQBANK_PAGE_URL_MARKERS)
        ]
        if eqbank_pages:
            replacement = eqbank_pages[-1]
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
        await asyncio.sleep(EQBANK_PAGE_REPLACEMENT_POLL_SECONDS)


def _eqbank_origin_from_url(url: str | None) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and (host.endswith(".eqbank.ca") or host == "eqbank.ca"):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _is_eqbank_storage_host(host: str) -> bool:
    normalized = str(host or "").lower()
    return normalized == "eqbank.ca" or normalized.endswith(".eqbank.ca") or normalized.endswith(".auth0.com")


def storage_state_has_eqbank_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if _is_eqbank_storage_host(domain):
            return True
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if _is_eqbank_storage_host(host):
            return True
    return False


def sanitize_eqbank_bootstrap_storage_state(storage_state: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {"removed_auth_cookies": 0, "removed_auth_origins": 0}
    if not isinstance(storage_state, dict):
        return None, details
    sanitized = dict(storage_state)
    safe_cookies: list[dict[str, Any]] = []
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        name = str(cookie.get("name") or "").lower()
        if _is_eqbank_storage_host(domain):
            if "consent" in name or "optanon" in name or "onetrust" in name:
                safe_cookies.append(dict(cookie))
            else:
                details["removed_auth_cookies"] += 1
            continue
        safe_cookies.append(dict(cookie))
    safe_origins: list[dict[str, Any]] = []
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        host = (urlsplit(str(origin.get("origin") or "")).hostname or "").lower()
        if _is_eqbank_storage_host(host):
            details["removed_auth_origins"] += 1
            continue
        safe_origins.append(dict(origin))
    sanitized["cookies"] = safe_cookies
    sanitized["origins"] = safe_origins
    if not safe_cookies and not safe_origins:
        return None, details
    return sanitized, details


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_eqbank_material,
        origin_from_url=_eqbank_origin_from_url,
        storage_state_timeout_seconds=EQBANK_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=EQBANK_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=EQBANK_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def build_eqbank_session_artifact(auth_signals: dict[str, Any], storage_state: dict[str, Any], *, user_agent: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    access_token = str(auth_signals.get("access_token") or "").strip()
    if not _eqbank_token_is_fresh(access_token):
        access_token = _extract_storage_state_access_token(storage_state)
    return {
        "captured_at": now,
        "authenticated_at": now,
        "access_token": access_token or None,
        "user_agent": user_agent,
        "browser_profile": {"user_agent": user_agent} if user_agent else {},
    }


def collect_eqbank_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if username and password and not visible_auth._is_masked_secret(password):
        return username, password
    return None


async def open_eqbank_login_page(page) -> None:
    raw_page = getattr(page, "raw_page", page)
    await raw_page.goto(EQBANK_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await raw_page.wait_for_timeout(1500)
    await _dismiss_eqbank_cookie_banner(page)
    try:
        for selector in EQBANK_PUBLIC_LOGIN_SELECTORS:
            locator = raw_page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1000)
                break
    except Exception:
        pass
    await _dismiss_eqbank_cookie_banner(page)


async def run() -> None:
    if PROVIDER != "eqbank":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for EQ Bank visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    auth_signals: dict[str, Any] = {"authenticated": False}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {"removed_auth_cookies": 0, "removed_auth_origins": 0}
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_eqbank_bootstrap_storage_state(
            loaded_storage_state
        )
        if bootstrap_storage_state is None:
            bootstrap_storage_slot = ""
        bootstrap_session_artifact, bootstrap_session_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="session",
            slots=("quarantine", "active"),
        )

    har_timestamp_slug = visible_auth.har_timestamp_slug() if VISIBLE_AUTH_RECORD_HAR else ""
    raw_har_path = _eqbank_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening EQ Bank secure login browser.")
    visible_auth.print_status("Opening EQ Bank secure login browser.")

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
                "EQ Bank visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "EQ Bank visible-auth HAR capture is disabled."
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
                    EQBANK_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _eqbank_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="EQ Bank",
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
                message="EQ Bank visible-auth browser runtime selected.",
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
                message="EQ Bank visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_storage_sanitized": not VISIBLE_AUTH_ADD_FLOW,
                    "bootstrap_auth_cookies_removed": bootstrap_storage_sanitize_details["removed_auth_cookies"],
                    "bootstrap_auth_origins_removed": bootstrap_storage_sanitize_details["removed_auth_origins"],
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
                bind_eqbank_page_watcher(new_page, captured_credentials, auth_signals)
                if popup_lock is None:
                    return

                async def bind_locked_page() -> None:
                    try:
                        should_lock = popup_lock.locked or (
                            bool(saved_credentials) and not bool(auth_signals.get("manual_prefill_complete"))
                        )
                        await popup_lock.bind_page(new_page, await context.new_cdp_session(new_page))
                        if should_lock:
                            await popup_lock.set_locked(True)
                    except Exception:
                        return

                asyncio.create_task(bind_locked_page())

            context.on("page", on_new_page)
            page = await _prepare_eqbank_context_page(context, captured_credentials, auth_signals)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = visible_auth.PopupInteractionLock(
                        page,
                        await context.new_cdp_session(page),
                        lock_attribute_name="_eqbank_popup_lock",
                    )
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_eqbank_login_page(page)
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
                provider_display_name="EQ Bank",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = getattr(page, "raw_page", page)
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_eqbank_material": storage_state_has_eqbank_material(storage_state),
            }
            if not storage_state_has_eqbank_material(storage_state):
                raise RuntimeError("EQ Bank storage state could not be captured for future syncs.")
            session_artifact = build_eqbank_session_artifact(
                auth_signals,
                storage_state,
                user_agent=user_agent,
            )
            if not _eqbank_token_is_fresh(str(session_artifact.get("access_token") or "")):
                raise RuntimeError("EQ Bank session bearer could not be captured for future syncs.")
            session_artifact_details = {
                "has_access_token": bool(session_artifact.get("access_token")),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            captured_pair = collect_eqbank_credentials(captured_credentials)
            if not captured_pair:
                raise RuntimeError("EQ Bank credentials could not be captured from the secure login flow. Start EQ Bank login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="EQ Bank",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated EQ Bank session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="EQ Bank visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="EQ Bank visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            storage_result = await persist_visible_auth_artifact(
                artifact_type="storage_state",
                payload=storage_state,
            )
            session_result = await persist_visible_auth_artifact(
                artifact_type="session",
                payload=session_artifact,
            )
            storage_message = "EQ Bank secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured EQ Bank credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"EQ Bank secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
                        finalize_eqbank_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized EQ Bank HAR capture.",
                            details={
                                "har_result": run_result,
                                "path": str(sanitized_har_path),
                            },
                        )
                except Exception as exc:
                    await log_support_event(
                        stage="har capture",
                        level="warning",
                        debug=True,
                        message="Could not finalize the sanitized EQ Bank HAR capture.",
                        details={
                            "har_result": run_result,
                            "error": str(exc),
                        },
                    )
            await visible_auth.close_visible_auth_context_quietly(None, browser)


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
