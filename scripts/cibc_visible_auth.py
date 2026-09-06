#!/usr/bin/env python3
"""Desktop visible-auth add-flow runner for CIBC.

This script is launched by the Electron visible-auth broker inside the
managed private/packaged Patchright environment. It opens a visible browser at
the CIBC card page, lets the user complete the real sign-in flow manually,
captures the authenticated session artifacts needed for saved-artifact reuse,
and then hands off to the normal backend sync flow.
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
CIBC_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "cibc"
CIBC_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "CIBC")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "cibc").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
CIBC_VISIBLE_AUTH_PLATFORM = visible_auth.resolve_provider_visible_auth_platform(PROVIDER)
CIBC_VISIBLE_AUTH_FLOW = visible_auth.provider_visible_auth_flow(PROVIDER, CIBC_VISIBLE_AUTH_PLATFORM)
# cibc:linux is the baseline CIBC flow. Platform-specific behavior must stay behind
# the explicit platform flags below so Linux remains untouched when Windows diverges.
CIBC_LINUX_FLOW = CIBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_LINUX
CIBC_WINDOWS_FLOW = CIBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_WINDOWS
CIBC_MAC_FLOW = CIBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_MAC
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

CIBC_ORIGIN = "https://www.cibconline.cibc.com"
CIBC_SECURE_ORIGIN = "https://secure.cibc.com"
CIBC_AUTH_GATEWAY_LOGIN_URL = f"{CIBC_ORIGIN}/ebm-resources/public/auth-gateway/main/client/index.html?locale=en&auth_context=login"
CIBC_PAGE_URL_MARKERS = (
    "cibconline.cibc.com",
    "secure.cibc.com",
)
CIBC_ACCOUNTS_ROUTE_FRAGMENT = "/ebm-resources/online-banking/accounts/client/index.html"
CIBC_APP_ROUTE_FRAGMENT = "/ebm-resources/public/banking/cibc/client/web/index.html"
CIBC_AUTH_REQUEST_PATHS = {
    "/cah/api/v3/oidc/authorize",
    "/cah/api/v3/oidc/step/get",
    "/cah/api/v3/oidc/step/complete",
}
CIBC_HAR_FILENAME_PREFIX = "cibc_visible_auth"
CIBC_MAX_SANITIZED_HARS_PER_USER = 3
CIBC_REDACTED_VALUE = "<redacted>"
CIBC_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "active",
        "category",
        "code",
        "display",
        "error",
        "errorcode",
        "flowname",
        "message",
        "messagecode",
        "mode",
        "outcome",
        "processerror",
        "reason",
        "result",
        "status",
        "statuscode",
        "step",
        "taskname",
        "type",
    }
)
CIBC_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
CIBC_WAIT_POLL_SECONDS = 0.35
CIBC_PAGE_REPLACEMENT_RETRY_SECONDS = 5
CIBC_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
CIBC_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
CIBC_STORAGE_STATE_TIMEOUT_SECONDS = 15
CIBC_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
CIBC_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
CIBC_RUNTIME_BOOTSTRAP_TIMEOUT_SECONDS = 5
CIBC_CARD_INPUT_SELECTOR = 'form[name="card-number-frm-cah"] input[data-test-id="card-number-input"]'
CIBC_PASSWORD_INPUT_SELECTOR = 'form[name="password-frm-cah"] input[data-test-id="password-input"]'
CIBC_CONTACT_METHOD_FORM_SELECTOR = 'form[name="otvc-contact-methods-frm-cah"]'
CIBC_CODE_INPUT_SELECTOR = 'form[name="otvc-verification-frm-cah"] input[data-test-id="verification-code-input"]'
CIBC_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
CIBC_INVALID_CREDENTIAL_SELECTOR = '[data-test-id="global-error"] [data-error-id="0008"]'
CIBC_INVALID_CODE_SELECTOR = '[data-error-id="5065"]'
CIBC_AUTH_INVALID_CREDENTIAL_CODES = {"0008"}
CIBC_AUTH_LOCKED_CREDENTIAL_CODES = {"0013"}
CIBC_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1,C0005:1"
CIBC_COOKIE_BANNER_INIT_SCRIPT = """
(() => {
    const isCibcHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("cibc.com");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isCibcHost()) {
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
    if (window.__breaktwentyCibcConsentNoiseInstalled) {
        clearBanner();
        return;
    }
    window.__breaktwentyCibcConsentNoiseInstalled = true;
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
LOCKED_CLICK_SCRIPT = visible_auth.LOCKED_CLICK_SCRIPT
HOST_BROWSER_CLOSED_MESSAGE = (
    "Login window was closed before secure login finished. "
    "Start sync again and keep the bank browser window open until the app says it is done."
)


def _cibc_consent_cookie_payload() -> list[dict[str, Any]]:
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
            ("groups", CIBC_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".cibc.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".cibc.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "ot-consent-one-time",
            "value": "trigger",
            "domain": ".cibc.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]


async def _cibc_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_cibc_consent_cookie_payload())
    except Exception:
        pass


def _cibc_browser_launch_kwargs(browser_runtime: dict[str, Any] | None) -> dict[str, Any]:
    return visible_auth.managed_browser_launch_kwargs(
        browser_runtime,
        windows_managed_brave_stabilizers=CIBC_WINDOWS_FLOW,
        mac_managed_brave_stabilizers=CIBC_MAC_FLOW,
    )


def _cibc_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return CIBC_DESKTOP_AUTH_RAW_HAR_DIR / f"{CIBC_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _cibc_file_component(value: str | None, *, max_length: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:max_length]


def _cibc_run_suffix(sync_id: str | None, attempt_id: str | None) -> str:
    parts: list[str] = []
    sync_component = _cibc_file_component(sync_id)
    attempt_component = _cibc_file_component(attempt_id)
    if sync_component:
        parts.append(f"sync-{sync_component}")
    if attempt_component:
        parts.append(f"attempt-{attempt_component}")
    return "_".join(parts)


def _cibc_sanitized_har_path(
    user_id: int,
    timestamp_slug: str,
    result: str,
    *,
    sync_id: str | None = None,
    attempt_id: str | None = None,
) -> Path:
    filename_parts = [
        CIBC_HAR_FILENAME_PREFIX,
        f"user{user_id}",
        timestamp_slug,
    ]
    run_suffix = _cibc_run_suffix(sync_id, attempt_id)
    if run_suffix:
        filename_parts.append(run_suffix)
    filename_parts.append(_cibc_file_component(result, max_length=40) or "finished")
    return CIBC_DIAGNOSTIC_DIR / f"{'_'.join(filename_parts)}.har"


def _cibc_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=CIBC_REDACTED_VALUE)


def _cibc_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if "cibc.com" not in host:
        return False
    if host == "secure.cibc.com" and parsed.path in CIBC_AUTH_REQUEST_PATHS:
        return True
    lowered_content_type = content_type.lower()
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_cibc_har_capture(
    raw_har_path: Path,
    *,
    user_id: int,
    timestamp_slug: str,
    result: str,
    sync_id: str | None = None,
    attempt_id: str | None = None,
) -> Path | None:
    sanitized_path = _cibc_sanitized_har_path(
        user_id,
        timestamp_slug,
        result,
        sync_id=sync_id,
        attempt_id=attempt_id,
    )
    pattern = f"{CIBC_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        CIBC_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=CIBC_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_cibc_should_keep_response_body,
        safe_response_keys=CIBC_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=CIBC_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=CIBC_REDACTED_VALUE,
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


def _cibc_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in {CIBC_ORIGIN, CIBC_SECURE_ORIGIN}:
        return origin
    return None


def page_label(page) -> str:
    return visible_auth.page_label(page, url_sanitizer=_cibc_sanitize_url)


class PopupInteractionLock(visible_auth.PopupInteractionLock):
    def __init__(self, page, cdp_session) -> None:
        super().__init__(page, cdp_session, lock_attribute_name="_cibc_popup_lock")


async def _bind_cibc_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)


def _schedule_cibc_popup_lock(page) -> None:
    popup_lock = getattr(page, "_cibc_popup_lock", None)
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


async def get_viable_cibc_page(context, current_page, popup_lock: PopupInteractionLock | None = None):
    deadline = asyncio.get_running_loop().time() + CIBC_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    pages = []
    while True:
        try:
            pages = [page for page in context.pages if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"CIBC browser context is no longer available: {exc}") from exc
        if pages:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(CIBC_PAGE_REPLACEMENT_POLL_SECONDS)

    cibc_pages = [
        page
        for page in pages
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in CIBC_PAGE_URL_MARKERS)
    ]
    if cibc_pages:
        replacement = cibc_pages[-1]
        if current_raw_page is replacement:
            return current_page
        await _bind_cibc_popup_lock_to_page(context, popup_lock, replacement)
        if current_page is not None:
            await log_support_event(
                stage="page replacement",
                debug=True,
                message="CIBC page handle replaced.",
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
    await _bind_cibc_popup_lock_to_page(context, popup_lock, replacement)
    if current_page is not None:
        await log_support_event(
            stage="page replacement",
            debug=True,
            message="CIBC page handle replaced.",
            details={
                "from_page": page_label(current_page),
                "to_page": page_label(replacement),
            },
        )
    return popup_lock.page if popup_lock is not None else replacement


def _cibc_url_is_authenticated(url: str | None) -> bool:
    lowered = str(url or "").lower()
    return (
        lowered.startswith(CIBC_ORIGIN.lower())
        and (
            CIBC_ACCOUNTS_ROUTE_FRAGMENT in lowered
            or CIBC_APP_ROUTE_FRAGMENT in lowered
        )
    )


async def _goto_cibc_entry(page, url: str, *, stage: str, timeout_ms: int = 90000) -> None:
    message = f"CIBC entry step: {stage}."
    await log_support_event(
        stage=stage,
        debug=True,
        message=message,
        details={"entry_url": _cibc_sanitize_url(url)},
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page_message = f"CIBC entry success: {stage}."
    await log_support_event(
        stage=f"{stage}_ok",
        debug=True,
        message=page_message,
        details={"page_url": page_label(page)},
    )


async def open_cibc_login_page(page, *, entry_url: str) -> None:
    await _goto_cibc_entry(page, entry_url, stage="auth_gateway")
    await _cibc_clear_cookie_consent_noise(page)


async def get_cibc_page_state(page) -> dict[str, Any]:
    url = visible_auth.safe_page_url(page)
    authenticated_url = _cibc_url_is_authenticated(url)
    auth_task = "" if authenticated_url else str(getattr(page, "_cibc_auth_task", "") or "").strip().lower()
    auth_error_code = "" if authenticated_url else str(getattr(page, "_cibc_auth_error_code", "") or "").strip()
    access_denied = bool(getattr(page, "_cibc_access_denied", False))
    password_visible = await visible_auth.selector_is_visible(page, CIBC_PASSWORD_INPUT_SELECTOR) or auth_task == "getpassword"
    card_visible = await visible_auth.selector_is_visible(page, CIBC_CARD_INPUT_SELECTOR) or (
        CIBC_WINDOWS_FLOW and auth_task == "geteci"
    )
    contact_method_visible = (
        await visible_auth.selector_is_visible(page, CIBC_CONTACT_METHOD_FORM_SELECTOR)
        or auth_task == "getotvcdeliverymethod"
    )
    code_visible = await visible_auth.selector_is_visible(page, CIBC_CODE_INPUT_SELECTOR) or auth_task == "getotvc"
    invalid_credentials = (
        await visible_auth.selector_is_visible(page, CIBC_INVALID_CREDENTIAL_SELECTOR)
        or auth_error_code in CIBC_AUTH_INVALID_CREDENTIAL_CODES
    )
    locked_credentials = auth_error_code in CIBC_AUTH_LOCKED_CREDENTIAL_CODES
    return {
        "url": url,
        "authenticated": authenticated_url and not access_denied,
        "card_visible": card_visible,
        "password_visible": password_visible,
        "contact_method_visible": contact_method_visible,
        "code_visible": code_visible,
        "invalid_credentials": invalid_credentials,
        "locked_credentials": locked_credentials,
        "invalid_code": await visible_auth.selector_is_visible(page, CIBC_INVALID_CODE_SELECTOR),
        "access_denied": access_denied,
        "auth_task": auth_task,
        "auth_error_code": auth_error_code,
    }


def describe_cibc_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated CIBC session detected. Saving session for background sync."
    if state.get("locked_credentials"):
        return "Your CIBC password is locked due to unsuccessful attempts. Reset it with CIBC and try again."
    if state.get("invalid_credentials"):
        return "CIBC showed a credential error. Popup interaction is unlocked so you can correct the credentials and continue."
    if state.get("invalid_code"):
        return "CIBC showed a verification code error. Popup interaction is unlocked so you can correct the code and continue."
    if state.get("access_denied"):
        return "CIBC rejected this login session. Wait a few minutes and try again."
    if state.get("card_visible"):
        return "Waiting for manual sign-in at the CIBC card page."
    if state.get("password_visible"):
        return "Waiting for CIBC password entry in the secure browser."
    if state.get("contact_method_visible"):
        return "Waiting for CIBC verification method selection in the secure browser."
    if state.get("code_visible"):
        return "Waiting for CIBC verification code entry in the secure browser."
    return "Waiting for CIBC secure login to advance."


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_cibc_material,
        origin_from_url=_cibc_origin_from_url,
        storage_state_timeout_seconds=CIBC_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=CIBC_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=CIBC_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def storage_state_has_cibc_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "cibc.com" in domain:
            return True
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _cibc_origin_from_url(origin.get("origin")):
            return True
    return False


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


def _extract_cibc_bootstrap_from_storage_state(storage_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(storage_state, dict):
        return {}

    artifact: dict[str, Any] = {}
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        name_lower = name.lower()
        if value and name_lower.startswith("ab.storage.deviceid.") and "device_id" not in artifact:
            artifact["device_id"] = value

    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for entry in origin.get("localStorage") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            value = entry.get("value")
            if "auth_token" not in artifact:
                token = _extract_cibc_auth_token_from_value(value)
                if token:
                    artifact["auth_token"] = token
            if "device_id" not in artifact:
                device_id = _extract_cibc_device_id_from_value(value, current_key=name)
                if device_id:
                    artifact["device_id"] = device_id
            if artifact.get("auth_token") and artifact.get("device_id"):
                return artifact
    return artifact


def _capture_cibc_runtime_headers(page, headers: dict[str, Any] | None) -> None:
    if not isinstance(headers, dict):
        return
    auth_token = headers.get("x-auth-token") or headers.get("X-Auth-Token")
    if auth_token:
        setattr(page, "_cibc_auth_token", str(auth_token))
    device_id = headers.get("x-device-id") or headers.get("X-Device-Id")
    if device_id:
        setattr(page, "_cibc_device_id", str(device_id))


def _apply_cibc_runtime_bootstrap(page, bootstrap: dict[str, Any] | None) -> None:
    if not isinstance(bootstrap, dict):
        return
    auth_token = str(bootstrap.get("auth_token") or "").strip()
    if auth_token:
        setattr(page, "_cibc_auth_token", auth_token)
    device_id = str(bootstrap.get("device_id") or "").strip()
    if device_id:
        setattr(page, "_cibc_device_id", device_id)


async def _read_cibc_runtime_bootstrap(page, *, timeout_seconds: float | None = None) -> dict[str, str]:
    wait_seconds = CIBC_RUNTIME_BOOTSTRAP_TIMEOUT_SECONDS if timeout_seconds is None else max(float(timeout_seconds), 0.0)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_result: dict[str, str] = {}
    while True:
        try:
            result = await page.evaluate(
                """
                () => {
                    const readTokenFromValue = (value, depth = 0) => {
                        if (!value || depth > 6) return "";
                        if (typeof value === "string") {
                            if (value.startsWith("ebkpce.")) return value;
                            try {
                                return readTokenFromValue(JSON.parse(value), depth + 1);
                            } catch (_err) {
                                return "";
                            }
                        }
                        if (typeof value === "object") {
                            for (const key of ["authenticationToken", "authToken", "sessionToken", "x-auth-token", "X-Auth-Token", "token"]) {
                                const token = readTokenFromValue(value[key], depth + 1);
                                if (token) return token;
                            }
                            for (const nested of Object.values(value)) {
                                const token = readTokenFromValue(nested, depth + 1);
                                if (token) return token;
                            }
                        }
                        return "";
                    };
                    const readDeviceIdFromValue = (value, currentKey = "", depth = 0) => {
                        if (!value || depth > 6) return "";
                        const keyLower = String(currentKey || "").trim().toLowerCase();
                        if (typeof value === "string") {
                            try {
                                return readDeviceIdFromValue(JSON.parse(value), currentKey, depth + 1);
                            } catch (_err) {
                                return keyLower.includes("deviceid") || keyLower.includes("device-id") ? value.trim() : "";
                            }
                        }
                        if (typeof value === "object") {
                            const preferredKeys = Object.keys(value).filter((key) => {
                                const normalizedKey = String(key || "").trim().toLowerCase();
                                return normalizedKey.includes("deviceid") || normalizedKey.includes("device-id");
                            });
                            for (const key of preferredKeys) {
                                const deviceId = readDeviceIdFromValue(value[key], key, depth + 1);
                                if (deviceId) return deviceId;
                            }
                            for (const [key, nested] of Object.entries(value)) {
                                const deviceId = readDeviceIdFromValue(nested, key, depth + 1);
                                if (deviceId) return deviceId;
                            }
                        }
                        if (Array.isArray(value)) {
                            for (const nested of value) {
                                const deviceId = readDeviceIdFromValue(nested, currentKey, depth + 1);
                                if (deviceId) return deviceId;
                            }
                        }
                        return "";
                    };
                    const readStoreValue = (store) => {
                        for (let i = 0; i < store.length; i += 1) {
                            const key = store.key(i) || "";
                            const value = store.getItem(key) || "";
                            const token = readTokenFromValue(value);
                            const deviceId = readDeviceIdFromValue(value, key);
                            if (token || deviceId) {
                                return {
                                    auth_token: token,
                                    device_id: deviceId,
                                };
                            }
                        }
                        return {
                            auth_token: "",
                            device_id: "",
                        };
                    };
                    const sessionValues = readStoreValue(window.sessionStorage);
                    if (sessionValues.auth_token || sessionValues.device_id) return sessionValues;
                    return readStoreValue(window.localStorage);
                }
                """
            )
        except Exception:
            result = {}
        if not isinstance(result, dict):
            result = {}
        normalized = {
            "auth_token": str(result.get("auth_token") or "").strip(),
            "device_id": str(result.get("device_id") or "").strip(),
        }
        if normalized["auth_token"] or normalized["device_id"]:
            return normalized
        last_result = normalized
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return last_result
        await asyncio.sleep(min(0.25, remaining))


async def collect_cibc_session_artifact(
    page,
    *,
    active_target=None,
    user_agent: str = "",
    storage_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    storage_bootstrap = _extract_cibc_bootstrap_from_storage_state(storage_state)
    if storage_bootstrap:
        for key in ("auth_token", "device_id"):
            value = str(storage_bootstrap.get(key) or "").strip()
            if value and key not in artifact:
                artifact[key] = value
        for target in targets:
            _apply_cibc_runtime_bootstrap(target, storage_bootstrap)
    for target in targets:
        auth_token = str(getattr(target, "_cibc_auth_token", "") or "").strip()
        if auth_token and "auth_token" not in artifact:
            artifact["auth_token"] = auth_token
        device_id = str(getattr(target, "_cibc_device_id", "") or "").strip()
        if device_id and "device_id" not in artifact:
            artifact["device_id"] = device_id
    if not session_artifact_has_cibc_bootstrap(artifact):
        for target in targets:
            bootstrap = await _read_cibc_runtime_bootstrap(target)
            _apply_cibc_runtime_bootstrap(target, bootstrap)
            auth_token = str(getattr(target, "_cibc_auth_token", "") or "").strip()
            if auth_token and "auth_token" not in artifact:
                artifact["auth_token"] = auth_token
            device_id = str(getattr(target, "_cibc_device_id", "") or "").strip()
            if device_id and "device_id" not in artifact:
                artifact["device_id"] = device_id
            if session_artifact_has_cibc_bootstrap(artifact):
                break
    normalized_user_agent = str(user_agent or "").strip()
    if normalized_user_agent:
        artifact["user_agent"] = normalized_user_agent
    return artifact


def session_artifact_has_cibc_bootstrap(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    return bool(str(artifact.get("auth_token") or "").strip()) or bool(str(artifact.get("device_id") or "").strip())


def _parse_cibc_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _extract_cibc_scalar(value: Any, *, preferred_keys: tuple[str, ...] = ()) -> str:
    if isinstance(value, dict):
        for key in preferred_keys:
            nested = _extract_cibc_scalar(value.get(key), preferred_keys=())
            if nested:
                return nested
        return ""
    if isinstance(value, (list, tuple)):
        return ""
    return str(value or "").strip()


def _normalize_cibc_username(value: Any) -> str:
    raw = _extract_cibc_scalar(value, preferred_keys=("value", "cardNumber", "eci"))
    if not raw or "*" in raw:
        return ""
    digits = "".join(character for character in raw if character.isdigit())
    if 13 <= len(digits) <= 19:
        return digits
    return ""


def _normalize_cibc_password(value: Any) -> str:
    raw = _extract_cibc_scalar(value, preferred_keys=("value", "password"))
    if not raw or raw.startswith("{") or raw.startswith("["):
        return ""
    return raw


async def _cibc_input_value(page, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        return str(await locator.input_value(timeout=500) or "")
    except Exception:
        return ""


def _cibc_prefill_phase_key(page, state: dict[str, Any]) -> tuple[int, int, str, str]:
    navigation_token = int(getattr(page, "_cibc_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        str(state.get("auth_task") or ""),
    )


def _cibc_prefill_slot_key(page, state: dict[str, Any], field: str) -> tuple[int, int, str, str, str]:
    navigation_token = int(getattr(page, "_cibc_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        str(state.get("auth_task") or ""),
        field,
    )


async def _prefill_cibc_input(page, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        await page.wait_for_selector(selector, state="visible", timeout=1000)
    except Exception:
        return False
    current_value = await _cibc_input_value(page, selector)
    if current_value:
        return False
    try:
        await page.fill(selector, value)
    except Exception:
        return False
    return bool(await _cibc_input_value(page, selector))


async def _maybe_prefill_cibc_credentials(
    page,
    *,
    state: dict[str, Any],
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    username, password = credentials
    attempted = prefill_state.setdefault("attempted", set())
    lock_started_at = prefill_state.setdefault("lock_started_at", {})
    phase_key = _cibc_prefill_phase_key(page, state)
    card_key = _cibc_prefill_slot_key(page, state, "card")
    password_key = _cibc_prefill_slot_key(page, state, "password")
    needs_card_prefill = (
        state.get("card_visible")
        and bool(username)
        and card_key not in attempted
        and not await _cibc_input_value(page, CIBC_CARD_INPUT_SELECTOR)
    )
    needs_password_prefill = (
        state.get("password_visible")
        and bool(password)
        and password_key not in attempted
        and not await _cibc_input_value(page, CIBC_PASSWORD_INPUT_SELECTOR)
    )
    if not (needs_card_prefill or needs_password_prefill):
        if state.get("card_visible"):
            attempted.add(card_key)
        if state.get("password_visible"):
            attempted.add(password_key)
        if popup_lock is not None and popup_lock.locked:
            started_at = float(lock_started_at.setdefault(phase_key, asyncio.get_running_loop().time()))
            should_release = (
                state.get("authenticated")
                or state.get("contact_method_visible")
                or state.get("code_visible")
                or state.get("invalid_credentials")
                or state.get("locked_credentials")
                or state.get("invalid_code")
                or state.get("access_denied")
                or (asyncio.get_running_loop().time() - started_at) >= CIBC_CREDENTIAL_LOCK_GRACE_SECONDS
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
        if needs_card_prefill:
            prefills_applied = await _prefill_cibc_input(page, CIBC_CARD_INPUT_SELECTOR, username)
            attempted.add(card_key)
        if needs_password_prefill:
            password_prefilled = await _prefill_cibc_input(page, CIBC_PASSWORD_INPUT_SELECTOR, password)
            attempted.add(password_key)
            if password_prefilled:
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved CIBC credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
    finally:
        lock_started_at.pop(phase_key, None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


async def _quick_prefill_cibc_credentials(
    page,
    *,
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    auth_task = str(getattr(page, "_cibc_auth_task", "") or "").strip().lower()
    state = {
        "url": visible_auth.safe_page_url(page),
        "auth_task": auth_task,
        "authenticated": False,
        "card_visible": await visible_auth.selector_is_visible(page, CIBC_CARD_INPUT_SELECTOR),
        "password_visible": await visible_auth.selector_is_visible(page, CIBC_PASSWORD_INPUT_SELECTOR) or auth_task == "getpassword",
        "contact_method_visible": False,
        "code_visible": False,
        "invalid_credentials": False,
        "locked_credentials": False,
        "invalid_code": False,
        "access_denied": False,
    }
    await _maybe_prefill_cibc_credentials(
        page,
        state=state,
        credentials=credentials,
        prefill_state=prefill_state,
        popup_lock=popup_lock,
    )


def _is_cibc_credential_submit_url(url: str) -> bool:
    try:
        return urlsplit(url).path == "/cah/api/v3/oidc/step/complete"
    except ValueError:
        return False


def _capture_cibc_credentials_from_request(
    captured_credentials: dict[str, str],
    *,
    url: str,
    post_data: str | None,
) -> None:
    if not _is_cibc_credential_submit_url(url):
        return
    payload = _parse_cibc_json_body(post_data)
    if not isinstance(payload, dict):
        return

    username = _normalize_cibc_username(payload.get("eci"))
    if username:
        captured_credentials["username"] = username

    password = _normalize_cibc_password(payload.get("password"))
    if password:
        captured_credentials["password"] = password


def _install_credential_capture(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_cibc_credential_capture_installed", False):
        return
    setattr(page, "_cibc_credential_capture_installed", True)

    def on_request(request) -> None:
        url = str(getattr(request, "url", "") or "")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def capture_runtime_headers() -> None:
            headers = await visible_auth.request_headers(request)
            _capture_cibc_runtime_headers(page, headers)

        loop.create_task(capture_runtime_headers())
        if _is_cibc_credential_submit_url(url):
            _capture_cibc_credentials_from_request(
                captured_credentials,
                url=url,
                post_data=visible_auth.safe_request_post_data(request),
            )

    page.on("request", on_request)


def _install_auth_state_capture(page) -> None:
    if getattr(page, "_cibc_auth_state_capture_installed", False):
        return
    setattr(page, "_cibc_auth_state_capture_installed", True)

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        try:
            path = urlsplit(url).path
        except ValueError:
            return
        if path == "/cah/api/v3/oidc/step/get" and not CIBC_WINDOWS_FLOW:
            return
        if path not in {"/cah/api/v3/oidc/step/get", "/cah/api/v3/oidc/step/complete"}:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def capture_auth_state() -> None:
            try:
                status = int(getattr(response, "status", 0) or 0)
                body_text = await response.text()
            except Exception:
                return
            payload = _parse_cibc_json_body(body_text)
            if isinstance(payload, dict):
                params = payload.get("taskParameters")
                if not isinstance(params, dict):
                    params = {}
                auth_task = str(payload.get("taskName") or "").strip()
                auth_error_code = str(params.get("uiError") or "").strip()
                if (
                    path == "/cah/api/v3/oidc/step/get"
                    and not auth_error_code
                    and str(getattr(page, "_cibc_auth_error_code", "") or "").strip()
                ):
                    return
                setattr(page, "_cibc_auth_task", auth_task)
                setattr(page, "_cibc_auth_error_code", auth_error_code)
                setattr(page, "_cibc_access_denied", False)
                if auth_task.lower() == "getpassword":
                    _schedule_cibc_popup_lock(page)
            elif status == 403 and "access denied" in body_text.lower():
                setattr(page, "_cibc_access_denied", True)
                return
            if status == 200:
                setattr(page, "_cibc_access_denied", False)

        loop.create_task(capture_auth_state())

    page.on("response", on_response)


def bind_cibc_page_watcher(
    page,
    captured_credentials: dict[str, str],
) -> None:
    if getattr(page, "_cibc_page_watcher_installed", False):
        return
    setattr(page, "_cibc_page_watcher_installed", True)
    setattr(page, "_cibc_navigation_token", int(getattr(page, "_cibc_navigation_token", 0) or 0))

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                current_token = int(getattr(page, "_cibc_navigation_token", 0) or 0)
                setattr(page, "_cibc_navigation_token", current_token + 1)
                _schedule_cibc_popup_lock(page)
        except Exception:
            return

    page.on("framenavigated", on_frame_navigated)
    _install_credential_capture(page, captured_credentials)
    _install_auth_state_capture(page)


async def _cibc_clear_cookie_consent_noise(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(CIBC_COOKIE_BANNER_INIT_SCRIPT)
    except Exception:
        pass
    try:
        accept_button = await base_page.query_selector(CIBC_COOKIE_ACCEPT_SELECTOR)
    except Exception:
        accept_button = None
    if not accept_button:
        return
    try:
        await accept_button.evaluate(LOCKED_CLICK_SCRIPT)
    except Exception:
        pass


async def wait_for_authenticated_session(
    context,
    page,
    *,
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: PopupInteractionLock | None = None,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    last_description = ""
    prefill_state = {"announced": False, "attempted": set()}
    manual_takeover_announced = set()
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_cibc_page(context, page, popup_lock=popup_lock)
            await _cibc_clear_cookie_consent_noise(page)
            if not VISIBLE_AUTH_ADD_FLOW:
                await _quick_prefill_cibc_credentials(
                    page,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            try:
                state = await get_cibc_page_state(page)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_cibc_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            description = describe_cibc_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="CIBC",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
                return page
            if state.get("locked_credentials"):
                raise RuntimeError("Your CIBC password is locked due to unsuccessful attempts. Reset it with CIBC and try again.")
            if state.get("invalid_credentials") or state.get("invalid_code"):
                takeover_key = "invalid_code" if state.get("invalid_code") else "invalid_credentials"
                if popup_lock is not None and popup_lock.locked:
                    await popup_lock.set_locked(False)
                if takeover_key not in manual_takeover_announced:
                    await log_support_event(stage="manual takeover", message=description, last_output=description)
                    manual_takeover_announced.add(takeover_key)
                await asyncio.sleep(CIBC_WAIT_POLL_SECONDS)
                continue
            if state.get("access_denied"):
                raise RuntimeError("CIBC rejected this login session. Wait a few minutes and try again.")
            await asyncio.sleep(CIBC_WAIT_POLL_SECONDS)

        raise TimeoutError("Timed out waiting for an authenticated CIBC session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def _prepare_cibc_context_page(context, captured_credentials: dict[str, str]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_cibc_page_watcher(page, captured_credentials)
        for extra_page in pages[1:]:
            bind_cibc_page_watcher(extra_page, captured_credentials)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_cibc_page_watcher(page, captured_credentials)
    return page


async def run() -> None:
    if PROVIDER != "cibc":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for CIBC visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        bootstrap_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        bootstrap_session_artifact, bootstrap_session_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="session",
            slots=("quarantine", "active"),
        )
        if not storage_state_has_cibc_material(bootstrap_storage_state):
            bootstrap_storage_state = None
            bootstrap_storage_slot = ""
    har_timestamp_slug = visible_auth.har_timestamp_slug() if VISIBLE_AUTH_RECORD_HAR else ""
    raw_har_path = _cibc_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening CIBC secure login browser.")
    visible_auth.print_status("Opening CIBC secure login browser.")

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
            direct_message = "CIBC is using direct auth gateway entry."
            await log_support_event(stage="direct_entry", debug=True, message=direct_message)
            har_capture_message = (
                "CIBC visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "CIBC visible-auth HAR capture is disabled."
            )
            await log_support_event(
                stage="har capture",
                debug=True,
                message=har_capture_message,
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )
            try:
                launch_kwargs = _cibc_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    CIBC_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _cibc_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="CIBC",
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
                message="CIBC visible-auth browser runtime selected.",
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
                message="CIBC visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "locale_policy": "browser_default",
                    "timezone_id": browser_timezone,
                    "har_capture_enabled": VISIBLE_AUTH_RECORD_HAR,
                    "visible_auth_platform": CIBC_VISIBLE_AUTH_PLATFORM,
                    "visible_auth_flow": CIBC_VISIBLE_AUTH_FLOW,
                    "cibc_linux_flow": CIBC_LINUX_FLOW,
                    "cibc_windows_flow": CIBC_WINDOWS_FLOW,
                    "cibc_mac_flow": CIBC_MAC_FLOW,
                    "windows_managed_brave_launch_args_enabled": bool(
                        CIBC_WINDOWS_FLOW and launch_kwargs.get("args")
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

            popup_lock: PopupInteractionLock | None = None

            def on_new_page(new_page) -> None:
                bind_cibc_page_watcher(new_page, captured_credentials)
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
            page = await _prepare_cibc_context_page(context, captured_credentials)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_cibc_login_page(page, entry_url=CIBC_AUTH_GATEWAY_LOGIN_URL)
            page = await wait_for_authenticated_session(
                context,
                page,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="CIBC",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = page
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_cibc_material": storage_state_has_cibc_material(storage_state),
            }
            if not storage_state_has_cibc_material(storage_state):
                raise RuntimeError("CIBC storage state could not be captured for future syncs.")
            session_artifact = await collect_cibc_session_artifact(
                page,
                active_target=active_target,
                user_agent=user_agent,
                storage_state=storage_state,
            )
            if not session_artifact_has_cibc_bootstrap(session_artifact):
                raise RuntimeError("CIBC session bootstrap data could not be captured for future syncs.")
            session_artifact_details = {
                "has_auth_token": bool(session_artifact.get("auth_token")),
                "has_device_id": bool(session_artifact.get("device_id")),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            username = captured_credentials.get("username") or ""
            password = captured_credentials.get("password") or ""
            if not (username and password):
                raise RuntimeError("CIBC credentials could not be captured from the secure login flow. Start CIBC login again.")

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="CIBC",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated CIBC session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="CIBC visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="CIBC visible-auth session artifact captured.",
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
            storage_message = "CIBC secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured CIBC credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"CIBC secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
            visible_auth.print_status(success_message)
            await log_support_event(stage="sync handoff", message=success_message, last_output=success_message)

            result_payload = visible_auth.build_visible_auth_handoff_result(
                provider=PROVIDER,
                attempt_id=ATTEMPT_ID,
                sync_id=VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID or "",
                storage_result=storage_result,
                session_result=session_result,
                credentials_result=credentials_result,
                credentials_captured=bool(captured_credentials.get("username") and captured_credentials.get("password")),
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
                        finalize_cibc_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                        sync_id=VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
                        attempt_id=ATTEMPT_ID,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized CIBC HAR capture.",
                            details={
                                "phase": "phase1_visible_auth",
                                "har_result": run_result,
                                "path": str(sanitized_har_path),
                            },
                        )
                except Exception as exc:
                    await log_support_event(
                        stage="har capture",
                        level="warning",
                        debug=True,
                        message="Could not finalize the sanitized CIBC HAR capture.",
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
