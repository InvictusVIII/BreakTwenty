#!/usr/bin/env python3
"""Desktop visible-auth runner for Interactive Brokers."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
IBKR_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "ibkr"
IBKR_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "IBKR")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "ibkr").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=False)
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

IBKR_LOGIN_URL = "https://ndcdyn.interactivebrokers.com/sso/Login"
IBKR_ACCOUNTS_URL = "https://portal.interactivebrokers.com/portal.proxy/v1/portal/portfolio2/accounts"
IBKR_PAGE_URL_MARKERS = (
    "interactivebrokers.com",
    "interactivebrokers.ca",
    "ibkr.com",
)
IBKR_USERNAME_SELECTORS = (
    "#xyz-field-username",
    "input[name='username']",
    "input[name='user_name']",
    "input[name='j_username']",
    "input[type='email']",
    "input[type='text']",
)
IBKR_PASSWORD_SELECTORS = (
    "#xyz-field-password",
    "input[name='password']",
    "input[name='j_password']",
    "input[type='password']",
)
IBKR_CODE_SELECTORS = (
    "#xyz-field-silver-response",
    "input[name='silver-response']",
    "input[name='otp']",
    "input[name='code']",
    "input[type='tel']",
)
IBKR_ERROR_SELECTORS = (
    ".xyz-error",
    ".alert-danger",
    "[class*='error-msg']",
    "[class*='error']",
    ".error",
)
IBKR_WAIT_POLL_SECONDS = 0.35
IBKR_STORAGE_STATE_TIMEOUT_SECONDS = 15
IBKR_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 5
IBKR_LOCAL_STORAGE_TIMEOUT_SECONDS = 1.5
IBKR_HAR_FILENAME_PREFIX = "ibkr_visible_auth"
IBKR_MAX_SANITIZED_HARS_PER_USER = 3
IBKR_REDACTED_VALUE = "<redacted>"
IBKR_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "authenticated",
        "code",
        "error",
        "errorcode",
        "message",
        "reason",
        "result",
        "status",
        "statuscode",
    }
)
IBKR_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
HOST_BROWSER_CLOSED_MESSAGE = (
    "Login window was closed before secure login finished. "
    "Start sync again and keep the Interactive Brokers browser window open until the app says it is done."
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


def _ibkr_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return IBKR_DESKTOP_AUTH_RAW_HAR_DIR / f"{IBKR_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _ibkr_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return IBKR_DIAGNOSTIC_DIR / f"{IBKR_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _ibkr_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if not _is_ibkr_storage_host(host):
        return False
    if parsed.path.endswith("/portfolio2/accounts"):
        return True
    lowered_content_type = content_type.lower()
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_ibkr_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _ibkr_sanitized_har_path(user_id, timestamp_slug, result)
    pattern = f"{IBKR_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        IBKR_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=IBKR_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_ibkr_should_keep_response_body,
        safe_response_keys=IBKR_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=IBKR_HAR_SENSITIVE_HEADER_NAMES,
        redacted_value=IBKR_REDACTED_VALUE,
    )


def _is_ibkr_storage_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower().lstrip(".")
    return any(
        normalized == marker or normalized.endswith(f".{marker}")
        for marker in IBKR_PAGE_URL_MARKERS
    )


def _ibkr_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and _is_ibkr_storage_host(host):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def storage_state_has_ibkr_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    for cookie in storage_state.get("cookies") or []:
        if isinstance(cookie, dict) and _is_ibkr_storage_host(str(cookie.get("domain") or "")):
            return True
    for origin in storage_state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        if _ibkr_origin_from_url(origin.get("origin")):
            return True
    return False


def sanitize_ibkr_bootstrap_storage_state(
    storage_state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    details = {
        "removed_auth_cookies": 0,
        "kept_non_auth_cookies": 0,
        "removed_auth_origins": 0,
    }
    if not isinstance(storage_state, dict):
        return None, details
    try:
        sanitized = json.loads(json.dumps(storage_state))
    except Exception:
        sanitized = dict(storage_state)

    kept_cookies: list[dict[str, Any]] = []
    for cookie in (sanitized.get("cookies") if isinstance(sanitized.get("cookies"), list) else []):
        if not isinstance(cookie, dict):
            continue
        if _is_ibkr_storage_host(str(cookie.get("domain") or "")):
            details["removed_auth_cookies"] += 1
            continue
        details["kept_non_auth_cookies"] += 1
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    kept_origins: list[dict[str, Any]] = []
    for origin in (sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []):
        if not isinstance(origin, dict):
            continue
        if _ibkr_origin_from_url(origin.get("origin")):
            details["removed_auth_origins"] += 1
            continue
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not kept_cookies and not kept_origins:
        return None, details
    return sanitized, details


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_ibkr_material,
        origin_from_url=_ibkr_origin_from_url,
        storage_state_timeout_seconds=IBKR_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=IBKR_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=IBKR_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=True,
    )


def _capture_username(captured_credentials: dict[str, str], username: str, *, source: str) -> None:
    normalized = str(username or "").strip()
    if not normalized:
        return
    if visible_auth.username_looks_like_placeholder(normalized):
        captured_credentials[f"_{source}_username_placeholder_count"] = str(
            int(captured_credentials.get(f"_{source}_username_placeholder_count") or 0) + 1
        )
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


def _extract_credentials_from_mapping(payload: dict[str, Any]) -> tuple[str, str | None] | None:
    username = ""
    password = ""
    for key, value in payload.items():
        key_lower = str(key or "").strip().lower()
        if not username and key_lower in {"username", "user", "userid", "user_id", "login", "j_username"}:
            username = str(value or "").strip()
        if not password and ("password" in key_lower or key_lower in {"pass", "pwd", "j_password"}):
            password = str(value or "")
    if username or password:
        return username, password or None
    for value in payload.values():
        if isinstance(value, dict):
            nested = _extract_credentials_from_mapping(value)
            if nested is not None:
                return nested
    return None


def _credentials_from_post_data(post_data: str | None) -> tuple[str, str | None] | None:
    text = str(post_data or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        credentials = _extract_credentials_from_mapping(parsed)
        if credentials is not None:
            return credentials
    pairs = dict(parse_qsl(text, keep_blank_values=True))
    if pairs:
        return _extract_credentials_from_mapping(pairs)
    return None


async def _capture_ibkr_raw_input_credentials(page, captured_credentials: dict[str, str]) -> None:
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
                "usernameSelectors": list(IBKR_USERNAME_SELECTORS),
                "passwordSelectors": list(IBKR_PASSWORD_SELECTORS),
            },
        )
    except Exception:
        return
    if not isinstance(values, dict):
        return
    _capture_username(captured_credentials, str(values.get("username") or ""), source="raw")
    _capture_password(captured_credentials, str(values.get("password") or ""), source="raw")


def _capture_ibkr_credentials_from_request(captured_credentials: dict[str, str], request) -> None:
    method = str(getattr(request, "method", "") or "").upper()
    url = str(getattr(request, "url", "") or "")
    if method != "POST" or not _is_ibkr_storage_host(urlsplit(url).hostname):
        return
    credentials = _credentials_from_post_data(visible_auth.safe_request_post_data(request))
    if credentials is None:
        return
    username, password = credentials
    if username:
        _capture_username(captured_credentials, username, source="request")
    if password:
        _capture_password(captured_credentials, password, source="request")


async def _capture_ibkr_response(response, auth_signals: dict[str, Any]) -> None:
    url = str(getattr(response, "url", "") or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return
    if not _is_ibkr_storage_host(parsed.hostname) or not parsed.path.endswith("/portfolio2/accounts"):
        return
    try:
        text = await response.text()
    except Exception:
        text = ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, list) and payload:
        auth_signals["authenticated"] = True


def bind_ibkr_page_watcher(page, captured_credentials: dict[str, str], auth_signals: dict[str, Any]) -> None:
    if getattr(page, "_ibkr_visible_auth_watcher_installed", False):
        return
    setattr(page, "_ibkr_visible_auth_watcher_installed", True)

    def on_request(request) -> None:
        try:
            method = str(getattr(request, "method", "") or "").upper()
            url = str(getattr(request, "url", "") or "")
            if method != "POST" or not _is_ibkr_storage_host(urlsplit(url).hostname):
                return
            asyncio.create_task(_capture_ibkr_raw_input_credentials(page, captured_credentials))
            _capture_ibkr_credentials_from_request(captured_credentials, request)
        except Exception:
            return

    def on_response(response) -> None:
        asyncio.create_task(_capture_ibkr_response(response, auth_signals))

    page.on("request", on_request)
    page.on("response", on_response)


async def _prepare_ibkr_context_page(context, captured_credentials: dict[str, str], auth_signals: dict[str, Any]):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_ibkr_page_watcher(page, captured_credentials, auth_signals)
        for extra_page in pages[1:]:
            bind_ibkr_page_watcher(extra_page, captured_credentials, auth_signals)
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_ibkr_page_watcher(page, captured_credentials, auth_signals)
    return page


async def _probe_authenticated_session(page, auth_signals: dict[str, Any]) -> bool:
    raw_page = getattr(page, "raw_page", page)
    try:
        result = await raw_page.evaluate(
            """
            async (url) => {
                const response = await fetch(url, {
                    credentials: "include",
                    headers: { Accept: "application/json, text/plain, */*" },
                });
                const text = await response.text();
                let payload = null;
                try {
                    payload = JSON.parse(text);
                } catch (_err) {
                    payload = null;
                }
                return {
                    status: response.status,
                    url: response.url,
                    accounts: Array.isArray(payload) ? payload.length : 0,
                };
            }
            """,
            IBKR_ACCOUNTS_URL,
        )
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    if int(result.get("status") or 0) in (401, 403):
        return False
    if int(result.get("accounts") or 0) > 0:
        auth_signals["authenticated"] = True
        return True
    return False


async def ensure_ibkr_portal_api_ready(page, auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    result = await raw_page.evaluate(
        """
        async (url) => {
            const response = await fetch(url, {
                credentials: "include",
                headers: { Accept: "*/*" },
            });
            const text = await response.text();
            let payload = null;
            try {
                payload = JSON.parse(text);
            } catch (_err) {
                payload = null;
            }
            return {
                status: response.status,
                url: response.url,
                accounts: Array.isArray(payload) ? payload.length : 0,
            };
        }
        """,
        IBKR_ACCOUNTS_URL,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Interactive Brokers Client Portal API probe returned an unexpected response.")
    if int(result.get("status") or 0) in (401, 403) or int(result.get("accounts") or 0) <= 0:
        raise RuntimeError("Interactive Brokers Client Portal API session was not ready after secure login.")
    auth_signals["authenticated"] = True
    return result


async def get_ibkr_page_state(page, auth_signals: dict[str, Any]) -> dict[str, Any]:
    raw_page = getattr(page, "raw_page", page)
    try:
        state = await raw_page.evaluate(
            """
            ({ usernameSelectors, passwordSelectors, codeSelectors, errorSelectors }) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                };
                const visibleMatches = (selectors) => selectors.some((selector) =>
                    Array.from(document.querySelectorAll(selector)).some(isVisible)
                );
                const path = window.location.pathname || "";
                const href = window.location.href || "";
                const usernameVisible = visibleMatches(usernameSelectors);
                const passwordVisible = visibleMatches(passwordSelectors);
                const codeVisible = visibleMatches(codeSelectors);
                const invalidCredentials = visibleMatches(errorSelectors);
                const authenticatedRoute = path.includes("/portal") && !path.includes("/sso");
                return {
                    current_url: href,
                    username_visible: usernameVisible,
                    password_visible: passwordVisible,
                    code_visible: codeVisible,
                    invalid_credentials: invalidCredentials,
                    authenticated_route: authenticatedRoute,
                    authenticated: false,
                };
            }
            """,
            {
                "usernameSelectors": list(IBKR_USERNAME_SELECTORS),
                "passwordSelectors": list(IBKR_PASSWORD_SELECTORS),
                "codeSelectors": list(IBKR_CODE_SELECTORS),
                "errorSelectors": list(IBKR_ERROR_SELECTORS),
            },
        )
    except Exception:
        state = {"current_url": visible_auth.safe_page_url(raw_page), "authenticated": False}
    if not isinstance(state, dict):
        state = {"current_url": visible_auth.safe_page_url(raw_page), "authenticated": False}
    if auth_signals.get("authenticated"):
        state["authenticated"] = True
    elif state.get("authenticated_route") and not (
        state.get("username_visible") or state.get("password_visible") or state.get("code_visible")
    ):
        state["authenticated"] = await _probe_authenticated_session(page, auth_signals)
    return state


def describe_ibkr_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated Interactive Brokers session detected."
    if state.get("invalid_credentials"):
        return "Interactive Brokers showed a credential error. Correct the credentials in the secure browser and continue."
    if state.get("code_visible"):
        return "Complete Interactive Brokers verification in the secure browser."
    if state.get("username_visible") or state.get("password_visible"):
        return "Sign in to Interactive Brokers in the secure browser."
    return "Waiting for Interactive Brokers secure login."


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


async def _maybe_prefill_ibkr_credentials(
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
    if state.get("authenticated") or state.get("code_visible") or state.get("invalid_credentials"):
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        return False
    if not (state.get("username_visible") or state.get("password_visible")):
        return False
    username, password = credentials
    username_filled = await _fill_first_visible(page, IBKR_USERNAME_SELECTORS, username)
    password_filled = await _fill_first_visible(page, IBKR_PASSWORD_SELECTORS, password)
    if username_filled:
        _capture_username(captured_credentials, username, source="prefill")
    if password_filled:
        _capture_password(captured_credentials, password, source="prefill")
    if username_filled or password_filled:
        prefill_state["done"] = True
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)
        message = "Saved Interactive Brokers credentials were prefilled in the secure browser. Review them and continue sign-in."
        visible_auth.print_status(message)
        await log_support_event(stage="credentials prefilled", message=message, last_output=message)
        return True
    return False


async def get_viable_ibkr_page(
    context,
    current_page,
    captured_credentials: dict[str, str],
    auth_signals: dict[str, Any],
    *,
    popup_lock: visible_auth.PopupInteractionLock | None = None,
):
    raw_current = getattr(current_page, "raw_page", current_page)
    pages = [page for page in list(getattr(context, "pages", []) or []) if not visible_auth.is_page_closed(page)]
    for page in pages:
        bind_ibkr_page_watcher(page, captured_credentials, auth_signals)
    if raw_current is not None and not visible_auth.is_page_closed(raw_current):
        await visible_auth.bind_popup_lock_to_page(context, popup_lock, raw_current)
        return current_page
    for page in pages:
        if any(marker in visible_auth.safe_page_url(page).lower() for marker in IBKR_PAGE_URL_MARKERS):
            await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)
            return popup_lock.page if popup_lock is not None and popup_lock.raw_page is page else page
    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)


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
            page = await get_viable_ibkr_page(
                context,
                page,
                captured_credentials,
                auth_signals,
                popup_lock=popup_lock,
            )
            await _capture_ibkr_raw_input_credentials(page, captured_credentials)
            try:
                state = await get_ibkr_page_state(page, auth_signals)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_ibkr_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                    captured_credentials=captured_credentials,
                )
            if (
                (state.get("authenticated") or state.get("authenticated_route"))
                and not (state.get("username_visible") or state.get("password_visible") or state.get("code_visible"))
                and not state.get("invalid_credentials")
            ):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="Interactive Brokers",
                    log_support_event=log_support_event,
                    reason="authenticated_route",
                )
            description = describe_ibkr_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                return page
            if (
                (state.get("invalid_credentials") or state.get("code_visible"))
                and popup_lock is not None
                and popup_lock.locked
            ):
                await popup_lock.set_locked(False)
            await asyncio.sleep(IBKR_WAIT_POLL_SECONDS)
        raise TimeoutError("Timed out waiting for an authenticated Interactive Brokers session.")
    finally:
        if popup_lock is not None and popup_lock.locked and not visible_auth.visible_auth_context_handoff_lock_started(context):
            await popup_lock.set_locked(False)


def build_ibkr_session_artifact(*, user_agent: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "captured_at": now,
        "authenticated_at": now,
        "user_agent": str(user_agent or "").strip(),
    }


def collect_ibkr_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    password = str(captured_credentials.get("password") or "")
    if (
        username
        and not visible_auth.username_looks_like_placeholder(username)
        and password
        and not visible_auth._is_masked_secret(password)
    ):
        return username, password
    return None


async def open_ibkr_login_page(page) -> None:
    await page.goto(IBKR_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)


async def run() -> None:
    if PROVIDER != "ibkr":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for IBKR visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    auth_signals: dict[str, Any] = {"authenticated": False}
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    bootstrap_storage_sanitize_details: dict[str, int] = {}
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_ibkr_bootstrap_storage_state(
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
    raw_har_path = _ibkr_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening Interactive Brokers secure login browser.")
    visible_auth.print_status("Opening Interactive Brokers secure login browser.")

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
                "IBKR visible-auth HAR capture is enabled."
                if VISIBLE_AUTH_RECORD_HAR
                else "IBKR visible-auth HAR capture is disabled."
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
                    IBKR_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(**context_kwargs)
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="Interactive Brokers",
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
                message="IBKR visible-auth browser runtime selected.",
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
                message="IBKR visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_storage_sanitized": not VISIBLE_AUTH_ADD_FLOW,
                    "bootstrap_storage_sanitize_details": bootstrap_storage_sanitize_details,
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
                bind_ibkr_page_watcher(new_page, captured_credentials, auth_signals)
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
            page = await _prepare_ibkr_context_page(context, captured_credentials, auth_signals)
            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = visible_auth.PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            await open_ibkr_login_page(page)
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
                provider_display_name="Interactive Brokers",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            active_target = getattr(page, "raw_page", page)
            portal_api_probe = await ensure_ibkr_portal_api_ready(page, auth_signals)
            portal_api_probe_details = {
                "status": int(portal_api_probe.get("status") or 0),
                "accounts": int(portal_api_probe.get("accounts") or 0),
                "url": str(portal_api_probe.get("url") or ""),
            }
            await asyncio.sleep(1.5)
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "storage_cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_ibkr_material": storage_state_has_ibkr_material(storage_state),
            }
            if not storage_state_has_ibkr_material(storage_state):
                raise RuntimeError("Interactive Brokers storage state could not be captured for future syncs.")

            session_artifact = build_ibkr_session_artifact(user_agent=user_agent)
            session_artifact_details = {"has_user_agent": bool(session_artifact.get("user_agent"))}
            captured_pair = collect_ibkr_credentials(captured_credentials)
            credential_capture_details = {
                "raw_username_capture_count": int(captured_credentials.get("_raw_username_capture_count") or 0),
                "request_username_capture_count": int(captured_credentials.get("_request_username_capture_count") or 0),
                "prefill_username_capture_count": int(captured_credentials.get("_prefill_username_capture_count") or 0),
                "raw_username_placeholder_count": int(captured_credentials.get("_raw_username_placeholder_count") or 0),
                "request_username_placeholder_count": int(captured_credentials.get("_request_username_placeholder_count") or 0),
                "prefill_username_placeholder_count": int(captured_credentials.get("_prefill_username_placeholder_count") or 0),
                "raw_password_capture_count": int(captured_credentials.get("_raw_password_capture_count") or 0),
                "request_password_capture_count": int(captured_credentials.get("_request_password_capture_count") or 0),
                "prefill_password_capture_count": int(captured_credentials.get("_prefill_password_capture_count") or 0),
                "username_captured": bool(captured_credentials.get("username")),
                "password_captured": bool(captured_credentials.get("password")),
            }
            if captured_pair is None:
                raise RuntimeError("Interactive Brokers credentials could not be captured from the secure login flow. Start IBKR login again.")
            username, password = captured_pair

            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="Interactive Brokers",
                    log_support_event=log_support_event,
                ):
                    context = None

            await log_support_event(
                stage="api session ready",
                debug=True,
                message="IBKR visible-auth Client Portal API session is ready.",
                details=portal_api_probe_details,
            )
            auth_message = "Authenticated Interactive Brokers session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="IBKR visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="IBKR visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            await log_support_event(
                stage="credential capture summary",
                debug=True,
                message="IBKR visible-auth credential capture summary.",
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
            storage_message = "Interactive Brokers secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured Interactive Brokers credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(stage="credentials saved", message=credentials_message, last_output=credentials_message)
            success_message = f"Interactive Brokers secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts in the background."
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
                        finalize_ibkr_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized IBKR HAR capture.",
                            details={"har_result": run_result, "path": str(sanitized_har_path)},
                        )
                except Exception as exc:
                    await log_support_event(
                        stage="har capture",
                        level="warning",
                        debug=True,
                        message="Could not finalize the sanitized IBKR HAR capture.",
                        details={"har_result": run_result, "error": str(exc)},
                    )
            await visible_auth.close_visible_auth_context_quietly(None, browser)


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
