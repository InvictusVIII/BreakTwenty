#!/usr/bin/env python3
"""Desktop visible-auth runner for TD.

Electron launches this runner inside the managed private/packaged Patchright
environment. It opens a BreakTwenty-managed Brave browser, lets the user complete
TD EasyWeb sign-in, stages reusable auth artifacts, and hands off to the normal
backend sync route for profile-guarded validation and persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

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
TD_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "td"
TD_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "TD")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "td").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
TD_VISIBLE_AUTH_PLATFORM = visible_auth.resolve_provider_visible_auth_platform(PROVIDER)
TD_VISIBLE_AUTH_FLOW = visible_auth.provider_visible_auth_flow(PROVIDER, TD_VISIBLE_AUTH_PLATFORM)
TD_LINUX_FLOW = TD_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_LINUX
TD_WINDOWS_FLOW = TD_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_WINDOWS
TD_MAC_FLOW = TD_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_MAC
TD_WINDOWS_MANAGED_BRAVE_STABILIZERS = False
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

TD_AUTH_UI_URL = "https://authentication.td.com/uap-ui/?consumer=easyweb&locale=en_CA"
TD_AUTH_BASIC_PATH = "/waw/idp/authn/v1/authenticate/basic"
TD_ACCOUNTS_SUMMARY_PATH = "/ms/uainq/v1/accounts/summary"
TD_USERNAME_SELECTOR = "#username"
TD_PASSWORD_SELECTOR = "#uapPassword"
TD_OTP_INPUT_SELECTOR = "#code"
TD_OTP_CHOICE_ROOT_SELECTORS = (
    "core-otp-choice-modal",
    "#otpChoiceModalTitle",
    "mat-dialog-container[aria-labelledby='otpChoiceModalTitle']",
)
TD_OTP_CHALLENGE_ROOT_SELECTORS = (
    "core-otp-challenge-modal",
    "#otpChallengeModalTitle",
    "mat-dialog-container[aria-labelledby='otpChallengeModalTitle']",
)
TD_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
TD_COOKIE_CONSENT_SELECTORS = (
    TD_COOKIE_ACCEPT_SELECTOR,
    "button#onetrust-accept-btn-handler",
    "[data-testid='cookie-accept']",
    "[data-test='cookie-accept']",
    "[data-qa='cookie-accept']",
)
TD_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1"
TD_COOKIE_BANNER_SCRIPT = """
(() => {
    const isTdHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("td.com");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isTdHost()) {
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
    if (window.__breaktwentyTdConsentNoiseInstalled) {
        clearBanner();
        return;
    }
    window.__breaktwentyTdConsentNoiseInstalled = true;
    const start = () => {
        clearBanner();
        if (!(document.documentElement instanceof HTMLElement)) {
            return;
        }
        const observer = new MutationObserver(() => {
            clearBanner();
        });
        observer.observe(document.documentElement, {
            subtree: true,
            childList: true,
            attributes: true,
        });
    };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
        return;
    }
    start();
})();
"""
TD_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
TD_WAIT_POLL_SECONDS = 0.35
TD_PAGE_REPLACEMENT_RETRY_SECONDS = 5
TD_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
TD_STORAGE_STATE_TIMEOUT_SECONDS = 15
TD_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
TD_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
TD_HAR_FILENAME_PREFIX = "td_visible_auth"
TD_MAX_SANITIZED_HARS_PER_USER = 3
TD_REDACTED_VALUE = "<redacted>"
TD_VISIBLE_ERROR_SNAPSHOT_MAX_CHARS = 1200
TD_VISIBLE_ERROR_TEXT_MAX_CHARS = 900
TD_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "active",
        "category",
        "code",
        "error",
        "message",
        "reason",
        "result",
        "status",
        "statuscode",
        "type",
    }
)
TD_HAR_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-device-id",
        "x-xsrf-token",
    }
)
TD_SAFE_BOOTSTRAP_COOKIE_NAMES = frozenset(
    {
        "optanonalertboxclosed",
        "optanonconsent",
        "ot-consent-one-time",
    }
)
HOST_BROWSER_CLOSED_MESSAGE = (
    "Login window was closed before secure login finished. "
    "Start sync again and keep the bank browser window open until the app says it is done."
)


async def log_support_event(
    *,
    stage: str,
    result: str = "",
    level: str = "info",
    debug: bool = False,
    message: str = "",
    last_output: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    global VISIBLE_AUTH_SYNC_ID
    await VISIBLE_AUTH_CLIENT.log_support_event(
        stage=stage,
        result=result,
        level=level,
        debug=debug,
        message=message,
        last_output=last_output,
        details=details,
    )
    VISIBLE_AUTH_SYNC_ID = VISIBLE_AUTH_CLIENT.sync_id


async def persist_visible_auth_artifact(
    *,
    artifact_type: str,
    payload: Any,
) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_artifact(
        artifact_type=artifact_type,
        payload=payload,
    )


async def persist_visible_auth_credentials(username: str, password: str) -> dict[str, Any]:
    return await VISIBLE_AUTH_CLIENT.persist_credentials(username, password)


def _td_browser_launch_kwargs(browser_runtime: dict[str, Any] | None) -> dict[str, Any]:
    return visible_auth.managed_browser_launch_kwargs(
        browser_runtime,
        windows_managed_brave_stabilizers=TD_WINDOWS_FLOW and TD_WINDOWS_MANAGED_BRAVE_STABILIZERS,
        mac_managed_brave_stabilizers=TD_MAC_FLOW,
    )


def _td_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return TD_DESKTOP_AUTH_RAW_HAR_DIR / f"{TD_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}.har"


def _td_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return TD_DIAGNOSTIC_DIR / f"{TD_HAR_FILENAME_PREFIX}_user-{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _td_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=TD_REDACTED_VALUE)


def _td_should_keep_har_response_body(url: str, status: int | None, content_type: str) -> bool:
    url_lower = str(url or "").lower()
    if "json" not in str(content_type or "").lower():
        return False
    if "/waw/idp/authn/" in url_lower:
        return True
    return status is not None and int(status) >= 400


def finalize_td_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _td_sanitized_har_path(user_id, timestamp_slug, result)
    sanitized_paths = sorted(
        TD_DIAGNOSTIC_DIR.glob(f"{TD_HAR_FILENAME_PREFIX}_user-{user_id}_*.har"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=TD_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_td_should_keep_har_response_body,
        safe_response_keys=TD_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=TD_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=TD_REDACTED_VALUE,
    )


def _td_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname or ""
    if not host.endswith("td.com"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _td_consent_cookie_payload() -> list[dict[str, Any]]:
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
            ("groups", TD_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".td.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".td.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]


async def _td_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_td_consent_cookie_payload())
    except Exception:
        return


async def _td_click_first_enabled(page, selectors: tuple[str, ...]) -> bool:
    try:
        clicked = await page.evaluate(
            """
            (selectors) => {
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                };
                for (const selector of selectors) {
                    const candidates = Array.from(document.querySelectorAll(selector));
                    for (const el of candidates) {
                        if (!isVisible(el)) continue;
                        if (el.disabled || (el.getAttribute("aria-disabled") || "").toLowerCase() === "true") continue;
                        el.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            list(selectors),
        )
        return bool(clicked)
    except Exception:
        return False


async def _dismiss_td_cookie_consent(page) -> bool:
    if await _td_click_first_enabled(page, TD_COOKIE_CONSENT_SELECTORS):
        try:
            await page.wait_for_timeout(300)
        except Exception:
            pass
        return True
    return False


async def _td_clear_cookie_consent_noise(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(TD_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass
    await _dismiss_td_cookie_consent(page)


async def _td_selector_visible(page, selector: str) -> bool:
    try:
        locator = page.locator(selector).first
        if await locator.count() < 1:
            return False
        return await locator.is_visible()
    except Exception:
        return False


async def _td_any_selector_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await _td_selector_visible(page, selector):
            return True
    return False


async def _td_otp_visible(page) -> bool:
    if await _td_any_selector_visible(page, TD_OTP_CHOICE_ROOT_SELECTORS):
        return True
    if await _td_any_selector_visible(page, TD_OTP_CHALLENGE_ROOT_SELECTORS):
        return True
    return await _td_selector_visible(page, TD_OTP_INPUT_SELECTOR)


def _td_easyweb_gateway_url(url: str) -> bool:
    return str(url or "").lower().startswith("https://easyweb.td.com/ui/gateway")


def storage_state_has_td_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "td.com" in domain:
            return True
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if "td.com" in str(origin.get("origin") or "").lower():
            return True
    return False


def sanitize_td_bootstrap_storage_state(
    storage_state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {
        "removed_auth_cookies": 0,
        "kept_safe_cookies": 0,
        "removed_td_origins": 0,
        "kept_non_td_origins": 0,
    }
    if not isinstance(storage_state, dict):
        return None, details
    try:
        sanitized = json.loads(json.dumps(storage_state))
    except Exception:
        sanitized = dict(storage_state)

    kept_cookies: list[dict[str, Any]] = []
    for cookie in sanitized.get("cookies") if isinstance(sanitized.get("cookies"), list) else []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip().lower()
        if "td.com" in domain and name not in TD_SAFE_BOOTSTRAP_COOKIE_NAMES:
            details["removed_auth_cookies"] += 1
            continue
        if "td.com" in domain:
            details["kept_safe_cookies"] += 1
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    kept_origins: list[dict[str, Any]] = []
    for origin in sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []:
        if not isinstance(origin, dict):
            continue
        origin_text = str(origin.get("origin") or "").lower()
        if "td.com" in origin_text:
            details["removed_td_origins"] += 1
            continue
        details["kept_non_td_origins"] += 1
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not storage_state_has_td_material(sanitized):
        return None, details
    return sanitized, details


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_td_material,
        origin_from_url=_td_origin_from_url,
        storage_state_timeout_seconds=TD_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=TD_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=TD_LOCAL_STORAGE_TIMEOUT_SECONDS,
    )


async def collect_td_session_artifact(page, *, user_agent: str = "") -> dict[str, Any]:
    if not user_agent:
        user_agent = await visible_auth.collect_user_agent(page)
    return {
        "user_agent": str(user_agent or "").strip(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def session_artifact_has_td_bootstrap(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    return bool(str(artifact.get("user_agent") or "").strip())


def _field_value_from_mapping(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    key_fragments = tuple(key.lower() for key in keys)
    for key, value in mapping.items():
        key_lower = str(key or "").strip().lower()
        if any(fragment in key_lower for fragment in key_fragments):
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _credentials_from_value(value: Any, *, depth: int = 0) -> tuple[str, str] | None:
    if value is None or depth > 6:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return _credentials_from_value(parsed, depth=depth + 1)
        return None
    if isinstance(value, list):
        for nested in value:
            found = _credentials_from_value(nested, depth=depth + 1)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None

    username = _field_value_from_mapping(
        value,
        ("username", "userid", "user_id", "loginid", "login_id", "accesscard", "cardnumber", "card_number"),
    )
    password = _field_value_from_mapping(value, ("password", "passcode", "pwd"))
    if username and password:
        return username, password

    for nested in value.values():
        found = _credentials_from_value(nested, depth=depth + 1)
        if found:
            return found
    return None


def _credentials_from_post_data(post_data: str) -> tuple[str, str] | None:
    text = str(post_data or "").strip()
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        found = _credentials_from_value(parsed)
        if found:
            return found
    pairs = {name: value for name, value in parse_qsl(text, keep_blank_values=True)}
    if pairs:
        return _credentials_from_value(pairs)
    return None


def _normalize_td_username(username: str) -> str:
    return "".join(str(username or "").split())


def _sanitize_td_visible_error_text(text: Any, captured_credentials: dict[str, str] | None = None) -> str:
    sanitized = re.sub(r"\s+", " ", str(text or "")).strip()
    for value in (
        (captured_credentials or {}).get("username"),
        (captured_credentials or {}).get("password"),
    ):
        secret = str(value or "").strip()
        if not secret:
            continue
        sanitized = sanitized.replace(secret, TD_REDACTED_VALUE)
        normalized_secret = _normalize_td_username(secret)
        if normalized_secret and normalized_secret != secret:
            sanitized = sanitized.replace(normalized_secret, TD_REDACTED_VALUE)
    sanitized = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", TD_REDACTED_VALUE, sanitized, flags=re.I)
    sanitized = re.sub(r"\b(?:\d[\s-]?){5,}\b", TD_REDACTED_VALUE, sanitized)
    if len(sanitized) <= TD_VISIBLE_ERROR_SNAPSHOT_MAX_CHARS:
        return sanitized
    return f"{sanitized[:TD_VISIBLE_ERROR_SNAPSHOT_MAX_CHARS].rstrip()}..."


async def _collect_td_visible_error_snapshot(page, captured_credentials: dict[str, str]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "url": _td_sanitize_url(visible_auth.safe_page_url(page)),
        "title": "",
        "visible_text_sample": "",
        "capture_error": "",
    }
    try:
        title = await page.title()
    except Exception:
        title = ""
    snapshot["title"] = _sanitize_td_visible_error_text(title, captured_credentials)
    try:
        text = await page.evaluate(
            """
            (limit) => {
                const text = String(document.body?.innerText || document.documentElement?.innerText || "");
                return text.slice(0, limit);
            }
            """,
            TD_VISIBLE_ERROR_TEXT_MAX_CHARS,
        )
        snapshot["visible_text_sample"] = _sanitize_td_visible_error_text(text, captured_credentials)
    except Exception as exc:
        snapshot["capture_error"] = _sanitize_td_visible_error_text(type(exc).__name__, {})
    return snapshot


def _is_td_credential_submit_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.hostname == "authentication.td.com" and parsed.path.lower() == TD_AUTH_BASIC_PATH


def _capture_td_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = _normalize_td_username(username)
    if not normalized:
        return
    captured_credentials["username"] = normalized
    captured_credentials[f"_{source}_username_capture_count"] = str(
        int(captured_credentials.get(f"_{source}_username_capture_count") or 0) + 1
    )


def _capture_td_password(captured_credentials: dict[str, str], password: str, *, source: str) -> None:
    if not password:
        return
    captured_credentials["password"] = str(password)
    captured_credentials[f"_{source}_password_capture_count"] = str(
        int(captured_credentials.get(f"_{source}_password_capture_count") or 0) + 1
    )


async def _frame_input_value(frame, selector: str) -> str:
    try:
        locator = frame.locator(selector).first
        if await locator.count() < 1:
            return ""
        return str(await locator.input_value(timeout=500) or "")
    except Exception:
        return ""


async def _capture_td_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    for frame in list(getattr(page, "frames", []) or []):
        username = await _frame_input_value(frame, TD_USERNAME_SELECTOR)
        password = await _frame_input_value(frame, TD_PASSWORD_SELECTOR)
        if username:
            _capture_td_username(captured_credentials, username, source="raw")
        if password:
            _capture_td_password(captured_credentials, password, source="raw")


def _capture_td_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    if not _is_td_credential_submit_url(str(getattr(request, "url", "") or "")):
        return
    credential_pair = _credentials_from_post_data(visible_auth.safe_request_post_data(request))
    if credential_pair is None:
        return
    username, password = credential_pair
    _capture_td_username(captured_credentials, username, source="request")
    _capture_td_password(captured_credentials, password, source="request")


def _record_td_auth_response_status(auth_signals: dict[str, Any], *, url: str, status: int) -> None:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return
    path = parsed.path.lower()
    if parsed.hostname != "authentication.td.com" or path != TD_AUTH_BASIC_PATH:
        return
    if status in (400, 401, 403):
        auth_signals["invalid_credentials"] = True
        return
    if TD_WINDOWS_FLOW and status >= 500:
        auth_signals["provider_error"] = {
            "status": status,
            "host": parsed.hostname,
            "path": path,
        }


async def _collect_td_auth_response_diagnostic(response, *, url: str, status: int) -> dict[str, Any]:
    details: dict[str, Any] = {
        "status": status,
        "content_type": "",
        "body": TD_REDACTED_VALUE,
        "body_captured": False,
        "capture_error": "",
    }
    headers = getattr(response, "headers", {}) or {}
    if callable(headers):
        try:
            headers = headers()
        except Exception:
            headers = {}
    if isinstance(headers, dict):
        details["content_type"] = str(headers.get("content-type") or headers.get("Content-Type") or "")
    try:
        body_text = await response.text()
    except Exception as exc:
        details["capture_error"] = type(exc).__name__
        return details
    if not body_text:
        return details
    sanitized = visible_auth.sanitize_response_content(
        url,
        {"status": status},
        {"mimeType": details["content_type"], "text": body_text},
        should_keep_response_body=_td_should_keep_har_response_body,
        safe_response_keys=TD_HAR_SAFE_RESPONSE_KEYS,
        redacted_value=TD_REDACTED_VALUE,
    )
    if isinstance(sanitized, dict):
        details["body"] = str(sanitized.get("text") or TD_REDACTED_VALUE)
        details["body_captured"] = details["body"] != TD_REDACTED_VALUE
    return details


def _install_td_page_watchers(
    page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
) -> None:
    if getattr(page, "_td_visible_auth_watchers_installed", False):
        return
    setattr(page, "_td_visible_auth_watchers_installed", True)

    def on_request(request) -> None:
        if _is_td_credential_submit_url(str(getattr(request, "url", "") or "")):
            asyncio.create_task(_capture_td_raw_input_credentials(page, captured_credentials))
            _capture_td_credentials_from_request(captured_credentials, request)

    async def handle_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        status = int(getattr(response, "status", 0) or 0)
        try:
            parsed = urlsplit(url)
        except ValueError:
            return
        path = parsed.path.lower()
        _record_td_auth_response_status(auth_signals, url=url, status=status)
        if TD_WINDOWS_FLOW and parsed.hostname == "authentication.td.com" and path == TD_AUTH_BASIC_PATH and status >= 500:
            auth_signals["provider_error_response"] = await _collect_td_auth_response_diagnostic(
                response,
                url=url,
                status=status,
            )
        if parsed.hostname == "easyweb.td.com" and path == TD_ACCOUNTS_SUMMARY_PATH and 200 <= status < 300:
            auth_signals["accounts_summary"] = True

    def on_response(response) -> None:
        asyncio.create_task(handle_response(response))

    page.on("request", on_request)
    page.on("response", on_response)


async def _prepare_td_context_page(context, captured_credentials: dict[str, str], auth_signals: dict[str, Any]):
    pages = [page for page in list(context.pages or []) if not visible_auth.is_page_closed(page)]
    if pages:
        page = pages[0]
        _install_td_page_watchers(page, captured_credentials, auth_signals)
        for extra_page in pages[1:]:
            _install_td_page_watchers(extra_page, captured_credentials, auth_signals)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    _install_td_page_watchers(page, captured_credentials, auth_signals)
    return page


async def _active_td_page(context, current_page, captured_credentials: dict[str, str], auth_signals: dict[str, Any]):
    current_raw_page = getattr(current_page, "raw_page", current_page)
    if current_raw_page is not None and not visible_auth.is_page_closed(current_raw_page):
        _install_td_page_watchers(current_raw_page, captured_credentials, auth_signals)
        return current_page
    for page in reversed(list(getattr(context, "pages", []) or [])):
        if visible_auth.is_page_closed(page):
            continue
        _install_td_page_watchers(page, captured_credentials, auth_signals)
        return page
    return await _prepare_td_context_page(context, captured_credentials, auth_signals)


async def open_td_login_page(page) -> None:
    await log_support_event(
        stage="login page open",
        debug=True,
        message="Opening TD EasyWeb login.",
        details={"entry_url": _td_sanitize_url(TD_AUTH_UI_URL)},
    )
    await page.goto(
        TD_AUTH_UI_URL,
        wait_until="domcontentloaded",
        timeout=90_000,
    )
    try:
        await page.wait_for_timeout(250)
    except Exception:
        pass
    await _td_clear_cookie_consent_noise(page)


async def _maybe_prefill_td_credentials(
    page,
    *,
    credentials: tuple[str, str] | None,
    popup_lock: visible_auth.PopupInteractionLock | None,
    auth_signals: dict[str, Any],
) -> bool:
    if not credentials:
        return False
    username, password = credentials
    if not username and not password:
        return False

    if auth_signals.get("accounts_summary") or await _td_otp_visible(page):
        if popup_lock is not None:
            await popup_lock.set_locked(False)
        return True

    filled_any = False
    if username and await _td_selector_visible(page, TD_USERNAME_SELECTOR):
        try:
            await page.locator(TD_USERNAME_SELECTOR).fill(_normalize_td_username(username))
            filled_any = True
        except Exception:
            pass
    if password and await _td_selector_visible(page, TD_PASSWORD_SELECTOR):
        try:
            await page.locator(TD_PASSWORD_SELECTOR).fill(password)
            filled_any = True
        except Exception:
            pass

    if filled_any:
        if popup_lock is not None:
            await popup_lock.set_locked(False)
        message = "Saved TD credentials were prefilled in the secure browser. Review them and continue sign-in."
        visible_auth.print_status(message)
        await log_support_event(stage="credentials prefilled", message=message, last_output=message)
        return True
    return False


async def _wait_for_page_replacement(
    context,
    page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
):
    deadline = asyncio.get_running_loop().time() + TD_PAGE_REPLACEMENT_RETRY_SECONDS
    while visible_auth.visible_auth_deadline_active(deadline):
        candidate = await _active_td_page(context, page, captured_credentials, auth_signals)
        if candidate is not None and not visible_auth.is_page_closed(candidate):
            return candidate
        await asyncio.sleep(TD_PAGE_REPLACEMENT_POLL_SECONDS)
    return page


async def wait_for_authenticated_session(
    context,
    page,
    *,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
    prefill_already_done: bool = False,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    lock_started_at = asyncio.get_running_loop().time() if popup_lock is not None and popup_lock.locked else 0.0
    prefill_done = prefill_already_done
    invalid_logged = False
    while visible_auth.visible_auth_deadline_active(deadline):
        page = await _wait_for_page_replacement(context, page, captured_credentials, auth_signals)
        await visible_auth.bind_popup_lock_to_page(context, popup_lock, getattr(page, "raw_page", page))
        if popup_lock is not None:
            page = popup_lock.page

        if not VISIBLE_AUTH_ADD_FLOW:
            await _capture_td_raw_input_credentials(page, captured_credentials)
        current_url = visible_auth.safe_page_url(page)
        if auth_signals.get("accounts_summary"):
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="TD",
                log_support_event=log_support_event,
                reason="accounts_summary",
            )
            return page
        provider_error = auth_signals.get("provider_error")
        if isinstance(provider_error, dict):
            status = int(provider_error.get("status") or 0)
            provider_error = {
                **provider_error,
                "response": auth_signals.get("provider_error_response") or {},
                "visible_page": await _collect_td_visible_error_snapshot(page, captured_credentials),
            }
            message = (
                f"TD returned HTTP {status} during credential submit in the Windows secure login browser. "
                "Start TD login again; if it repeats, export diagnostics from this attempt."
            )
            visible_auth.print_status(message)
            await log_support_event(
                stage="provider auth error",
                result="failed",
                level="error",
                message=message,
                last_output=message,
                details=provider_error,
            )
            raise RuntimeError(message)
        if _td_easyweb_gateway_url(current_url):
            await asyncio.sleep(1)
            continue

        if auth_signals.get("invalid_credentials") and not invalid_logged:
            invalid_logged = True
            if popup_lock is not None:
                await popup_lock.set_locked(False)
            message = "TD showed a credential error. Popup interaction is unlocked so you can correct the credentials and continue."
            visible_auth.print_status(message)
            await log_support_event(stage="provider credential error", level="warning", message=message, last_output=message)

        if prefill_credentials and not prefill_done:
            prefill_done = await _maybe_prefill_td_credentials(
                page,
                credentials=prefill_credentials,
                popup_lock=popup_lock,
                auth_signals=auth_signals,
            )
        elif popup_lock is not None and popup_lock.locked:
            lock_age = asyncio.get_running_loop().time() - lock_started_at if lock_started_at else 0.0
            if await _td_otp_visible(page) or lock_age >= TD_CREDENTIAL_LOCK_GRACE_SECONDS:
                await popup_lock.set_locked(False)

        await _td_clear_cookie_consent_noise(page)
        await asyncio.sleep(TD_WAIT_POLL_SECONDS)

    raise TimeoutError("Timed out waiting for an authenticated TD session.")


async def run() -> None:
    if PROVIDER != "td":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for TD visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    auth_signals: dict[str, Any] = {"accounts_summary": False, "invalid_credentials": False}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {
        "removed_auth_cookies": 0,
        "kept_safe_cookies": 0,
        "removed_td_origins": 0,
        "kept_non_td_origins": 0,
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
        if storage_state_has_td_material(loaded_storage_state):
            bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_td_bootstrap_storage_state(
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
    raw_har_path = _td_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening TD secure login browser.")
    visible_auth.print_status("Opening TD secure login browser.")

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
                "TD visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "TD visible-auth HAR capture is disabled."
            )
            await log_support_event(
                stage="har capture",
                debug=True,
                message=har_capture_message,
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )

            try:
                launch_kwargs = _td_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    TD_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _td_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="TD",
                        retry_message=f"Restart {visible_auth.APP_BRAND_NAME} Desktop so it can prepare the browser runtime, then try again.",
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
                message="TD visible-auth browser runtime selected.",
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
                message="TD visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_auth_cookies_removed": bootstrap_storage_sanitize_details["removed_auth_cookies"],
                    "bootstrap_td_origins_removed": bootstrap_storage_sanitize_details["removed_td_origins"],
                    "locale_policy": "browser_default",
                    "timezone_id": browser_timezone,
                    "har_capture_enabled": VISIBLE_AUTH_RECORD_HAR,
                    "visible_auth_platform": TD_VISIBLE_AUTH_PLATFORM,
                    "visible_auth_flow": TD_VISIBLE_AUTH_FLOW,
                    "td_linux_flow": TD_LINUX_FLOW,
                    "td_windows_flow": TD_WINDOWS_FLOW,
                    "td_mac_flow": TD_MAC_FLOW,
                    "windows_managed_brave_launch_args_enabled": bool(
                        TD_WINDOWS_FLOW and launch_kwargs.get("args")
                    ),
                    **visible_auth.browser_launch_details(launch_kwargs),
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
                _install_td_page_watchers(new_page, captured_credentials, auth_signals)
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
            page = await _prepare_td_context_page(context, captured_credentials, auth_signals)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = visible_auth.PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_td_login_page(page)
            prefill_already_done = False
            if saved_credentials:
                prefill_already_done = await _maybe_prefill_td_credentials(
                    page,
                    credentials=saved_credentials,
                    popup_lock=popup_lock,
                    auth_signals=auth_signals,
                )
            page = await wait_for_authenticated_session(
                context,
                page,
                captured_credentials=captured_credentials,
                auth_signals=auth_signals,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
                prefill_already_done=prefill_already_done,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="TD",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = page
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_td_material": storage_state_has_td_material(storage_state),
            }
            if not storage_state_has_td_material(storage_state):
                raise RuntimeError("TD storage state could not be captured for future syncs.")

            session_artifact = await collect_td_session_artifact(active_target, user_agent=user_agent)
            if not session_artifact_has_td_bootstrap(session_artifact):
                raise RuntimeError("TD session bootstrap data could not be captured for future syncs.")
            session_artifact_details = {
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }

            username = captured_credentials.get("username") or ""
            password = captured_credentials.get("password") or ""
            credential_capture_details = {
                "raw_username_capture_count": int(captured_credentials.get("_raw_username_capture_count") or 0),
                "request_username_capture_count": int(captured_credentials.get("_request_username_capture_count") or 0),
                "raw_password_capture_count": int(captured_credentials.get("_raw_password_capture_count") or 0),
                "request_password_capture_count": int(captured_credentials.get("_request_password_capture_count") or 0),
                "username_captured": bool(username),
                "password_captured": bool(password),
            }
            if not (username and password):
                raise RuntimeError("TD credentials could not be captured from the secure login flow. Start TD login again.")

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="TD",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated TD session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="TD visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="TD visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="TD visible-auth credential capture summary.",
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
            storage_message = "TD secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured TD credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"TD secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
                        finalize_td_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized TD HAR capture.",
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
                        message="Could not finalize the sanitized TD HAR capture.",
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
