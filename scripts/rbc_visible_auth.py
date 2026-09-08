#!/usr/bin/env python3
"""Desktop visible-auth runner for RBC."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine
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
RBC_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "rbc"
RBC_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "RBC")
BACKEND_API_BASE_URL = os.getenv("BREAKTWENTY_BACKEND_API_URL", "http://127.0.0.1:8000/api")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "rbc").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
VISIBLE_AUTH_SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
VISIBLE_AUTH_ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_TIMEZONE = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_TIMEZONE") or DEFAULT_USER_TIMEZONE).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds()
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR", default=False)
VISIBLE_AUTH_CAPTURE_LEVEL = visible_auth.normalize_capture_level(
    os.getenv("BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL")
)
RBC_DEV_CAPTURE_LOCAL = VISIBLE_AUTH_CAPTURE_LEVEL == visible_auth.CAPTURE_LEVEL_DEVELOPER_LOCAL
RBC_VISIBLE_AUTH_PLATFORM = visible_auth.resolve_provider_visible_auth_platform(PROVIDER)
RBC_VISIBLE_AUTH_FLOW = visible_auth.provider_visible_auth_flow(PROVIDER, RBC_VISIBLE_AUTH_PLATFORM)
# rbc:linux is the baseline RBC flow. Platform-specific behavior must stay behind
# the explicit platform flags below so Linux remains untouched when Windows diverges.
RBC_LINUX_FLOW = RBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_LINUX
RBC_WINDOWS_FLOW = RBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_WINDOWS
RBC_MAC_FLOW = RBC_VISIBLE_AUTH_PLATFORM == visible_auth.VISIBLE_AUTH_PLATFORM_MAC
DEFAULT_VISIBLE_AUTH_LOG_PATH = VISIBLE_AUTH_LOG_DIR / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
VISIBLE_AUTH_LOG_PATH = Path(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_VISIBLE_AUTH_LOG_PATH)).expanduser()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=VISIBLE_AUTH_ADD_FLOW,
    sync_id=VISIBLE_AUTH_SYNC_ID,
)

RBC_LOGIN_URL = "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi?F6=1&F7=IB&F21=IB&F22=IB&REQUEST=ClientSignin&LANGUAGE=ENGLISH"
RBC_ACCOUNT_LIST_URL = "https://www1.royalbank.com/sgw5/digital/product-summary-presentation-service-v3/v3/accountListSummary"
RBC_PAGE_URL_MARKERS = (
    "royalbank.com",
    "rbc.com",
)
RBC_USERNAME_SELECTORS = (
    "input#K1[name='K1']",
    "form#rbunxcgi input#K1[name='K1']",
    "form[name='rbunxcgi'] input#K1[name='K1']",
)
RBC_PASSWORD_SELECTORS = (
    "input#QQ[name='QQ']",
    "input#QQ",
    "input[name='QQ']",
    "form#rbunxcgi input#QQ[name='QQ']",
    "form[name='rbunxcgi'] input#QQ[name='QQ']",
    "input#Q1",
    "input[name='Q1']",
    "input[type='password']",
    "input[autocomplete='current-password']",
)
RBC_MASK_CHARACTERS = frozenset({"*", "\u2022", "\u25cf", "\u25e6", "\u2217"})
RBC_OTP_INPUT_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[inputmode='numeric']",
    "input[type='tel']",
    "input[name*='code' i]",
    "input[id*='code' i]",
    "input[data-testid*='code' i]",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "input[data-testid*='otp' i]",
    "input[name*='passcode' i]",
    "input[id*='passcode' i]",
)
RBC_COOKIE_ACCEPT_SELECTOR = "#onetrust-accept-btn-handler"
RBC_BOOTSTRAP_KEEP_COOKIE_NAMES = frozenset(
    {
        "optanonalertboxclosed",
        "optanonconsent",
    }
)
RBC_CREDENTIAL_LOCK_GRACE_SECONDS = 8.0
RBC_WAIT_POLL_SECONDS = 0.35
RBC_CREDENTIAL_CAPTURE_POLL_SECONDS = 0.15
RBC_CREDENTIAL_CAPTURE_BACKGROUND_POLL_SECONDS = 0.2
RBC_CREDENTIAL_CAPTURE_BACKGROUND_IDLE_POLL_SECONDS = 0.4
RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS = 75
RBC_CREDENTIAL_CAPTURE_REQUEST_BURST_SECONDS = 1.5 if RBC_WINDOWS_FLOW else 0.45
RBC_CREDENTIAL_CAPTURE_REQUEST_BURST_POLL_SECONDS = 0.03 if RBC_WINDOWS_FLOW else 0.05
RBC_CREDENTIAL_CAPTURE_TASK_DRAIN_SECONDS = 2.0
RBC_WINDOWS_LOGIN_ROUTE_CAPTURE_TIMEOUT_SECONDS = 1.0
RBC_WINDOWS_LOGIN_ROUTE_PATTERN = "**/cgi-bin/rbaccess/**"
RBC_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS = 8.0
RBC_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS = 4.0
RBC_GENERATED_SECRET_MAX_LENGTH = 512
RBC_GENERATED_SECRET_PAYLOAD_RE = re.compile(r"^(?:\d+,)?eyJ[A-Za-z0-9+/=_-]{80,}$")
RBC_GENERATED_SECRET_HEX_RE = re.compile(r"^[0-9a-fA-F]{160,}$")


@dataclass
class RBCCredentialCaptureState:
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    accepting_tasks: bool = True
    task_scheduled_count: int = 0
    task_completed_count: int = 0
    task_failed_count: int = 0
    task_cancelled_count: int = 0
    task_rejected_count: int = 0
    task_error_types: set[str] = field(default_factory=set)
    cdp_install_attempt_count: int = 0
    cdp_install_success_count: int = 0
    cdp_install_failure_count: int = 0
    cdp_install_error_types: set[str] = field(default_factory=set)
    cdp_rbc_post_count: int = 0
    request_rbc_post_count: int = 0
    cdp_login_post_count: int = 0
    request_login_post_count: int = 0
    cdp_post_data_inline_count: int = 0
    cdp_post_data_retrieved_count: int = 0
    cdp_post_data_missing_count: int = 0
    request_post_data_inline_count: int = 0
    request_post_data_missing_count: int = 0
    credential_post_count: int = 0
    cdp_credential_post_count: int = 0
    request_credential_post_count: int = 0
    post_data_fetch_error_types: set[str] = field(default_factory=set)
    request_capture_error_types: set[str] = field(default_factory=set)
    route_install_attempt_count: int = 0
    route_install_success_count: int = 0
    route_install_failure_count: int = 0
    route_install_error_types: set[str] = field(default_factory=set)
    route_login_post_count: int = 0
    route_capture_error_types: set[str] = field(default_factory=set)
    field_observations: dict[str, dict[str, set[str]]] = field(
        default_factory=lambda: {
            source: {"K1": set(), "QQ": set(), "Q1": set()}
            for source in ("cdp_request", "request", "route")
        }
    )
    drain_started_count: int = 0
    drain_completed_count: int = 0
    drain_timeout_count: int = 0
    drain_initial_pending_count: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "capture_task_scheduled_count": self.task_scheduled_count,
            "capture_task_completed_count": self.task_completed_count,
            "capture_task_failed_count": self.task_failed_count,
            "capture_task_cancelled_count": self.task_cancelled_count,
            "capture_task_rejected_count": self.task_rejected_count,
            "capture_task_pending_count": len(self.tasks),
            "capture_task_error_types": sorted(self.task_error_types),
            "capture_task_drain_started_count": self.drain_started_count,
            "capture_task_drain_completed_count": self.drain_completed_count,
            "capture_task_drain_timeout_count": self.drain_timeout_count,
            "capture_task_drain_initial_pending_count": self.drain_initial_pending_count,
            "cdp_install_attempt_count": self.cdp_install_attempt_count,
            "cdp_install_success_count": self.cdp_install_success_count,
            "cdp_install_failure_count": self.cdp_install_failure_count,
            "cdp_install_error_types": sorted(self.cdp_install_error_types),
            "cdp_request_listener_installed": self.cdp_install_success_count > 0,
            "cdp_rbc_post_count": self.cdp_rbc_post_count,
            "request_rbc_post_count": self.request_rbc_post_count,
            "cdp_login_post_count": self.cdp_login_post_count,
            "request_login_post_count": self.request_login_post_count,
            "cdp_post_data_inline_count": self.cdp_post_data_inline_count,
            "cdp_post_data_retrieved_count": self.cdp_post_data_retrieved_count,
            "cdp_post_data_missing_count": self.cdp_post_data_missing_count,
            "request_post_data_inline_count": self.request_post_data_inline_count,
            "request_post_data_missing_count": self.request_post_data_missing_count,
            "credential_post_count": self.credential_post_count,
            "cdp_credential_post_count": self.cdp_credential_post_count,
            "request_credential_post_count": self.request_credential_post_count,
            "post_data_fetch_error_types": sorted(self.post_data_fetch_error_types),
            "request_capture_error_types": sorted(self.request_capture_error_types),
            "route_install_attempt_count": self.route_install_attempt_count,
            "route_install_success_count": self.route_install_success_count,
            "route_install_failure_count": self.route_install_failure_count,
            "route_install_error_types": sorted(self.route_install_error_types),
            "route_login_post_count": self.route_login_post_count,
            "route_capture_error_types": sorted(self.route_capture_error_types),
            "windows_login_route_listener_installed": self.route_install_success_count > 0,
            "credential_field_observations": {
                source: {
                    key: sorted(values) if values else ["missing"]
                    for key, values in fields.items()
                }
                for source, fields in self.field_observations.items()
            },
        }


def _schedule_rbc_capture_task(
    capture_state: RBCCredentialCaptureState,
    coroutine: Coroutine[Any, Any, Any],
) -> asyncio.Task[Any] | None:
    if not capture_state.accepting_tasks:
        capture_state.task_rejected_count += 1
        coroutine.close()
        return None
    try:
        task = asyncio.create_task(coroutine)
    except RuntimeError:
        capture_state.task_rejected_count += 1
        coroutine.close()
        return None
    capture_state.task_scheduled_count += 1
    capture_state.tasks.add(task)

    def capture_done(completed_task: asyncio.Task[Any]) -> None:
        capture_state.tasks.discard(completed_task)
        if completed_task.cancelled():
            capture_state.task_cancelled_count += 1
            return
        error = completed_task.exception()
        if error is None:
            capture_state.task_completed_count += 1
            return
        capture_state.task_failed_count += 1
        capture_state.task_error_types.add(type(error).__name__)

    task.add_done_callback(capture_done)
    return task


async def _drain_rbc_capture_tasks(
    capture_state: RBCCredentialCaptureState,
    *,
    timeout_seconds: float = RBC_CREDENTIAL_CAPTURE_TASK_DRAIN_SECONDS,
) -> bool:
    capture_state.accepting_tasks = False
    capture_state.drain_started_count += 1
    capture_state.drain_initial_pending_count = len(capture_state.tasks)
    if not capture_state.tasks:
        capture_state.drain_completed_count += 1
        return True
    done, pending = await asyncio.wait(
        set(capture_state.tasks),
        timeout=max(0.0, timeout_seconds),
    )
    del done
    if not pending:
        capture_state.drain_completed_count += 1
        return True
    capture_state.drain_timeout_count += 1
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return False
RBC_ENTRY_NAVIGATION_ATTEMPTS = 3
RBC_ENTRY_NAVIGATION_RETRY_SECONDS = 2.0
RBC_ACCOUNT_LIST_FETCH_TIMEOUT_SECONDS = 0.75
RBC_ACCOUNT_SHELL_PAYLOAD_GRACE_SECONDS = 8.0
RBC_ACCOUNT_SHELL_PAYLOAD_GRACE_POLL_SECONDS = 0.4
RBC_PAGE_REPLACEMENT_RETRY_SECONDS = 5
RBC_PAGE_REPLACEMENT_POLL_SECONDS = 0.25
RBC_STORAGE_STATE_TIMEOUT_SECONDS = 2
RBC_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS = 1
RBC_LOCAL_STORAGE_TIMEOUT_SECONDS = 0.75
RBC_HAR_FILENAME_PREFIX = "rbc_visible_auth"
RBC_MAX_SANITIZED_HARS_PER_USER = 3
RBC_REDACTED_VALUE = "<redacted>"
RBC_HAR_SAFE_RESPONSE_KEYS = frozenset(
    {
        "active",
        "code",
        "error",
        "errorcode",
        "haserror",
        "message",
        "reason",
        "result",
        "status",
        "statuscode",
        "type",
    }
)
RBC_HAR_SENSITIVE_HEADER_NAMES = frozenset(
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
RBC_RETRYABLE_ENTRY_NAVIGATION_ERRORS = (
    ("err_name_not_resolved", "dns_name_not_resolved"),
    ("err_name_resolution_failed", "dns_resolution_failed"),
    ("err_network_changed", "network_changed"),
    ("err_internet_disconnected", "internet_disconnected"),
    ("err_connection_timed_out", "connection_timed_out"),
)
RBC_COOKIE_BANNER_SCRIPT = """
(() => {
    const host = String(window.location?.hostname || "").trim().toLowerCase();
    if (!host.endsWith("royalbank.com") && !host.endsWith("rbc.com")) {
        return;
    }
    const selectors = [
        "#onetrust-banner-sdk",
        "#onetrust-consent-sdk",
        "#onetrust-pc-sdk",
        ".onetrust-pc-dark-filter",
        ".ot-sdk-container",
    ];
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


def _rbc_host_matches(url: str | None) -> bool:
    try:
        host = urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return False
    return any(marker in host for marker in RBC_PAGE_URL_MARKERS)


def _rbc_origin_from_url(url: str | None) -> str | None:
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    if _rbc_host_matches(url):
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _rbc_sanitize_url(url: str | None) -> str:
    return visible_auth.sanitize_url(url, redacted_value=RBC_REDACTED_VALUE)


def page_label(page) -> str:
    return visible_auth.page_label(page, url_sanitizer=_rbc_sanitize_url)


def _rbc_browser_launch_kwargs(browser_runtime: dict[str, Any] | None) -> dict[str, Any]:
    return visible_auth.managed_browser_launch_kwargs(
        browser_runtime,
        windows_managed_brave_stabilizers=RBC_WINDOWS_FLOW,
        mac_managed_brave_stabilizers=RBC_MAC_FLOW,
    )


def _rbc_entry_navigation_retry_reason(exc: Exception) -> str:
    lowered = str(exc or "").lower()
    for marker, reason in RBC_RETRYABLE_ENTRY_NAVIGATION_ERRORS:
        if marker in lowered:
            return reason
    return ""


def _rbc_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return RBC_DESKTOP_AUTH_RAW_HAR_DIR / f"{RBC_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"


def _rbc_sanitized_har_path(user_id: int, timestamp_slug: str, result: str) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(
        VISIBLE_AUTH_CLIENT.sync_id or VISIBLE_AUTH_SYNC_ID,
        ATTEMPT_ID,
    )
    scope_part = f"_{scope}" if scope else ""
    return RBC_DIAGNOSTIC_DIR / f"{RBC_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"


def _rbc_should_keep_response_body(url: str, status: int | None, content_type: str) -> bool:
    if not _rbc_host_matches(url):
        return False
    lowered_content_type = content_type.lower()
    if RBC_DEV_CAPTURE_LOCAL:
        return "json" in lowered_content_type or "text" in lowered_content_type
    return int(status or 0) >= 400 and ("json" in lowered_content_type or "text" in lowered_content_type)


def finalize_rbc_har_capture(raw_har_path: Path, *, user_id: int, timestamp_slug: str, result: str) -> Path | None:
    sanitized_path = _rbc_sanitized_har_path(user_id, timestamp_slug, result)
    pattern = f"{RBC_HAR_FILENAME_PREFIX}_user{user_id}_*.har"
    sanitized_paths = sorted(
        RBC_DIAGNOSTIC_DIR.glob(pattern),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=sanitized_paths,
        keep=RBC_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_rbc_should_keep_response_body,
        safe_response_keys=RBC_HAR_SAFE_RESPONSE_KEYS,
        sensitive_header_names=RBC_HAR_SENSITIVE_HEADER_NAMES,
        capture_level=VISIBLE_AUTH_CAPTURE_LEVEL,
        redacted_value=RBC_REDACTED_VALUE,
    )


class PopupInteractionLock(visible_auth.PopupInteractionLock):
    def __init__(self, page, cdp_session) -> None:
        super().__init__(page, cdp_session, lock_attribute_name="_rbc_popup_lock")


async def _bind_rbc_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    await visible_auth.bind_popup_lock_to_page(context, popup_lock, page)


def _schedule_rbc_popup_lock(page) -> None:
    popup_lock = getattr(page, "_rbc_popup_lock", None)
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


async def get_viable_rbc_page(context, current_page, popup_lock: PopupInteractionLock | None = None):
    deadline = asyncio.get_running_loop().time() + RBC_PAGE_REPLACEMENT_RETRY_SECONDS
    current_raw_page = getattr(current_page, "raw_page", current_page)
    while True:
        try:
            pages = [page for page in context.pages if not visible_auth.is_page_closed(page)]
        except Exception as exc:
            raise RuntimeError(f"RBC browser context is no longer available: {exc}") from exc
        if pages:
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
        await asyncio.sleep(RBC_PAGE_REPLACEMENT_POLL_SECONDS)

    rbc_pages = [
        page
        for page in pages
        if _rbc_host_matches(visible_auth.safe_page_url(page))
    ]
    if rbc_pages:
        replacement = rbc_pages[-1]
        if current_raw_page is replacement:
            return current_page
        await _bind_rbc_popup_lock_to_page(context, popup_lock, replacement)
        if current_page is not None:
            await log_support_event(
                stage="page replacement",
                debug=True,
                message="RBC page handle replaced.",
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
    await _bind_rbc_popup_lock_to_page(context, popup_lock, replacement)
    if current_page is not None:
        await log_support_event(
            stage="page replacement",
            debug=True,
            message="RBC page handle replaced.",
            details={
                "from_page": page_label(current_page),
                "to_page": page_label(replacement),
            },
        )
    return popup_lock.page if popup_lock is not None else replacement


async def _goto_rbc_entry(
    page,
    url: str,
    *,
    stage: str,
    timeout_ms: int = 90000,
    wait_until: str = "domcontentloaded",
) -> None:
    await log_support_event(
        stage=stage,
        debug=True,
        message=f"RBC entry step: {stage}.",
        details={"entry_url": _rbc_sanitize_url(url), "wait_until": wait_until},
    )
    for attempt_number in range(1, RBC_ENTRY_NAVIGATION_ATTEMPTS + 1):
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            break
        except Exception as exc:
            retry_reason = _rbc_entry_navigation_retry_reason(exc)
            if not retry_reason or attempt_number >= RBC_ENTRY_NAVIGATION_ATTEMPTS:
                raise
            await log_support_event(
                stage=f"{stage}_retry",
                level="warning",
                debug=True,
                message="RBC entry navigation failed before the login page loaded. Retrying.",
                details={
                    "entry_url": _rbc_sanitize_url(url),
                    "wait_until": wait_until,
                    "attempt": attempt_number,
                    "max_attempts": RBC_ENTRY_NAVIGATION_ATTEMPTS,
                    "retry_reason": retry_reason,
                },
            )
            await asyncio.sleep(RBC_ENTRY_NAVIGATION_RETRY_SECONDS)
    await log_support_event(
        stage=f"{stage}_ok",
        debug=True,
        message=f"RBC entry success: {stage}.",
        details={"page_url": page_label(page), "wait_until": wait_until},
    )


async def open_rbc_login_page(page, *, entry_url: str) -> None:
    wait_until = "domcontentloaded" if VISIBLE_AUTH_ADD_FLOW else "commit"
    await _goto_rbc_entry(page, entry_url, stage="login_entry", wait_until=wait_until)
    await _dismiss_rbc_cookie_banner(page)


async def any_selector_is_visible(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await visible_auth.selector_is_visible(page, selector):
            return True
    return False


async def _dismiss_rbc_cookie_banner(page) -> None:
    base_page = getattr(page, "raw_page", page)
    try:
        await base_page.evaluate(RBC_COOKIE_BANNER_SCRIPT)
    except Exception:
        pass
    try:
        accept_button = await base_page.query_selector(RBC_COOKIE_ACCEPT_SELECTOR)
    except Exception:
        accept_button = None
    if not accept_button:
        return
    try:
        await accept_button.evaluate(visible_auth.LOCKED_CLICK_SCRIPT)
    except Exception:
        pass


def _parse_rbc_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _rbc_account_list_has_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    error_state = result.get("errorState") if isinstance(result.get("errorState"), dict) else {}
    return bool(error_state.get("hasError"))


def _rbc_account_list_has_accounts(result: Any) -> bool:
    if not isinstance(result, dict) or _rbc_account_list_has_error(result):
        return False
    for category_key in ("depositAccounts", "creditCards", "linesLoans", "mortgages", "investments"):
        category = result.get(category_key) if isinstance(result.get(category_key), dict) else {}
        if isinstance(category.get("accounts"), list) and category.get("accounts"):
            return True
    return False


async def _fetch_rbc_account_list_payload(page) -> dict[str, Any] | None:
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                f"""
                async () => {{
                    try {{
                        const response = await fetch('{RBC_ACCOUNT_LIST_URL}', {{
                            credentials: 'include',
                            headers: {{ 'accept': 'application/json' }},
                        }});
                        if (!response.ok) return null;
                        return await response.json();
                    }} catch (_) {{
                        return null;
                    }}
                }}
                """
            ),
            timeout=RBC_ACCOUNT_LIST_FETCH_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _rbc_authenticated_app_route(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").lower()
    return host == "www1.royalbank.com" and path.startswith("/sgw1/olb/")


async def _rbc_page_summary(page) -> dict[str, Any]:
    base_page = getattr(page, "raw_page", page)
    accounts_payload = getattr(base_page, "_rbc_account_list_payload", None)
    current_url = visible_auth.safe_page_url(base_page)
    summary_dom_visible = await visible_auth.selector_is_visible(
        page,
        "rbc-account-summary-layout, [data-testid='account-summary-tabs'], [data-testid='account-list-widget'], rbc-account-list-widget",
    )
    summary_route = _rbc_authenticated_app_route(current_url)
    account_shell_visible = summary_route or summary_dom_visible
    if account_shell_visible and not _rbc_account_list_has_accounts(accounts_payload):
        accounts_payload = await _fetch_rbc_account_list_payload(page)
        if _rbc_account_list_has_accounts(accounts_payload):
            setattr(base_page, "_rbc_account_list_payload", accounts_payload)
    try:
        parsed_url = urlsplit(current_url)
    except ValueError:
        parsed_url = None
    host = (parsed_url.hostname or "") if parsed_url else ""
    path = (parsed_url.path or "") if parsed_url else ""
    hash_path = (parsed_url.fragment or "").split("?", 1)[0] if parsed_url else ""
    mfa_method = ""
    if parsed_url:
        search_params = dict(parse_qsl(parsed_url.query or ""))
        hash_query = parsed_url.fragment.split("?", 1)[1] if "?" in parsed_url.fragment else ""
        hash_params = dict(parse_qsl(hash_query))
        mfa_method = str(search_params.get("mfaMethodName") or hash_params.get("mfaMethodName") or "").upper()
    login_visible = await any_selector_is_visible(page, RBC_USERNAME_SELECTORS) and await any_selector_is_visible(page, RBC_PASSWORD_SELECTORS)
    is_mfa_app_url = host == "omni.royalbank.com" and "/statics/rbc-mfa-app/" in path
    mfa_notification_visible = (
        await visible_auth.selector_is_visible(page, "core-banking-trusted-device-notification, core-banking-notification-view, [data-testid='notification_button'][data-dig-id='MFA_PMSM_001']")
        or hash_path.endswith("/notification")
        or mfa_method == "MFA_NOTIFICATION"
    )
    return {
        "url": current_url,
        "login_visible": login_visible,
        "signin_url": any(marker in current_url.lower() for marker in ("clientsignin", "signin", "sign-in", "login")),
        "mfa_app_visible": is_mfa_app_url or await visible_auth.selector_is_visible(page, "core-banking-root"),
        "mfa_method": mfa_method,
        "mfa_notification_visible": mfa_notification_visible,
        "mfa_code_visible": await any_selector_is_visible(page, RBC_OTP_INPUT_SELECTORS),
        "summary_route": summary_route,
        "summary_dom_visible": summary_dom_visible,
        "account_shell_visible": account_shell_visible,
        "accounts_payload_ready": _rbc_account_list_has_accounts(accounts_payload),
        "auth_error_visible": await visible_auth.selector_is_visible(page, "[role='alert'], .rbc-alert--danger, rbc-alert.rbc-alert--danger, [aria-invalid='true'], .error, .errorMessage"),
        "transient_error_visible": await visible_auth.selector_is_visible(page, "rbc-error-page, rbc-account-summary-error, rbc-alert.rbc-alert--danger, .rbc-alert--danger"),
    }


async def get_rbc_page_state(page) -> dict[str, Any]:
    summary = await _rbc_page_summary(page)
    accounts_payload_ready = bool(summary.get("accounts_payload_ready"))
    return {
        **summary,
        "authenticated": bool(accounts_payload_ready),
        "invalid_credentials": bool(summary.get("login_visible") and summary.get("auth_error_visible")),
    }


def describe_rbc_state(state: dict[str, Any]) -> str:
    if state.get("authenticated"):
        return "Authenticated RBC session detected. Saving session for background sync."
    if state.get("invalid_credentials"):
        return "RBC showed a credential error. Popup interaction is unlocked so you can correct the credentials and continue."
    if state.get("mfa_notification_visible"):
        return "Waiting for RBC app approval in the secure browser."
    if state.get("mfa_code_visible"):
        return "Waiting for RBC security code entry in the secure browser."
    if state.get("login_visible"):
        return "Waiting for manual sign-in at the RBC login page."
    if state.get("mfa_app_visible"):
        return "Waiting for RBC secure verification to advance."
    if state.get("account_shell_visible"):
        return "Waiting for RBC account data to become available."
    return "Waiting for RBC secure login to advance."


def _install_rbc_response_capture(page) -> None:
    if getattr(page, "_rbc_response_capture_installed", False):
        return
    setattr(page, "_rbc_response_capture_installed", True)

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        if "accountListSummary" not in url:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def capture_response() -> None:
            try:
                if int(getattr(response, "status", 0) or 0) != 200:
                    return
                payload = _parse_rbc_json_body(await response.text())
                if _rbc_account_list_has_accounts(payload):
                    setattr(page, "_rbc_account_list_payload", payload)
            except Exception:
                return

        loop.create_task(capture_response())

    page.on("response", on_response)


def _is_rbc_masked_secret(value: str | None) -> bool:
    text = str(value or "").strip()
    return len(text) >= 3 and all(character in RBC_MASK_CHARACTERS for character in text)


def _is_rbc_generated_secret_payload(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) > RBC_GENERATED_SECRET_MAX_LENGTH:
        return True
    return bool(
        RBC_GENERATED_SECRET_PAYLOAD_RE.fullmatch(text)
        or RBC_GENERATED_SECRET_HEX_RE.fullmatch(text)
    )


def _rbc_credential_mapping_from_post_data(post_data: str) -> dict[str, Any]:
    parsed = _parse_rbc_json_body(post_data)
    if isinstance(parsed, dict):
        return parsed
    return dict(parse_qsl(post_data, keep_blank_values=True))


def _rbc_credential_field_classifications(mapping: dict[str, Any]) -> dict[str, str]:
    normalized = {str(key).upper(): value for key, value in mapping.items()}
    classifications: dict[str, str] = {}
    for key in ("K1", "QQ", "Q1"):
        value = str(normalized.get(key) or "")
        if not value:
            classifications[key] = "missing"
        elif _is_rbc_masked_secret(value):
            classifications[key] = "masked"
        elif _is_rbc_generated_secret_payload(value):
            classifications[key] = "generated"
        else:
            classifications[key] = "raw"
    return classifications


def _rbc_is_login_request_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        "rbunxcgi" in lowered
        or "rbcgi3m01" in lowered
        or "request=clientsignin" in lowered
    )


def _record_rbc_post_data_observation(
    capture_state: RBCCredentialCaptureState,
    *,
    post_data: str,
    source: str,
) -> bool:
    mapping = _rbc_credential_mapping_from_post_data(post_data)
    classifications = _rbc_credential_field_classifications(mapping)
    credential_post = any(value != "missing" for value in classifications.values())
    if not credential_post:
        return False
    capture_state.credential_post_count += 1
    if source == "cdp_request":
        capture_state.cdp_credential_post_count += 1
    elif source == "request":
        capture_state.request_credential_post_count += 1
    source_observations = capture_state.field_observations[source]
    for key, classification in classifications.items():
        source_observations[key].add(classification)
    return True


def _capture_rbc_username(
    captured_credentials: dict[str, str],
    username: str,
    *,
    source: str,
) -> None:
    normalized = str(username or "").strip()
    if not normalized:
        return
    if captured_credentials.get("username") != normalized:
        captured_credentials["username"] = normalized
        key = f"_{source}_username_capture_count"
        captured_credentials[key] = str(int(captured_credentials.get(key) or 0) + 1)


def _capture_rbc_password(
    captured_credentials: dict[str, str],
    password: str,
    *,
    source: str,
) -> None:
    value = str(password or "")
    if not value:
        return
    if _is_rbc_masked_secret(value):
        key = f"_{source}_masked_password_count"
        captured_credentials[key] = str(int(captured_credentials.get(key) or 0) + 1)
        return
    if _is_rbc_generated_secret_payload(value):
        key = f"_{source}_generated_password_count"
        captured_credentials[key] = str(int(captured_credentials.get(key) or 0) + 1)
        return
    if captured_credentials.get("password") != value:
        captured_credentials["password"] = value
        key = f"_{source}_password_capture_count"
        captured_credentials[key] = str(int(captured_credentials.get(key) or 0) + 1)


def _credentials_from_mapping(mapping: dict[str, Any]) -> tuple[str, str] | None:
    username = ""
    password = ""
    for key in ("K1", "k1", "username", "userName", "clientCard", "client_card"):
        value = mapping.get(key)
        if value:
            username = str(value).strip()
            break
    password_fallback = ""
    for key in ("QQ", "qq", "Q1", "q1", "password", "passcode"):
        value = mapping.get(key)
        if value:
            candidate = str(value)
            if _is_rbc_generated_secret_payload(candidate):
                continue
            if not password_fallback:
                password_fallback = candidate
            if not _is_rbc_masked_secret(candidate):
                password = candidate
                break
    if not password:
        password = password_fallback
    if username and password:
        return username, password
    return None


def _capture_rbc_credentials_from_post_data(
    captured_credentials: dict[str, str],
    *,
    post_data: str,
    source: str,
    capture_state: RBCCredentialCaptureState | None = None,
) -> bool:
    if not post_data:
        return False

    mapping = _rbc_credential_mapping_from_post_data(post_data)
    credential_request = (
        _record_rbc_post_data_observation(capture_state, post_data=post_data, source=source)
        if capture_state is not None
        else any(key in {str(item).upper() for item in mapping} for key in ("K1", "QQ", "Q1"))
    )
    credential_pair = _credentials_from_mapping(mapping)
    if credential_pair is None:
        return credential_request
    username, password = credential_pair
    _capture_rbc_username(captured_credentials, username, source=source)
    _capture_rbc_password(captured_credentials, password, source=source)
    return credential_request


def _capture_rbc_credentials_from_request(
    captured_credentials: dict[str, str],
    request,
    *,
    post_data: str | None = None,
    capture_state: RBCCredentialCaptureState | None = None,
) -> bool:
    method = str(getattr(request, "method", "") or "").upper()
    if method != "POST":
        return False
    url = str(getattr(request, "url", "") or "")
    if not _rbc_host_matches(url):
        return False
    if post_data is None:
        post_data = visible_auth.safe_request_post_data(request)
    if not post_data:
        return False
    return _capture_rbc_credentials_from_post_data(
        captured_credentials,
        post_data=post_data,
        source="request",
        capture_state=capture_state,
    )


async def _install_rbc_windows_cdp_request_capture(
    context,
    page,
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState | None = None,
) -> None:
    if not RBC_WINDOWS_FLOW or getattr(page, "_rbc_windows_cdp_request_capture_installed", False):
        return
    capture_state = capture_state or RBCCredentialCaptureState()
    capture_state.cdp_install_attempt_count += 1
    try:
        session = await context.new_cdp_session(page)
        await session.send("Network.enable")
    except Exception as exc:
        capture_state.cdp_install_failure_count += 1
        capture_state.cdp_install_error_types.add(type(exc).__name__)
        return

    async def capture_cdp_request(params: dict[str, Any]) -> None:
        request = params.get("request") if isinstance(params, dict) else None
        if not isinstance(request, dict):
            return
        method = str(request.get("method") or "").upper()
        url = str(request.get("url") or "")
        if method != "POST" or not _rbc_host_matches(url):
            return
        capture_state.cdp_rbc_post_count += 1
        if _rbc_is_login_request_url(url):
            capture_state.cdp_login_post_count += 1
        post_data = str(request.get("postData") or "")
        if post_data:
            capture_state.cdp_post_data_inline_count += 1
        elif request.get("hasPostData") and params.get("requestId"):
            try:
                body = await session.send(
                    "Network.getRequestPostData",
                    {"requestId": str(params.get("requestId"))},
                )
            except Exception as exc:
                capture_state.post_data_fetch_error_types.add(type(exc).__name__)
                body = {}
            if isinstance(body, dict):
                post_data = str(body.get("postData") or "")
            if post_data:
                capture_state.cdp_post_data_retrieved_count += 1
        if not post_data:
            capture_state.cdp_post_data_missing_count += 1
            return
        credential_request = _capture_rbc_credentials_from_post_data(
            captured_credentials,
            post_data=post_data,
            source="cdp_request",
            capture_state=capture_state,
        )
        if credential_request and not _rbc_has_unmasked_password(captured_credentials):
            await _capture_rbc_raw_input_credentials_burst(page, captured_credentials)

    def on_request_will_be_sent(params) -> None:
        request = params.get("request") if isinstance(params, dict) else None
        if not isinstance(request, dict):
            return
        if str(request.get("method") or "").upper() != "POST":
            return
        if not _rbc_host_matches(str(request.get("url") or "")):
            return
        _schedule_rbc_capture_task(capture_state, capture_cdp_request(params))

    try:
        session.on("Network.requestWillBeSent", on_request_will_be_sent)
    except Exception as exc:
        capture_state.cdp_install_failure_count += 1
        capture_state.cdp_install_error_types.add(type(exc).__name__)
        return
    setattr(page, "_rbc_windows_cdp_request_capture_session", session)
    setattr(page, "_rbc_windows_cdp_request_capture_installed", True)
    capture_state.cdp_install_success_count += 1


async def _install_rbc_windows_login_route_capture(
    context,
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState,
) -> None:
    if not RBC_WINDOWS_FLOW or getattr(context, "_rbc_windows_login_route_capture_installed", False):
        return
    capture_state.route_install_attempt_count += 1

    async def capture_login_route(route, request) -> None:
        try:
            method = str(getattr(request, "method", "") or "").upper()
            url = str(getattr(request, "url", "") or "")
            if method != "POST" or not _rbc_host_matches(url) or not _rbc_is_login_request_url(url):
                return
            capture_state.route_login_post_count += 1
            request_frame = getattr(request, "frame", None)
            request_page = getattr(request_frame, "page", None)
            if request_page is not None and not visible_auth.is_page_closed(request_page):
                try:
                    async with asyncio.timeout(RBC_WINDOWS_LOGIN_ROUTE_CAPTURE_TIMEOUT_SECONDS):
                        await _capture_rbc_raw_input_credentials(
                            request_page,
                            captured_credentials,
                            timeout_ms=RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS,
                            include_hidden_fallback=True,
                        )
                except Exception as exc:
                    capture_state.route_capture_error_types.add(type(exc).__name__)
            post_data = visible_auth.safe_request_post_data(request)
            if post_data:
                _capture_rbc_credentials_from_post_data(
                    captured_credentials,
                    post_data=post_data,
                    source="route",
                    capture_state=capture_state,
                )
        except Exception as exc:
            capture_state.route_capture_error_types.add(type(exc).__name__)
        finally:
            try:
                await route.continue_()
            except Exception as exc:
                capture_state.route_capture_error_types.add(type(exc).__name__)

    try:
        await context.route(RBC_WINDOWS_LOGIN_ROUTE_PATTERN, capture_login_route)
    except Exception as exc:
        capture_state.route_install_failure_count += 1
        capture_state.route_install_error_types.add(type(exc).__name__)
        return
    setattr(context, "_rbc_windows_login_route_capture_installed", True)
    capture_state.route_install_success_count += 1


def _install_rbc_request_capture(
    page,
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState | None = None,
) -> None:
    if getattr(page, "_rbc_request_capture_installed", False):
        return
    setattr(page, "_rbc_request_capture_installed", True)

    def on_request(request) -> None:
        try:
            method = str(getattr(request, "method", "") or "").upper()
            url = str(getattr(request, "url", "") or "")
            if method != "POST" or not _rbc_host_matches(url):
                return
            if capture_state is not None:
                capture_state.request_rbc_post_count += 1
                if _rbc_is_login_request_url(url):
                    capture_state.request_login_post_count += 1
            post_data = visible_auth.safe_request_post_data(request)
            if not post_data:
                if capture_state is not None:
                    capture_state.request_post_data_missing_count += 1
                return
            if capture_state is not None:
                capture_state.request_post_data_inline_count += 1
            credential_request = _capture_rbc_credentials_from_request(
                captured_credentials,
                request,
                post_data=post_data,
                capture_state=capture_state,
            )
            if credential_request and not _rbc_has_unmasked_password(captured_credentials):
                if capture_state is not None:
                    _schedule_rbc_capture_task(
                        capture_state,
                        _capture_rbc_raw_input_credentials_burst(page, captured_credentials),
                    )
                else:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_capture_rbc_raw_input_credentials_burst(page, captured_credentials))
                    except RuntimeError:
                        pass
        except Exception as exc:
            if capture_state is not None:
                capture_state.request_capture_error_types.add(type(exc).__name__)
            return

    page.on("request", on_request)


def bind_rbc_page_watcher(
    page,
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState | None = None,
) -> None:
    if getattr(page, "_rbc_page_watcher_installed", False):
        return
    setattr(page, "_rbc_page_watcher_installed", True)
    setattr(page, "_rbc_navigation_token", int(getattr(page, "_rbc_navigation_token", 0) or 0))

    def on_frame_navigated(frame) -> None:
        try:
            if frame == page.main_frame:
                current_token = int(getattr(page, "_rbc_navigation_token", 0) or 0)
                setattr(page, "_rbc_navigation_token", current_token + 1)
                _schedule_rbc_popup_lock(page)
        except Exception:
            return

    page.on("framenavigated", on_frame_navigated)
    _install_rbc_request_capture(page, captured_credentials, capture_state)
    _install_rbc_response_capture(page)


async def collect_storage_state(context) -> dict[str, Any]:
    return await visible_auth.collect_storage_state(
        context,
        has_material=storage_state_has_rbc_material,
        origin_from_url=_rbc_origin_from_url,
        storage_state_timeout_seconds=RBC_STORAGE_STATE_TIMEOUT_SECONDS,
        fallback_timeout_seconds=RBC_STORAGE_STATE_FALLBACK_TIMEOUT_SECONDS,
        local_storage_timeout_seconds=RBC_LOCAL_STORAGE_TIMEOUT_SECONDS,
        indexed_db=False,
    )


def storage_state_has_rbc_material(storage_state: dict[str, Any] | None) -> bool:
    if not isinstance(storage_state, dict):
        return False
    cookies = storage_state.get("cookies") if isinstance(storage_state.get("cookies"), list) else []
    origins = storage_state.get("origins") if isinstance(storage_state.get("origins"), list) else []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if any(marker in domain for marker in RBC_PAGE_URL_MARKERS):
            return True
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _rbc_origin_from_url(origin.get("origin")):
            return True
    return False


def sanitize_rbc_bootstrap_storage_state(
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

    cookies = sanitized.get("cookies") if isinstance(sanitized.get("cookies"), list) else []
    kept_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        name = str(cookie.get("name") or "").strip().lower()
        if any(marker in domain for marker in RBC_PAGE_URL_MARKERS):
            if name in RBC_BOOTSTRAP_KEEP_COOKIE_NAMES:
                details["kept_non_auth_cookies"] += 1
                kept_cookies.append(cookie)
            else:
                details["removed_auth_cookies"] += 1
            continue
        kept_cookies.append(cookie)
    sanitized["cookies"] = kept_cookies

    origins = sanitized.get("origins") if isinstance(sanitized.get("origins"), list) else []
    kept_origins: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if _rbc_origin_from_url(origin.get("origin")):
            details["removed_auth_origins"] += 1
            continue
        kept_origins.append(origin)
    sanitized["origins"] = kept_origins

    if not storage_state_has_rbc_material(sanitized):
        return None, details
    return sanitized, details


async def collect_rbc_session_artifact(*, user_agent: str = "") -> dict[str, Any]:
    artifact: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    normalized_user_agent = str(user_agent or "").strip()
    if normalized_user_agent:
        artifact["user_agent"] = normalized_user_agent
    return artifact


async def _rbc_input_targets(page) -> list[Any]:
    targets: list[Any] = [page]
    try:
        main_frame = page.main_frame
    except Exception:
        main_frame = None
    for frame in getattr(page, "frames", []) or []:
        if frame is main_frame:
            continue
        targets.append(frame)
    return targets


async def _rbc_first_input_value(
    page,
    selectors: tuple[str, ...],
    *,
    visible_only: bool = False,
    reject_transformed: bool = False,
    timeout_ms: int = 250,
) -> str:
    targets = await _rbc_input_targets(page)
    fallback = ""
    fallback_is_transformed = False
    selector_group = ", ".join(selectors)
    for target in targets:
        try:
            locator = target.locator(selector_group)
            count = min(await locator.count(), 8)
        except Exception:
            continue
        for index in range(count):
            input_locator = locator.nth(index)
            visible = False
            if visible_only:
                try:
                    visible = bool(await input_locator.is_visible(timeout=timeout_ms))
                except Exception:
                    visible = False
                if not visible:
                    continue
            try:
                value = str(await input_locator.input_value(timeout=timeout_ms) or "")
            except Exception:
                continue
            if not value:
                continue
            value_is_transformed = _is_rbc_masked_secret(value) or _is_rbc_generated_secret_payload(value)
            if reject_transformed and value_is_transformed:
                fallback = value
                fallback_is_transformed = True
                continue
            if not visible_only or visible:
                return value
            if not fallback or fallback_is_transformed:
                fallback = value
                fallback_is_transformed = value_is_transformed
    if reject_transformed and fallback_is_transformed:
        return ""
    return fallback


async def _rbc_first_visible_input_value(
    page,
    selectors: tuple[str, ...],
    *,
    reject_transformed: bool = False,
    timeout_ms: int = 250,
) -> str:
    return await _rbc_first_input_value(
        page,
        selectors,
        visible_only=True,
        reject_transformed=reject_transformed,
        timeout_ms=timeout_ms,
    )


async def _rbc_first_prefill_input_value(page, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            value = str(await locator.input_value(timeout=100) or "")
        except Exception:
            continue
        if value:
            return value
    return ""


def _rbc_has_unmasked_password(captured_credentials: dict[str, str]) -> bool:
    password = str(captured_credentials.get("password") or "")
    return bool(
        password
        and not _is_rbc_masked_secret(password)
        and not _is_rbc_generated_secret_payload(password)
    )


async def _capture_rbc_raw_input_credentials(
    page,
    captured_credentials: dict[str, str],
    *,
    timeout_ms: int = 250,
    include_hidden_fallback: bool = False,
) -> None:
    if include_hidden_fallback:
        password = await _rbc_first_input_value(
            page,
            RBC_PASSWORD_SELECTORS,
            reject_transformed=True,
            timeout_ms=timeout_ms,
        )
        password_source = "raw_hidden"
    else:
        password = await _rbc_first_visible_input_value(
            page,
            RBC_PASSWORD_SELECTORS,
            reject_transformed=True,
            timeout_ms=timeout_ms,
        )
        password_source = "raw"
    _capture_rbc_password(captured_credentials, password, source=password_source)

    if include_hidden_fallback:
        username = await _rbc_first_input_value(
            page,
            RBC_USERNAME_SELECTORS,
            timeout_ms=timeout_ms,
        )
        username_source = "raw_hidden"
    else:
        username = await _rbc_first_visible_input_value(
            page,
            RBC_USERNAME_SELECTORS,
            timeout_ms=timeout_ms,
        )
        username_source = "raw"
    _capture_rbc_username(captured_credentials, username, source=username_source)


async def _capture_rbc_context_raw_input_credentials(
    context,
    captured_credentials: dict[str, str],
    *,
    timeout_ms: int = RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS,
    include_hidden_fallback: bool = True,
    capture_state: RBCCredentialCaptureState | None = None,
) -> None:
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        return
    for page in reversed(pages):
        if visible_auth.is_page_closed(page):
            continue
        if not _rbc_host_matches(visible_auth.safe_page_url(page)):
            continue
        bind_rbc_page_watcher(page, captured_credentials, capture_state)
        try:
            await _capture_rbc_raw_input_credentials(
                page,
                captured_credentials,
                timeout_ms=timeout_ms,
                include_hidden_fallback=include_hidden_fallback,
            )
        except Exception:
            continue
        if captured_credentials.get("username") and _rbc_has_unmasked_password(captured_credentials):
            return


async def _capture_rbc_raw_input_credentials_burst(page, captured_credentials: dict[str, str]) -> None:
    deadline = asyncio.get_running_loop().time() + RBC_CREDENTIAL_CAPTURE_REQUEST_BURST_SECONDS
    while visible_auth.visible_auth_deadline_active(deadline):
        try:
            await _capture_rbc_raw_input_credentials(
                page,
                captured_credentials,
                timeout_ms=RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS,
                include_hidden_fallback=True,
            )
        except Exception:
            return
        if captured_credentials.get("username") and _rbc_has_unmasked_password(captured_credentials):
            return
        await asyncio.sleep(RBC_CREDENTIAL_CAPTURE_REQUEST_BURST_POLL_SECONDS)


async def _run_rbc_credential_capture_sampler(
    context,
    captured_credentials: dict[str, str],
    stop_event: asyncio.Event,
    capture_state: RBCCredentialCaptureState | None = None,
) -> None:
    while not stop_event.is_set():
        try:
            await _capture_rbc_context_raw_input_credentials(
                context,
                captured_credentials,
                capture_state=capture_state,
            )
        except Exception as exc:
            if visible_auth.is_browser_closed_error(exc):
                return
        interval = (
            RBC_CREDENTIAL_CAPTURE_BACKGROUND_IDLE_POLL_SECONDS
            if captured_credentials.get("username") and _rbc_has_unmasked_password(captured_credentials)
            else RBC_CREDENTIAL_CAPTURE_BACKGROUND_POLL_SECONDS
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def _sleep_with_rbc_credential_capture(page, captured_credentials: dict[str, str]) -> None:
    deadline = asyncio.get_running_loop().time() + RBC_WAIT_POLL_SECONDS
    while visible_auth.visible_auth_deadline_active(deadline):
        await asyncio.sleep(RBC_CREDENTIAL_CAPTURE_POLL_SECONDS)
        await _capture_rbc_raw_input_credentials(
            page,
            captured_credentials,
            timeout_ms=RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS,
            include_hidden_fallback=True,
        )


def _rbc_prefill_phase_key(page, state: dict[str, Any]) -> tuple[int, int, str]:
    base_page = getattr(page, "raw_page", page)
    navigation_token = int(getattr(base_page, "_rbc_navigation_token", 0) or 0)
    return (
        id(base_page),
        navigation_token,
        str(state.get("url") or ""),
    )


def _rbc_prefill_slot_key(page, state: dict[str, Any], field: str) -> tuple[int, int, str, str]:
    base_page = getattr(page, "raw_page", page)
    navigation_token = int(getattr(base_page, "_rbc_navigation_token", 0) or 0)
    return (
        id(base_page),
        navigation_token,
        str(state.get("url") or ""),
        field,
    )


async def _prefill_rbc_input(page, selectors: tuple[str, ...], value: str) -> bool:
    if not value:
        return False
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, state="visible", timeout=250)
        except Exception:
            continue
        current_value = await _rbc_first_prefill_input_value(page, (selector,))
        if current_value:
            return False
        try:
            await page.fill(selector, value)
        except Exception:
            continue
        return bool(await _rbc_first_prefill_input_value(page, (selector,)))
    return False


async def _maybe_prefill_rbc_credentials(
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
    phase_key = _rbc_prefill_phase_key(page, state)
    username_key = _rbc_prefill_slot_key(page, state, "username")
    password_key = _rbc_prefill_slot_key(page, state, "password")
    username_visible = bool(state.get("username_visible", state.get("login_visible")))
    password_visible = bool(state.get("password_visible", state.get("login_visible")))
    needs_username_prefill = (
        username_visible
        and bool(username)
        and username_key not in attempted
        and not await _rbc_first_prefill_input_value(page, RBC_USERNAME_SELECTORS)
    )
    needs_password_prefill = (
        password_visible
        and bool(password)
        and password_key not in attempted
        and not await _rbc_first_prefill_input_value(page, RBC_PASSWORD_SELECTORS)
    )
    if not (needs_username_prefill or needs_password_prefill):
        if username_visible:
            attempted.add(username_key)
        if password_visible:
            attempted.add(password_key)
        if popup_lock is not None and popup_lock.locked:
            started_at = float(lock_started_at.setdefault(phase_key, asyncio.get_running_loop().time()))
            should_release = (
                state.get("authenticated")
                or state.get("mfa_notification_visible")
                or state.get("mfa_code_visible")
                or state.get("invalid_credentials")
                or (asyncio.get_running_loop().time() - started_at) >= RBC_CREDENTIAL_LOCK_GRACE_SECONDS
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
            prefills_applied = await _prefill_rbc_input(page, RBC_USERNAME_SELECTORS, username)
            attempted.add(username_key)
        if needs_password_prefill:
            password_prefilled = await _prefill_rbc_input(page, RBC_PASSWORD_SELECTORS, password)
            attempted.add(password_key)
            if password_prefilled:
                prefills_applied = True
        if prefills_applied and not prefill_state.get("announced"):
            message = "Saved RBC credentials were prefilled in the secure browser. Review them and continue sign-in."
            visible_auth.print_status(message)
            await log_support_event(stage="credentials prefilled", message=message, last_output=message)
            prefill_state["announced"] = True
    finally:
        lock_started_at.pop(phase_key, None)
        if popup_lock is not None and popup_lock.locked:
            await popup_lock.set_locked(False)


async def _quick_prefill_rbc_credentials(
    page,
    *,
    credentials: tuple[str, str] | None,
    prefill_state: dict[str, Any],
    popup_lock: PopupInteractionLock | None = None,
) -> None:
    if not credentials:
        return
    username_visible = await any_selector_is_visible(page, RBC_USERNAME_SELECTORS)
    password_visible = await any_selector_is_visible(page, RBC_PASSWORD_SELECTORS)
    state = {
        "url": visible_auth.safe_page_url(getattr(page, "raw_page", page)),
        "login_visible": username_visible and password_visible,
        "username_visible": username_visible,
        "password_visible": password_visible,
        "authenticated": False,
        "mfa_notification_visible": False,
        "mfa_code_visible": False,
        "invalid_credentials": False,
    }
    await _maybe_prefill_rbc_credentials(
        page,
        state=state,
        credentials=credentials,
        prefill_state=prefill_state,
        popup_lock=popup_lock,
    )


def collect_rbc_credentials(captured_credentials: dict[str, str]) -> tuple[str, str] | None:
    username = str(captured_credentials.get("username") or "").strip()
    if username and _rbc_has_unmasked_password(captured_credentials):
        password = str(captured_credentials.get("password") or "")
        return username, password
    return None


async def wait_for_authenticated_session(
    context,
    page,
    *,
    captured_credentials: dict[str, str],
    prefill_credentials: tuple[str, str] | None = None,
    popup_lock: PopupInteractionLock | None = None,
    capture_state: RBCCredentialCaptureState | None = None,
) -> Any:
    deadline = visible_auth.visible_auth_deadline(TIMEOUT_SECONDS)
    last_description = ""
    prefill_state = {"announced": False, "attempted": set()}
    manual_takeover_announced = False
    account_shell_first_seen_at: float | None = None
    account_shell_grace_announced = False
    wait_loop_started_at: float | None = None
    wait_loop_iterations = 0
    login_form_seen_logged = False
    capture_stop_event = asyncio.Event()
    capture_task = asyncio.create_task(
        _run_rbc_credential_capture_sampler(
            context,
            captured_credentials,
            capture_stop_event,
            capture_state,
        )
    )
    try:
        while visible_auth.visible_auth_deadline_active(deadline):
            page = await get_viable_rbc_page(context, page, popup_lock=popup_lock)
            wait_loop_iterations += 1
            if RBC_WINDOWS_FLOW and wait_loop_started_at is None:
                wait_loop_started_at = asyncio.get_running_loop().time()
                await log_support_event(
                    stage="wait loop start",
                    debug=True,
                    message="RBC sign-in wait loop started.",
                    details={"platform": RBC_VISIBLE_AUTH_PLATFORM},
                )
            await _dismiss_rbc_cookie_banner(page)
            if not VISIBLE_AUTH_ADD_FLOW:
                await _quick_prefill_rbc_credentials(
                    page,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            await _capture_rbc_raw_input_credentials(
                page,
                captured_credentials,
                timeout_ms=RBC_CREDENTIAL_CAPTURE_FAST_TIMEOUT_MS,
                include_hidden_fallback=True,
            )
            try:
                state = await get_rbc_page_state(page)
            except Exception as exc:
                if visible_auth.is_browser_closed_error(exc):
                    raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE) from exc
                raise
            if RBC_WINDOWS_FLOW and not login_form_seen_logged and state.get("login_visible"):
                login_form_seen_logged = True
                started = wait_loop_started_at or asyncio.get_running_loop().time()
                await log_support_event(
                    stage="login form detected",
                    debug=True,
                    message="RBC login form detected by wait loop; credential prefill can proceed.",
                    details={
                        "wait_loop_iterations": wait_loop_iterations,
                        "elapsed_ms_since_loop_start": int(
                            (asyncio.get_running_loop().time() - started) * 1000
                        ),
                    },
                )
            if not VISIBLE_AUTH_ADD_FLOW:
                await _maybe_prefill_rbc_credentials(
                    page,
                    state=state,
                    credentials=prefill_credentials,
                    prefill_state=prefill_state,
                    popup_lock=popup_lock,
                )
            description = describe_rbc_state(state)
            if description != last_description:
                visible_auth.print_status(description)
                await log_support_event(stage="wait", message=description, last_output=description)
                last_description = description
            if state.get("authenticated"):
                await visible_auth.ensure_visible_auth_context_handoff_lock(
                    context,
                    provider_display_name="RBC",
                    log_support_event=log_support_event,
                    reason="authenticated",
                )
                return page
            account_shell_ready = bool(
                state.get("account_shell_visible")
                and not state.get("login_visible")
                and not state.get("mfa_notification_visible")
                and not state.get("mfa_code_visible")
                and not state.get("mfa_app_visible")
                and not state.get("invalid_credentials")
            )
            if account_shell_ready:
                now = asyncio.get_running_loop().time()
                if account_shell_first_seen_at is None:
                    account_shell_first_seen_at = now
                grace_elapsed_seconds = now - account_shell_first_seen_at
                grace_expired = grace_elapsed_seconds >= RBC_ACCOUNT_SHELL_PAYLOAD_GRACE_SECONDS
                if not grace_expired:
                    if not account_shell_grace_announced:
                        account_shell_grace_announced = True
                        await log_support_event(
                            stage="account shell waiting payload",
                            debug=True,
                            message="RBC account shell reached; waiting briefly for account data to load before handoff.",
                            details={
                                "grace_seconds": RBC_ACCOUNT_SHELL_PAYLOAD_GRACE_SECONDS,
                            },
                        )
                    await asyncio.sleep(RBC_ACCOUNT_SHELL_PAYLOAD_GRACE_POLL_SECONDS)
                    continue
                if not visible_auth.visible_auth_context_handoff_lock_started(context):
                    await visible_auth.ensure_visible_auth_context_handoff_lock(
                        context,
                        provider_display_name="RBC",
                        log_support_event=log_support_event,
                        reason="account_shell",
                    )
                await log_support_event(
                    stage="account shell ready",
                    debug=True,
                    message="RBC account shell reached; backend sync will validate the saved session.",
                    details={
                        "accounts_payload_ready": bool(state.get("accounts_payload_ready")),
                        "shell_payload_grace_seconds": round(grace_elapsed_seconds, 3),
                    },
                )
                return page
            if state.get("invalid_credentials"):
                if popup_lock is not None and popup_lock.locked:
                    await popup_lock.set_locked(False)
                if not manual_takeover_announced:
                    await log_support_event(stage="manual takeover", message=description, last_output=description)
                    manual_takeover_announced = True
            await _sleep_with_rbc_credential_capture(page, captured_credentials)

        raise TimeoutError("Timed out waiting for an authenticated RBC session.")
    finally:
        capture_stop_event.set()
        try:
            await asyncio.wait_for(capture_task, timeout=1.0)
        except Exception:
            capture_task.cancel()
        if (
            popup_lock is not None
            and popup_lock.locked
            and not visible_auth.visible_auth_context_handoff_lock_started(context)
        ):
            await popup_lock.set_locked(False)


async def _prepare_rbc_context_page(
    context,
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState | None = None,
):
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        page = pages[0]
        bind_rbc_page_watcher(page, captured_credentials, capture_state)
        await _install_rbc_windows_cdp_request_capture(context, page, captured_credentials, capture_state)
        for extra_page in pages[1:]:
            bind_rbc_page_watcher(extra_page, captured_credentials, capture_state)
            await _install_rbc_windows_cdp_request_capture(
                context,
                extra_page,
                captured_credentials,
                capture_state,
            )
            try:
                await extra_page.close()
            except Exception:
                pass
        return page
    page = await context.new_page()
    bind_rbc_page_watcher(page, captured_credentials, capture_state)
    await _install_rbc_windows_cdp_request_capture(context, page, captured_credentials, capture_state)
    return page


def _rbc_credential_capture_details(
    captured_credentials: dict[str, str],
    capture_state: RBCCredentialCaptureState,
) -> dict[str, Any]:
    unmasked_login_value_capture_count = sum(
        int(captured_credentials.get(key) or 0)
        for key in (
            "_raw_password_capture_count",
            "_raw_hidden_password_capture_count",
            "_request_password_capture_count",
            "_cdp_request_password_capture_count",
            "_route_password_capture_count",
        )
    )
    return {
        "unmasked_login_value_capture_count": unmasked_login_value_capture_count,
        "unmasked_login_value_captured": _rbc_has_unmasked_password(captured_credentials),
        "raw_username_capture_count": int(captured_credentials.get("_raw_username_capture_count") or 0),
        "raw_hidden_username_capture_count": int(
            captured_credentials.get("_raw_hidden_username_capture_count") or 0
        ),
        "request_username_capture_count": int(captured_credentials.get("_request_username_capture_count") or 0),
        "cdp_request_username_capture_count": int(
            captured_credentials.get("_cdp_request_username_capture_count") or 0
        ),
        "raw_password_capture_count": int(captured_credentials.get("_raw_password_capture_count") or 0),
        "raw_hidden_password_capture_count": int(
            captured_credentials.get("_raw_hidden_password_capture_count") or 0
        ),
        "request_password_capture_count": int(captured_credentials.get("_request_password_capture_count") or 0),
        "cdp_request_password_capture_count": int(
            captured_credentials.get("_cdp_request_password_capture_count") or 0
        ),
        "route_username_capture_count": int(
            captured_credentials.get("_route_username_capture_count") or 0
        ),
        "route_password_capture_count": int(
            captured_credentials.get("_route_password_capture_count") or 0
        ),
        "request_masked_password_count": int(captured_credentials.get("_request_masked_password_count") or 0),
        "cdp_request_masked_password_count": int(
            captured_credentials.get("_cdp_request_masked_password_count") or 0
        ),
        "raw_masked_password_count": int(captured_credentials.get("_raw_masked_password_count") or 0),
        "raw_hidden_masked_password_count": int(
            captured_credentials.get("_raw_hidden_masked_password_count") or 0
        ),
        "request_generated_password_count": int(
            captured_credentials.get("_request_generated_password_count") or 0
        ),
        "cdp_request_generated_password_count": int(
            captured_credentials.get("_cdp_request_generated_password_count") or 0
        ),
        "route_masked_password_count": int(
            captured_credentials.get("_route_masked_password_count") or 0
        ),
        "route_generated_password_count": int(
            captured_credentials.get("_route_generated_password_count") or 0
        ),
        "raw_generated_password_count": int(captured_credentials.get("_raw_generated_password_count") or 0),
        "raw_hidden_generated_password_count": int(
            captured_credentials.get("_raw_hidden_generated_password_count") or 0
        ),
        "username_captured": bool(captured_credentials.get("username")),
        "password_captured": _rbc_has_unmasked_password(captured_credentials),
        **capture_state.summary(),
    }


async def run() -> None:
    if PROVIDER != "rbc":
        raise RuntimeError(f"Unsupported provider {PROVIDER!r} for RBC visible auth.")
    if not ATTEMPT_ID:
        raise RuntimeError("Visible auth attempt ID is required.")

    captured_credentials = {"username": "", "password": ""}
    capture_state = RBCCredentialCaptureState()
    saved_credentials = None
    bootstrap_storage_state = None
    bootstrap_storage_slot = ""
    bootstrap_storage_sanitize_details = {
        "removed_auth_cookies": 0,
        "kept_non_auth_cookies": 0,
        "removed_auth_origins": 0,
    }
    bootstrap_session_artifact = None
    bootstrap_session_slot = ""
    if not VISIBLE_AUTH_ADD_FLOW:
        saved_credentials = visible_auth.load_provider_credentials(provider=PROVIDER)
        if saved_credentials and (
            _is_rbc_masked_secret(saved_credentials[1])
            or _is_rbc_generated_secret_payload(saved_credentials[1])
        ):
            saved_credentials = (saved_credentials[0], "")
        loaded_storage_state, bootstrap_storage_slot = visible_auth.load_provider_artifact(
            provider=PROVIDER,
            artifact_kind="storage_state",
            slots=("quarantine", "active"),
        )
        if storage_state_has_rbc_material(loaded_storage_state):
            bootstrap_storage_state, bootstrap_storage_sanitize_details = sanitize_rbc_bootstrap_storage_state(
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
    raw_har_path = _rbc_raw_har_path(USER_ID, har_timestamp_slug) if VISIBLE_AUTH_RECORD_HAR else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await log_support_event(stage="launch", result="launched", message="Opening RBC secure login browser.")
    visible_auth.print_status("Opening RBC secure login browser.")

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
            await log_support_event(stage="direct_entry", debug=True, message="RBC is using login entry.")
            await log_support_event(
                stage="har capture",
                debug=True,
                message=(
                    "RBC visible-auth HAR capture is enabled."
                    if VISIBLE_AUTH_RECORD_HAR
                    else "RBC visible-auth HAR capture is disabled."
                ),
                details={
                    "enabled": VISIBLE_AUTH_RECORD_HAR,
                    "capture_level": VISIBLE_AUTH_CAPTURE_LEVEL,
                    "developer_local": RBC_DEV_CAPTURE_LOCAL,
                },
            )
            try:
                launch_kwargs = _rbc_browser_launch_kwargs(browser_runtime)
                context_kwargs: dict[str, Any] = {
                    "timezone_id": browser_timezone,
                }
                if bootstrap_storage_state is not None:
                    context_kwargs["storage_state"] = bootstrap_storage_state
                    launch_mode = "isolated_context_bootstrap"
                if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                    RBC_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
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
                        provider_display_name="RBC",
                    )
                ) from exc

            await _install_rbc_windows_login_route_capture(
                context,
                captured_credentials,
                capture_state,
            )

            popup_lock: PopupInteractionLock | None = None

            def on_new_page(new_page) -> None:
                bind_rbc_page_watcher(new_page, captured_credentials, capture_state)

                async def bind_locked_page() -> None:
                    try:
                        await _install_rbc_windows_cdp_request_capture(
                            context,
                            new_page,
                            captured_credentials,
                            capture_state,
                        )
                        should_lock = popup_lock.locked
                        await popup_lock.bind_page(new_page, await context.new_cdp_session(new_page))
                        if should_lock:
                            await popup_lock.set_locked(True)
                    except Exception:
                        return

                if popup_lock is not None:
                    _schedule_rbc_capture_task(capture_state, bind_locked_page())
                elif RBC_WINDOWS_FLOW:
                    _schedule_rbc_capture_task(
                        capture_state,
                        _install_rbc_windows_cdp_request_capture(
                            context,
                            new_page,
                            captured_credentials,
                            capture_state,
                        ),
                    )

            context.on("page", on_new_page)
            page = await _prepare_rbc_context_page(context, captured_credentials, capture_state)

            runtime_description = browser_runtime.get("runtime") or "unknown"
            runtime_path = browser_runtime.get("executable_path") or "patchright_default"
            runtime_mode = browser_runtime.get("mode") or "unknown"
            runtime_version = browser_runtime.get("version") or ""
            runtime_platform = browser_runtime.get("platform") or ""
            runtime_installed_now = bool(browser_runtime.get("installed_now"))
            await log_support_event(
                stage="browser runtime",
                debug=True,
                message="RBC visible-auth browser runtime selected.",
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
                message="RBC visible-auth browser context.",
                details={
                    "launch_mode": launch_mode,
                    "bootstrap_storage_slot": bootstrap_storage_slot or "none",
                    "bootstrap_session_slot": bootstrap_session_slot or "none",
                    "bootstrap_storage_used": bool(bootstrap_storage_state is not None),
                    "bootstrap_user_agent_used": False,
                    "bootstrap_auth_cookies_removed": bootstrap_storage_sanitize_details["removed_auth_cookies"],
                    "bootstrap_non_auth_cookies_kept": bootstrap_storage_sanitize_details["kept_non_auth_cookies"],
                    "bootstrap_auth_origins_removed": bootstrap_storage_sanitize_details["removed_auth_origins"],
                    "locale_policy": "browser_default",
                    "timezone_id": browser_timezone,
                    "har_capture_enabled": VISIBLE_AUTH_RECORD_HAR,
                    "capture_level": VISIBLE_AUTH_CAPTURE_LEVEL,
                    "developer_local_capture": RBC_DEV_CAPTURE_LOCAL,
                    "visible_auth_platform": RBC_VISIBLE_AUTH_PLATFORM,
                    "visible_auth_flow": RBC_VISIBLE_AUTH_FLOW,
                    "rbc_linux_flow": RBC_LINUX_FLOW,
                    "rbc_windows_flow": RBC_WINDOWS_FLOW,
                    "rbc_mac_flow": RBC_MAC_FLOW,
                    "windows_managed_brave_launch_args_enabled": bool(
                        RBC_WINDOWS_FLOW and launch_kwargs.get("args")
                    ),
                    **visible_auth.browser_launch_details(launch_kwargs),
                    "browser_init_scripts_enabled": False,
                    "credential_capture_strategy": "bounded_post_load_input_polling_and_structured_request_capture_with_windows_login_route_pause",
                    "credential_capture_background_poll_seconds": RBC_CREDENTIAL_CAPTURE_BACKGROUND_POLL_SECONDS,
                    "windows_cdp_request_capture_enabled": RBC_WINDOWS_FLOW,
                    "windows_login_route_capture_enabled": RBC_WINDOWS_FLOW,
                    "browser_runtime": runtime_description,
                    "runtime_mode": runtime_mode,
                    "runtime_version": runtime_version,
                    "runtime_platform": runtime_platform,
                    "installed_now": runtime_installed_now,
                    "executable_path": runtime_path,
                },
            )

            if saved_credentials and not VISIBLE_AUTH_ADD_FLOW:
                try:
                    popup_lock = PopupInteractionLock(page, await context.new_cdp_session(page))
                    await popup_lock.set_locked(True)
                    page = popup_lock.page
                except Exception:
                    popup_lock = None

            early_capture_stop_event = asyncio.Event()
            # Match the proven Linux/macOS lifecycle on Windows: sample live
            # fields before navigation as well as inside the authenticated wait.
            early_capture_task = asyncio.create_task(
                _run_rbc_credential_capture_sampler(
                    context,
                    captured_credentials,
                    early_capture_stop_event,
                    capture_state,
                )
            )
            try:
                await open_rbc_login_page(page, entry_url=RBC_LOGIN_URL)
                page = await wait_for_authenticated_session(
                    context,
                    page,
                    captured_credentials=captured_credentials,
                    prefill_credentials=saved_credentials,
                    popup_lock=popup_lock,
                    capture_state=capture_state,
                )
            finally:
                early_capture_stop_event.set()
                try:
                    await asyncio.wait_for(early_capture_task, timeout=1.0)
                except Exception:
                    early_capture_task.cancel()
            await visible_auth.ensure_visible_auth_context_handoff_lock(
                context,
                provider_display_name="RBC",
                log_support_event=log_support_event,
                reason="authenticated",
            )
            await _drain_rbc_capture_tasks(capture_state)
            await _capture_rbc_context_raw_input_credentials(
                context,
                captured_credentials,
                capture_state=capture_state,
            )
            captured_pair = collect_rbc_credentials(captured_credentials)
            credential_capture_details = _rbc_credential_capture_details(
                captured_credentials,
                capture_state,
            )
            await log_support_event(
                stage="credential capture summary",
                message="RBC visible-auth credential capture summary.",
                details=credential_capture_details,
            )
            if not captured_pair:
                raise RuntimeError(
                    "RBC raw credentials could not be captured from the secure login flow. Start RBC login again."
                )
            username, password = captured_pair

            capture_started_at = asyncio.get_running_loop().time()
            active_target = page
            user_agent = await visible_auth.collect_user_agent(active_target)

            storage_state = await collect_storage_state(context)
            storage_state_details = {
                "cookies": len(storage_state.get("cookies") or []) if isinstance(storage_state, dict) else 0,
                "origins": len(storage_state.get("origins") or []) if isinstance(storage_state, dict) else 0,
                "has_rbc_material": storage_state_has_rbc_material(storage_state),
            }
            if not storage_state_has_rbc_material(storage_state):
                raise RuntimeError("RBC storage state could not be captured for future syncs.")
            session_artifact = await collect_rbc_session_artifact(user_agent=user_agent)
            session_artifact_details = {
                "has_user_agent": bool(session_artifact.get("user_agent")),
                "handoff_capture_ms": int((asyncio.get_running_loop().time() - capture_started_at) * 1000),
            }
            await log_support_event(
                stage="storage_state captured",
                debug=True,
                message="RBC visible-auth storage state captured.",
                details=storage_state_details,
            )
            await log_support_event(
                stage="session_artifact captured",
                debug=True,
                message="RBC visible-auth session artifact captured.",
                details=session_artifact_details,
            )
            if context is not None:
                if await visible_auth.close_visible_auth_context_after_capture(
                    context,
                    provider_display_name="RBC",
                    log_support_event=log_support_event,
                    context_close_timeout_seconds=(
                        RBC_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS if RBC_WINDOWS_FLOW else None
                    ),
                    browser_close_timeout_seconds=(
                        RBC_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS if RBC_WINDOWS_FLOW else None
                    ),
                    force_browser_process_close=RBC_WINDOWS_FLOW,
                    browser_executable_path=runtime_path if RBC_WINDOWS_FLOW else None,
                ):
                    context = None

            auth_message = "Authenticated RBC session detected. Saving session for background sync."
            await log_support_event(stage="authenticated", message=auth_message, last_output=auth_message)
            storage_result = await persist_visible_auth_artifact(
                artifact_type="storage_state",
                payload=storage_state,
            )
            session_result = await persist_visible_auth_artifact(
                artifact_type="session",
                payload=session_artifact,
            )

            storage_message = "RBC secure-login artifacts were captured for validation and background sync."
            visible_auth.print_status(storage_message)
            await log_support_event(stage="artifact saved", message=storage_message, last_output=storage_message)
            credentials_result = await persist_visible_auth_credentials(username, password)
            credentials_message = "Captured RBC credentials secured for this sync attempt."
            visible_auth.print_status(credentials_message)
            await log_support_event(
                stage="credentials saved",
                message=credentials_message,
                last_output=credentials_message,
            )
            success_message = f"RBC secure login is complete. {visible_auth.APP_BRAND_NAME} is syncing accounts and transactions in the background."
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
            if capture_state.accepting_tasks or capture_state.tasks:
                await _drain_rbc_capture_tasks(capture_state, timeout_seconds=0.0)
            await visible_auth.close_visible_auth_context_quietly(
                context,
                context_close_timeout_seconds=(
                    RBC_WINDOWS_CONTEXT_CLOSE_TIMEOUT_SECONDS if RBC_WINDOWS_FLOW else None
                ),
            )
            if VISIBLE_AUTH_RECORD_HAR and raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_rbc_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_timestamp_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await log_support_event(
                            stage="har capture",
                            debug=True,
                            message="Saved sanitized RBC HAR capture.",
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
                        message="Could not finalize the sanitized RBC HAR capture.",
                        details={
                            "har_result": run_result,
                            "error": str(exc),
                        },
                    )
            await visible_auth.close_visible_auth_context_quietly(
                None,
                browser,
                browser_close_timeout_seconds=(
                    RBC_WINDOWS_BROWSER_CLOSE_TIMEOUT_SECONDS if RBC_WINDOWS_FLOW else None
                ),
            )


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
