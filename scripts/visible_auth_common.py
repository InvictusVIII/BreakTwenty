from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import subprocess  # nosec B404
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


VISIBLE_AUTH_RESULT_PREFIX = "BREAKTWENTY_VISIBLE_AUTH_RESULT "
VISIBLE_AUTH_LOG_PATH_PREFIX = "BREAKTWENTY_VISIBLE_AUTH_LOG_PATH "
VISIBLE_AUTH_HEARTBEAT_PREFIX = "BREAKTWENTY_VISIBLE_AUTH_HEARTBEAT "
VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX = "BREAKTWENTY_VISIBLE_AUTH_DIAGNOSTICS_READY "
APP_BRAND_NAME = "BreakTwenty"
PATCHRIGHT_SETUP_MESSAGE = (
    "The managed visible-auth runtime is incomplete. Relaunch BreakTwenty through its "
    "desktop launcher so dependencies are restored from the hash-verified "
    "scripts/desktop_visible_auth_requirements.lock file."
)
DEFAULT_VISIBLE_AUTH_HEARTBEAT_SECONDS = 30.0
REDACTED_VALUE = "<redacted>"
VISIBLE_AUTH_PAGE_CLOSE_TIMEOUT_SECONDS = 1.5
VISIBLE_AUTH_CONTEXT_CLOSE_TIMEOUT_SECONDS = 2.0
VISIBLE_AUTH_BROWSER_CLOSE_TIMEOUT_SECONDS = 2.0
VISIBLE_AUTH_CDP_RELEASE_TIMEOUT_SECONDS = 0.5
VISIBLE_AUTH_PROCESS_FORCE_CLOSE_TIMEOUT_SECONDS = 5.0
VISIBLE_AUTH_USER_AGENT_TIMEOUT_SECONDS = 1.0
# HAR sanitization matches the backend's two capture levels: the always-on
# `redacted_rich` baseline (sanitized/allowlisted bodies, URL path/query
# identifiers and narrative values redacted) and `developer_local` (keeps URL
# identifiers for first-party debugging). There is no "off" — a HAR-capable
# provider always produces at least a redacted-rich HAR.
CAPTURE_LEVEL_REDACTED_RICH = "redacted_rich"
CAPTURE_LEVEL_DEVELOPER_LOCAL = "developer_local"
CAPTURE_LEVELS = frozenset(
    {
        CAPTURE_LEVEL_REDACTED_RICH,
        CAPTURE_LEVEL_DEVELOPER_LOCAL,
    }
)
DEFAULT_SENSITIVE_HEADER_NAMES = frozenset(
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
DEVELOPER_LOCAL_SENSITIVE_BODY_FIELD_NAMES = frozenset(
    {
        "authorization",
        "auth",
        "bearer",
        "client_secret",
        "code",
        "csrf",
        "k1",
        "mfa",
        "otp",
        "passcode",
        "password",
        "pin",
        "proxy-authorization",
        "q1",
        "qq",
        "secret",
        "session",
        "token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)
DEVELOPER_LOCAL_SENSITIVE_BODY_FIELD_MARKERS = (
    "authorization",
    "bearer",
    "credential",
    "csrf",
    "mfa",
    "otp",
    "passcode",
    "password",
    "secret",
    "session",
    "token",
    "xsrf",
)
URL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._~+=:-]{10,}$")
VISIBLE_AUTH_PLATFORM_LINUX = "linux"
VISIBLE_AUTH_PLATFORM_WINDOWS = "windows"
VISIBLE_AUTH_PLATFORM_MAC = "mac"
DIRECT_PREFILL_SCRIPT = r"""(el, value) => {
    if (!el) return;
    if (typeof el.focus === "function") {
        el.focus();
    }
    const proto =
        el instanceof HTMLInputElement
            ? HTMLInputElement.prototype
            : el instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : el instanceof HTMLSelectElement
                    ? HTMLSelectElement.prototype
                    : null;
    const descriptor = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
    if (descriptor && typeof descriptor.set === "function") {
        descriptor.set.call(el, value);
    } else {
        el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
}"""
LOCKED_CLICK_SCRIPT = r"""(el) => {
    if (!el || typeof el.click !== "function") return;
    el.click();
}"""
LOCKED_FOCUS_SCRIPT = r"""(el) => {
    if (!el || typeof el.focus !== "function") return;
    el.focus();
}"""
CREDENTIAL_FETCH_TIMEOUT_SECONDS = 10
RUNNER_BOOTSTRAP_MAX_BYTES = 1024
RUNNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")
LOOPBACK_BACKEND_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_runner_session_token = ""
_runner_session_lock = threading.Lock()
_visible_auth_backend_clients: list[Any] = []
_SHELL_PLACEHOLDER_USERNAME_RE = re.compile(r"^\$(?:[A-Z][A-Z0-9_]*|\{[A-Z][A-Z0-9_]*\})$")
_PERCENT_PLACEHOLDER_USERNAME_RE = re.compile(r"^%[A-Z][A-Z0-9_]*%$")
_TEMPLATE_PLACEHOLDER_USERNAME_RE = re.compile(
    r"^(?:\{\{\s*(?:username|user_name|user|userid|user_id|login|email)\s*\}\}|"
    r"<\s*(?:username|user name|user_name|userid|user id|user_id|login|email)\s*>)$",
    re.IGNORECASE,
)


def resolve_runtime_path(env_name: str, default: Path, *, base: Path | None = None) -> Path:
    configured = str(os.getenv(env_name) or "").strip()
    path = Path(configured).expanduser() if configured else default
    if not path.is_absolute():
        path = (base or default.parent) / path
    return path.resolve()


def resolve_desktop_auth_dir(repo_root: Path) -> Path:
    return resolve_runtime_path("BREAKTWENTY_DESKTOP_AUTH_DIR", repo_root / ".desktop-auth", base=repo_root)


def resolve_diagnostic_dir(repo_root: Path, provider_dir: str) -> Path:
    diagnostic_root = resolve_runtime_path(
        "BREAKTWENTY_DIAGNOSTIC_DIR", repo_root / "data" / "logs" / "providers", base=repo_root
    )
    return diagnostic_root / provider_dir


def diagnostic_scope_suffix(sync_id: str | None, attempt_id: str | None) -> str:
    parts: list[str] = []
    for label, value in (("sync", sync_id), ("attempt", attempt_id)):
        component = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")[:96]
        if component:
            parts.append(f"{label}-{component}")
    return "_".join(parts)


def env_flag(name: str, *, default: bool) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_visible_auth_platform(value: str | None, *, default: str | None = None) -> str:
    raw = str(value or "").strip().lower()
    if ":" in raw:
        raw = raw.rsplit(":", 1)[1].strip()
    normalized = raw.replace("_", "-").replace(" ", "-")
    if normalized in {"linux", "linux2"}:
        return VISIBLE_AUTH_PLATFORM_LINUX
    if normalized in {"windows", "win", "win32"}:
        return VISIBLE_AUTH_PLATFORM_WINDOWS
    if normalized in {"mac", "macos", "darwin", "osx", "os-x"}:
        return VISIBLE_AUTH_PLATFORM_MAC
    if default is not None:
        return normalize_visible_auth_platform(default)
    return VISIBLE_AUTH_PLATFORM_LINUX


def current_visible_auth_platform() -> str:
    return normalize_visible_auth_platform(platform.system())


def _provider_env_key(provider: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(provider or "").strip().upper()).strip("_")


def resolve_provider_visible_auth_platform(provider: str) -> str:
    provider_key = _provider_env_key(provider)
    env_names = []
    if provider_key:
        env_names.append(f"BREAKTWENTY_{provider_key}_VISIBLE_AUTH_PLATFORM")
    env_names.append("BREAKTWENTY_VISIBLE_AUTH_PLATFORM")
    for env_name in env_names:
        configured = str(os.getenv(env_name) or "").strip()
        if configured:
            return normalize_visible_auth_platform(configured)
    return current_visible_auth_platform()


def provider_visible_auth_flow(provider: str, platform_name: str) -> str:
    normalized_provider = str(provider or "").strip().lower() or "provider"
    return f"{normalized_provider}:{normalize_visible_auth_platform(platform_name)}"


def print_status(message: str) -> None:
    print(message, flush=True)


def print_visible_auth_result(payload: dict[str, Any]) -> None:
    print(
        f"{VISIBLE_AUTH_RESULT_PREFIX}{json.dumps(payload, separators=(',', ':'))}",
        flush=True,
    )


def print_visible_auth_log_path(log_path: Path | str) -> None:
    print(f"{VISIBLE_AUTH_LOG_PATH_PREFIX}{log_path}", flush=True)


def format_managed_browser_launch_error(
    exc: Exception,
    *,
    provider_display_name: str,
    retry_message: str | None = None,
) -> str:
    retry_message = retry_message or f"Restart {APP_BRAND_NAME} Desktop so it can finish browser setup."
    message = str(exc or "").strip()
    lowered = message.lower()
    if (
        "executable doesn't exist" in lowered
        or "browser executable" in lowered
        or "please run the following command" in lowered
        or "failed to launch" in lowered
    ):
        return (
            f"{APP_BRAND_NAME} could not launch the managed browser runtime for {provider_display_name} secure login. "
            f"{retry_message}"
        )
    return f"Could not open the {provider_display_name} secure browser. {message}".strip()


def build_visible_auth_handoff_result(
    *,
    provider: str,
    attempt_id: str,
    sync_id: str,
    storage_result: dict[str, Any],
    session_result: dict[str, Any],
    credentials_result: dict[str, Any],
    synced: bool = False,
    authenticated: bool = True,
    credentials_captured: bool | None = None,
    response_status: str = "handoff_ready",
) -> dict[str, Any]:
    return {
        "provider": provider,
        "attemptId": attempt_id,
        "syncId": sync_id,
        "synced": synced,
        "authenticated": authenticated,
        "storageStateStaged": bool(storage_result.get("staged")),
        "sessionArtifactStaged": bool(session_result.get("staged")),
        "credentialsCaptured": bool(credentials_result) if credentials_captured is None else credentials_captured,
        "credentialsStaged": bool(credentials_result.get("staged")),
        "responseStatus": response_status,
    }


def visible_auth_timeout_seconds(
    env_name: str = "BREAKTWENTY_VISIBLE_AUTH_TIMEOUT_SECONDS",
    *,
    default: float | int | None = None,
) -> float | None:
    raw = str(os.getenv(env_name) or "").strip().lower()
    if raw in {"", "0", "false", "no", "off", "none", "null", "unbounded"}:
        if raw:
            return None
        return float(default) if default and float(default) > 0 else None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return float(default) if default and float(default) > 0 else None
    return seconds if seconds > 0 else None


def visible_auth_deadline(timeout_seconds: float | int | None) -> float | None:
    if timeout_seconds is None:
        return None
    return asyncio.get_running_loop().time() + float(timeout_seconds)


def visible_auth_deadline_active(deadline: float | None) -> bool:
    return deadline is None or asyncio.get_running_loop().time() < deadline


async def _visible_auth_heartbeat_loop(interval_seconds: float) -> None:
    while True:
        print(f"{VISIBLE_AUTH_HEARTBEAT_PREFIX}{datetime.now(timezone.utc).isoformat()}", flush=True)
        await asyncio.sleep(max(float(interval_seconds), 1.0))


async def run_with_heartbeat(
    awaitable: Awaitable[Any],
    *,
    interval_seconds: float = DEFAULT_VISIBLE_AUTH_HEARTBEAT_SECONDS,
) -> Any:
    heartbeat_task = asyncio.create_task(_visible_auth_heartbeat_loop(interval_seconds))
    try:
        return await awaitable
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


def visible_auth_error_message(exc: BaseException, host_browser_closed_message: str | None = None) -> str:
    if host_browser_closed_message and (
        is_browser_closed_error(exc) or str(exc) == host_browser_closed_message
    ):
        return host_browser_closed_message
    return str(exc)


def run_visible_auth_main(
    run_factory: Callable[[], Awaitable[Any]],
    *,
    host_browser_closed_message: str | None = None,
    install_signal_handlers: bool = False,
) -> int:
    if install_signal_handlers:
        return _run_visible_auth_signal_main(
            run_factory,
            host_browser_closed_message=host_browser_closed_message,
        )
    exit_code = 0
    terminal_error = ""
    try:
        asyncio.run(run_with_heartbeat(run_factory()))
    except KeyboardInterrupt:
        exit_code = 130
        terminal_error = "Desktop visible auth was cancelled."
    except Exception as exc:
        exit_code = 1
        terminal_error = visible_auth_error_message(exc, host_browser_closed_message)
    finally:
        print_visible_auth_diagnostics_ready(
            fallback_result="cancelled" if exit_code == 130 else ("failed" if exit_code else ""),
            fallback_message=terminal_error,
        )
        revoke_runner_session()
    if terminal_error:
        print(terminal_error, file=sys.stderr, flush=True)
    return exit_code


def _run_visible_auth_signal_main(
    run_factory: Callable[[], Awaitable[Any]],
    *,
    host_browser_closed_message: str | None = None,
) -> int:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task: asyncio.Task[Any] | None = None

    def request_shutdown() -> None:
        if task is not None and not task.done():
            task.cancel()

    exit_code = 0
    terminal_error = ""
    try:
        task = loop.create_task(run_with_heartbeat(run_factory()))
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except (NotImplementedError, RuntimeError):
                pass
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        exit_code = 130
        terminal_error = "Desktop visible auth was cancelled."
    except KeyboardInterrupt:
        exit_code = 130
        terminal_error = "Desktop visible auth was cancelled."
    except Exception as exc:
        exit_code = 1
        terminal_error = visible_auth_error_message(exc, host_browser_closed_message)
    finally:
        try:
            pending = [
                pending_task
                for pending_task in asyncio.all_tasks(loop)
                if not pending_task.done()
            ]
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            print_visible_auth_diagnostics_ready(
                fallback_result="cancelled" if exit_code == 130 else ("failed" if exit_code else ""),
                fallback_message=terminal_error,
            )
            loop.close()
            revoke_runner_session()
    if terminal_error:
        print(terminal_error, file=sys.stderr, flush=True)
    return exit_code


def browser_runtime_details(browser_runtime: dict[str, Any] | None) -> dict[str, Any]:
    runtime = browser_runtime or {}
    return {
        "browser_runtime": str(runtime.get("runtime") or "unknown"),
        "runtime_mode": str(runtime.get("mode") or "unknown"),
        "runtime_version": str(runtime.get("version") or ""),
        "runtime_platform": str(runtime.get("platform") or ""),
        "installed_now": bool(runtime.get("installed_now")),
        "executable_path": str(runtime.get("executable_path") or "patchright_default"),
    }


def managed_browser_launch_args() -> list[str]:
    return []


WINDOWS_MANAGED_BRAVE_STABILIZER_ARGS = (
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
)

MAC_MANAGED_BRAVE_STABILIZER_ARGS: tuple[str, ...] = ()


def managed_browser_launch_kwargs(
    browser_runtime: dict[str, Any] | None,
    *,
    windows_managed_brave_stabilizers: bool = False,
    mac_managed_brave_stabilizers: bool = False,
) -> dict[str, Any]:
    launch_kwargs: dict[str, Any] = {"headless": False}
    executable_path = str((browser_runtime or {}).get("executable_path") or "").strip()
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    launch_args = managed_browser_launch_args()
    if launch_args:
        launch_kwargs["args"] = launch_args
    runtime_platform = str((browser_runtime or {}).get("platform") or "").strip().lower()
    if windows_managed_brave_stabilizers and runtime_platform == "win64":
        stabilized_args = list(launch_kwargs.get("args") or [])
        for arg in WINDOWS_MANAGED_BRAVE_STABILIZER_ARGS:
            if arg not in stabilized_args:
                stabilized_args.append(arg)
        launch_kwargs["args"] = stabilized_args
    if mac_managed_brave_stabilizers and runtime_platform.startswith("macos-"):
        stabilized_args = list(launch_kwargs.get("args") or [])
        for arg in MAC_MANAGED_BRAVE_STABILIZER_ARGS:
            if arg not in stabilized_args:
                stabilized_args.append(arg)
        launch_kwargs["args"] = stabilized_args
    return launch_kwargs


def browser_launch_details(launch_kwargs: dict[str, Any] | None) -> dict[str, Any]:
    launch = launch_kwargs or {}
    return {
        "headless": bool(launch.get("headless")),
        "launch_args": list(launch.get("args") or []),
    }


def browser_context_details(context_kwargs: dict[str, Any] | None) -> dict[str, Any]:
    context = context_kwargs or {}
    return {
        "timezone_id": str(context.get("timezone_id") or ""),
        "storage_state_used": "storage_state" in context,
        "user_agent_used": bool(context.get("user_agent")),
        "har_capture_enabled": bool(context.get("record_har_path")),
        "record_har_mode": str(context.get("record_har_mode") or ""),
        "record_har_content": str(context.get("record_har_content") or ""),
    }


async def log_browser_launch_stage(
    log_support_event: Callable[..., Any],
    *,
    provider_display_name: str,
    stage: str,
    result: str,
    browser_runtime: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    message: str = "",
) -> None:
    event_details: dict[str, Any] = {"provider_display_name": provider_display_name}
    if browser_runtime is not None:
        event_details.update(browser_runtime_details(browser_runtime))
    if details:
        event_details.update(details)
    event_message = message or f"{provider_display_name} visible-auth {stage} {result}."
    await log_support_event(
        stage=stage,
        result=result,
        debug=True,
        message=event_message,
        details=event_details,
    )


def username_looks_like_placeholder(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(
        _SHELL_PLACEHOLDER_USERNAME_RE.fullmatch(text)
        or _PERCENT_PLACEHOLDER_USERNAME_RE.fullmatch(text)
        or _TEMPLATE_PLACEHOLDER_USERNAME_RE.fullmatch(text)
    )


def _is_masked_secret(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text) and set(text) <= {"*", "x", "X", ".", "-", " "}


async def selector_is_visible(page, selector: str) -> bool:
    try:
        element = await page.query_selector(selector)
    except Exception:
        return False
    if not element:
        return False
    try:
        return await element.is_visible()
    except Exception:
        return False


def _credentials_from_payload(payload: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    if username_looks_like_placeholder(username):
        return None
    if username or password:
        return (username, password)
    return None


def _normalize_local_backend_api_url(value: Any) -> str:
    backend_api_base_url = str(value or "")
    if not backend_api_base_url or backend_api_base_url != backend_api_base_url.strip() or "\\" in backend_api_base_url:
        return ""
    try:
        parsed = urlsplit(backend_api_base_url)
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    normalized_path = parsed.path.rstrip("/") or "/"
    if (
        parsed.scheme != "http"
        or hostname not in LOOPBACK_BACKEND_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port == 0
        or normalized_path != "/api"
    ):
        return ""
    authority = f"[{hostname}]" if hostname == "::1" else hostname
    if port is not None:
        authority = f"{authority}:{port}"
    return f"http://{authority}/api"


def _backend_api_base_url_from_env() -> str:
    return _normalize_local_backend_api_url(os.getenv("BREAKTWENTY_BACKEND_API_URL"))


def _validate_local_backend_request_url(url: str, *, backend_api_base_url: str) -> tuple[str, str]:
    normalized_base_url = _normalize_local_backend_api_url(backend_api_base_url)
    if not normalized_base_url:
        raise RuntimeError(f"Refusing to contact an unsupported {APP_BRAND_NAME} backend URL")
    parsed_base = urlsplit(normalized_base_url)
    try:
        parsed_url = urlsplit(str(url or ""))
        target_port = parsed_url.port
    except ValueError as exc:
        raise RuntimeError(f"Refusing to contact an unsupported {APP_BRAND_NAME} backend URL") from exc
    base_port = parsed_base.port or 80
    if (
        parsed_url.scheme != "http"
        or str(parsed_url.hostname or "").lower() != str(parsed_base.hostname or "").lower()
        or (target_port or 80) != base_port
        or parsed_url.username
        or parsed_url.password
        or parsed_url.fragment
        or "\\" in str(url or "")
        or not (parsed_url.path == "/api" or parsed_url.path.startswith("/api/"))
    ):
        raise RuntimeError(f"Refusing to contact an unsupported {APP_BRAND_NAME} backend URL")
    return str(url), normalized_base_url


def _validate_runner_token(value: Any, *, label: str) -> str:
    token = str(value or "")
    if not RUNNER_TOKEN_RE.fullmatch(token):
        raise RuntimeError(f"{APP_BRAND_NAME} {label} is invalid")
    import base64
    import binascii

    try:
        decoded = base64.b64decode(token.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise RuntimeError(f"{APP_BRAND_NAME} {label} is invalid") from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != token:
        raise RuntimeError(f"{APP_BRAND_NAME} {label} is invalid")
    return token


def _runner_authorization_header() -> str:
    global _runner_session_token
    if _runner_session_token:
        return f"Bearer {_runner_session_token}"
    with _runner_session_lock:
        if _runner_session_token:
            return f"Bearer {_runner_session_token}"
        bootstrap_bytes = sys.stdin.buffer.readline(RUNNER_BOOTSTRAP_MAX_BYTES + 1)
        if (
            not bootstrap_bytes
            or len(bootstrap_bytes) > RUNNER_BOOTSTRAP_MAX_BYTES
            or not bootstrap_bytes.endswith(b"\n")
        ):
            raise RuntimeError(f"{APP_BRAND_NAME} runner authorization bootstrap is missing")
        try:
            bootstrap = json.loads(bootstrap_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{APP_BRAND_NAME} runner authorization bootstrap is invalid") from exc
        if not isinstance(bootstrap, dict) or bootstrap.get("version") != 1:
            raise RuntimeError(f"{APP_BRAND_NAME} runner authorization bootstrap is unsupported")
        grant = _validate_runner_token(bootstrap.get("grant"), label="runner grant")
        backend_api_base_url = _backend_api_base_url_from_env()
        if not backend_api_base_url:
            raise RuntimeError(f"{APP_BRAND_NAME} backend URL is unavailable")
        response = _post_json_with_bearer(
            f"{backend_api_base_url}/auth/runner-session",
            {},
            backend_api_base_url=backend_api_base_url,
            authorization=f"Bearer {grant}",
        )
        if response.get("status") != "ok" or str(response.get("token_type") or "").lower() != "bearer":
            raise RuntimeError(f"{APP_BRAND_NAME} runner grant exchange failed")
        _runner_session_token = _validate_runner_token(
            response.get("access_token"),
            label="runner session",
        )
        return f"Bearer {_runner_session_token}"


def load_provider_credentials(*, provider: str) -> tuple[str, str] | None:
    backend_api_base_url = _backend_api_base_url_from_env()
    if not backend_api_base_url:
        return None
    normalized_provider = str(provider or "").strip().lower()
    scoped_provider = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or "").strip().lower()
    if not normalized_provider or normalized_provider != scoped_provider:
        return None
    url = f"{backend_api_base_url}/auth/runner-credentials"
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": _runner_authorization_header(),
        },
        method="GET",
    )
    try:
        # Backend base URL is restricted to the loopback API by _backend_api_base_url_from_env.
        with urllib_request.urlopen(request, timeout=CREDENTIAL_FETCH_TIMEOUT_SECONDS) as response:  # nosec B310
            body = response.read().decode("utf-8")
    except Exception:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if payload.get("status") != "ok":
        return None
    return _credentials_from_payload(payload)


def load_provider_artifact(
    *,
    provider: str,
    artifact_kind: str,
    slots: tuple[str, ...] = ("active",),
) -> tuple[Any | None, str | None]:
    backend_api_base_url = _backend_api_base_url_from_env()
    if not backend_api_base_url:
        return None, None
    normalized_slots = tuple(str(slot or "").strip().lower() for slot in slots if str(slot or "").strip())
    if not normalized_slots:
        return None, None
    query = urlencode([("slots", slot) for slot in normalized_slots])
    url = (
        f"{backend_api_base_url}/sync/runtime-artifact/"
        f"{quote(str(provider or '').strip().lower(), safe='')}/"
        f"{quote(str(artifact_kind or '').strip().lower(), safe='')}"
    )
    if query:
        url = f"{url}?{query}"
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": _runner_authorization_header(),
        },
        method="GET",
    )
    try:
        # Backend base URL is restricted to the loopback API by _backend_api_base_url_from_env.
        with urllib_request.urlopen(request, timeout=CREDENTIAL_FETCH_TIMEOUT_SECONDS) as response:  # nosec B310
            body = response.read().decode("utf-8")
    except Exception:
        return None, None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    if payload.get("status") != "ok":
        return None, None
    return payload.get("payload"), str(payload.get("slot") or "")


def _post_json_with_bearer(
    url: str,
    payload: dict[str, Any],
    *,
    backend_api_base_url: str,
    authorization: str,
) -> dict[str, Any]:
    validated_url, normalized_base_url = _validate_local_backend_request_url(
        url,
        backend_api_base_url=backend_api_base_url,
    )
    request_payload = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        validated_url,
        data=request_payload,
        headers={"Content-Type": "application/json", "Authorization": authorization},
        method="POST",
    )
    try:
        # URL origin and API prefix are validated above before urlopen.
        with urllib_request.urlopen(request, timeout=30) as response:  # nosec B310
            body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(body or str(exc)) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not reach the local {APP_BRAND_NAME} backend at {normalized_base_url}.") from exc

    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Received an invalid response from the local {APP_BRAND_NAME} backend.") from exc


def backend_post_json(url: str, payload: dict[str, Any], *, backend_api_base_url: str) -> dict[str, Any]:
    return _post_json_with_bearer(
        url,
        payload,
        backend_api_base_url=backend_api_base_url,
        authorization=_runner_authorization_header(),
    )


def revoke_runner_session(*, backend_api_base_url: str | None = None) -> None:
    global _runner_session_token
    token = _runner_session_token
    _runner_session_token = None
    base_url = _normalize_local_backend_api_url(backend_api_base_url or _backend_api_base_url_from_env())
    if not token or not base_url:
        return
    try:
        _post_json_with_bearer(
            f"{base_url}/auth/runner-session/revoke",
            {},
            backend_api_base_url=base_url,
            authorization=f"Bearer {token}",
        )
    except Exception:
        return


@dataclass
class VisibleAuthBackendClient:
    backend_api_base_url: str
    provider: str
    attempt_id: str
    add_flow: bool
    sync_id: str = ""
    artifact_endpoint: str = field(init=False)
    credentials_endpoint: str = field(init=False)
    support_event_endpoint: str = field(init=False)
    terminal_support_event: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        base = _normalize_local_backend_api_url(self.backend_api_base_url)
        if not base:
            raise ValueError(f"{APP_BRAND_NAME} backend URL must use the private local API boundary")
        self.backend_api_base_url = base
        self.artifact_endpoint = f"{base}/sync/visible-auth-artifact"
        self.credentials_endpoint = f"{base}/sync/visible-auth-credentials"
        self.support_event_endpoint = f"{base}/settings/support-logs/client-event"
        _visible_auth_backend_clients.append(self)

    def runtime_artifact_endpoint(self, artifact_type: str) -> str:
        return (
            f"{self.backend_api_base_url}/sync/runtime-artifact/"
            f"{quote(self.provider, safe='')}/"
            f"{quote(str(artifact_type or '').strip().lower(), safe='')}"
        )

    async def log_support_event(
        self,
        *,
        stage: str,
        result: str = "",
        level: str = "info",
        debug: bool = False,
        message: str = "",
        last_output: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized_stage = str(stage or "").strip().lower()
        normalized_result = str(result or "").strip().lower()
        if normalized_stage == "sync result" and normalized_result in {"error", "failed", "cancelled"}:
            self.terminal_support_event = {
                "provider": self.provider,
                "attempt_id": self.attempt_id,
                "sync_id": self.sync_id,
                "add_flow": self.add_flow,
                "result": normalized_result,
                "message": str(message or ""),
                "last_output": str(last_output or ""),
            }
        payload = {
            "source": "desktop_visible_auth",
            "provider": self.provider,
            "sync_id": self.sync_id or None,
            "add_flow": self.add_flow,
            "attempt_id": self.attempt_id,
            "stage": stage,
            "result": result,
            "level": level,
            "debug": debug,
            "message": message,
            "last_output": last_output,
            "details": details or {},
        }
        try:
            response = await asyncio.to_thread(
                backend_post_json,
                self.support_event_endpoint,
                payload,
                backend_api_base_url=self.backend_api_base_url,
            )
            sync_id = str(response.get("sync_id") or "").strip()
            if sync_id:
                self.sync_id = sync_id
        except Exception:
            return

    def consume_diagnostics_ready_payload(
        self,
        *,
        fallback_result: str = "",
        fallback_message: str = "",
    ) -> dict[str, Any] | None:
        payload = self.terminal_support_event
        self.terminal_support_event = None
        if payload is None:
            normalized_result = str(fallback_result or "").strip().lower()
            if normalized_result not in {"error", "failed", "cancelled"} or not self.sync_id:
                return None
            payload = {
                "provider": self.provider,
                "attempt_id": self.attempt_id,
                "sync_id": self.sync_id,
                "add_flow": self.add_flow,
                "result": normalized_result,
                "message": str(fallback_message or ""),
                "last_output": str(fallback_message or ""),
            }
        payload["sync_id"] = str(payload.get("sync_id") or self.sync_id or "").strip()
        return payload if payload["sync_id"] else None

    async def persist_artifact(
        self,
        *,
        artifact_type: str,
        payload: Any,
    ) -> dict[str, Any]:
        response = await asyncio.to_thread(
            backend_post_json,
            self.artifact_endpoint,
            {
                "provider": self.provider,
                "attempt_id": self.attempt_id,
                "artifact_type": artifact_type,
                "payload": payload,
                "add_flow": self.add_flow,
            },
            backend_api_base_url=self.backend_api_base_url,
        )
        if response.get("status") != "ok":
            raise RuntimeError(response.get("message") or f"Failed to persist the {artifact_type} artifact.")
        return response

    async def persist_runtime_artifact(
        self,
        *,
        artifact_type: str,
        payload: Any,
    ) -> dict[str, Any]:
        response = await asyncio.to_thread(
            backend_post_json,
            self.runtime_artifact_endpoint(artifact_type),
            {"payload": payload},
            backend_api_base_url=self.backend_api_base_url,
        )
        if response.get("status") not in {"ok", "not_ready"}:
            raise RuntimeError(response.get("message") or f"Failed to persist the {artifact_type} runtime artifact.")
        return response

    async def persist_credentials(self, username: str, password: str) -> dict[str, Any]:
        response = await asyncio.to_thread(
            backend_post_json,
            self.credentials_endpoint,
            {
                "provider": self.provider,
                "attempt_id": self.attempt_id,
                "add_flow": self.add_flow,
                "username": username,
                "password": password,
            },
            backend_api_base_url=self.backend_api_base_url,
        )
        if response.get("status") != "ok":
            raise RuntimeError(response.get("message") or "Failed to persist the visible-auth credentials.")
        return response


def print_visible_auth_diagnostics_ready(
    *,
    fallback_result: str = "",
    fallback_message: str = "",
) -> None:
    clients = tuple(_visible_auth_backend_clients)
    _visible_auth_backend_clients.clear()
    for client in clients:
        payload = client.consume_diagnostics_ready_payload(
            fallback_result=fallback_result,
            fallback_message=fallback_message,
        )
        if payload is None:
            continue
        print(
            f"{VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX}{json.dumps(payload, separators=(',', ':'))}",
            flush=True,
        )


def is_browser_closed_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "target page" in message or "target closed" in message or "has been closed" in message


def is_page_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


async def close_visible_auth_context_after_capture(
    context,
    *,
    provider_display_name: str,
    log_support_event: Callable[..., Any] | None = None,
    context_close_timeout_seconds: float | None = None,
    browser_close_timeout_seconds: float | None = None,
    force_browser_process_close: bool = False,
    browser_executable_path: str | Path | None = None,
) -> bool:
    if context is None:
        return False
    context_close_timeout = context_close_timeout_seconds or VISIBLE_AUTH_CONTEXT_CLOSE_TIMEOUT_SECONDS
    browser_close_timeout = browser_close_timeout_seconds or VISIBLE_AUTH_BROWSER_CLOSE_TIMEOUT_SECONDS
    pages_closed = 0
    page_close_timeouts = 0
    page_close_failures = 0
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    release_details = await _release_visible_auth_handoff_lock_sessions(context, pages)
    for page in pages:
        if is_page_closed(page):
            continue
        try:
            await asyncio.wait_for(page.close(), timeout=VISIBLE_AUTH_PAGE_CLOSE_TIMEOUT_SECONDS)
            pages_closed += 1
        except asyncio.TimeoutError:
            page_close_timeouts += 1
        except Exception:
            page_close_failures += 1
    context_closed = False
    context_close_timed_out = False
    context_close_failed = False
    browser_closed = False
    browser_close_timed_out = False
    browser_close_failed = False
    browser = _visible_auth_context_browser(context)
    browser_process = _visible_auth_browser_process(browser) if browser is not None else None
    process_close_details = {
        "browser_process_pid_found": False,
        "browser_process_force_close_attempted": False,
        "browser_process_force_closed": False,
        "browser_process_force_close_failed": False,
        "browser_process_executable_match_attempted": False,
        "browser_process_executable_match_found": False,
        "browser_process_executable_match_closed": False,
    }
    try:
        await asyncio.wait_for(context.close(), timeout=context_close_timeout)
        context_closed = True
    except asyncio.TimeoutError:
        context_close_timed_out = True
    except Exception as exc:
        if is_browser_closed_error(exc):
            context_closed = True
        else:
            context_close_failed = True
    if not context_closed:
        if browser is not None and not browser_closed:
            try:
                await asyncio.wait_for(browser.close(), timeout=browser_close_timeout)
                browser_closed = True
            except asyncio.TimeoutError:
                browser_close_timed_out = True
            except Exception as exc:
                if is_browser_closed_error(exc):
                    browser_closed = True
                else:
                    browser_close_failed = True
    if force_browser_process_close and (browser_process is not None or browser_executable_path):
        process_close_details = await asyncio.to_thread(
            _force_close_visible_auth_browser_process,
            browser_process,
            executable_path=browser_executable_path,
        )
        if process_close_details.get("browser_process_force_closed"):
            browser_closed = True
    closed = context_closed or browser_closed
    if log_support_event is not None:
        try:
            await log_support_event(
                stage="browser closed" if closed else "browser close failed",
                debug=True,
                level="info" if closed else "warning",
                message=(
                    f"{provider_display_name} secure browser closed after handoff material was captured."
                    if closed
                    else f"{provider_display_name} secure browser did not close cleanly after handoff material was captured."
                ),
                details={
                    "closed_after_capture": closed,
                    "pages_closed": pages_closed,
                    "page_close_timeouts": page_close_timeouts,
                    "page_close_failures": page_close_failures,
                    "context_closed": context_closed,
                    "context_close_timed_out": context_close_timed_out,
                    "context_close_failed": context_close_failed,
                    "browser_closed": browser_closed,
                    "browser_close_timed_out": browser_close_timed_out,
                    "browser_close_failed": browser_close_failed,
                    **process_close_details,
                    **release_details,
                },
            )
        except Exception:
            pass
    return closed


async def close_visible_auth_context_quietly(
    context,
    browser=None,
    *,
    context_close_timeout_seconds: float | None = None,
    browser_close_timeout_seconds: float | None = None,
) -> None:
    context_timeout = context_close_timeout_seconds or VISIBLE_AUTH_CONTEXT_CLOSE_TIMEOUT_SECONDS
    browser_timeout = browser_close_timeout_seconds or VISIBLE_AUTH_BROWSER_CLOSE_TIMEOUT_SECONDS
    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=context_timeout)
        except Exception:
            pass
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=browser_timeout)
        except Exception:
            pass


def _visible_auth_context_browser(context):
    try:
        browser = getattr(context, "browser", None)
        if callable(browser):
            return browser()
        return browser
    except Exception:
        return None


def _visible_auth_browser_process(browser) -> Any | None:
    for attr_path in (
        ("process",),
        ("_process",),
        ("_impl_obj", "_connection", "_transport", "_proc"),
        ("_connection", "_transport", "_proc"),
    ):
        current = browser
        for attr_name in attr_path:
            try:
                current = getattr(current, attr_name)
            except Exception:
                current = None
            if current is None:
                break
        if current is None:
            continue
        if attr_path == ("process",) and callable(current):
            try:
                current = current()
            except Exception:
                current = None
        if _visible_auth_process_pid(current):
            return current
    return None


def _visible_auth_process_pid(process: Any | None) -> int | None:
    try:
        pid = int(getattr(process, "pid", 0) or 0)
    except Exception:
        return None
    return pid if pid > 0 else None


def _visible_auth_process_exited(process: Any | None) -> bool:
    try:
        poll = getattr(process, "poll", None)
        if callable(poll):
            return poll() is not None
    except Exception:
        pass
    try:
        return getattr(process, "returncode", None) is not None
    except Exception:
        return False


def _optional_system_tool(name: str) -> str:
    return shutil.which(name) or name


def _force_close_visible_auth_browser_process(
    browser_or_process,
    *,
    executable_path: str | Path | None = None,
) -> dict[str, bool]:
    details = {
        "browser_process_pid_found": False,
        "browser_process_force_close_attempted": False,
        "browser_process_force_closed": False,
        "browser_process_force_close_failed": False,
        "browser_process_executable_match_attempted": False,
        "browser_process_executable_match_found": False,
        "browser_process_executable_match_closed": False,
    }
    process = browser_or_process
    if not _visible_auth_process_pid(process):
        process = _visible_auth_browser_process(browser_or_process)
    pid = _visible_auth_process_pid(process)
    if not pid:
        if executable_path and current_visible_auth_platform() == VISIBLE_AUTH_PLATFORM_WINDOWS:
            executable_details = _force_close_windows_processes_by_executable_path(executable_path)
            details.update(executable_details)
            if executable_details.get("browser_process_executable_match_closed"):
                details["browser_process_force_closed"] = True
        return details
    details["browser_process_pid_found"] = True
    if _visible_auth_process_exited(process):
        details["browser_process_force_closed"] = True
        if executable_path and current_visible_auth_platform() == VISIBLE_AUTH_PLATFORM_WINDOWS:
            executable_details = _force_close_windows_processes_by_executable_path(executable_path)
            details.update(executable_details)
            if executable_details.get("browser_process_executable_match_closed"):
                details["browser_process_force_closed"] = True
        return details
    if current_visible_auth_platform() != VISIBLE_AUTH_PLATFORM_WINDOWS:
        return details
    details["browser_process_force_close_attempted"] = True
    try:
        taskkill = _optional_system_tool("taskkill")
        subprocess_kwargs: dict[str, Any] = {}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            subprocess_kwargs["creationflags"] = creationflags
        completed = subprocess.run(
            [taskkill, "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=VISIBLE_AUTH_PROCESS_FORCE_CLOSE_TIMEOUT_SECONDS,
            check=False,
            **subprocess_kwargs,
        )  # nosec B603
        details["browser_process_force_closed"] = completed.returncode == 0 or _visible_auth_process_exited(process)
    except Exception:
        details["browser_process_force_close_failed"] = True
    if executable_path and current_visible_auth_platform() == VISIBLE_AUTH_PLATFORM_WINDOWS:
        executable_details = _force_close_windows_processes_by_executable_path(executable_path)
        details.update(executable_details)
        if executable_details.get("browser_process_executable_match_closed"):
            details["browser_process_force_closed"] = True
    if not details["browser_process_force_closed"]:
        details["browser_process_force_close_failed"] = True
    return details


def _force_close_windows_processes_by_executable_path(executable_path: str | Path) -> dict[str, bool]:
    details = {
        "browser_process_executable_match_attempted": False,
        "browser_process_executable_match_found": False,
        "browser_process_executable_match_closed": False,
    }
    if current_visible_auth_platform() != VISIBLE_AUTH_PLATFORM_WINDOWS:
        return details
    target = str(executable_path or "").strip()
    if not target:
        return details
    details["browser_process_executable_match_attempted"] = True
    escaped_target = target.replace("'", "''")
    script = (
        f"$target = '{escaped_target}'; "
        "$items = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $target }); "
        "$closed = 0; "
        "foreach ($item in $items) { "
        "taskkill.exe /PID $item.ProcessId /T /F | Out-Null; "
        "if ($LASTEXITCODE -eq 0) { $closed += 1 } "
        "} "
        'Write-Output ("found={0};closed={1}" -f $items.Count, $closed)'
    )
    try:
        powershell = _optional_system_tool("powershell.exe")
        subprocess_kwargs: dict[str, Any] = {}
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            subprocess_kwargs["creationflags"] = creationflags
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=VISIBLE_AUTH_PROCESS_FORCE_CLOSE_TIMEOUT_SECONDS,
            check=False,
            **subprocess_kwargs,
        )  # nosec B603
    except Exception:
        return details
    output = str(completed.stdout or "")
    found_match = re.search(r"found=(\d+)", output)
    closed_match = re.search(r"closed=(\d+)", output)
    found = int(found_match.group(1)) if found_match else 0
    closed = int(closed_match.group(1)) if closed_match else 0
    details["browser_process_executable_match_found"] = found > 0
    details["browser_process_executable_match_closed"] = closed > 0
    return details


async def _release_visible_auth_handoff_lock_sessions(context, pages: list[Any] | None = None) -> dict[str, int]:
    sessions: list[Any] = []
    try:
        sessions.extend(list(getattr(context, "_visible_auth_handoff_lock_sessions", []) or []))
    except Exception:
        pass
    for page in pages or []:
        try:
            sessions.extend(list(getattr(page, "_visible_auth_handoff_lock_sessions", []) or []))
        except Exception:
            pass
    seen: set[int] = set()
    released_sessions = 0
    detached_sessions = 0
    release_failures = 0
    for session in sessions:
        session_id = id(session)
        if session_id in seen:
            continue
        seen.add(session_id)
        try:
            await asyncio.wait_for(
                session.send("Input.setIgnoreInputEvents", {"ignore": False}),
                timeout=VISIBLE_AUTH_CDP_RELEASE_TIMEOUT_SECONDS,
            )
            released_sessions += 1
        except Exception:
            release_failures += 1
        try:
            detach = getattr(session, "detach", None)
            if callable(detach):
                await asyncio.wait_for(detach(), timeout=VISIBLE_AUTH_CDP_RELEASE_TIMEOUT_SECONDS)
                detached_sessions += 1
        except Exception:
            pass
    return {
        "lock_sessions_seen": len(seen),
        "lock_sessions_released": released_sessions,
        "lock_sessions_detached": detached_sessions,
        "lock_session_release_failures": release_failures,
    }


async def _apply_visible_auth_page_handoff_lock(context, page) -> bool:
    if context is None or is_page_closed(page):
        return False
    if getattr(page, "_visible_auth_handoff_lock_applied", False):
        return True
    try:
        cdp_session = await context.new_cdp_session(page)
        await cdp_session.send("Input.setIgnoreInputEvents", {"ignore": True})
    except Exception:
        return False
    try:
        setattr(page, "_visible_auth_handoff_lock_applied", True)
    except Exception:
        pass
    try:
        page_cdp_sessions = list(getattr(page, "_visible_auth_handoff_lock_sessions", []) or [])
        page_cdp_sessions.append(cdp_session)
        setattr(page, "_visible_auth_handoff_lock_sessions", page_cdp_sessions)
    except Exception:
        pass
    try:
        cdp_sessions = list(getattr(context, "_visible_auth_handoff_lock_sessions", []) or [])
        cdp_sessions.append(cdp_session)
        setattr(context, "_visible_auth_handoff_lock_sessions", cdp_sessions)
    except Exception:
        pass
    return True


def _install_visible_auth_context_handoff_relock(context) -> None:
    if context is None or getattr(context, "_visible_auth_handoff_relock_installed", False):
        return
    try:
        setattr(context, "_visible_auth_handoff_relock_installed", True)
    except Exception:
        pass

    def on_page(page) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def lock_new_page() -> None:
            try:
                await _apply_visible_auth_page_handoff_lock(context, page)
            except Exception:
                return

        loop.create_task(lock_new_page())

    try:
        context.on("page", on_page)
    except Exception:
        return


def visible_auth_context_handoff_lock_started(context) -> bool:
    if context is None:
        return False
    try:
        return bool(getattr(context, "_visible_auth_context_handoff_lock_started", False))
    except Exception:
        return False


async def lock_visible_auth_context_after_authentication(
    context,
    *,
    provider_display_name: str,
    log_support_event: Callable[..., Any] | None = None,
    reason: str | None = None,
) -> int:
    if context is None:
        return 0
    if not visible_auth_context_handoff_lock_started(context):
        try:
            setattr(context, "_visible_auth_context_handoff_lock_started", True)
            setattr(context, "_visible_auth_context_handoff_lock_reason", str(reason or "authenticated"))
        except Exception:
            pass
    _install_visible_auth_context_handoff_relock(context)
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    locked_pages = 0
    failed_pages = 0
    for page in pages:
        if is_page_closed(page):
            continue
        if await _apply_visible_auth_page_handoff_lock(context, page):
            locked_pages += 1
        else:
            failed_pages += 1
    if log_support_event is not None:
        try:
            await log_support_event(
                stage="browser input locked",
                debug=True,
                message=f"{provider_display_name} secure browser input locked after authentication.",
                details={
                    "locked_after_authentication": True,
                    "locked_pages": locked_pages,
                    "failed_pages": failed_pages,
                    "lock_reason": str(reason or getattr(context, "_visible_auth_context_handoff_lock_reason", "") or "authenticated"),
                },
            )
        except Exception:
            pass
    return locked_pages


async def ensure_visible_auth_context_handoff_lock(
    context,
    *,
    provider_display_name: str,
    log_support_event: Callable[..., Any] | None = None,
    reason: str = "authenticated",
) -> int:
    if context is None:
        return 0
    lock = getattr(context, "_visible_auth_context_handoff_lock_mutex", None)
    if lock is None:
        lock = asyncio.Lock()
        try:
            setattr(context, "_visible_auth_context_handoff_lock_mutex", lock)
        except Exception:
            pass
    async with lock:
        first_start = not visible_auth_context_handoff_lock_started(context)
        locked_pages = await lock_visible_auth_context_after_authentication(
            context,
            provider_display_name=provider_display_name,
            log_support_event=log_support_event if first_start else None,
            reason=reason,
        )
        return locked_pages


def safe_page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def normalize_capture_level(value: str | None = None) -> str:
    normalized = str(value if value is not None else os.getenv("BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL") or "").strip().lower()
    return normalized if normalized in CAPTURE_LEVELS else CAPTURE_LEVEL_REDACTED_RICH


def _looks_like_url_identifier(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        return False
    if len(re.sub(r"\D+", "", text)) >= 6:
        return True
    return bool(URL_IDENTIFIER_RE.fullmatch(text)) and any(character.isdigit() for character in text)


def _sanitize_url_path(path: str, *, redacted_value: str) -> str:
    return "/".join(
        redacted_value if _looks_like_url_identifier(segment) else segment
        for segment in str(path or "").split("/")
    )


def _sanitize_query_name(name: str, *, redacted_value: str) -> str:
    return redacted_value if _looks_like_url_identifier(name) else name


def sanitize_url(
    url: str | None,
    *,
    redacted_value: str = REDACTED_VALUE,
    redact_path_identifiers: bool = False,
    redact_query_names: bool = False,
    preserve_query_values: bool = False,
) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return str(url or "")
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    sanitized_query = urlencode(
        [
            (
                _sanitize_query_name(name, redacted_value=redacted_value)
                if redact_query_names
                else name,
                value
                if preserve_query_values and not _developer_local_body_field_is_sensitive(name)
                else redacted_value,
            )
            for name, value in query_items
        ],
        doseq=True,
    )
    path = _sanitize_url_path(parsed.path, redacted_value=redacted_value) if redact_path_identifiers else parsed.path
    return urlunsplit((parsed.scheme, parsed.netloc, path, sanitized_query, ""))


def page_label(page, *, url_sanitizer: Callable[[str | None], str] = sanitize_url) -> str:
    url = url_sanitizer(safe_page_url(page))
    if url:
        return url
    if page is not None and is_page_closed(page):
        return "<closed page>"
    return "<page without URL>"


async def request_headers(request) -> dict[str, str]:
    try:
        headers = await request.all_headers()
    except Exception:
        try:
            headers = request.headers or {}
        except Exception:
            headers = {}
    if not isinstance(headers, dict):
        headers = {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        normalized[str(key).lower()] = str(value)
    return normalized


def safe_request_post_data(request) -> str:
    try:
        post_data = getattr(request, "post_data", None)
    except Exception:
        post_data = None
    if callable(post_data):
        try:
            post_data = post_data()
        except Exception:
            post_data = None
    if isinstance(post_data, bytes):
        try:
            return post_data.decode("utf-8")
        except Exception:
            return ""
    if post_data:
        return str(post_data)
    try:
        post_data_buffer = getattr(request, "post_data_buffer", None)
    except Exception:
        return ""
    if callable(post_data_buffer):
        try:
            post_data_buffer = post_data_buffer()
        except Exception:
            return ""
    if isinstance(post_data_buffer, bytes):
        try:
            return post_data_buffer.decode("utf-8")
        except Exception:
            return ""
    return str(post_data_buffer or "")


class PopupInteractionLock:
    def __init__(self, page, cdp_session, *, lock_attribute_name: str | None = None) -> None:
        self._cdp_session = cdp_session
        self._locked = False
        self._raw_page = page
        self._lock_attribute_name = lock_attribute_name
        if lock_attribute_name:
            setattr(page, lock_attribute_name, self)
        self.page = PopupShieldedPage(page, self)

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def raw_page(self):
        return self._raw_page

    async def set_locked(self, locked: bool) -> bool:
        if self._locked == locked:
            return True
        try:
            await self._cdp_session.send("Input.setIgnoreInputEvents", {"ignore": locked})
        except Exception:
            self._locked = False
            return False
        self._locked = locked
        return True

    async def bind_page(self, page, cdp_session) -> bool:
        was_locked = self._locked
        self._cdp_session = cdp_session
        self._raw_page = page
        if self._lock_attribute_name:
            setattr(page, self._lock_attribute_name, self)
        self.page = PopupShieldedPage(page, self)
        self._locked = False
        return await self.set_locked(was_locked)


class PopupShieldedLocator:
    def __init__(self, locator, popup_lock: PopupInteractionLock) -> None:
        self._locator = locator
        self._popup_lock = popup_lock

    @property
    def first(self):
        return PopupShieldedLocator(self._locator.first, self._popup_lock)

    @property
    def last(self):
        return PopupShieldedLocator(self._locator.last, self._popup_lock)

    def nth(self, index: int):
        return PopupShieldedLocator(self._locator.nth(index), self._popup_lock)

    async def click(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._locator.click(*args, **kwargs)
        return await self._locator.evaluate(LOCKED_CLICK_SCRIPT)

    async def fill(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._locator.fill(*args, **kwargs)
        if not args:
            raise TypeError("locator.fill() missing value")
        return await self._locator.evaluate(DIRECT_PREFILL_SCRIPT, args[0])

    async def focus(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._locator.focus(*args, **kwargs)
        return await self._locator.evaluate(LOCKED_FOCUS_SCRIPT)

    async def type(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._locator.type(*args, **kwargs)
        if not args:
            raise TypeError("locator.type() missing text")
        return await self._locator.evaluate(DIRECT_PREFILL_SCRIPT, args[0])

    def locator(self, *args, **kwargs):
        return PopupShieldedLocator(self._locator.locator(*args, **kwargs), self._popup_lock)

    def __getattr__(self, name):
        return getattr(self._locator, name)


class PopupShieldedPage:
    def __init__(self, page, popup_lock: PopupInteractionLock) -> None:
        self._page = page
        self._popup_lock = popup_lock

    @property
    def raw_page(self):
        return self._page

    async def click(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._page.click(*args, **kwargs)
        if not args:
            raise TypeError("page.click() missing selector")
        return await self._page.locator(args[0]).evaluate(LOCKED_CLICK_SCRIPT)

    async def fill(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._page.fill(*args, **kwargs)
        if len(args) < 2:
            raise TypeError("page.fill() missing selector or value")
        return await self._page.locator(args[0]).evaluate(DIRECT_PREFILL_SCRIPT, args[1])

    async def focus(self, *args, **kwargs):
        if not self._popup_lock.locked:
            return await self._page.focus(*args, **kwargs)
        if not args:
            raise TypeError("page.focus() missing selector")
        return await self._page.locator(args[0]).evaluate(LOCKED_FOCUS_SCRIPT)

    def locator(self, *args, **kwargs):
        return PopupShieldedLocator(self._page.locator(*args, **kwargs), self._popup_lock)

    def __getattr__(self, name):
        return getattr(self._page, name)


async def bind_popup_lock_to_page(context, popup_lock: PopupInteractionLock | None, page) -> None:
    if popup_lock is None or popup_lock.raw_page is page:
        return
    try:
        cdp_session = await context.new_cdp_session(page)
    except Exception:
        return
    try:
        await popup_lock.bind_page(page, cdp_session)
    except Exception:
        return


async def page_local_storage(page, *, timeout_seconds: float) -> list[dict[str, str]]:
    try:
        items = await asyncio.wait_for(
            page.evaluate(
                """() => Object.entries(window.localStorage || {}).map(([name, value]) => ({
                    name: String(name),
                    value: String(value),
                }))"""
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return [
        {"name": str(item.get("name")), "value": str(item.get("value"))}
        for item in items
        if isinstance(item, dict) and item.get("name") is not None
    ]


async def storage_state_fallback(
    context,
    *,
    origin_from_url: Callable[[str | None], str | None],
    cookies_timeout_seconds: float,
    local_storage_timeout_seconds: float,
) -> dict[str, Any]:
    try:
        cookies = await asyncio.wait_for(
            context.cookies(),
            timeout=cookies_timeout_seconds,
        )
    except Exception:
        cookies = []

    origins_by_url: dict[str, dict[str, list[dict[str, str]]]] = {}
    for page in list(getattr(context, "pages", []) or []):
        if is_page_closed(page):
            continue
        origin = origin_from_url(safe_page_url(page))
        if not origin or origin in origins_by_url:
            continue
        local_storage = await page_local_storage(page, timeout_seconds=local_storage_timeout_seconds)
        if local_storage:
            origins_by_url.setdefault(origin, {})["localStorage"] = local_storage

    return {
        "cookies": cookies or [],
        "origins": [
            {"origin": origin, **storage}
            for origin, storage in origins_by_url.items()
        ],
    }


async def collect_storage_state(
    context,
    *,
    has_material: Callable[[dict[str, Any] | None], bool],
    origin_from_url: Callable[[str | None], str | None],
    storage_state_timeout_seconds: float,
    fallback_timeout_seconds: float,
    local_storage_timeout_seconds: float,
    indexed_db: bool = True,
) -> dict[str, Any]:
    try:
        storage_state = await asyncio.wait_for(
            context.storage_state(indexed_db=indexed_db),
            timeout=storage_state_timeout_seconds,
        )
    except Exception:
        storage_state = await storage_state_fallback(
            context,
            origin_from_url=origin_from_url,
            cookies_timeout_seconds=fallback_timeout_seconds,
            local_storage_timeout_seconds=local_storage_timeout_seconds,
        )
    if not isinstance(storage_state, dict):
        storage_state = {"cookies": [], "origins": []}
    if has_material(storage_state):
        return storage_state
    fallback_state = await storage_state_fallback(
        context,
        origin_from_url=origin_from_url,
        cookies_timeout_seconds=fallback_timeout_seconds,
        local_storage_timeout_seconds=local_storage_timeout_seconds,
    )
    if has_material(fallback_state):
        return fallback_state
    return storage_state


async def collect_user_agent(page, *, timeout_seconds: float = VISIBLE_AUTH_USER_AGENT_TIMEOUT_SECONDS) -> str:
    try:
        value = await asyncio.wait_for(
            page.evaluate(
                """
                () => navigator.userAgent
                """
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return ""
    return str(value or "").strip()


def har_timestamp_slug(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.strftime("%Y%m%dT%H%M%SZ")


def prune_files(paths: list[Path], *, keep: int) -> None:
    for stale_path in paths[keep:]:
        try:
            stale_path.unlink()
        except FileNotFoundError:
            continue


def collapse_text(value: str, *, limit: int = 160) -> str:
    collapsed = " ".join(str(value or "").split()).strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: max(limit - 3, 0)]}..."


def _developer_local_body_field_is_sensitive(key: Any) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    if normalized in DEVELOPER_LOCAL_SENSITIVE_BODY_FIELD_NAMES:
        return True
    return any(marker in normalized for marker in DEVELOPER_LOCAL_SENSITIVE_BODY_FIELD_MARKERS)


def cookie_header_names(value: str) -> list[str]:
    names: list[str] = []
    for piece in str(value or "").split(";"):
        name = piece.split("=", 1)[0].strip()
        if name:
            names.append(name)
    return names


def sanitize_json_structure(
    value,
    *,
    preserve_safe_scalars: bool,
    preserve_all_scalars: bool = False,
    safe_response_keys: frozenset[str],
    redacted_value: str = REDACTED_VALUE,
    current_key: str | None = None,
):
    if isinstance(value, dict):
        return {
            str(key): sanitize_json_structure(
                nested,
                preserve_safe_scalars=preserve_safe_scalars,
                preserve_all_scalars=preserve_all_scalars,
                safe_response_keys=safe_response_keys,
                redacted_value=redacted_value,
                current_key=str(key),
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [
            sanitize_json_structure(
                nested,
                preserve_safe_scalars=preserve_safe_scalars,
                preserve_all_scalars=preserve_all_scalars,
                safe_response_keys=safe_response_keys,
                redacted_value=redacted_value,
                current_key=current_key,
            )
            for nested in value
        ]
    if isinstance(value, bool) or value is None:
        return value
    key_lower = str(current_key or "").strip().lower()
    if preserve_all_scalars and not _developer_local_body_field_is_sensitive(key_lower):
        if isinstance(value, str) and (
            key_lower.endswith("url")
            or key_lower.endswith("uri")
            or key_lower in {"location", "redirect"}
        ):
            return sanitize_url(
                value,
                redacted_value=redacted_value,
                redact_path_identifiers=False,
                redact_query_names=False,
                preserve_query_values=True,
            )
        return value
    if preserve_safe_scalars and isinstance(value, str):
        if key_lower.endswith("url") or key_lower.endswith("uri") or key_lower in {"location", "redirect"}:
            return sanitize_url(value, redacted_value=redacted_value)
        if key_lower in safe_response_keys:
            return collapse_text(value)
    if preserve_safe_scalars and isinstance(value, (int, float)) and key_lower in safe_response_keys:
        return value
    return redacted_value


def sanitize_header_entries(
    headers,
    *,
    sensitive_header_names: frozenset[str] = DEFAULT_SENSITIVE_HEADER_NAMES,
    redacted_value: str = REDACTED_VALUE,
    capture_level: str = CAPTURE_LEVEL_REDACTED_RICH,
) -> list[dict]:
    sanitized_headers: list[dict] = []
    for header in headers if isinstance(headers, list) else []:
        if not isinstance(header, dict):
            continue
        sanitized = dict(header)
        header_name = str(sanitized.get("name") or "")
        header_lower = header_name.lower()
        value = str(sanitized.get("value") or "")
        if header_lower == "cookie":
            names = cookie_header_names(value)
            sanitized["value"] = "; ".join(f"{name}={redacted_value}" for name in names) or redacted_value
        elif header_lower == "set-cookie":
            name = value.split("=", 1)[0].strip()
            sanitized["value"] = f"{name}={redacted_value}" if name else redacted_value
        elif header_lower in sensitive_header_names:
            sanitized["value"] = redacted_value
        elif header_lower in {"origin", "referer", "location"}:
            developer_local = normalize_capture_level(capture_level) == CAPTURE_LEVEL_DEVELOPER_LOCAL
            sanitized["value"] = sanitize_url(
                value,
                redacted_value=redacted_value,
                redact_path_identifiers=not developer_local,
                redact_query_names=not developer_local,
                preserve_query_values=developer_local,
            )
        sanitized_headers.append(sanitized)
    return sanitized_headers


def sanitize_value_items(
    items,
    *,
    preserve_values: bool = False,
    redacted_value: str = REDACTED_VALUE,
) -> list[dict]:
    sanitized_items: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sanitized = dict(item)
        if "value" in sanitized:
            item_name = str(sanitized.get("name") or "")
            if preserve_values and not _developer_local_body_field_is_sensitive(item_name):
                sanitized["value"] = sanitized.get("value")
            else:
                sanitized["value"] = redacted_value
        sanitized_items.append(sanitized)
    return sanitized_items


def sanitize_post_data(
    post_data: dict | None,
    *,
    safe_response_keys: frozenset[str],
    capture_level: str = CAPTURE_LEVEL_REDACTED_RICH,
    redacted_value: str = REDACTED_VALUE,
) -> dict | None:
    if not isinstance(post_data, dict):
        return post_data
    sanitized = dict(post_data)
    developer_local = normalize_capture_level(capture_level) == CAPTURE_LEVEL_DEVELOPER_LOCAL
    sanitized["params"] = sanitize_value_items(
        post_data.get("params"),
        preserve_values=developer_local,
        redacted_value=redacted_value,
    )
    mime_type = str(post_data.get("mimeType") or "").lower()
    text = post_data.get("text")
    if isinstance(text, str) and text:
        if "json" in mime_type:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                sanitized["text"] = redacted_value
            else:
                sanitized["text"] = json.dumps(
                    sanitize_json_structure(
                        parsed,
                        preserve_safe_scalars=developer_local,
                        preserve_all_scalars=developer_local,
                        safe_response_keys=safe_response_keys,
                        redacted_value=redacted_value,
                    ),
                    separators=(",", ":"),
                )
        elif "application/x-www-form-urlencoded" in mime_type:
            pairs = parse_qsl(text, keep_blank_values=True)
            sanitized["text"] = urlencode(
                [
                    (
                        name,
                        value
                        if developer_local and not _developer_local_body_field_is_sensitive(name)
                        else redacted_value,
                    )
                    for name, value in pairs
                ],
                doseq=True,
            )
        else:
            sanitized["text"] = redacted_value
    return sanitized


def sanitize_response_content(
    url: str,
    response: dict | None,
    content: dict | None,
    *,
    should_keep_response_body: Callable[[str, int | None, str], bool],
    safe_response_keys: frozenset[str],
    capture_level: str = CAPTURE_LEVEL_REDACTED_RICH,
    redacted_value: str = REDACTED_VALUE,
) -> dict | None:
    if not isinstance(content, dict):
        return content
    sanitized = dict(content)
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return sanitized
    status = response.get("status") if isinstance(response, dict) else None
    content_type = str(content.get("mimeType") or "")
    developer_local = normalize_capture_level(capture_level) == CAPTURE_LEVEL_DEVELOPER_LOCAL
    lowered_content_type = content_type.lower()
    dev_capture_text_body = developer_local and ("json" in lowered_content_type or "text" in lowered_content_type)
    if not should_keep_response_body(url, status, content_type) and not dev_capture_text_body:
        sanitized["text"] = redacted_value
        return sanitized
    if "json" in lowered_content_type:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            sanitized["text"] = redacted_value
        else:
            sanitized["text"] = json.dumps(
                sanitize_json_structure(
                        parsed,
                        preserve_safe_scalars=True,
                        preserve_all_scalars=developer_local,
                        safe_response_keys=safe_response_keys,
                        redacted_value=redacted_value,
                    ),
                    separators=(",", ":"),
                )
        return sanitized
    if developer_local and "text" in lowered_content_type:
        return sanitized
    sanitized["text"] = redacted_value
    return sanitized


def sanitize_har_entry(
    entry: dict,
    *,
    should_keep_response_body: Callable[[str, int | None, str], bool],
    safe_response_keys: frozenset[str],
    sensitive_header_names: frozenset[str] = DEFAULT_SENSITIVE_HEADER_NAMES,
    capture_level: str = CAPTURE_LEVEL_REDACTED_RICH,
    redacted_value: str = REDACTED_VALUE,
) -> dict:
    request_payload = entry.get("request")
    response_payload = entry.get("response")
    request_url = ""
    if isinstance(request_payload, dict):
        request_url = str(request_payload.get("url") or "")
        request_payload["url"] = sanitize_url(
            request_url,
            redacted_value=redacted_value,
            redact_path_identifiers=capture_level != CAPTURE_LEVEL_DEVELOPER_LOCAL,
            redact_query_names=capture_level != CAPTURE_LEVEL_DEVELOPER_LOCAL,
            preserve_query_values=capture_level == CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )
        request_payload["headers"] = sanitize_header_entries(
            request_payload.get("headers"),
            sensitive_header_names=sensitive_header_names,
            capture_level=capture_level,
            redacted_value=redacted_value,
        )
        request_payload["cookies"] = sanitize_value_items(request_payload.get("cookies"), redacted_value=redacted_value)
        request_payload["queryString"] = sanitize_value_items(
            request_payload.get("queryString"),
            preserve_values=capture_level == CAPTURE_LEVEL_DEVELOPER_LOCAL,
            redacted_value=redacted_value,
        )
        request_payload["postData"] = sanitize_post_data(
            request_payload.get("postData"),
            safe_response_keys=safe_response_keys,
            capture_level=capture_level,
            redacted_value=redacted_value,
        )
    if isinstance(response_payload, dict):
        response_payload["headers"] = sanitize_header_entries(
            response_payload.get("headers"),
            sensitive_header_names=sensitive_header_names,
            capture_level=capture_level,
            redacted_value=redacted_value,
        )
        response_payload["cookies"] = sanitize_value_items(
            response_payload.get("cookies"),
            redacted_value=redacted_value,
        )
        if "redirectURL" in response_payload:
            response_payload["redirectURL"] = sanitize_url(
                response_payload.get("redirectURL"),
                redacted_value=redacted_value,
                redact_path_identifiers=capture_level != CAPTURE_LEVEL_DEVELOPER_LOCAL,
                redact_query_names=capture_level != CAPTURE_LEVEL_DEVELOPER_LOCAL,
                preserve_query_values=capture_level == CAPTURE_LEVEL_DEVELOPER_LOCAL,
            )
        response_payload["content"] = sanitize_response_content(
            request_url,
            response_payload,
            response_payload.get("content"),
            should_keep_response_body=should_keep_response_body,
            safe_response_keys=safe_response_keys,
            capture_level=capture_level,
            redacted_value=redacted_value,
        )
    return entry


def finalize_har_capture(
    raw_har_path: Path,
    *,
    sanitized_har_path: Path,
    sanitized_paths_to_prune: list[Path],
    keep: int,
    should_keep_response_body: Callable[[str, int | None, str], bool],
    safe_response_keys: frozenset[str],
    sensitive_header_names: frozenset[str] = DEFAULT_SENSITIVE_HEADER_NAMES,
    capture_level: str | None = None,
    redacted_value: str = REDACTED_VALUE,
) -> Path | None:
    if not raw_har_path.exists():
        return None
    normalized_capture_level = normalize_capture_level(capture_level)
    with raw_har_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    log_payload = payload.get("log")
    if isinstance(log_payload, dict):
        entries = log_payload.get("entries")
        if isinstance(entries, list):
            log_payload["entries"] = [
                sanitize_har_entry(
                    entry if isinstance(entry, dict) else {},
                    should_keep_response_body=should_keep_response_body,
                    safe_response_keys=safe_response_keys,
                    sensitive_header_names=sensitive_header_names,
                    capture_level=normalized_capture_level,
                    redacted_value=redacted_value,
                )
                for entry in entries
            ]
    sanitized_har_path.parent.mkdir(parents=True, exist_ok=True)
    with sanitized_har_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)
    candidates = {str(path): path for path in sanitized_paths_to_prune}
    candidates[str(sanitized_har_path)] = sanitized_har_path
    prune_files(
        sorted(
            candidates.values(),
            key=lambda path: (path.stat().st_mtime, path.name),
            reverse=True,
        ),
        keep=keep,
    )
    try:
        raw_har_path.unlink()
    except FileNotFoundError:
        pass
    return sanitized_har_path
