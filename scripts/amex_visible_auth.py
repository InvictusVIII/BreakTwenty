#!/usr/bin/env python3
"""Desktop visible-auth runner for American Express."""

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
AMEX_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "amex"
AMEX_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "AMEX")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "amex").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
AMEX_VISIBLE_AUTH_PLATFORM = visible_auth.resolve_provider_visible_auth_platform(PROVIDER)
AMEX_VISIBLE_AUTH_FLOW = visible_auth.provider_visible_auth_flow(PROVIDER, AMEX_VISIBLE_AUTH_PLATFORM)
# amex:linux is the baseline AMEX flow. Platform-specific behavior must stay behind
# the explicit platform flags below so Linux remains untouched when Windows diverges.
AMEX_LINUX_FLOW = AMEX_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_LINUX
AMEX_WINDOWS_FLOW = AMEX_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_WINDOWS
AMEX_MAC_FLOW = AMEX_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_MAC
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

AMEX_LOGIN_URL = "https://www.americanexpress.com/en-ca/account/login"
AMEX_ORIGIN_MARKERS = (
    "americanexpress.com",
)
AMEX_LOGIN_POST_PATH = "/myca/logon/canlac/action/login"
AMEX_DASHBOARD_SIGNAL_PATHS = frozenset(
    {
        "/readarbitrationforaccounts.v1",
        "/readlinksforaccount.v1",
        "/readreferralofferforaccount.v1",
        "/readcardaccountofferslist.v1",
    }
)
AMEX_USERNAME_SELECTOR = "#eliloUserID"
AMEX_PASSWORD_SELECTOR = "#eliloPassword"
AMEX_OTP_CHANNEL_SELECTOR = 'input[name="otp-channel"], input[id^="channel_"]'
AMEX_OTP_CODE_SELECTOR = "#question-input"
AMEX_TRUST_SELECTOR = "#trustDevice, #device-name-input"
AMEX_AUTH_MODULE_SELECTOR = (
    '[data-module-name="one-identity-two-step-verification"], '
    '[data-module-name="identity-ui-two-step-verification"], '
    '[data-module-name="one-identity-authentication"], '
    '[data-module-name="one-identity-authentication-legacy"]'
)
AMEX_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
AMEX_SAFE_BOOTSTRAP_COOKIE_NAMES = frozenset(
    {
        "optanonalertboxclosed",
        "optanonconsent",
        "notice_behavior",
        "notice_gdpr_prefs",
        "notice_preferences",
    }
)
AMEX_CONSENT_GROUPS = "C0001:1,C0002:1,C0003:1,C0004:1,C0005:1"
AMEX_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
AMEX_WAIT_POLL_SECONDS = 0.35
AMEX_PAGE_REPLACEMENT_RETRY_SECONDS = 5
AMEX_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
AMEX_STORAGE_STATE_TIMEOUT_SECONDS = 15
AMEX_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
AMEX_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
AMEX_HAR_FILENAME_PREFIX = "amex_visible_auth"
AMEX_MAX_SANITIZED_HARS_PER_USER = 3
AMEX_REDACTED_VALUE = "<redacted>"
AMEX_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "challenge",
        "code",
        "error",
        "errorcode",
        "message",
        "reason",
        "reauth",
        "result",
        "status",
        "statuscode",
        "type",
    }
)
AMEX_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
AMEX_COOKIE_BANNER_SCRIPT = """
(() => {
    const isAmexHost = () => {
        const host = String(window.location?.hostname || "").trim().toLowerCase();
        return host.endsWith("americanexpress.com");
    };
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
    const clearBanner = () => {
        if (!isAmexHost()) {
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


def _amex_consent_cookie_payload() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    expiry = int(now.timestamp()) + (365 * 24 * 60 * 60)
    datestamp = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    consent_value = urlencode(
        [
            ("isGpcEnabled", "0"),
            ("datestamp", datestamp),
            ("version", "202401.1.0"),
            ("browserGpcFlag", "1"),
            ("isIABGlobal", "false"),
            ("hosts", ""),
            ("consentId", str(uuid.uuid4())),
            ("interactionCount", "1"),
            ("isAnonUser", "1"),
            ("prevHadToken", "0"),
            ("landingPath", "NotLandingPage"),
            ("groups", AMEX_CONSENT_GROUPS),
            ("intType", "1"),
            ("crTime", str(now_ms)),
        ],
        doseq=True,
    )
    return [
        {
            "name": "OptanonConsent",
            "value": consent_value,
            "domain": ".americanexpress.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
        {
            "name": "OptanonAlertBoxClosed",
            "value": datestamp,
            "domain": ".americanexpress.com",
            "path": "/",
            "expires": expiry,
            "httpOnly": False,
            "secure": False,
            "sameSite": "Lax",
        },
    ]


async def _amex_preseed_cookie_consent(context) -> None:
    try:
        await context.add_cookies(_amex_consent_cookie_payload())
    except Exception:
        pass


def _amex_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return AMEX_DESKTOP_AUTH_RAW_HAR_DIR / f"{AMEX_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _amex_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return AMEX_DIAGNOSTIC_DIR / f"{AMEX_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _amex_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=AMEX_REDACTED_VALUE)


def _amex_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if "americanexpress.com" not in host:
        return False
    if parsed.path == AMEX_LOGIN_POST_PATH:
        return True
    lowered_content_type = content_type.lower()
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_amex_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _amex_sanitized_har_path(user_id, timestamp_slug, result)
    pattern = f"{AMEX_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        AMEX_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=AMEX_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_amex_should_keep_response_body,
        safe_response_keys=AMEX_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=AMEX_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=AMEX_REDACTED_VALUE,
    )


def _amex_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.endswith("americanexpress.com"):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def page_label(page) -> str:
    return visible_auth.page_label(page, url_sanitizer=_amex_sanitize_url)


class PopupInteractionLock(visible_auth.PopupInteractionLock):
    def __init__(self, page, cdp_session) -> None:
        super().__init__(page, cdp_session, lock_attribute_name="_amex_popup_lock")


async def _bind_amex_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)


def _schedule_amex_popup_lock(page) -> None:
    popup_lock = getattr(page, "_amex_popup_lock", None)
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


async def get_viable_amex_page(context, current_page, popup_lock: PopupInteractionLock | None = None):
    deadline = asyncio.get_running_loop().time() + AMEX_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    pages = []
    while True:
        try:
            pages = [page for page in context.pages if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"American Express browser context is no longer available: {exc}") from exc
        if pages:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(AMEX_PAGE_REPLACEMENT_POLL_SECONDS)

    amex_pages = [
        page
        for page in pages
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in AMEX_ORIGIN_MARKERS)
    ]
    if amex_pages:
        replacement = amex_pages[-1]
        if current_raw_page is replacement:
            return current_page
        await _bind_amex_popup_lock_to_page(context, popup_lock, replacement)
        if current_page is not None:
            await log_support_event(
                stage="page replacement",
                debug=True,
                message="American Express page handle replaced.",
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
    await _bind_amex_popup_lock_to_page(context, popup_lock, replacement)
    if current_page is not None:
        await log_support_event(
            stage="page replacement",
            debug=True,
            message="American Express page handle replaced.",
            details={
                "from_page": page_label(current_page),
                "to_page": page_label(replacement),
            },
        )
    return popup_lock.page if popup_lock is not None else replacement


async def _goto_amex_entry(page, url: str, *, stage: str, timeout_ms: int = 90000) -> None:
    await log_support_event(
        stage=stage,
        debug=True,
        message=f"American Express entry step: {stage}.",
        details={"entry_url": _amex_sanitize_url(url)},
    )
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    await log_support_event(
        stage=f"{stage}_ok",
        debug=True,
        message=f"American Express entry success: {stage}.",
        details={"page_url": page_label(page)},
    )


async def open_amex_login_page(page, *, entry_url: str) -> None:
    await _goto_amex_entry(page, entry_url, stage="login_entry")
    await _dismiss_amex_cookie_banner(page)


async def selector_is_visible(page, selector: str) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                (selector) => Array.from(document.querySelectorAll(selector)).some((el) => {
                    if (!(el instanceof HTMLElement) || el.disabled) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                })
                """,
                selector,
            )
        )
    except Exception:
        return False


async def _dismiss_amex_cookie_banner(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(AMEX_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass
    try:
        accept_button = await base_page.query_selector(AMEX_COOKIE_ACCEPT_SELECTOR)
    except Exception:
        accept_button = None
    if not accept_button:
        return
    try:
        await accept_button.evaluate(visible_auth.LOCKED_CLICK_SCRIPT)
    except Exception:
        pass


def _parse_amex_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _amex_login_response_requires_2fa(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    reauth = payload.get("reauth")
    if not isinstance(reauth, dict):
        return bool(payload.get("challenge"))
    return bool(
        reauth.get("mfaId")
        or reauth.get("assessmentToken")
        or payload.get("challenge")
    )


def _amex_login_response_rejected(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if _amex_login_response_requires_2fa(payload):
        return False
    status_code = str(payload.get("statusCode") or "").strip()
    error_code = str(payload.get("errorCode") or "").strip()
    if status_code in {"", "0"} and not error_code:
        return False
    return bool(error_code or status_code not in {"", "0"})


def _amex_login_response_authenticated(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if _amex_login_response_requires_2fa(payload) or _amex_login_response_rejected(payload):
        return False
    status_code = str(payload.get("statusCode") or "").strip()
    error_code = str(payload.get("errorCode") or "").strip()
    return status_code == "0" and not error_code


async def _amex_dashboard_shell_machine_state_ready(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => Boolean(
                    window.__INITIAL_STATE__
                    || document.querySelector('#initial-state')?.textContent
                    || document.querySelector('[data-module-name="axp-myca-root"]')
                    || document.querySelector('[data-module-name="axp-consumer-navigation"]')
                    || document.querySelector('#gnav_logout')
                )
                """
            )
        )
    except Exception:
        return False


async def _amex_dashboard_account_state_ready(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                    const serializedInitialState = (content) => {
                        if (!content) return null;
                        const match = String(content).match(/window\\.__INITIAL_STATE__\\s*=\\s*("(?:\\\\.|[^"\\\\])*")/s);
                        if (!match) return null;
                        try {
                            return JSON.parse(match[1]);
                        } catch {
                            return null;
                        }
                    };
                    const rawState = (() => {
                        const fromWindow = window.__INITIAL_STATE__;
                        if (typeof fromWindow === 'string') return fromWindow;
                        if (Array.isArray(fromWindow) || (fromWindow && typeof fromWindow === 'object')) {
                            return fromWindow;
                        }
                        const script = document.querySelector('#initial-state');
                        return serializedInitialState(script ? script.textContent || '' : '');
                    })();
                    if (!rawState) return false;

                    const decodeTransit = (value) => {
                        if (Array.isArray(value)) {
                            if (value[0] === '^ ') {
                                const mapped = {};
                                for (let index = 1; index < value.length; index += 2) {
                                    mapped[String(decodeTransit(value[index]))] = decodeTransit(value[index + 1]);
                                }
                                return mapped;
                            }
                            if (
                                value.length === 2
                                && (value[0] === '~#iM' || value[0] === '~#cmap')
                                && Array.isArray(value[1])
                            ) {
                                const mapped = {};
                                const pairs = value[1];
                                for (let index = 0; index < pairs.length; index += 2) {
                                    mapped[String(decodeTransit(pairs[index]))] = decodeTransit(pairs[index + 1]);
                                }
                                return mapped;
                            }
                            if (value.length === 2 && value[0] === '~#iL' && Array.isArray(value[1])) {
                                return value[1].map(decodeTransit);
                            }
                            if (value.length === 2 && typeof value[0] === 'string' && value[0].startsWith('~#')) {
                                return decodeTransit(value[1]);
                            }
                            return value.map(decodeTransit);
                        }
                        if (value && typeof value === 'object') {
                            const mapped = {};
                            for (const [key, nested] of Object.entries(value)) {
                                mapped[key] = decodeTransit(nested);
                            }
                            return mapped;
                        }
                        if (value === '~#nil') return null;
                        if (value === '~#true') return true;
                        if (value === '~#false') return false;
                        if (typeof value === 'string' && value.startsWith('~:')) return value.slice(2);
                        return value;
                    };

                    let state;
                    try {
                        const stateInput = typeof rawState === 'string' ? JSON.parse(rawState) : rawState;
                        state = decodeTransit(stateInput);
                    } catch {
                        return false;
                    }

                    const modules = state?.modules || {};
                    const root = modules?.['axp-myca-root'] || {};
                    const navigation = modules?.['axp-consumer-navigation'] || {};
                    const cardProductSources = [
                        root?.products?.details?.types?.CARD_PRODUCT,
                        navigation?.products?.details?.types?.CARD_PRODUCT,
                    ];
                    for (const source of cardProductSources) {
                        if (!source || typeof source !== 'object') continue;
                        const productsList = source.productsList || {};
                        if (productsList && typeof productsList === 'object' && Object.keys(productsList).length > 0) {
                            return true;
                        }
                    }
                    const memberProductItems = [
                        ...Object.values(root?.resources?.memberProductsV2?.items || {}),
                        ...Object.values(navigation?.resources?.memberProductsV2?.items || {}),
                    ];
                    return memberProductItems.some((item) => Array.isArray(item?.accounts) && item.accounts.length > 0);
                }
                """
            )
        )
    except Exception:
        return False


async def get_amex_page_state(page) -> dict[str, Any]:
    current_url = visible_auth.safe_page_url(page)
    login_visible = await selector_is_visible(page, AMEX_USERNAME_SELECTOR)
    password_visible = await selector_is_visible(page, AMEX_PASSWORD_SELECTOR)
    otp_channel_visible = await selector_is_visible(page, AMEX_OTP_CHANNEL_SELECTOR)
    otp_code_visible = await selector_is_visible(page, AMEX_OTP_CODE_SELECTOR)
    trust_visible = await selector_is_visible(page, AMEX_TRUST_SELECTOR)
    auth_module_visible = await selector_is_visible(page, AMEX_AUTH_MODULE_SELECTOR)
    dashboard_shell_ready = await _amex_dashboard_shell_machine_state_ready(page)
    dashboard_accounts_ready = await _amex_dashboard_account_state_ready(page)
    login_complete = bool(getattr(page, "_amex_login_complete", False))
    dashboard_account_signal = bool(getattr(page, "_amex_dashboard_account_signal", False))
    auth_status = int(getattr(page, "_amex_auth_status", 0) or 0)
    auth_payload = getattr(page, "_amex_auth_payload", None)
    auth_rejected = bool(getattr(page, "_amex_invalid_credentials", False))
    try:
        parsed_url = urlsplit(current_url)
    except ValueError:
        parsed_url = None
    path = parsed_url.path.lower() if parsed_url else ""
    authenticated_route = "dashboard" in path
    requires_2fa = _amex_login_response_requires_2fa(auth_payload if isinstance(auth_payload, dict) else None)
    windows_dashboard_shell_handoff = bool(
        AMEX_WINDOWS_FLOW
        and authenticated_route
        and dashboard_shell_ready
    )
    return {
        "url": current_url,
        "login_visible": login_visible,
        "password_visible": password_visible,
        "otp_channel_visible": otp_channel_visible,
        "otp_code_visible": otp_code_visible,
        "trust_visible": trust_visible,
        "auth_module_visible": auth_module_visible,
        "dashboard_shell_ready": dashboard_shell_ready,
        "dashboard_accounts_ready": dashboard_accounts_ready,
        "login_complete": login_complete,
        "dashboard_account_signal": dashboard_account_signal,
        "authenticated_route": authenticated_route,
        "windows_dashboard_shell_handoff": windows_dashboard_shell_handoff,
        # `authenticated` is the source of truth used to (a) return the page
        # from the wait loop, (b) trigger the handoff input lock, and (c)
        # decide whether re-auth saved sessions are usable. It MUST only fire
        # once AMEX has genuinely delivered the post-auth dashboard.
        #
        # The previous `(login_complete and (authenticated_route or
        # dashboard_shell_ready))` path was unsafe: AMEX's login POST returns
        # success after the FIRST factor (credentials) but BEFORE 2FA. During
        # the ~100-500ms while the URL transitions through a *dashboard* path
        # toward the 2FA picker, no auth selectors are visible yet — so
        # login_complete + authenticated_route + (no auth UI snapshot) all
        # line up for one poll, `authenticated` flips true, the caller locks
        # the page via CDP Input.setIgnoreInputEvents, then the picker
        # renders on top of a frozen input layer. Reproduces on add flow and
        # re-auth flow. Restrict the positive criteria to strong post-auth
        # signals only: an explicit dashboard data API call seen, real
        # account data parsed from window.__INITIAL_STATE__, or the strict
        # Windows-only handoff combo.
        "authenticated": bool(
            (
                windows_dashboard_shell_handoff
                or dashboard_accounts_ready
                or dashboard_account_signal
            )
            and not (login_visible or password_visible or otp_channel_visible or otp_code_visible or trust_visible or auth_module_visible)
        ),
        "auth_server_error": auth_status >= 500,
        "auth_rejected": auth_rejected,
        "requires_2fa": requires_2fa,
    }


def describe_amex_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated American Express session detected. Saving session for background sync."
    if state.get("auth_server_error"):
        return "American Express returned a server error during secure login. Try again later."
    if state.get("auth_rejected"):
        return "American Express rejected this login attempt. Popup interaction is unlocked so you can correct it and continue."
    if state.get("otp_code_visible"):
        return "Waiting for American Express security code entry in the secure browser."
    if state.get("trust_visible"):
        return "Waiting for American Express trusted-device confirmation in the secure browser."
    if state.get("otp_channel_visible") or state.get("auth_module_visible") or state.get("requires_2fa"):
        return "Waiting for American Express security verification in the secure browser."
    if state.get("login_visible") or state.get("password_visible"):
        return "Waiting for manual sign-in at the American Express login page."
    return "Waiting for American Express secure login to advance."


def _is_amex_credential_submit_url(url: str) -> bool:
    try:
        return urlsplit(url).path == AMEX_LOGIN_POST_PATH
    except ValueError:
        return False


def _flatten_credential_payload(value: Any, *, depth: int = 0) -> dict[str, str]:
    if value is None or depth > 5:
        return {}
    if isinstance(value, str):
        parsed = _parse_amex_json_body(value)
        if parsed is not None:
            return _flatten_credential_payload(parsed, depth=depth + 1)
        return {}
    if isinstance(value, list):
        merged: dict[str, str] = {}
        for item in value:
            merged.update(_flatten_credential_payload(item, depth=depth + 1))
        return merged
    if not isinstance(value, dict):
        return {}
    flattened: dict[str, str] = {}
    for key, nested in value.items():
        key_text = str(key or "").strip()
        if isinstance(nested, (str, int, float)):
            flattened[key_text] = str(nested)
            continue
        flattened.update(_flatten_credential_payload(nested, depth=depth + 1))
    return flattened


def _credentials_from_post_data(post_data: str | None) -> tuple[str, str] | None:
    body = str(post_data or "").strip()
    if not body:
        return None
    parsed_json = _parse_amex_json_body(body)
    flattened = _flatten_credential_payload(parsed_json) if parsed_json is not None else {}
    if not flattened:
        flattened = {key: value for key, value in parse_qsl(body, keep_blank_values=True)}

    username = ""
    password = ""
    for key, value in flattened.items():
        key_lower = str(key or "").strip().lower()
        value_text = str(value or "")
        if not username and key_lower in {
            "elilouserid",
            "loginid",
            "user",
            "userid",
            "user_id",
            "username",
        }:
            username = value_text.strip()
        if not password and key_lower in {
            "elilopassword",
            "password",
            "passcode",
        }:
            password = value_text
    return (username, password) if username and password else None


def _amex_value_is_masked(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and all(character in "*•●" for character in text)


def _capture_amex_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = str(username or "").strip()
    if not normalized or _amex_value_is_masked(normalized):
        return
    if captured_credentials.get("username") != normalized:
        captured_credentials[f"_{source}_username_capture_count"] = str(
            int(captured_credentials.get(f"_{source}_username_capture_count") or 0) + 1
        )
    captured_credentials["username"] = normalized


def _capture_amex_password(captured_credentials: dict[str, str], password: str, *, source: str) -> None:
    normalized = str(password or "")
    if not normalized or _amex_value_is_masked(normalized):
        return
    if captured_credentials.get("password") != normalized:
        captured_credentials[f"_{source}_password_capture_count"] = str(
            int(captured_credentials.get(f"_{source}_password_capture_count") or 0) + 1
        )
    captured_credentials["password"] = normalized


async def _amex_input_value(page, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        return str(await locator.input_value(timeout=500) or "")
    except Exception:
        return ""


async def _capture_amex_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
    username = await _amex_input_value(page, AMEX_USERNAME_SELECTOR)
    password = await _amex_input_value(page, AMEX_PASSWORD_SELECTOR)
    _capture_amex_username(captured_credentials, username, source="raw")
    _capture_amex_password(captured_credentials, password, source="raw")


def _capture_amex_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    if not _is_amex_credential_submit_url(str(getattr(request, "url", "") or "")):
        return
    credentials = _credentials_from_post_data(visible_auth.safe_request_post_data(request))
    if credentials is None:
        return
    username, password = credentials
    _capture_amex_username(captured_credentials, username, source="request")
    _capture_amex_password(captured_credentials, password, source="request")


def _amex_request_failure_error(request) -> str:
    try:
        failure = getattr(request, "failure", None)
        failure = failure() if callable(failure) else failure
    except Exception:
        failure = None
    if isinstance(failure, dict):
        value = str(failure.get("errorText") or "").strip()
    else:
        value = str(failure or "").strip()
    normalized = value.replace("::", "_")
    if (
        not normalized
        or len(normalized) > 160
        or not all(character.isalnum() or character in " ._-/" for character in normalized)
    ):
        return "unavailable"
    return normalized


def _install_amex_page_watchers(page, captured_credentials: dict[str, str]) -> None:
    if getattr(page, "_amex_page_watchers_installed", False):
        return
    setattr(page, "_amex_page_watchers_installed", True)
    setattr(page, "_amex_navigation_token", int(getattr(page, "_amex_navigation_token", 0) or 0))

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                current_token = int(getattr(page, "_amex_navigation_token", 0) or 0)
                setattr(page, "_amex_navigation_token", current_token + 1)
                _schedule_amex_popup_lock(page)
        except Exception:
            return

    def on_request(request) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and _is_amex_credential_submit_url(str(getattr(request, "url", "") or "")):
            loop.create_task(_capture_amex_raw_input_credentials(page, captured_credentials))
        _capture_amex_credentials_from_request(captured_credentials, request)

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
                if parsed.hostname == "functions.americanexpress.com" and parsed.path.lower() in AMEX_DASHBOARD_SIGNAL_PATHS and 200 <= status < 300:
                    setattr(page, "_amex_dashboard_account_signal", True)
                if parsed.path != AMEX_LOGIN_POST_PATH:
                    return
                setattr(page, "_amex_auth_status", status)
                payload = _parse_amex_json_body(await response.text())
                if isinstance(payload, dict):
                    payload["_http_status"] = status
                    setattr(page, "_amex_auth_payload", payload)
                    setattr(page, "_amex_invalid_credentials", _amex_login_response_rejected(payload))
                    setattr(page, "_amex_login_complete", _amex_login_response_authenticated(payload))
            except Exception:
                return

        loop.create_task(capture_response())

    def on_request_failed(request) -> None:
        url = str(getattr(request, "url", "") or "")
        if not _is_amex_credential_submit_url(url):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            parsed = urlsplit(url)
        except ValueError:
            return
        loop.create_task(
            log_support_event(
                stage="login request failed",
                level="warning",
                message="American Express login request failed before a response.",
                details={
                    "request_host": parsed.hostname or "",
                    "request_path": parsed.path,
                    "request_method": str(getattr(request, "method", "") or ""),
                    "network_error": _amex_request_failure_error(request),
                },
            )
        )

    page.on("framenavigated", on_frame_navigated)
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


async def _prepare_amex_context_page(context, captured_credentials: dict[str, str]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        _install_amex_page_watchers(page, captured_credentials)
        for extra_page in pages[1:]:
            _install_amex_page_watchers(extra_page, captured_credentials)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    _install_amex_page_watchers(page, captured_credentials)
    return page


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_amex_material,
        origin_from_url=_amex_origin_from_url,
        storage_state_timeout_seconds=AMEX_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=AMEX_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=AMEX_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def storage_state_has_amex_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if "americanexpress.com" in domain:
            return True
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _amex_origin_from_url(origin.get("origin")):
            return True
    return False


def sanitize_amex_bootstrap_storage_state(
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
        if "americanexpress.com" in domain and name not in AMEX_SAFE_BOOTSTRAP_COOKIE_NAMES:
            details["removed_auth_cookies"] += 1
            continue
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    origins = sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []
    kept_origins: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _amex_origin_from_url(origin.get("origin")):
            details["removed_auth_local_storage"] += len(
                origin.get("localStorage") if isinstance(origin.get("localStorage"), list) else []
            )
            if "indexedDB" in origin:
                details["removed_auth_indexed_db"] += 1
            continue
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not (sanitized.get("cookies") or sanitized.get("origins")):
        return None, details
    return sanitized, details


async def collect_amex_session_artifact(page, *, user_agent: str = "") -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    normalized_user_agent = str(user_agent or "").strip()
    if normalized_user_agent:
        artifact["user_agent"] = normalized_user_agent
    current_url = visible_auth.safe_page_url(page)
    if current_url:
        artifact["dashboard_url"] = _amex_sanitize_url(current_url)
    return artifact


def _amex_prefill_phase_key(page, state: dict[str, Any]) -> tuple[int, int, str]:
    navigation_token = int(getattr(page, "_amex_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
    )


def _amex_prefill_slot_key(page, state: dict[str, Any], field: str) -> tuple[int, int, str, str]:
    navigation_token = int(getattr(page, "_amex_navigation_token", 0) or 0)
    return (
        id(page),
        navigation_token,
        str(state.get("url") or ""),
        field,
    )


async def _prefill_amex_input(page, selector: str, value: str) -> bool:
    if not value:
        return False
    try:
        await page.wait_for_selector(selector, state="visible", timeout=1000)
    except Exception:
        return False
    current_value = await _amex_input_value(page, selector)
    if current_value:
        return False
    try:
        await page.fill(selector, value)
    except Exception:
        return False
    return bool(await _amex_input_value(page, selector))


async def _maybe_prefill_amex_credentials(
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
    phase_key = _amex_prefill_phase_key(page, state)
    username_key = _amex_prefill_slot_key(page, state, "username")
    password_key = _amex_prefill_slot_key(page, state, "password")
    needs_username_prefill = (
        state.get("login_visible")
        and bool(username)
        and username_key not in attempted
        and not await _amex_input_value(page, AMEX_USERNAME_SELECTOR)
    )
    needs_password_prefill = (
        state.get("password_visible")
        and bool(password)
        and password_key not in attempted
        and not await _amex_input_value(page, AMEX_PASSWORD_SELECTOR)
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
                or state.get("otp_channel_visible")
                or state.get("otp_code_visible")
                or state.get("trust_visible")
                or state.get("auth_rejected")
                or state.get("auth_server_error")
                or (asyncio.get_running_loop().time() - started_at) >= AMEX_CREDENTIAL_LOCK_GRACE_SECONDS
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
            username_prefilled = await _prefill_amex_input(page, AMEX_USERNAME_SELECTOR, username)
            attempted.add(username_key)
            if username_prefilled:
                _capture_amex_username(captured_credentials, username, source="prefill")
                prefills_applied = True
        if needs_password_prefill:
            password_prefilled = await _prefill_amex_input(page, AMEX_PASSWORD_SELECTOR, password)
            attempted.add(password_key)
            if password_prefilled:
                _capture_amex_password(captured_credentials, password, source="prefill")
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved American Express credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
    finally:
        lock_started_at.pop(phase_key, None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


async def _quick_prefill_amex_credentials(
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
        "login_visible": await selector_is_visible(page, AMEX_USERNAME_SELECTOR),
        "password_visible": await selector_is_visible(page, AMEX_PASSWORD_SELECTOR),
        "otp_channel_visible": False,
        "otp_code_visible": False,
        "trust_visible": False,
        "auth_rejected": False,
        "auth_server_error": False,
    }
    await _maybe_prefill_amex_credentials(
        page,
        state=state,
        credentials=credentials,
        prefill_state=prefill_state,
        captured_credentials=captured_credentials,
        popup_lock=popup_lock,
    )


def collect_amex_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if username and password:
        return username, password
    return None


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
    credential_validation_reset = False
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_amex_page(context, page, popup_lock=popup_lock)
            await _dismiss_amex_cookie_banner(page)
            await _capture_amex_raw_input_credentials(page, captured_credentials)
            if not VISIBLE_AUTH_ADD_FLOW:
                await _quick_prefill_amex_credentials(
                    page,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    captured_credentials=captured_credentials,
                    popup_lock=popup_lock,
                )
            try:
                state = await get_amex_page_state(page)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if (
                not VISIBLE_AUTH_ADD_FLOW
                and state.get("authenticated")
                and collect_amex_credentials(captured_credentials) is None
            ):
                if not credential_validation_reset:
                    credential_validation_reset = True
                    await log_support_event(
                        stage="credential validation reset",
                        level="warning",
                        message="American Express reached an authenticated page before credentials were validated; returning to login.",
                    )
                    try:
                        await context.clear_cookies()
                    except Exception:
                        pass
                    try:
                        await page.goto(AMEX_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    await asyncio.sleep(AMEX_WAIT_POLL_SECONDS)
                    continue
                state["authenticated"] = False
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_amex_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    captured_credentials=captured_credentials,
                    popup_lock=popup_lock,
                )
            # Only apply the handoff input lock once the page state is fully
            # `authenticated` (the strict check). Weaker URL/shell signals
            # (`authenticated_route`, `dashboard_shell_ready`) race against
            # AMEX's re-auth flow: when saved cookies trigger a brief redirect
            # through a dashboard-pathed URL before the 2FA picker JS renders,
            # those signals flash true for one poll while no auth selectors are
            # yet visible. The lock fires permanently, the picker then renders
            # on top of a CDP-locked page, and the user can't click anything.
            # Mirror Tangerine/RBC and only lock on the fully-computed
            # `authenticated` state.
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="American Express",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
            description = describe_amex_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                return page
            if state.get("auth_server_error"):
                raise RuntimeError("American Express returned a server error during secure login. Try again later.")
            if state.get("auth_rejected"):
                if popup_lock is not None and popup_lock.locked:
                    await popup_lock.set_locked(False)
                if not manual_takeover_announced:
                    await log_support_event(stage="manual takeover", message=description, last_output=description)
                    manual_takeover_announced = True
            await asyncio.sleep(AMEX_WAIT_POLL_SECONDS)

        raise TimeoutError("Timed out waiting for an authenticated American Express session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


async def run() -> None:
    if PROVIDER != "amex":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for AMEX visible auth.")
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
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_amex_bootstrap_storage_state(
            loaded_storage_state
        )
        if bootstrap_storage_state is None:
            bootstrap_storage_slot = ""
    har_timestamp_slug = visible_auth.har_timestamp_slug() if VISIBLE_AUTH_RECORD_HAR else ""
    raw_har_path = _amex_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening American Express secure login browser.")
    visible_auth.print_status("Opening American Express secure login browser.")

    await visible_auth.log_browser_launch_stage(
        log_support_event,
        provider_display_name="AMEX",
        stage="patchright driver",
        result="started",
        details={"driver": "patchright"},
    )
    async with async_playwright() as playwright:
        await visible_auth.log_browser_launch_stage(
            log_support_event,
            provider_display_name="AMEX",
            stage="patchright driver",
            result="ready",
            details={"driver": "patchright"},
        )
        browser = None
        context = None
        browser_runtime: dict[str, Any] = {}
        browser_timezone = normalize_browser_timezone(VISIBLE_AUTH_TIMEZONE)
        launch_mode = "isolated_context_fresh"
        try:
            await visible_auth.log_browser_launch_stage(
                log_support_event,
                provider_display_name="AMEX",
                stage="browser runtime ensure",
                result="started",
                details={"requested_runtime": "brave_browser"},
            )
            try:
                browser_runtime = ensure_brave_browser_runtime()
            except DesktopBrowserRuntimeError as exc:
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage="browser runtime ensure",
                    result="failed",
                    details={"requested_runtime": "brave_browser", "error": str(exc)},
                )
                raise RuntimeError(str(exc)) from exc
            await visible_auth.log_browser_launch_stage(
                log_support_event,
                provider_display_name="AMEX",
                stage="browser runtime ensure",
                result="ready",
                browser_runtime=browser_runtime,
            )
            await log_support_event(stage="direct_entry", debug=True, message="American Express is using direct login entry.")
            await log_support_event(
                stage="har capture",
                debug=True,
                message=(
                    "AMEX visible-auth HAR capture is enabled."
                    if VISIBLE_AUTH_RECORD_HAR
                    else "AMEX visible-auth HAR capture is disabled."
                ),
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
            )
            current_launch_stage = "browser launch setup"
            try:
                launch_kwargs = visible_auth.managed_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    AMEX_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                current_launch_stage = "browser launch"
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage=current_launch_stage,
                    result="started",
                    browser_runtime=browser_runtime,
                    details=visible_auth.browser_launch_details(launch_kwargs),
                )
                browser = await playwright.chromium.launch(**launch_kwargs)
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage=current_launch_stage,
                    result="ready",
                    browser_runtime=browser_runtime,
                    details=visible_auth.browser_launch_details(launch_kwargs),
                )
                current_launch_stage = "browser context"
                context_launch_details = {
                    **visible_auth.browser_context_details(context_kwargs),
                    "launch_mode": launch_mode,
                }
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage=current_launch_stage,
                    result="started",
                    browser_runtime=browser_runtime,
                    details=context_launch_details,
                )
                context = await browser.new_context(**context_kwargs)
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage=current_launch_stage,
                    result="ready",
                    browser_runtime=browser_runtime,
                    details=context_launch_details,
                )
                await _amex_preseed_cookie_consent(context)
            except Exception as exc:
                await visible_auth.log_browser_launch_stage(
                    log_support_event,
                    provider_display_name="AMEX",
                    stage=current_launch_stage,
                    result="failed",
                    browser_runtime=browser_runtime,
                    details={"error": str(exc)[:500]},
                )
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="American Express",
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
                message="AMEX visible-auth browser runtime selected.",
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
                message="AMEX visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
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
                    "visible_auth_platform": AMEX_VISIBLE_AUTH_PLATFORM,
                    "visible_auth_flow": AMEX_VISIBLE_AUTH_FLOW,
                    "amex_linux_flow": AMEX_LINUX_FLOW,
                    "amex_windows_flow": AMEX_WINDOWS_FLOW,
                    "amex_mac_flow": AMEX_MAC_FLOW,
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
                _install_amex_page_watchers(new_page, captured_credentials)

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
            page = await _prepare_amex_context_page(context, captured_credentials)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_amex_login_page(page, entry_url=AMEX_LOGIN_URL)
            page = await wait_for_authenticated_session(
                context,
                page,
                captured_credentials=captured_credentials,
                prefill_credentials=saved_credentials,
                popup_lock=popup_lock,
            )
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="American Express",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = page
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_amex_material": storage_state_has_amex_material(storage_state),
            }
            if not storage_state_has_amex_material(storage_state):
                raise RuntimeError("American Express storage state could not be captured for future syncs.")
            session_artifact = await collect_amex_session_artifact(active_target, user_agent=user_agent)
            session_artifact_details = {
                "has_user_agent": bool(session_artifact.get("user_agent")),
                "has_dashboard_url": bool(session_artifact.get("dashboard_url")),
            }
            captured_pair = collect_amex_credentials(captured_credentials)
            credential_capture_details = {
                "raw_username_capture_count": int(captured_credentials.get("_raw_username_capture_count") or 0),
                "request_username_capture_count": int(captured_credentials.get("_request_username_capture_count") or 0),
                "prefill_username_capture_count": int(captured_credentials.get("_prefill_username_capture_count") or 0),
                "raw_password_capture_count": int(captured_credentials.get("_raw_password_capture_count") or 0),
                "request_password_capture_count": int(captured_credentials.get("_request_password_capture_count") or 0),
                "prefill_password_capture_count": int(captured_credentials.get("_prefill_password_capture_count") or 0),
                "username_captured": bool(captured_credentials.get("username")),
                "password_captured": bool(captured_credentials.get("password")),
            }
            if not captured_pair:
                raise RuntimeError("American Express credentials could not be captured from the secure login flow. Start AMEX login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="American Express",
                    log_support_event=log_support_event,
                ):
                    context = None

            auth_message = "Authenticated American Express session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="AMEX visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="AMEX visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="AMEX visible-auth credential capture summary.",
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

            storage_message = "American Express secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured American Express credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"American Express secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
            browser = None
            if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_amex_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized AMEX HAR capture.",
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
                        message="Could not finalize the sanitized AMEX HAR capture.",
                        details={
                            "har_result": run_result,
                            "error": str(exc),
                        },
                    )

def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
