#!/usr/bin/env python3
"""Desktop visible-auth runner for Scotiabank."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

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
SCOTIA_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "scotiabank"
SCOTIA_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "Scotiabank")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "scotiabank").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
SCOTIA_VISIBLE_AUTH_PLATFORM = visible_auth.resolve_provider_visible_auth_platform(PROVIDER)
SCOTIA_VISIBLE_AUTH_FLOW = visible_auth.provider_visible_auth_flow(PROVIDER, SCOTIA_VISIBLE_AUTH_PLATFORM)
SCOTIA_LINUX_FLOW = SCOTIA_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_LINUX
SCOTIA_WINDOWS_FLOW = SCOTIA_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_WINDOWS
SCOTIA_MAC_FLOW = SCOTIA_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_MAC
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

SCOTIA_SECURE_ORIGIN = "https://secure.scotiabank.com"
SCOTIA_ACCOUNTS_URL = f"{SCOTIA_SECURE_ORIGIN}/accounts?lng=en"
SCOTIA_SUMMARY_PATH = "/api/accounts/summary"
SCOTIA_SUMMARY_URL = f"{SCOTIA_SECURE_ORIGIN}{SCOTIA_SUMMARY_PATH}?isAccountSummaryPage=true"
SCOTIA_PAGE_URL_MARKERS = (
    "scotiabank.com",
    "scotiaonline.scotiabank.com",
)
SCOTIA_AUTHENTICATIONS_PATH = "/v2/authentications"
SCOTIA_USERNAME_SELECTOR = "#usernameInput-input"
SCOTIA_PASSWORD_SELECTOR = "#password-input"
SCOTIA_OTP_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[aria-label*="code" i]',
)
SCOTIA_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
SCOTIA_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1,C0005:1"
SCOTIA_BOOTSTRAP_AUTH_COOKIE_NAMES = frozenset(
    {
        "_csrf",
        "_csrfauth",
        "canary-session",
        "session-id",
        "x-auth-sid",
        "bns-auth-saved-users",
    }
)
SCOTIA_DIRECT_REQUIRED_AUTH_COOKIE_NAMES = frozenset({"session-id"})
SCOTIA_DIRECT_CSRF_COOKIE_NAMES = frozenset({"_csrf", "_csrfauth"})
SCOTIA_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES = frozenset(
    {
        "aphishi-lws_at",
        "aphishi-saved-data",
    }
)
SCOTIA_BOOTSTRAP_AUTH_LOCAL_STORAGE_PREFIXES = (
    "orion__",
)
SCOTIA_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
SCOTIA_WAIT_POLL_SECONDS = 0.35
SCOTIA_PAGE_REPLACEMENT_RETRY_SECONDS = 5
SCOTIA_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
SCOTIA_GENERIC_ERROR_RECOVERY_RETRY_LIMIT = 2
SCOTIA_GENERIC_ERROR_RECOVERY_WAIT_SECONDS = 5
SCOTIA_SUMMARY_FETCH_TIMEOUT_SECONDS = 1.0
SCOTIA_STORAGE_STATE_TIMEOUT_SECONDS = 4
SCOTIA_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 1.5
SCOTIA_LOCAL_STORAGE_TIMEOUT_SECONDS = 0.75
SCOTIA_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS = 8.0
SCOTIA_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS = 4.0
SCOTIA_HAR_FILENAME_PREFIX = "scotiabank_visible_auth"
SCOTIA_MAX_SANITIZED_HARS_PER_USER = 3
SCOTIA_REDACTED_VALUE = "<redacted>"
SCOTIA_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "active",
        "category",
        "code",
        "error",
        "message",
        "reason",
        "result",
        "status",
        "status_code",
        "type",
    }
)
SCOTIA_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
SCOTIA_COOKIE_BANNER_SCRIPT = """
(() => {
    const isScotiaHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("scotiabank.com");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isScotiaHost()) {
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


def _scotia_consent_cookie_payload() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    expiry = int(now.timestamp()) + (365 * 24 * 60 * 60)
    datestamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    consent_value = urlencode(
        [
            ("isGpcEnabled", "0"),
            ("datestamp", datestamp),
            ("version", "202308.1.0"),
            ("browserGpcFlag", "1"),
            ("isIABGlobal", "false"),
            ("hosts", ""),
            ("consentId", str(uuid.uuid4())),
            ("interactionCount", "1"),
            ("isAnonUser", "1"),
            ("prevHadToken", "0"),
            ("landingPath", "NotLandingPage"),
            ("groups", SCOTIA_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".scotiabank.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".scotiabank.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]


async def _scotia_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_scotia_consent_cookie_payload())
    except Exception:
        pass


def _scotia_browser_launch_kwargs(browser_runtime: dict[str, Any] | None) -> dict[str, Any]:
    return visible_auth.managed_browser_launch_kwargs(
        browser_runtime,
        windows_managed_brave_stabilizers=SCOTIA_WINDOWS_FLOW,
        mac_managed_brave_stabilizers=SCOTIA_MAC_FLOW,
    )


def _scotia_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return SCOTIA_DESKTOP_AUTH_RAW_HAR_DIR / f"{SCOTIA_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _scotia_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return SCOTIA_DIAGNOSTIC_DIR / f"{SCOTIA_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _scotia_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=SCOTIA_REDACTED_VALUE)


def _scotia_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if "scotiabank.com" not in host:
        return False
    lowered_content_type = content_type.lower()
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_scotia_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _scotia_sanitized_har_path(user_id, timestamp_slug, result)
    pattern = f"{SCOTIA_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        SCOTIA_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=SCOTIA_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_scotia_should_keep_response_body,
        safe_response_keys=SCOTIA_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=SCOTIA_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=SCOTIA_REDACTED_VALUE,
    )


def _scotia_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.endswith("scotiabank.com"):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def page_label(page) -> str:
    return visible_auth.page_label(page, url_sanitizer=_scotia_sanitize_url)


class PopupInteractionLock(visible_auth.PopupInteractionLock):
    def __init__(self, page, cdp_session) -> None:
        super().__init__(page, cdp_session, lock_attribute_name="_scotia_popup_lock")


async def _bind_scotia_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)


def _schedule_scotia_popup_lock(page) -> None:
    popup_lock = getattr(page, "_scotia_popup_lock", None)
    if popup_lock is None or not popup_lock.locked:
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


async def get_viable_scotia_page(context, current_page, popup_lock: PopupInteractionLock | None = None):
    deadline = asyncio.get_running_loop().time() + SCOTIA_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    pages = []
    while True:
        try:
            pages = [page for page in context.pages if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"Scotiabank browser context is no longer available: {exc}") from exc
        if pages:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(SCOTIA_PAGE_REPLACEMENT_POLL_SECONDS)

    scotia_pages = [
        page
        for page in pages
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in SCOTIA_PAGE_URL_MARKERS)
    ]
    if scotia_pages:
        replacement = scotia_pages[-1]
        if current_raw_page is replacement:
            return current_page
        await _bind_scotia_popup_lock_to_page(context, popup_lock, replacement)
        if current_page is not None:
            await log_support_event(
                stage="page replacement",
                debug=True,
                message="Scotiabank page handle replaced.",
                details={
                    "from_page": page_label(current_page),
                    "to_page": page_label(replacement),
                },
            )
        return popup_lock.page if popup_lock is not None else replacement

    if current_raw_page is not None and current_raw_page in pages:
        return current_page

    replacement = pages[-1] if pages else None
    if replacement is None:
        raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
    await _bind_scotia_popup_lock_to_page(context, popup_lock, replacement)
    if current_page is not None:
        await log_support_event(
            stage="page replacement",
            debug=True,
            message="Scotiabank page handle replaced.",
            details={
                "from_page": page_label(current_page),
                "to_page": page_label(replacement),
            },
        )
    return popup_lock.page if popup_lock is not None else replacement


async def _goto_scotia_entry(page, url: str, *, stage: str, timeout_ms: int = 90000) -> None:
    await log_support_event(
        stage=stage,
        debug=True,
        message=f"Scotiabank entry step: {stage}.",
        details={"entry_url": _scotia_sanitize_url(url)},
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await log_support_event(
        stage=f"{stage}_ok",
        debug=True,
        message=f"Scotiabank entry success: {stage}.",
        details={"page_url": page_label(page)},
    )


async def open_scotia_login_page(page, *, entry_url: str) -> None:
    await _goto_scotia_entry(page, entry_url, stage="accounts_entry")
    await _dismiss_scotia_cookie_banner(page)


async def any_selector_is_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await visible_auth.selector_is_visible(page, selector):
            return True
    return False


async def _dismiss_scotia_cookie_banner(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(SCOTIA_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass
    try:
        accept_button = await base_page.query_selector(SCOTIA_COOKIE_ACCEPT_SELECTOR)
    except Exception:
        accept_button = None
    if not accept_button:
        return
    try:
        await accept_button.evaluate(visible_auth.LOCKED_CLICK_SCRIPT)
    except Exception:
        pass


def _scotia_products_from_summary_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    products = data.get("products") if isinstance(data, dict) else None
    return [product for product in products if isinstance(product, dict)] if isinstance(products, list) else []


def _parse_scotia_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _scotia_summary_payload_has_accounts(payload: Any) -> bool:
    return bool(_scotia_products_from_summary_payload(payload))


def _scotia_authenticated_accounts_route(url: str | None) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    return (parsed.hostname or "").lower() == "secure.scotiabank.com" and (parsed.path or "").lower().startswith("/accounts")


async def _fetch_scotia_summary_payload(page) -> dict[str, Any] | None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                f"""
                async () => {{
                    try {{
                        const response = await fetch('{SCOTIA_SUMMARY_URL}', {{ credentials: 'include' }});
                        if (!response.ok) return null;
                        return await response.json();
                    }} catch (_) {{
                        return null;
                    }}
                }}
                """
            ),
            timeout=SCOTIA_SUMMARY_FETCH_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    return result if isinstance(result, dict) else None


async def _scotia_page_summary(page) -> dict[str, Any]:
    accounts_payload = getattr(page, "_scotia_accounts_payload", None)
    current_url = visible_auth.safe_page_url(page)
    account_shell_visible = _scotia_authenticated_accounts_route(current_url)
    if (
        not _scotia_summary_payload_has_accounts(accounts_payload)
        and not account_shell_visible
    ):
        accounts_payload = await _fetch_scotia_summary_payload(page)
        if _scotia_summary_payload_has_accounts(accounts_payload):
            setattr(page, "_scotia_accounts_payload", accounts_payload)
    return {
        "url": current_url,
        "account_shell_visible": account_shell_visible,
        "login_visible": await visible_auth.selector_is_visible(page, SCOTIA_USERNAME_SELECTOR),
        "password_visible": await visible_auth.selector_is_visible(page, SCOTIA_PASSWORD_SELECTOR),
        "otp_visible": await any_selector_is_visible(page, SCOTIA_OTP_INPUT_SELECTORS),
        "accounts_payload_ready": _scotia_summary_payload_has_accounts(accounts_payload),
    }


async def get_scotia_page_state(page) -> dict[str, Any]:
    summary = await _scotia_page_summary(page)
    auth_status = int(getattr(page, "_scotia_auth_status", 0) or 0)
    try:
        parsed_url = urlsplit(str(summary.get("url") or ""))
    except ValueError:
        parsed_url = None
    path = parsed_url.path.lower() if parsed_url else ""
    return {
        **summary,
        "authenticated": bool(summary.get("accounts_payload_ready")),
        "generic_error": "generic-error" in path,
        "auth_server_error": auth_status >= 500,
        "auth_rejected": auth_status in (401, 403, 429),
    }


def describe_scotia_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated Scotiabank session detected. Saving session for background sync."
    if state.get("account_shell_visible"):
        return "Scotiabank account dashboard loaded. Securing the handoff while account data is captured."
    if state.get("auth_server_error"):
        return "Scotiabank returned a server error during secure login. Try again later."
    if state.get("auth_rejected"):
        return "Scotiabank rejected this login attempt. Popup interaction is unlocked so you can correct it and continue."
    if state.get("generic_error"):
        return "Scotiabank returned a generic error page. Trying the authenticated accounts page directly."
    if state.get("login_visible"):
        return "Waiting for manual sign-in at the Scotiabank login page."
    if state.get("password_visible"):
        return "Waiting for Scotiabank password entry in the secure browser."
    if state.get("otp_visible"):
        return "Waiting for Scotiabank security code entry in the secure browser."
    return "Waiting for Scotiabank secure login to advance."


def _install_scotia_request_capture(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_scotia_request_capture_installed", False):
        return
    setattr(page, "_scotia_request_capture_installed", True)

    def on_request(request) -> None:
        try:
            path = urlsplit(str(getattr(request, "url", "") or "")).path
        except ValueError:
            return
        if not path.startswith(SCOTIA_AUTHENTICATIONS_PATH):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_capture_scotia_raw_input_credentials(page, captured_credentials))

    page.on("request", on_request)


def _install_scotia_response_capture(page) -> None:
    if getattr(page, "_scotia_response_capture_installed", False):
        return
    setattr(page, "_scotia_response_capture_installed", True)

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def capture_response() -> None:
            try:
                status = int(getattr(response, "status", 0) or 0)
                if parsed.path.startswith(SCOTIA_AUTHENTICATIONS_PATH):
                    setattr(page, "_scotia_auth_status", status)
                    return
                if parsed.path != SCOTIA_SUMMARY_PATH or status != 200:
                    return
                payload = _parse_scotia_json_body(await response.text())
                if _scotia_summary_payload_has_accounts(payload):
                    setattr(page, "_scotia_accounts_payload", payload)
            except Exception:
                return

        loop.create_task(capture_response())

    page.on("response", on_response)


def bind_scotia_page_watcher(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_scotia_page_watcher_installed", False):
        return
    setattr(page, "_scotia_page_watcher_installed", True)
    setattr(page, "_scotia_navigation_token", int(getattr(page, "_scotia_navigation_token", 0) or 0))

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                current_token = int(getattr(page, "_scotia_navigation_token", 0) or 0)
                setattr(page, "_scotia_navigation_token", current_token + 1)
                _schedule_scotia_popup_lock(page)
        except Exception:
            return

    page.on("framenavigated", on_frame_navigated)
    _install_scotia_request_capture(page, captured_credentials)
    _install_scotia_response_capture(page)


async def collect_storage_state(context) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + SCOTIA_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS
    storage_state: dict[str, Any] = {"cookies": [], "origins": []}
    while True:
        storage_state = await visible_auth.collect_storage_state(
            context,
            has_material=storage_state_has_scotia_material,
            origin_from_url=_scotia_origin_from_url,
            storage_state_timeout_seconds=SCOTIA_STORAGE_STATE_TIMEOUT_SECONDS,
            fallback_timeout_seconds=SCOTIA_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
            local_storage_timeout_seconds=SCOTIA_LOCAL_STORAGE_TIMEOUT_SECONDS,
            indexed_db=True,
        )
        if storage_state_has_scotia_material(storage_state):
            return storage_state
        if asyncio.get_running_loop().time() >= deadline:
            return storage_state
        await asyncio.sleep(0.5)


def _scotia_storage_state_cookie_names(storage_state: dict[str, Any] | None) -> set[str]:
    if not isinstance(storage_state, dict):
        return set()
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    names: set[str] = set()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "scotiabank.com" in domain:
            name = str(cookie.get("name") or "").strip().lower()
            if name:
                names.add(name)
    return names


def storage_state_has_scotia_material(storage_state: dict[str, Any] | None) -> bool:
    cookie_names = _scotia_storage_state_cookie_names(storage_state)
    return (
        SCOTIA_DIRECT_REQUIRED_AUTH_COOKIE_NAMES.issubset(cookie_names)
        and bool(cookie_names & SCOTIA_DIRECT_CSRF_COOKIE_NAMES)
    )


def storage_state_has_scotia_bootstrap_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    if _scotia_storage_state_cookie_names(storage_state):
        return True
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _scotia_origin_from_url(origin.get("origin")):
            return True
    return False


def sanitize_scotia_bootstrap_storage_state(
    storage_state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
        "removed_auth_indexed_db": 0,
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
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip().lower()
        if "scotiabank.com" in domain and name in SCOTIA_BOOTSTRAP_AUTH_COOKIE_NAMES:
            details["removed_auth_cookies"] += 1
            continue
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    origins = sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []
    kept_origins: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if not _scotia_origin_from_url(origin.get("origin")):
            kept_origins.append(origin)
            continue
        local_storage = origin.get("localStorage") if isinstance(origin.get("localStorage"), list) else []
        kept_local_storage: list[dict[str, Any]] = []
        for item in local_storage:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            name_lower = name.lower()
            if name_lower in SCOTIA_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES or any(
                name_lower.startswith(prefix) for prefix in SCOTIA_BOOTSTRAP_AUTH_LOCAL_STORAGE_PREFIXES
            ):
                details["removed_auth_local_storage"] += 1
                continue
            kept_local_storage.append(item)
        origin["localStorage"] = kept_local_storage
        if "indexedDB" in origin:
            details["removed_auth_indexed_db"] += 1
            origin.pop("indexedDB", None)
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not storage_state_has_scotia_bootstrap_material(sanitized):
        return None, details
    return sanitized, details


async def collect_scotia_session_artifact(page, *, active_target=None, user_agent: str = "") -> dict[str, Any]:
    targets: list[Any] = []
    seen_targets: set[int] = set()
    for target in (active_target, page):
        if target is None:
            continue
        target_id = id(target)
        if target_id in seen_targets:
            continue
        seen_targets.add(target_id)
        targets.append(target)

    artifact: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    for target in targets:
        accounts_payload = getattr(target, "_scotia_accounts_payload", None)
        if _scotia_summary_payload_has_accounts(accounts_payload) and "accounts_payload" not in artifact:
            artifact["accounts_payload"] = accounts_payload
    normalized_user_agent = str(user_agent or "").strip()
    if normalized_user_agent:
        artifact["user_agent"] = normalized_user_agent
    return artifact


async def _scotia_input_value(page, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        return str(await locator.input_value(timeout=500) or "")
    except Exception:
        return ""


def _scotia_username_is_masked(value: str) -> bool:
    return "*" in value or "•" in value or "●" in value


def _scotia_secret_is_masked(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and set(text) <= {"*", "x", "X", ".", "-", " ", "•", "●"}


def _capture_scotia_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = str(username or "").strip()
    if not normalized:
        return
    if _scotia_username_is_masked(normalized):
        captured_credentials["_masked_username_count"] = str(
            int(captured_credentials.get("_masked_username_count") or 0) + 1
        )
        return
    if captured_credentials.get("username") != normalized:
        captured_credentials[f"_{source}_username_count"] = str(
            int(captured_credentials.get(f"_{source}_username_count") or 0) + 1
        )
    captured_credentials["username"] = normalized


def _capture_scotia_password(captured_credentials: dict[str, str], password: str, *, source: str) -> None:
    normalized = str(password or "")
    if not normalized:
        return
    if _scotia_secret_is_masked(normalized):
        captured_credentials[f"_{source}_masked_password_count"] = str(
            int(captured_credentials.get(f"_{source}_masked_password_count") or 0) + 1
        )
        return
    if captured_credentials.get("password") != normalized:
        captured_credentials[f"_{source}_password_count"] = str(
            int(captured_credentials.get(f"_{source}_password_count") or 0) + 1
        )
    captured_credentials["password"] = normalized


async def _capture_scotia_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    username = str(await _scotia_input_value(page, SCOTIA_USERNAME_SELECTOR) or "").strip()
    if username:
        _capture_scotia_username(captured_credentials, username, source="raw")

    password = str(await _scotia_input_value(page, SCOTIA_PASSWORD_SELECTOR) or "")
    if password:
        _capture_scotia_password(captured_credentials, password, source="raw")


def _scotia_prefill_phase_key(page, state: dict[str, Any]) -> tuple[int, int, str]:
    navigation_token = int(getattr(page, "_scotia_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
    )


def _scotia_prefill_slot_key(page, state: dict[str, Any], field: str) -> tuple[int, int, str, str]:
    navigation_token = int(getattr(page, "_scotia_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        field,
    )


async def _prefill_scotia_input(page, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        await page.wait_for_selector(selector, state="visible", timeout=1000)
    except Exception:
        return False
    current_value = await _scotia_input_value(page, selector)
    if current_value:
        return False
    try:
        await page.fill(selector, value)
    except Exception:
        return False
    return bool(await _scotia_input_value(page, selector))


async def _maybe_prefill_scotia_credentials(
    page,
    *,
    state: dict[str, Any],
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    captured_credentials: dict[str, str],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    username, password = credentials
    attempted = prefill_state.setdefault("attempted", set())
    lock_started_at = prefill_state.setdefault("lock_started_at", {})
    phase_key = _scotia_prefill_phase_key(page, state)
    username_key = _scotia_prefill_slot_key(page, state, "username")
    password_key = _scotia_prefill_slot_key(page, state, "password")
    needs_username_prefill = (
        state.get("login_visible")
        and bool(username)
        and username_key not in attempted
        and not await _scotia_input_value(page, SCOTIA_USERNAME_SELECTOR)
    )
    needs_password_prefill = (
        state.get("password_visible")
        and bool(password)
        and password_key not in attempted
        and not await _scotia_input_value(page, SCOTIA_PASSWORD_SELECTOR)
    )
    if not (needs_username_prefill or needs_password_prefill):
        if state.get("login_visible"):
            attempted.add(username_key)
        if state.get("password_visible"):
            attempted.add(password_key)
        if popup_lock is not None and popup_lock.locked:
            started_at = float(lock_started_at.setdefault(phase_key, asyncio.get_running_loop().time()))
            should_release = (
                state.get("authenticated")
                or state.get("otp_visible")
                or state.get("auth_rejected")
                or state.get("auth_server_error")
                or (asyncio.get_running_loop().time() - started_at) >= SCOTIA_CREDENTIAL_LOCK_GRACE_SECONDS
            )
            if should_release:
                await popup_lock.set_locked(False)
                lock_started_at.pop(phase_key, None)
        return

    if popup_lock is not None:
        await popup_lock.set_locked(True)
        lock_started_at[phase_key] = asyncio.get_running_loop().time()

    prefills_applied = False
    try:
        if needs_username_prefill:
            username_prefilled = await _prefill_scotia_input(page, SCOTIA_USERNAME_SELECTOR, username)
            attempted.add(username_key)
            if username_prefilled:
                _capture_scotia_username(captured_credentials, username, source="prefill")
                prefills_applied = True
        if needs_password_prefill:
            password_prefilled = await _prefill_scotia_input(page, SCOTIA_PASSWORD_SELECTOR, password)
            attempted.add(password_key)
            if password_prefilled:
                _capture_scotia_password(captured_credentials, password, source="prefill")
                if username and not captured_credentials.get("username"):
                    _capture_scotia_username(captured_credentials, username, source="saved_prefill")
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved Scotiabank credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
    finally:
        lock_started_at.pop(phase_key, None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


async def _quick_prefill_scotia_credentials(
    page,
    *,
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    captured_credentials: dict[str, str],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    state = {
        "url": visible_auth.safe_page_url(page),
        "authenticated": False,
        "login_visible": await visible_auth.selector_is_visible(page, SCOTIA_USERNAME_SELECTOR),
        "password_visible": await visible_auth.selector_is_visible(page, SCOTIA_PASSWORD_SELECTOR),
        "otp_visible": False,
        "auth_rejected": False,
        "auth_server_error": False,
    }
    await _maybe_prefill_scotia_credentials(
        page,
        state=state,
        credentials=credentials,
        prefill_state=prefill_state,
        captured_credentials=captured_credentials,
        popup_lock=popup_lock,
    )


def collect_scotia_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if username and password:
        return username, password
    return None


async def recover_generic_error_state(context, page, popup_lock: PopupInteractionLock | None = None):
    await asyncio.sleep(SCOTIA_GENERIC_ERROR_RECOVERY_WAIT_SECONDS)
    page = await get_viable_scotia_page(context, page, popup_lock)
    try:
        await page.goto(SCOTIA_ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        if visible_auth.is_browser_closed_error(exc):
            page = await get_viable_scotia_page(context, None, popup_lock)
        else:
            await log_support_event(
                stage="generic error recovery",
                level="warning",
                debug=True,
                message="Scotiabank accounts recovery navigation failed.",
                details={"error": str(exc)},
            )
    return await get_viable_scotia_page(context, page, popup_lock)


async def wait_for_authenticated_session(
    context,
    page,
    *,
    captured_credentials: dict[str, str],
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: PopupInteractionLock | None = None,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    last_description = ""
    prefill_state = {"announced": False, "attempted": set()}
    manual_takeover_announced = False
    generic_error_recovery_attempts = 0

    async def ensure_handoff_lock(reason: str) -> None:
        if visible_auth.visible_auth_context_handoff_lock_started(context):
            return
        await visible_auth.ensure_visible_auth_context_handoff_lock(
            context,
            provider_display_name="Scotiabank",
            log_support_event=log_support_event,
            reason=reason,
        )

    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_scotia_page(context, page, popup_lock=popup_lock)
            if _scotia_authenticated_accounts_route(visible_auth.safe_page_url(page)):
                await ensure_handoff_lock("accounts_route")
            await _dismiss_scotia_cookie_banner(page)
            await _capture_scotia_raw_input_credentials(page, captured_credentials)
            if not VISIBLE_AUTH_ADD_FLOW:
                await _quick_prefill_scotia_credentials(
                    page,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    captured_credentials=captured_credentials,
                    popup_lock=popup_lock,
                )
            try:
                state = await get_scotia_page_state(page)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if state.get("generic_error") and generic_error_recovery_attempts < SCOTIA_GENERIC_ERROR_RECOVERY_RETRY_LIMIT:
                generic_error_recovery_attempts += 1
                page = await recover_generic_error_state(context, page, popup_lock)
                state = await get_scotia_page_state(page)
            elif not state.get("generic_error"):
                generic_error_recovery_attempts = 0
            if state.get("account_shell_visible"):
                await ensure_handoff_lock("account_shell_visible")
                if (
                    not state.get("login_visible")
                    and not state.get("password_visible")
                    and not state.get("otp_visible")
                    and not state.get("auth_rejected")
                    and not state.get("auth_server_error")
                ):
                    description = describe_scotia_state(state)
                    if description != last_description:
                        visible_auth.print_status(description)
                        await log_support_event(stage="wait", message=description, last_output=description)
                        last_description = description
                    await log_support_event(
                        stage="account shell ready",
                        debug=True,
                        message="Scotiabank account shell reached; backend sync will validate the saved session.",
                        details={"accounts_payload_ready": bool(state.get("accounts_payload_ready"))},
                    )
                    return page
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_scotia_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    captured_credentials=captured_credentials,
                    popup_lock=popup_lock,
                )
            description = describe_scotia_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await ensure_handoff_lock("authenticated")
                return page
            if state.get("auth_server_error"):
                raise RuntimeError("Scotiabank returned a server error during secure login. Try again later.")
            if state.get("auth_rejected"):
                if popup_lock is not None and popup_lock.locked:
                    await popup_lock.set_locked(False)
                if not manual_takeover_announced:
                    await log_support_event(stage="manual takeover", message=description, last_output=description)
                    manual_takeover_announced = True
            await asyncio.sleep(SCOTIA_WAIT_POLL_SECONDS)

        raise TimeoutError("Timed out waiting for an authenticated Scotiabank session.")
    finally:
        if (
            popup_lock is not None
            and popup_lock.locked
            and not visible_auth.visible_auth_context_handoff_lock_started(context)
        ):
            await popup_lock.set_locked(False)


async def _prepare_scotia_context_page(context, captured_credentials: dict[str, str]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_scotia_page_watcher(page, captured_credentials)
        for extra_page in pages[1:]:
            bind_scotia_page_watcher(extra_page, captured_credentials)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_scotia_page_watcher(page, captured_credentials)
    return page


async def run() -> None:
    if PROVIDER != "scotiabank":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for Scotiabank visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
        "removed_auth_indexed_db": 0,
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
        if storage_state_has_scotia_bootstrap_material(loaded_storage_state):
            bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_scotia_bootstrap_storage_state(
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
    raw_har_path = _scotia_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening Scotiabank secure login browser.")
    visible_auth.print_status("Opening Scotiabank secure login browser.")

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
            await log_support_event(stage="direct_entry", debug=True, message="Scotiabank is using accounts entry.")
            await log_support_event(
                stage="har capture",
                debug=True,
                message=(
                    "Scotiabank visible-auth HAR capture is enabled."
                    if VISIBLE_AUTH_RECORD_HAR
                    else "Scotiabank visible-auth HAR capture is disabled."
                ),
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )
            try:
                launch_kwargs = _scotia_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    SCOTIA_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _scotia_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="Scotiabank",
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
                message="Scotiabank visible-auth browser runtime selected.",
                details={
                    "browser_runtime": runtime_description,
                    "runtime_mode": runtime_mode,
                    "runtime_version": runtime_version,
                    "runtime_platform": runtime_platform,
                    "installed_now": runtime_installed_now,
                    "executable_path": runtime_path,
                    "visible_auth_platform": SCOTIA_VISIBLE_AUTH_PLATFORM,
                    "visible_auth_flow": SCOTIA_VISIBLE_AUTH_FLOW,
                    "scotia_linux_flow": SCOTIA_LINUX_FLOW,
                    "scotia_windows_flow": SCOTIA_WINDOWS_FLOW,
                    "windows_managed_brave_launch_args_enabled": bool(
                        SCOTIA_WINDOWS_FLOW and launch_kwargs.get("args")
                    ),
                    **visible_auth.browser_launch_details(launch_kwargs),
                },
            )
            await log_support_event(
                stage="browser context",
                debug=True,
                message="Scotiabank visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_auth_cookies_removed": bootstrap_storage_sanitize_details["removed_auth_cookies"],
                    "bootstrap_auth_local_storage_removed": bootstrap_storage_sanitize_details[
                        "removed_auth_local_storage"
                    ],
                    "bootstrap_auth_indexed_db_removed": bootstrap_storage_sanitize_details["removed_auth_indexed_db"],
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

            popup_lock: PopupInteractionLock | None = None

            def on_new_page(new_page) -> None:
                bind_scotia_page_watcher(new_page, captured_credentials)

                async def bind_locked_page() -> None:
                    try:
                        should_lock = popup_lock.locked
                        await popup_lock.bind_page(new_page, await context.new_cdp_session(new_page))
                        if should_lock:
                            await popup_lock.set_locked(True)
                    except Exception:
                        return

                if popup_lock is not None:
                    asyncio.create_task(bind_locked_page())

            context.on("page", on_new_page)
            page = await _prepare_scotia_context_page(context, captured_credentials)
            await log_support_event(
                stage="credential capture",
                debug=True,
                message="Scotiabank visible-auth credential capture configured.",
                details={
                    "browser_init_scripts_enabled": False,
                    "credential_capture_strategy": "bounded_post_load_input_and_structured_response_capture",
                },
            )
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_scotia_login_page(page, entry_url=SCOTIA_ACCOUNTS_URL)
            page = await wait_for_authenticated_session(
                context,
                page,
                captured_credentials=captured_credentials,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="Scotiabank",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = page
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_scotia_material": storage_state_has_scotia_material(storage_state),
            }
            if not storage_state_has_scotia_material(storage_state):
                raise RuntimeError("Scotiabank storage state could not be captured for future syncs.")
            session_artifact = await collect_scotia_session_artifact(
                page,
                active_target=active_target,
                user_agent=user_agent,
            )
            session_artifact_details = {
                "has_accounts_payload": isinstance(session_artifact.get("accounts_payload"), dict),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            captured_pair = collect_scotia_credentials(captured_credentials)
            credential_capture_details = {
                "raw_username_count": int(captured_credentials.get("_raw_username_count") or 0),
                "prefill_username_count": int(captured_credentials.get("_prefill_username_count") or 0),
                "saved_prefill_username_count": int(captured_credentials.get("_saved_prefill_username_count") or 0),
                "masked_username_count": int(captured_credentials.get("_masked_username_count") or 0),
                "raw_password_count": int(captured_credentials.get("_raw_password_count") or 0),
                "prefill_password_count": int(captured_credentials.get("_prefill_password_count") or 0),
                "username_captured": bool(captured_credentials.get("username")),
                "password_captured": bool(captured_credentials.get("password")),
            }
            if not captured_pair:
                raise RuntimeError("Scotiabank credentials could not be confirmed from the secure login form or saved prefill. Start Scotiabank login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="Scotiabank",
                    log_support_event=log_support_event,
                    context_close_timeout_seconds=(
                        SCOTIA_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS if SCOTIA_WINDOWS_FLOW else None
                    ),
                    browser_close_timeout_seconds=(
                        SCOTIA_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS if SCOTIA_WINDOWS_FLOW else None
                    ),
                    force_browser_process_close=SCOTIA_WINDOWS_FLOW,
                    browser_executable_path=runtime_path if SCOTIA_WINDOWS_FLOW else None,
                ):
                    context = None

            auth_message = "Authenticated Scotiabank session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="Scotiabank visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="Scotiabank visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="Scotiabank visible-auth credential capture summary.",
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

            storage_message = "Scotiabank secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured Scotiabank credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"Scotiabank secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
            await visible_auth.close_visible_auth_context_quietly(
                context,
                context_close_timeout_seconds=(
                    SCOTIA_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS if SCOTIA_WINDOWS_FLOW else None
                ),
            )
            if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_scotia_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized Scotiabank HAR capture.",
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
                        message="Could not finalize the sanitized Scotiabank HAR capture.",
                        details={
                            "har_result": run_result,
                            "error": str(exc),
                        },
                    )
            await visible_auth.close_visible_auth_context_quietly(
                None,
                browser,
                browser_close_timeout_seconds=(
                    SCOTIA_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS if SCOTIA_WINDOWS_FLOW else None
                ),
            )


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
