#!/usr/bin/env python3
"""Desktop visible-auth runner for Tangerine.

This runner mirrors the CIBC desktop visible-auth handoff: Electron launches a
managed browser/Patchright session, the user completes Tangerine login there, the
runner captures reusable artifacts, and the frontend retries the normal backend
sync path with the same sync/attempt IDs.
"""

from __future__ import annotations

import asyncio
import json
import os
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
TANGERINE_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "tangerine"
TANGERINE_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "Tangerine")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "tangerine").strip().lower()
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

TANGERINE_WWW_ORIGIN = "https://www.tangerine.ca"
TANGERINE_SECURE_ORIGIN = "https://secure.tangerine.ca"
TANGERINE_LOGIN_URL = f"{TANGERINE_WWW_ORIGIN}/app/"
TANGERINE_LOGIN_ID_URL = f"{TANGERINE_WWW_ORIGIN}/app/#/login/login-id?locale=en_CA"
TANGERINE_ACCOUNTS_URL = f"{TANGERINE_WWW_ORIGIN}/app/#/accounts"
TANGERINE_ACCOUNTS_API_PATH = "/web/rest/pfm/v1/accounts"
TANGERINE_AUTH_POST_PATH = "/web/Tangerine.html"
TANGERINE_PAGE_URL_MARKERS = (
    "tangerine.ca",
)
TANGERINE_LOGIN_ID_SELECTOR = "#login-user-id-input"
TANGERINE_PASSWORD_SELECTOR = "#passwordId-input"
TANGERINE_OTP_SELECTOR = "#login-otp-input"
TANGERINE_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
TANGERINE_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1"
TANGERINE_BOOTSTRAP_AUTH_COOKIE_NAMES = frozenset(
    {
        "ctok",
        "jsessionid",
        "transaction_token",
    }
)
TANGERINE_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES = frozenset(
    {
        "tangerine.app.SESSION",
    }
)
TANGERINE_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
TANGERINE_WAIT_POLL_SECONDS = 0.35
TANGERINE_PAGE_REPLACEMENT_RETRY_SECONDS = 5
TANGERINE_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
TANGERINE_STORAGE_STATE_TIMEOUT_SECONDS = 15
TANGERINE_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
TANGERINE_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
TANGERINE_HAR_FILENAME_PREFIX = "tangerine_visible_auth"
TANGERINE_MAX_SANITIZED_HARS_PER_USER = 3
TANGERINE_REDACTED_VALUE = "<redacted>"
TANGERINE_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "active",
        "category",
        "code",
        "error",
        "http_code",
        "message",
        "reason",
        "response_status",
        "result",
        "status",
        "status_code",
        "status_text",
        "type",
    }
)
TANGERINE_HAR_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-device-id",
        "x-transaction-token",
        "x-xsrf-token",
    }
)
LOCKED_CLICK_SCRIPT = visible_auth.LOCKED_CLICK_SCRIPT
TANGERINE_COOKIE_BANNER_INIT_SCRIPT = """
(() => {
    const isTangerineHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("tangerine.ca");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isTangerineHost()) {
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
    if (window.__breaktwentyTangerineConsentNoiseInstalled) {
        clearBanner();
        return;
    }
    window.__breaktwentyTangerineConsentNoiseInstalled = true;
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


def _tangerine_consent_cookie_payload() -> list[dict[str, Any]]:
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
            ("groups", TANGERINE_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".tangerine.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".tangerine.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "ot-consent-one-time",
            "value": "trigger",
            "domain": ".tangerine.ca",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]


async def _tangerine_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_tangerine_consent_cookie_payload())
    except Exception:
        pass


def _tangerine_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return TANGERINE_DESKTOP_AUTH_RAW_HAR_DIR / f"{TANGERINE_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _tangerine_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return TANGERINE_DIAGNOSTIC_DIR / f"{TANGERINE_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _tangerine_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=TANGERINE_REDACTED_VALUE)


def _tangerine_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if "tangerine.ca" not in host:
        return False
    if parsed.path == TANGERINE_AUTH_POST_PATH:
        return True
    lowered_content_type = content_type.lower()
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_tangerine_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _tangerine_sanitized_har_path(user_id, timestamp_slug, result)
    pattern = f"{TANGERINE_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        TANGERINE_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=TANGERINE_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_tangerine_should_keep_response_body,
        safe_response_keys=TANGERINE_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=TANGERINE_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=TANGERINE_REDACTED_VALUE,
    )


def _tangerine_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in {TANGERINE_WWW_ORIGIN, TANGERINE_SECURE_ORIGIN}:
        return origin
    return None


def page_label(page) -> str:
    return visible_auth.page_label(page, url_sanitizer=_tangerine_sanitize_url)


class PopupInteractionLock(visible_auth.PopupInteractionLock):
    def __init__(self, page, cdp_session) -> None:
        super().__init__(page, cdp_session, lock_attribute_name="_tangerine_popup_lock")


async def _bind_tangerine_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)


def _schedule_tangerine_popup_lock(page) -> None:
    popup_lock = getattr(page, "_tangerine_popup_lock", None)
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


async def get_viable_tangerine_page(context, current_page, popup_lock: PopupInteractionLock | None = None):
    deadline = asyncio.get_running_loop().time() + TANGERINE_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    pages = []
    while True:
        try:
            pages = [page for page in context.pages if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"Tangerine browser context is no longer available: {exc}") from exc
        if pages:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(TANGERINE_PAGE_REPLACEMENT_POLL_SECONDS)

    tangerine_pages = [
        page
        for page in pages
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in TANGERINE_PAGE_URL_MARKERS)
    ]
    if tangerine_pages:
        replacement = tangerine_pages[-1]
        if current_raw_page is replacement:
            return current_page
        await _bind_tangerine_popup_lock_to_page(context, popup_lock, replacement)
        if current_page is not None:
            await log_support_event(
                stage="page replacement",
                debug=True,
                message="Tangerine page handle replaced.",
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
    await _bind_tangerine_popup_lock_to_page(context, popup_lock, replacement)
    if current_page is not None:
        await log_support_event(
            stage="page replacement",
            debug=True,
            message="Tangerine page handle replaced.",
            details={
                "from_page": page_label(current_page),
                "to_page": page_label(replacement),
            },
        )
    return popup_lock.page if popup_lock is not None else replacement


async def _goto_tangerine_entry(page, url: str, *, stage: str, timeout_ms: int = 90000) -> None:
    await log_support_event(
        stage=stage,
        debug=True,
        message=f"Tangerine entry step: {stage}.",
        details={"entry_url": _tangerine_sanitize_url(url)},
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await log_support_event(
        stage=f"{stage}_ok",
        debug=True,
        message=f"Tangerine entry success: {stage}.",
        details={"page_url": page_label(page)},
    )


async def open_tangerine_login_page(page, *, entry_url: str) -> None:
    await _goto_tangerine_entry(page, entry_url, stage="login_entry")
    await _dismiss_tangerine_cookie_banner(page)


async def _dismiss_tangerine_cookie_banner(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(TANGERINE_COOKIE_BANNER_INIT_SCRIPT)
    except Exception:
        pass
    try:
        accept_button = await base_page.query_selector(TANGERINE_COOKIE_ACCEPT_SELECTOR)
    except Exception:
        accept_button = None
    if not accept_button:
        return
    try:
        await accept_button.evaluate(LOCKED_CLICK_SCRIPT)
    except Exception:
        pass


def _tangerine_accounts_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    accounts = payload.get("accounts")
    return [dict(account) for account in accounts if isinstance(account, dict)] if isinstance(accounts, list) else []


def _parse_tangerine_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if normalized.startswith(")]}',"):
        normalized = normalized.split("\n", 1)[1] if "\n" in normalized else ""
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


async def _tangerine_page_summary(page) -> dict[str, Any]:
    dom_state: dict[str, Any] = {}
    try:
        dom_values = await page.evaluate(
            """
            () => {
                const bodyState = document.body ? (document.body.getAttribute('state') || '') : '';
                return {
                    body_state: bodyState.slice(0, 120),
                    login_user_id_component_visible: Boolean(document.querySelector('tngx-login-user-id')),
                    login_password_component_visible: Boolean(document.querySelector('tngx-login-password')),
                    push_visible: bodyState === 'loginAbstract.login2Sv' || Boolean(document.querySelector('tngx-login2-sv-component')),
                    accounts_dom_ready: bodyState === 'accountSummary.main' || Boolean(document.querySelector('tngx-account-summary')),
                };
            }
            """
        )
        if isinstance(dom_values, dict):
            dom_state = dom_values
    except Exception:
        dom_state = {}
    return {
        "url": visible_auth.safe_page_url(page),
        "login_id_visible": await visible_auth.selector_is_visible(page, TANGERINE_LOGIN_ID_SELECTOR),
        "password_visible": await visible_auth.selector_is_visible(page, TANGERINE_PASSWORD_SELECTOR),
        "otp_visible": await visible_auth.selector_is_visible(page, TANGERINE_OTP_SELECTOR),
        "body_state": str(dom_state.get("body_state") or ""),
        "login_user_id_component_visible": bool(dom_state.get("login_user_id_component_visible")),
        "login_password_component_visible": bool(dom_state.get("login_password_component_visible")),
        "push_visible": bool(dom_state.get("push_visible")),
        "accounts_dom_ready": bool(dom_state.get("accounts_dom_ready")),
    }


async def _tangerine_error_visible(page) -> bool:
    for selector in (
        "#login-pin-service-errors",
        "#message-body-login-pin-message-card-error",
        'tngx-message-card[aria-live="polite"]',
        'tngx-message-card[variant="critical"]',
        'tngx-message-card[type="critical"]',
        'tngx-message-card[status="critical"]',
        ".message-box.critical",
        ".service-error",
    ):
        if await visible_auth.selector_is_visible(page, selector):
            return True
    return False


async def get_tangerine_page_state(page) -> dict[str, Any]:
    summary = await _tangerine_page_summary(page)
    accounts_payload = getattr(page, "_tangerine_accounts_payload", None)
    auth_status = int(getattr(page, "_tangerine_auth_post_status", 0) or 0)
    return {
        **summary,
        "authenticated": (
            bool(_tangerine_accounts_from_payload(accounts_payload))
            or bool(summary.get("accounts_dom_ready"))
        ),
        "structured_error_visible": await _tangerine_error_visible(page),
        "auth_server_error": auth_status >= 500,
        "auth_rejected": auth_status in (401, 403, 429),
    }


def describe_tangerine_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated Tangerine session detected. Saving session for background sync."
    if state.get("auth_server_error"):
        return "Tangerine returned a server error during secure login. Try again later."
    if state.get("auth_rejected") or state.get("structured_error_visible"):
        return "Tangerine showed a login error. Popup interaction is unlocked so you can correct the credentials and continue."
    if state.get("login_id_visible"):
        return "Waiting for manual sign-in at the Tangerine Login ID page."
    if state.get("password_visible"):
        return "Waiting for Tangerine password entry in the secure browser."
    if state.get("otp_visible"):
        return "Waiting for Tangerine security code entry in the secure browser."
    if state.get("push_visible"):
        return "Waiting for Tangerine push approval in the secure browser."
    return "Waiting for Tangerine secure login to advance."


def _normalize_credential_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw.startswith("{") or raw.startswith("["):
        return ""
    return raw


def _capture_tangerine_credentials_from_request(
    captured_credentials: dict[str, str],
    *,
    url: str,
    post_data: str | None,
) -> None:
    try:
        if urlsplit(url).path != TANGERINE_AUTH_POST_PATH:
            return
    except ValueError:
        return
    pairs = parse_qsl(str(post_data or ""), keep_blank_values=True)
    if not pairs:
        payload = _parse_tangerine_json_body(post_data)
        if isinstance(payload, dict):
            pairs = [(str(key), str(value)) for key, value in payload.items()]
    for key, value in pairs:
        key_lower = key.lower().replace("-", "").replace("_", "")
        normalized_value = _normalize_credential_value(value)
        if not normalized_value:
            continue
        if (
            "loginuserid" in key_lower
            or "loginid" in key_lower
            or "userid" in key_lower
            or "username" in key_lower
            or key_lower in {"acn", "cif", "clientnumber"}
        ):
            captured_credentials["username"] = normalized_value
        elif "password" in key_lower or key_lower.endswith("pin") or "loginpin" in key_lower:
            captured_credentials["password"] = normalized_value


def _capture_tangerine_transaction_token(page, headers: dict[str, Any] | None) -> None:
    if not isinstance(headers, dict):
        return
    transaction_token = headers.get("x-transaction-token") or headers.get("X-Transaction-Token")
    if transaction_token:
        setattr(page, "_tangerine_transaction_token", str(transaction_token))


def _install_tangerine_request_capture(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_tangerine_request_capture_installed", False):
        return
    setattr(page, "_tangerine_request_capture_installed", True)

    def on_request(request) -> None:
        url = str(getattr(request, "url", "") or "")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def capture_runtime_headers() -> None:
            headers = await visible_auth.request_headers(request)
            _capture_tangerine_transaction_token(page, headers)

        loop.create_task(capture_runtime_headers())
        _capture_tangerine_credentials_from_request(
            captured_credentials,
            url=url,
            post_data=visible_auth.safe_request_post_data(request),
        )

    page.on("request", on_request)


def _install_tangerine_response_capture(page) -> None:
    if getattr(page, "_tangerine_response_capture_installed", False):
        return
    setattr(page, "_tangerine_response_capture_installed", True)

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        try:
            parsed_path = urlsplit(url).path
        except ValueError:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def capture_response() -> None:
            try:
                headers = getattr(response, "headers", {}) or {}
                _capture_tangerine_transaction_token(page, headers)
                status = int(getattr(response, "status", 0) or 0)
                if parsed_path == TANGERINE_AUTH_POST_PATH:
                    setattr(page, "_tangerine_auth_post_status", status)
                if parsed_path != TANGERINE_ACCOUNTS_API_PATH or status != 200:
                    return
                payload = _parse_tangerine_json_body(await response.text())
                if _tangerine_accounts_from_payload(payload):
                    setattr(page, "_tangerine_accounts_payload", payload)
            except Exception:
                return

        loop.create_task(capture_response())

    page.on("response", on_response)


def bind_tangerine_page_watcher(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_tangerine_page_watcher_installed", False):
        return
    setattr(page, "_tangerine_page_watcher_installed", True)
    setattr(page, "_tangerine_navigation_token", int(getattr(page, "_tangerine_navigation_token", 0) or 0))

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                current_token = int(getattr(page, "_tangerine_navigation_token", 0) or 0)
                setattr(page, "_tangerine_navigation_token", current_token + 1)
                _schedule_tangerine_popup_lock(page)
        except Exception:
            return

    page.on("framenavigated", on_frame_navigated)
    _install_tangerine_request_capture(page, captured_credentials)
    _install_tangerine_response_capture(page)


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_tangerine_material,
        origin_from_url=_tangerine_origin_from_url,
        storage_state_timeout_seconds=TANGERINE_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=TANGERINE_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=TANGERINE_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def storage_state_has_tangerine_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "tangerine.ca" in domain:
            return True
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _tangerine_origin_from_url(origin.get("origin")):
            return True
    return False


def sanitize_tangerine_bootstrap_storage_state(
    storage_state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
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
        if "tangerine.ca" in domain and name in TANGERINE_BOOTSTRAP_AUTH_COOKIE_NAMES:
            details["removed_auth_cookies"] += 1
            continue
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    origins = sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []
    kept_origins: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if not _tangerine_origin_from_url(origin.get("origin")):
            kept_origins.append(origin)
            continue
        local_storage = origin.get("localStorage") if isinstance(origin.get("localStorage"), list) else []
        kept_local_storage: list[dict[str, Any]] = []
        for item in local_storage:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name in TANGERINE_BOOTSTRAP_AUTH_LOCAL_STORAGE_NAMES:
                details["removed_auth_local_storage"] += 1
                continue
            kept_local_storage.append(item)
        origin["localStorage"] = kept_local_storage
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not storage_state_has_tangerine_material(sanitized):
        return None, details
    return sanitized, details


async def collect_tangerine_session_artifact(page, *, active_target=None, user_agent: str = "") -> dict[str, Any]:
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
        transaction_token = str(getattr(target, "_tangerine_transaction_token", "") or "").strip()
        if transaction_token and "transaction_token" not in artifact:
            artifact["transaction_token"] = transaction_token
        accounts_payload = getattr(target, "_tangerine_accounts_payload", None)
        if isinstance(accounts_payload, dict) and "accounts_payload" not in artifact:
            artifact["accounts_payload"] = accounts_payload
    normalized_user_agent = str(user_agent or "").strip()
    if normalized_user_agent:
        artifact["user_agent"] = normalized_user_agent
    return artifact


async def _tangerine_input_value(page, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        return str(await locator.input_value(timeout=500) or "")
    except Exception:
        return ""


def _tangerine_prefill_phase_key(page, state: dict[str, Any]) -> tuple[int, int, str, str]:
    navigation_token = int(getattr(page, "_tangerine_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        str(state.get("body_state") or ""),
    )


def _tangerine_prefill_slot_key(page, state: dict[str, Any], field: str) -> tuple[int, int, str, str, str]:
    navigation_token = int(getattr(page, "_tangerine_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        str(state.get("body_state") or ""),
        field,
    )


async def _prefill_tangerine_input(page, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        await page.wait_for_selector(selector, state="visible", timeout=1000)
    except Exception:
        return False
    current_value = await _tangerine_input_value(page, selector)
    if current_value:
        return False
    try:
        await page.fill(selector, value)
    except Exception:
        return False
    return bool(await _tangerine_input_value(page, selector))


async def _maybe_prefill_tangerine_credentials(
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
    phase_key = _tangerine_prefill_phase_key(page, state)
    login_id_key = _tangerine_prefill_slot_key(page, state, "login_id")
    password_key = _tangerine_prefill_slot_key(page, state, "password")
    needs_login_id_prefill = (
        state.get("login_id_visible")
        and bool(username)
        and login_id_key not in attempted
        and not await _tangerine_input_value(page, TANGERINE_LOGIN_ID_SELECTOR)
    )
    needs_password_prefill = (
        state.get("password_visible")
        and bool(password)
        and password_key not in attempted
        and not await _tangerine_input_value(page, TANGERINE_PASSWORD_SELECTOR)
    )
    if not (needs_login_id_prefill or needs_password_prefill):
        if state.get("login_id_visible"):
            attempted.add(login_id_key)
        if state.get("password_visible"):
            attempted.add(password_key)
        if popup_lock is not None and popup_lock.locked:
            started_at = float(lock_started_at.setdefault(phase_key, asyncio.get_running_loop().time()))
            should_release = (
                state.get("authenticated")
                or state.get("otp_visible")
                or state.get("push_visible")
                or state.get("structured_error_visible")
                or state.get("auth_rejected")
                or state.get("auth_server_error")
                or (asyncio.get_running_loop().time() - started_at) >= TANGERINE_CREDENTIAL_LOCK_GRACE_SECONDS
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
        if needs_login_id_prefill:
            prefills_applied = await _prefill_tangerine_input(page, TANGERINE_LOGIN_ID_SELECTOR, username)
            attempted.add(login_id_key)
        if needs_password_prefill:
            password_prefilled = await _prefill_tangerine_input(page, TANGERINE_PASSWORD_SELECTOR, password)
            attempted.add(password_key)
            if password_prefilled:
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved Tangerine credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
    finally:
        lock_started_at.pop(phase_key, None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


async def _quick_prefill_tangerine_credentials(
    page,
    *,
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    try:
        body_state = str(await page.evaluate("() => document.body ? (document.body.getAttribute('state') || '') : ''") or "")
    except Exception:
        body_state = ""
    state = {
        "url": visible_auth.safe_page_url(page),
        "body_state": body_state,
        "authenticated": False,
        "login_id_visible": await visible_auth.selector_is_visible(page, TANGERINE_LOGIN_ID_SELECTOR),
        "password_visible": await visible_auth.selector_is_visible(page, TANGERINE_PASSWORD_SELECTOR),
        "otp_visible": False,
        "push_visible": False,
        "structured_error_visible": False,
        "auth_rejected": False,
        "auth_server_error": False,
    }
    await _maybe_prefill_tangerine_credentials(
        page,
        state=state,
        credentials=credentials,
        prefill_state=prefill_state,
        popup_lock=popup_lock,
    )


def collect_tangerine_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "").strip()
    if username and password:
        return username, password
    return None


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
    manual_takeover_announced = False
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_tangerine_page(context, page, popup_lock=popup_lock)
            await _dismiss_tangerine_cookie_banner(page)
            if not VISIBLE_AUTH_ADD_FLOW:
                await _quick_prefill_tangerine_credentials(
                    page,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            try:
                state = await get_tangerine_page_state(page)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_tangerine_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            description = describe_tangerine_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="Tangerine",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
                return page
            if state.get("auth_server_error"):
                raise RuntimeError("Tangerine returned a server error during secure login. Try again later.")
            if state.get("auth_rejected") or state.get("structured_error_visible"):
                if popup_lock is not None and popup_lock.locked:
                    await popup_lock.set_locked(False)
                if not manual_takeover_announced:
                    message = description
                    await log_support_event(stage="manual takeover", message=message, last_output=message)
                    manual_takeover_announced = True
                await asyncio.sleep(TANGERINE_WAIT_POLL_SECONDS)
                continue
            await asyncio.sleep(TANGERINE_WAIT_POLL_SECONDS)

        raise TimeoutError("Timed out waiting for an authenticated Tangerine session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def _prepare_tangerine_context_page(context, captured_credentials: dict[str, str]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_tangerine_page_watcher(page, captured_credentials)
        for extra_page in pages[1:]:
            bind_tangerine_page_watcher(extra_page, captured_credentials)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_tangerine_page_watcher(page, captured_credentials)
    return page


async def _nudge_tangerine_accounts_capture(page) -> None:
    if _tangerine_accounts_from_payload(getattr(page, "_tangerine_accounts_payload", None)):
        return
    try:
        await page.goto(TANGERINE_ACCOUNTS_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return
    deadline = asyncio.get_running_loop().time() + 5
    while visible_auth.visible_auth_deadline_active(deadline):
        if _tangerine_accounts_from_payload(getattr(page, "_tangerine_accounts_payload", None)):
            return
        await asyncio.sleep(0.25)


async def run() -> None:
    if PROVIDER != "tangerine":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for Tangerine visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {
        "removed_auth_cookies": 0,
        "removed_auth_local_storage": 0,
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
        if storage_state_has_tangerine_material(loaded_storage_state):
            bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_tangerine_bootstrap_storage_state(
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
    raw_har_path = _tangerine_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening Tangerine secure login browser.")
    visible_auth.print_status("Opening Tangerine secure login browser.")

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
            await log_support_event(stage="direct_entry", debug=True, message="Tangerine is using direct login entry.")
            har_capture_message = (
                "Tangerine visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "Tangerine visible-auth HAR capture is disabled."
            )
            await log_support_event(
                stage="har capture",
                debug=True,
                message=har_capture_message,
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )
            try:
                launch_kwargs = visible_auth.managed_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    TANGERINE_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
                await _tangerine_preseed_cookie_consent(context)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="Tangerine",
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
                message="Tangerine visible-auth browser runtime selected.",
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
                message="Tangerine visible-auth browser context.",
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
                bind_tangerine_page_watcher(new_page, captured_credentials)
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
            page = await _prepare_tangerine_context_page(context, captured_credentials)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            entry_url = TANGERINE_LOGIN_URL if VISIBLE_AUTH_ADD_FLOW else TANGERINE_LOGIN_ID_URL
            await open_tangerine_login_page(page, entry_url=entry_url)
            page = await wait_for_authenticated_session(
                context,
                page,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="Tangerine",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = page
            await _nudge_tangerine_accounts_capture(active_target)
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_tangerine_material": storage_state_has_tangerine_material(storage_state),
            }
            if not storage_state_has_tangerine_material(storage_state):
                raise RuntimeError("Tangerine storage state could not be captured for future syncs.")
            session_artifact = await collect_tangerine_session_artifact(
                page,
                active_target=active_target,
                user_agent=user_agent,
            )
            session_artifact_details = {
                "has_transaction_token": bool(session_artifact.get("transaction_token")),
                "has_accounts_payload": isinstance(session_artifact.get("accounts_payload"), dict),
                "has_user_agent": bool(session_artifact.get("user_agent")),
            }
            captured_pair = collect_tangerine_credentials(captured_credentials)
            if not captured_pair:
                raise RuntimeError("Tangerine credentials could not be captured from the secure login flow. Start Tangerine login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="Tangerine",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated Tangerine session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="Tangerine visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="Tangerine visible-auth session artifact captured.",
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
            storage_message = "Tangerine secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured Tangerine credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"Tangerine secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
                        finalize_tangerine_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized Tangerine HAR capture.",
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
                        message="Could not finalize the sanitized Tangerine HAR capture.",
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
