#!/usr/bin/env python3
"""Managed desktop browser handoff for Moomoo OAuth authorization."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from desktop_browser_runtime import DesktopBrowserRuntimeError, ensure_brave_browser_runtime
import visible_auth_common as visible_auth

try:
    from patchright.async_api import async_playwright
except ImportError as exc:  # pragma: no cover - host environment only
    raise SystemExit(visible_auth.PATCHRIGHT_SETUP_MESSAGE) from exc


REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_AUTH_DIR = visible_auth.resolve_desktop_auth_dir(REPO_ROOT)
MOOMOO_DESKTOP_AUTH_RAW_HAR_DIR = DESKTOP_AUTH_DIR / "har-raw" / "moomoo"
MOOMOO_DIAGNOSTIC_DIR = visible_auth.resolve_diagnostic_dir(REPO_ROOT, "Moomoo")
PROVIDER = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "moomoo").strip().lower()
ATTEMPT_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID") or "").strip()
SYNC_ID = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_SYNC_ID") or "").strip()
USER_ID = int(str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_USER_ID") or "1").strip() or "1")
ADD_FLOW = visible_auth.env_flag("BREAKTWENTY_VISIBLE_AUTH_ADD_FLOW", default=True)
VISIBLE_AUTH_RECORD_HAR = visible_auth.env_flag(
    "BREAKTWENTY_VISIBLE_AUTH_RECORD_HAR",
    default=False,
)
AUTHORIZATION_URL = str(os.getenv("BREAKTWENTY_MOOMOO_OAUTH_AUTHORIZATION_URL") or "").strip()
BACKEND_API_BASE_URL = str(
    os.getenv("BREAKTWENTY_BACKEND_API_URL") or "http://127.0.0.1:8000/api"
).strip()
TIMEOUT_SECONDS = visible_auth.visible_auth_timeout_seconds(default=600)
DEFAULT_LOG_PATH = (
    DESKTOP_AUTH_DIR
    / "logs"
    / "visible-auth"
    / f"{PROVIDER}-{ATTEMPT_ID or 'unknown'}.log"
)
VISIBLE_AUTH_LOG_PATH = Path(
    str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or DEFAULT_LOG_PATH)
).expanduser()
HOST_BROWSER_CLOSED_MESSAGE = (
    "Moomoo authorization window was closed before authorization finished."
)
MOOMOO_PASSPORT_ORIGIN = "https://passport.moomoo.com"
MOOMOO_ENGLISH_LOCALE = "en-US"
MOOMOO_ENGLISH_LANGUAGE_PARAMETER = "en-us"
MOOMOO_MANAGED_BROWSER_WINDOW_WIDTH = 1600
MOOMOO_MANAGED_BROWSER_WINDOW_HEIGHT = 1000
MOOMOO_MANAGED_BROWSER_LAUNCH_ARGS = (
    f"--window-size={MOOMOO_MANAGED_BROWSER_WINDOW_WIDTH},{MOOMOO_MANAGED_BROWSER_WINDOW_HEIGHT}",
)
MOOMOO_HAR_FILENAME_PREFIX = "moomoo_visible_auth"
MOOMOO_MAX_SANITIZED_HARS_PER_USER = 3
MOOMOO_HAR_SAFE_RESPONSE_KEYS: frozenset[str] = frozenset()

VISIBLE_AUTH_CLIENT = visible_auth.VisibleAuthBackendClient(
    backend_api_base_url=BACKEND_API_BASE_URL,
    provider=PROVIDER,
    attempt_id=ATTEMPT_ID,
    add_flow=ADD_FLOW,
    sync_id=SYNC_ID,
)


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    return message.replace(AUTHORIZATION_URL, "[Moomoo authorization URL]")


def _english_moomoo_passport_url(raw_url: str) -> str:
    """Keep Moomoo's login redirect in English without changing its OAuth target."""
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return raw_url
    if (
        parsed.scheme != "https"
        or parsed.netloc != "passport.moomoo.com"
        or parsed.path not in {"", "/"}
    ):
        return raw_url
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "lang"
    ]
    query.insert(0, ("lang", MOOMOO_ENGLISH_LANGUAGE_PARAMETER))
    return urlunsplit(parsed._replace(query=urlencode(query)))


async def _enforce_english_moomoo_passport_route(route) -> None:
    request_url = route.request.url
    english_url = _english_moomoo_passport_url(request_url)
    if route.request.is_navigation_request() and english_url != request_url:
        await route.continue_(url=english_url)
        return
    await route.continue_()


async def _size_moomoo_browser_window(context, page) -> dict[str, Any]:
    details: dict[str, Any] = {
        "window_sized": False,
        "requested_width": MOOMOO_MANAGED_BROWSER_WINDOW_WIDTH,
        "requested_height": MOOMOO_MANAGED_BROWSER_WINDOW_HEIGHT,
    }
    cdp_session = None
    try:
        cdp_session = await context.new_cdp_session(page)
        target = await cdp_session.send("Browser.getWindowForTarget")
        window_id = int(target.get("windowId") or 0)
        if window_id <= 0:
            return details
        await cdp_session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "windowState": "normal",
                    "left": 0,
                    "top": 0,
                    "width": MOOMOO_MANAGED_BROWSER_WINDOW_WIDTH,
                    "height": MOOMOO_MANAGED_BROWSER_WINDOW_HEIGHT,
                },
            },
        )
        details["window_sized"] = True
        window = await cdp_session.send(
            "Browser.getWindowBounds",
            {"windowId": window_id},
        )
        bounds = window.get("bounds") if isinstance(window, dict) else None
        if isinstance(bounds, dict):
            for key in ("left", "top", "width", "height", "windowState"):
                if key in bounds:
                    details[f"actual_window_{key}"] = bounds[key]
        viewport = await page.evaluate(
            """
            () => ({
              width: window.innerWidth,
              height: window.innerHeight,
              devicePixelRatio: window.devicePixelRatio,
              screenWidth: window.screen?.width,
              screenHeight: window.screen?.height,
              availableWidth: window.screen?.availWidth,
              availableHeight: window.screen?.availHeight,
            })
            """
        )
        if isinstance(viewport, dict):
            for key, value in viewport.items():
                if isinstance(value, (int, float, str, bool)) or value is None:
                    details[f"actual_viewport_{key}"] = value
        return details
    except Exception:
        return details
    finally:
        if cdp_session is not None:
            try:
                await cdp_session.detach()
            except Exception:
                pass


def _moomoo_raw_har_path(user_id: int, timestamp_slug: str) -> Path:
    return (
        MOOMOO_DESKTOP_AUTH_RAW_HAR_DIR
        / f"{MOOMOO_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}.raw.har"
    )


def _moomoo_sanitized_har_path(
    user_id: int,
    timestamp_slug: str,
    result: str,
) -> Path:
    scope = visible_auth.diagnostic_scope_suffix(SYNC_ID, ATTEMPT_ID)
    scope_part = f"_{scope}" if scope else ""
    return (
        MOOMOO_DIAGNOSTIC_DIR
        / f"{MOOMOO_HAR_FILENAME_PREFIX}_user{user_id}_{timestamp_slug}{scope_part}_{result}.har"
    )


def _moomoo_should_keep_response_body(
    _url: str,
    _status: int | None,
    _content_type: str,
) -> bool:
    # The OAuth browser's navigation/status metadata is useful, while response
    # bodies can contain login or authorization material and are never needed.
    return False


def finalize_moomoo_har_capture(
    raw_har_path: Path,
    *,
    user_id: int,
    timestamp_slug: str,
    result: str,
) -> Path | None:
    sanitized_path = _moomoo_sanitized_har_path(user_id, timestamp_slug, result)
    existing = sorted(
        MOOMOO_DIAGNOSTIC_DIR.glob(f"{MOOMOO_HAR_FILENAME_PREFIX}_user{user_id}_*.har"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return visible_auth.finalize_har_capture(
        raw_har_path,
        sanitized_har_path=sanitized_path,
        sanitized_paths_to_prune=existing,
        keep=MOOMOO_MAX_SANITIZED_HARS_PER_USER,
        should_keep_response_body=_moomoo_should_keep_response_body,
        safe_response_keys=MOOMOO_HAR_SAFE_RESPONSE_KEYS,
        capture_level=visible_auth.CAPTURE_LEVEL_REDACTED_RICH,
    )


def _authorization_callback_target() -> tuple[str, str, int, str]:
    try:
        parsed = urlsplit(AUTHORIZATION_URL)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        redirect_values = query.get("redirect_uri") or []
        redirect = urlsplit(redirect_values[0] if len(redirect_values) == 1 else "")
        port = redirect.port
    except (TypeError, ValueError):
        port = None
        parsed = urlsplit("")
        redirect = urlsplit("")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "webapi.moomoo.com"
        or parsed.path != "/oauth2/authorize/confirm"
        or parsed.fragment
        or redirect.scheme != "http"
        or redirect.hostname != "localhost"
        or not port
        or redirect.path != "/api/auth/moomoo/oauth/callback"
        or redirect.query
        or redirect.fragment
    ):
        raise RuntimeError("Moomoo authorization browser handoff was incomplete or unsafe.")
    return redirect.scheme, redirect.hostname, port, redirect.path


def _deliver_callback(callback_url: str, target: tuple[str, str, int, str]) -> int:
    parsed = urlsplit(callback_url)
    expected_scheme, expected_host, expected_port, expected_path = target
    if (
        parsed.scheme != expected_scheme
        or parsed.hostname != expected_host
        or parsed.port != expected_port
        or parsed.path != expected_path
        or parsed.fragment
    ):
        raise RuntimeError("Moomoo returned an unexpected authorization callback.")
    request = urllib_request.Request(
        callback_url,
        headers={"Accept": "text/html", "Cache-Control": "no-store"},
        method="GET",
    )
    opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=45) as response:  # nosec B310 -- validated loopback URL
            response.read(1024)
            return int(getattr(response, "status", 200) or 200)
    except urllib_error.HTTPError as exc:
        exc.read(1024)
        return int(exc.code or 500)
    except (OSError, urllib_error.URLError) as exc:
        raise RuntimeError(
            "BreakTwenty could not receive Moomoo's authorization callback."
        ) from exc


async def run() -> None:
    if PROVIDER != "moomoo" or not ATTEMPT_ID or not SYNC_ID:
        raise RuntimeError("Moomoo managed authorization session was incomplete.")
    callback_target = _authorization_callback_target()
    callback_finished = asyncio.Event()
    browser_closed = asyncio.Event()
    callback_result: dict[str, Any] = {"status": 0, "error": ""}
    har_slug = visible_auth.har_timestamp_slug() if VISIBLE_AUTH_RECORD_HAR else ""
    raw_har_path = _moomoo_raw_har_path(USER_ID, har_slug) if har_slug else None
    run_result = "failed"

    visible_auth.print_visible_auth_log_path(VISIBLE_AUTH_LOG_PATH)
    await VISIBLE_AUTH_CLIENT.log_support_event(
        stage="oauth browser launch",
        result="launched",
        message="Opening Moomoo authorization in the managed secure browser.",
        details={"browser_runtime": "managed_brave", "callback_interception": True},
    )
    visible_auth.print_status("Opening Moomoo authorization in BreakTwenty's secure browser.")

    async with async_playwright() as playwright:
        browser = None
        context = None
        try:
            await VISIBLE_AUTH_CLIENT.log_support_event(
                stage="har capture",
                result="enabled" if VISIBLE_AUTH_RECORD_HAR else "disabled",
                message=(
                    "Moomoo visible-auth HAR capture is enabled."
                    if VISIBLE_AUTH_RECORD_HAR
                    else "Moomoo visible-auth HAR capture is disabled."
                ),
                details={"enabled": VISIBLE_AUTH_RECORD_HAR},
                debug=True,
            )
            try:
                browser_runtime = ensure_brave_browser_runtime()
                launch_kwargs = visible_auth.managed_browser_launch_kwargs(
                    browser_runtime,
                    windows_managed_brave_stabilizers=True,
                    mac_managed_brave_stabilizers=True,
                )
                launch_args = list(launch_kwargs.get("args") or [])
                for launch_arg in MOOMOO_MANAGED_BROWSER_LAUNCH_ARGS:
                    if launch_arg not in launch_args:
                        launch_args.append(launch_arg)
                launch_kwargs["args"] = launch_args
                browser = await playwright.chromium.launch(**launch_kwargs)
                context_kwargs: dict[str, Any] = {
                    "locale": MOOMOO_ENGLISH_LOCALE,
                    "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
                    # Use the actual headed window width. Moomoo hides its branded
                    # panel at Playwright's default 1280 px emulated viewport.
                    "no_viewport": True,
                }
                if raw_har_path is not None:
                    MOOMOO_DESKTOP_AUTH_RAW_HAR_DIR.mkdir(parents=True, exist_ok=True)
                    context_kwargs.update(
                        {
                            "record_har_path": str(raw_har_path),
                            "record_har_mode": "full",
                            "record_har_content": "embed",
                        }
                    )
                context = await browser.new_context(**context_kwargs)
            except DesktopBrowserRuntimeError as exc:
                raise RuntimeError(str(exc)) from exc
            except Exception as exc:
                raise RuntimeError(
                    visible_auth.format_managed_browser_launch_error(
                        exc,
                        provider_display_name="Moomoo",
                    )
                ) from exc

            def mark_browser_closed(*_args: Any) -> None:
                browser_closed.set()

            browser.on("disconnected", mark_browser_closed)

            await context.route(
                f"{MOOMOO_PASSPORT_ORIGIN}/**",
                _enforce_english_moomoo_passport_route,
            )

            async def handle_callback(route) -> None:
                if callback_finished.is_set():
                    await route.abort()
                    return
                try:
                    status = await asyncio.to_thread(
                        _deliver_callback,
                        route.request.url,
                        callback_target,
                    )
                    callback_result["status"] = status
                    title = "Moomoo connected" if status < 400 else "Moomoo authorization failed"
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=(
                            "<!doctype html><html><head><meta charset='utf-8'>"
                            f"<title>{title}</title></head><body><p>{title}. "
                            "This secure window will close automatically.</p></body></html>"
                        ),
                    )
                except Exception as exc:
                    callback_result["error"] = str(exc)
                    try:
                        await route.abort()
                    except Exception:
                        pass
                finally:
                    callback_finished.set()

            callback_pattern = (
                f"{callback_target[0]}://{callback_target[1]}:{callback_target[2]}"
                f"{callback_target[3]}*"
            )
            await context.route(callback_pattern, handle_callback)
            page = await context.new_page()
            page.on("close", mark_browser_closed)
            window_details = await _size_moomoo_browser_window(context, page)
            window_sized = bool(window_details.get("window_sized"))
            await VISIBLE_AUTH_CLIENT.log_support_event(
                stage="oauth browser window",
                result="sized" if window_sized else "sizing_unavailable",
                message=(
                    "Moomoo authorization browser opened at its desktop-layout size."
                    if window_sized
                    else "Moomoo authorization browser used the window size available."
                ),
                details=window_details,
            )
            await page.goto(AUTHORIZATION_URL, wait_until="domcontentloaded", timeout=60_000)

            callback_task = asyncio.create_task(callback_finished.wait())
            closed_task = asyncio.create_task(browser_closed.wait())
            done, pending = await asyncio.wait(
                {callback_task, closed_task},
                timeout=TIMEOUT_SECONDS or 600,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                raise RuntimeError("Moomoo authorization timed out. Start the connection again.")
            if closed_task in done and not callback_finished.is_set():
                raise RuntimeError(HOST_BROWSER_CLOSED_MESSAGE)
            if callback_result["error"]:
                raise RuntimeError(callback_result["error"])
            if int(callback_result["status"] or 0) >= 400:
                raise RuntimeError("Moomoo authorization did not finish. Return to BreakTwenty to retry.")

            await VISIBLE_AUTH_CLIENT.log_support_event(
                stage="oauth callback delivered",
                result="succeeded",
                message="Moomoo authorization returned to BreakTwenty; closing the managed browser.",
                details={"callback_intercepted": True},
            )
            run_result = "succeeded"
        except Exception as exc:
            safe_message = _safe_error_message(exc)
            await VISIBLE_AUTH_CLIENT.log_support_event(
                stage="sync result",
                result="failed",
                level="error",
                message=safe_message,
                last_output=safe_message,
            )
            raise RuntimeError(safe_message) from exc
        finally:
            await visible_auth.close_visible_auth_context_quietly(context, browser)
            if raw_har_path is not None:
                try:
                    sanitized_har_path = await asyncio.to_thread(
                        finalize_moomoo_har_capture,
                        raw_har_path,
                        user_id=USER_ID,
                        timestamp_slug=har_slug,
                        result=run_result,
                    )
                    if sanitized_har_path:
                        await VISIBLE_AUTH_CLIENT.log_support_event(
                            stage="har capture",
                            result="saved",
                            message="Saved sanitized Moomoo visible-auth HAR capture.",
                            details={
                                "har_result": run_result,
                                "path": str(sanitized_har_path),
                            },
                            debug=True,
                        )
                except Exception as exc:
                    await VISIBLE_AUTH_CLIENT.log_support_event(
                        stage="har capture",
                        result="finalize_failed",
                        level="warning",
                        message="Could not finalize the sanitized Moomoo HAR capture.",
                        details={
                            "har_result": run_result,
                            "error": _safe_error_message(exc),
                        },
                        debug=True,
                    )

    visible_auth.print_visible_auth_result(
        {
            "provider": PROVIDER,
            "attemptId": ATTEMPT_ID,
            "syncId": SYNC_ID,
            "synced": False,
            "authenticated": True,
            "storageStateStaged": False,
            "sessionArtifactStaged": False,
            "credentialsCaptured": False,
            "credentialsStaged": False,
            "responseStatus": "handoff_ready",
        }
    )


def main() -> int:
    return visible_auth.run_visible_auth_main(
        run,
        host_browser_closed_message=HOST_BROWSER_CLOSED_MESSAGE,
        install_signal_handlers=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
