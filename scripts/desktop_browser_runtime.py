#!/usr/bin/env python3
"""Managed browser runtime helpers for desktop visible-auth flows."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tarfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_BRAND_NAME = "BreakTwenty"


def _resolve_runtime_path(env_name: str, default: Path) -> Path:
    configured = str(os.getenv(env_name) or "").strip()
    path = Path(configured).expanduser() if configured else default
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


DESKTOP_AUTH_ROOT = _resolve_runtime_path("BREAKTWENTY_DESKTOP_AUTH_DIR", REPO_ROOT / ".desktop-auth")
DESKTOP_BROWSER_RUNTIME_ROOT = _resolve_runtime_path(
    "BREAKTWENTY_BROWSER_RUNTIME_DIR",
    DESKTOP_AUTH_ROOT / "browser-runtimes",
)
# The app uses one tested managed Brave release for every visible-auth provider.
BRAVE_BROWSER_STABLE_VERSION = "1.92.139"
BRAVE_CHROMIUM_MAJOR_BY_BRAVE_VERSION = {
    "1.92.139": "150",
}
WINDOWS_BRAVE_PREWARM_ARGS = (
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
)


def _brave_browser_platform_map() -> dict[str, dict[str, object]]:
    return {
        "linux64": {
            "archive_name": f"brave-browser_{BRAVE_BROWSER_STABLE_VERSION}_amd64.deb",
            "archive_format": "deb",
            "download_url": (
                "https://brave-browser-apt-release.s3.brave.com/pool/main/b/brave-browser/"
                f"brave-browser_{BRAVE_BROWSER_STABLE_VERSION}_amd64.deb"
            ),
            "relative_executable_path": "opt/brave.com/brave/brave-browser",
            "relative_executable_paths": [
                "opt/brave.com/brave/brave-browser",
                "opt/brave.com/brave/brave",
                "opt/brave.com/brave/chrome-management-service",
                "opt/brave.com/brave/chrome-sandbox",
                "opt/brave.com/brave/chrome_crashpad_handler",
                "opt/brave.com/brave/xdg-mime",
                "opt/brave.com/brave/xdg-settings",
            ],
        },
        "win64": {
            "archive_name": f"brave-v{BRAVE_BROWSER_STABLE_VERSION}-win32-x64.zip",
            "archive_format": "zip",
            "download_url": (
                "https://github.com/brave/brave-browser/releases/download/"
                f"v{BRAVE_BROWSER_STABLE_VERSION}/brave-v{BRAVE_BROWSER_STABLE_VERSION}-win32-x64.zip"
            ),
            "relative_executable_path": "brave.exe",
            "relative_executable_paths": ["brave.exe"],
        },
        "macos-arm64": {
            "archive_name": "Brave-Browser-arm64.dmg",
            "archive_format": "dmg",
            "download_url": (
                "https://github.com/brave/brave-browser/releases/download/"
                f"v{BRAVE_BROWSER_STABLE_VERSION}/Brave-Browser-arm64.dmg"
            ),
            "relative_executable_path": "Brave Browser.app/Contents/MacOS/Brave Browser",
            "relative_executable_paths": [
                "Brave Browser.app/Contents/MacOS/Brave Browser",
            ],
        },
        "macos-x64": {
            "archive_name": "Brave-Browser-x64.dmg",
            "archive_format": "dmg",
            "download_url": (
                "https://github.com/brave/brave-browser/releases/download/"
                f"v{BRAVE_BROWSER_STABLE_VERSION}/Brave-Browser-x64.dmg"
            ),
            "relative_executable_path": "Brave Browser.app/Contents/MacOS/Brave Browser",
            "relative_executable_paths": [
                "Brave Browser.app/Contents/MacOS/Brave Browser",
            ],
        },
    }


class DesktopBrowserRuntimeError(RuntimeError):
    """Raised when a managed desktop browser runtime cannot be prepared."""


def _required_tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise DesktopBrowserRuntimeError(f"Required executable not found on PATH: {name}")
    return executable


def _append_log(log_path: Path | None, message: str) -> None:
    if not log_path:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def _emit_visible_auth_status(message: str) -> None:
    if not (os.getenv("BREAKTWENTY_VISIBLE_AUTH_PROVIDER") or os.getenv("BREAKTWENTY_VISIBLE_AUTH_ATTEMPT_ID")):
        return
    print(message, flush=True)


def _visible_auth_log_path_from_env() -> Path | None:
    configured = str(os.getenv("BREAKTWENTY_VISIBLE_AUTH_LOG_PATH") or "").strip()
    if not configured:
        return None
    return Path(configured).expanduser()


def _brave_browser_platform_key() -> str:
    system_name = platform.system().lower()
    machine_name = platform.machine().lower()

    if system_name == "linux" and machine_name in {"x86_64", "amd64"}:
        return "linux64"
    if system_name == "windows" and machine_name in {"x86_64", "amd64"}:
        return "win64"
    if system_name == "darwin":
        if machine_name in {"arm64", "aarch64"}:
            return "macos-arm64"
        if machine_name in {"x86_64", "amd64"}:
            return "macos-x64"

    raise DesktopBrowserRuntimeError(
        f"{APP_BRAND_NAME} managed Brave runtime is not supported on this platform yet: "
        f"{platform.system()} {platform.machine()}"
    )


def _lock_pid(lock_path: Path) -> int | None:
    try:
        raw_pid = lock_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return False
        return True
    return True


def _runtime_lock_is_stale(lock_path: Path, *, stale_age_seconds: int) -> bool:
    pid = _lock_pid(lock_path)
    if pid is not None:
        return not _pid_is_running(pid)
    try:
        return time.time() - lock_path.stat().st_mtime >= stale_age_seconds
    except OSError:
        return False


@contextmanager
def _runtime_lock(lock_path: Path, *, timeout_seconds: int = 300) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    descriptor: int | None = None

    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _runtime_lock_is_stale(lock_path, stale_age_seconds=timeout_seconds):
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            if time.monotonic() - started_at >= timeout_seconds:
                raise DesktopBrowserRuntimeError(
                    f"Timed out waiting for browser runtime lock: {lock_path}"
                ) from None
            time.sleep(0.25)

    try:
        os.write(descriptor, f"{os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _download_file(url: str, destination: Path) -> None:
    url = str(url or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} refused to download a browser runtime from an unsupported URL."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib_request.Request(url, headers={"User-Agent": f"{APP_BRAND_NAME} Desktop Visible Auth"})
    try:
        # URL scheme/netloc are validated above before urlopen.
        with urllib_request.urlopen(request, timeout=120) as response, destination.open("wb") as handle:  # nosec B310
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    except urllib_error.URLError as exc:
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} could not download its managed browser runtime. "
            f"Check your internet connection and retry. Source: {url}. {exc}"
        ) from exc


def _tar_mode_for_member_name(member_name: str) -> str:
    if member_name.endswith(".tar.xz"):
        return "r:xz"
    if member_name.endswith(".tar.gz"):
        return "r:gz"
    if member_name.endswith(".tar.bz2"):
        return "r:bz2"
    if member_name.endswith(".tar"):
        return "r:"
    raise DesktopBrowserRuntimeError(
        f"{APP_BRAND_NAME} downloaded Brave, but the package used an unsupported archive format: "
        f"{member_name}"
    )


def _tar_link_stays_within_archive(member: tarfile.TarInfo) -> bool:
    target = str(member.linkname or "")
    target_path = PurePosixPath(target)
    if not target or target_path.is_absolute():
        return False
    if member.issym():
        target = posixpath.join(posixpath.dirname(member.name), target)
    normalized = posixpath.normpath(target)
    return normalized not in {"", ".", ".."} and not normalized.startswith("../")


def _safe_tar_member(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    if (member.issym() or member.islnk()) and not _tar_link_stays_within_archive(member):
        return None
    return tarfile.data_filter(member, destination)


def _extract_tar_archive(archive_path: Path, member_name: str, destination: Path) -> None:
    mode = _tar_mode_for_member_name(member_name)
    with tarfile.open(archive_path, mode=mode) as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise DesktopBrowserRuntimeError(
                    f"{APP_BRAND_NAME} downloaded Brave, but the package contained an unsafe path: "
                    f"{member.name}"
                )
        archive.extractall(destination, filter=_safe_tar_member)


def _extract_zip_archive(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for member_name in archive.namelist():
            member_path = Path(member_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise DesktopBrowserRuntimeError(
                    f"{APP_BRAND_NAME} downloaded a browser runtime, but the package contained an unsafe path: "
                    f"{member_name}"
                )
        archive.extractall(destination)  # nosec B202


def _extract_deb_data_archive(deb_path: Path, temp_archive_path: Path, destination: Path) -> None:
    with deb_path.open("rb") as handle:
        if handle.read(8) != b"!<arch>\n":
            raise DesktopBrowserRuntimeError(
                f"{APP_BRAND_NAME} downloaded Brave, but the package was not a valid Debian archive."
            )

        while True:
            header = handle.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"`\n":
                raise DesktopBrowserRuntimeError(
                    f"{APP_BRAND_NAME} downloaded Brave, but the Debian archive header was invalid."
                )

            member_name = header[:16].decode("utf-8", "replace").strip()
            if member_name.endswith("/"):
                member_name = member_name[:-1]
            try:
                member_size = int(header[48:58].decode("ascii", "replace").strip() or "0")
            except ValueError as exc:
                raise DesktopBrowserRuntimeError(
                    f"{APP_BRAND_NAME} downloaded Brave, but a Debian archive entry had an invalid size."
                ) from exc

            if member_name.startswith("data.tar"):
                temp_archive_path.parent.mkdir(parents=True, exist_ok=True)
                with temp_archive_path.open("wb") as temp_archive:
                    remaining = member_size
                    while remaining > 0:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise DesktopBrowserRuntimeError(
                                f"{APP_BRAND_NAME} downloaded Brave, but the Debian archive ended early."
                            )
                        temp_archive.write(chunk)
                        remaining -= len(chunk)
                if member_size % 2:
                    handle.read(1)
                _extract_tar_archive(temp_archive_path, member_name, destination)
                return

            handle.seek(member_size + (member_size % 2), os.SEEK_CUR)

    raise DesktopBrowserRuntimeError(
        f"{APP_BRAND_NAME} downloaded Brave, but no browser payload was found in the Debian package."
    )


def _mounted_app_bundle_entries(mount_root: Path) -> list[Path]:
    entries: list[Path] = []
    for entry in mount_root.iterdir():
        if entry.name.startswith(".") or entry.name.strip() == "":
            continue
        if entry.is_dir() and entry.suffix == ".app":
            entries.append(entry)
    return entries


def _extract_dmg_archive(dmg_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    mount_root = dmg_path.parent / f"{dmg_path.stem}-mount-{os.getpid()}-{time.time_ns()}"
    mount_attached = False
    hdiutil = _required_tool("hdiutil")
    try:
        mount_result = subprocess.run(
            [
                hdiutil,
                "attach",
                str(dmg_path),
                "-mountpoint",
                str(mount_root),
                "-nobrowse",
                "-quiet",
                "-readonly",
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )  # nosec B603
        if mount_result.returncode != 0:
            stderr = (mount_result.stderr or mount_result.stdout or "").strip()
            raise DesktopBrowserRuntimeError(
                f"{APP_BRAND_NAME} downloaded Brave, but mounting the macOS disk image failed: "
                f"{stderr or 'unknown hdiutil error'}"
            )
        mount_attached = True

        app_entries = _mounted_app_bundle_entries(mount_root)
        if not app_entries:
            raise DesktopBrowserRuntimeError(
                f"{APP_BRAND_NAME} downloaded Brave, but the macOS disk image did not contain an app bundle."
            )
        for entry in app_entries:
            target = destination / entry.name
            shutil.copytree(entry, target, symlinks=True)
    finally:
        if mount_attached:
            try:
                subprocess.run(
                    [hdiutil, "detach", str(mount_root), "-quiet"],
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )  # nosec B603
            except Exception:
                pass


def _extract_browser_archive(
    archive_path: Path,
    archive_format: str,
    temp_archive_path: Path,
    destination: Path,
) -> None:
    if archive_format == "zip":
        _extract_zip_archive(archive_path, destination)
        return
    if archive_format == "dmg":
        _extract_dmg_archive(archive_path, destination)
        return
    _extract_deb_data_archive(archive_path, temp_archive_path, destination)


def _brave_chromium_major_version(
    executable_path: Path,
    *,
    platform_key: str = "",
    runtime_version: str = BRAVE_BROWSER_STABLE_VERSION,
    log_path: Path | None = None,
) -> str:
    if platform_key == "win64":
        major = BRAVE_CHROMIUM_MAJOR_BY_BRAVE_VERSION.get(str(runtime_version))
        if major:
            _append_log(
                log_path,
                (
                    "managed brave runtime using pinned Windows Chromium major "
                    f"{major} for Brave {runtime_version}"
                ),
            )
            return major
        _append_log(
            log_path,
            f"managed brave runtime missing Windows Chromium metadata for Brave {runtime_version}",
        )
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} managed Brave runtime is missing Chromium version metadata for Windows."
        )

    try:
        _append_log(log_path, f"managed brave runtime probing version with {executable_path} --version")
        result = subprocess.run(
            [str(executable_path), "--version"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as exc:
        _append_log(log_path, f"managed brave runtime version probe failed: {exc}")
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} could not verify the installed Brave browser version."
        ) from exc

    version_output = " ".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    match = re.search(r"\b(?:Brave Browser|Chromium)\s+(\d+)(?:\.\d+)+\b", version_output)
    if result.returncode != 0 or match is None:
        _append_log(
            log_path,
            f"managed brave runtime version probe output was not usable: code={result.returncode}",
        )
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} could not identify the Chromium version in its managed Brave runtime."
        )
    _append_log(log_path, f"managed brave runtime detected Chromium major {match.group(1)}")
    return match.group(1)


def _browser_runtime_result(
    *,
    runtime_version: str,
    platform_key: str,
    platform_spec: dict[str, object],
    executable_path: Path,
    installed_now: bool,
    log_path: Path | None = None,
) -> dict[str, str | bool]:
    return {
        "runtime": "brave_browser",
        "mode": "breaktwenty_managed",
        "version": runtime_version,
        "chromium_major": _brave_chromium_major_version(
            executable_path,
            platform_key=platform_key,
            runtime_version=runtime_version,
            log_path=log_path,
        ),
        "platform": platform_key,
        "download_url": str(platform_spec["download_url"]),
        "executable_path": str(executable_path),
        "installed_now": installed_now,
    }


def ensure_brave_browser_runtime(
    *,
    runtime_root: Path = DESKTOP_BROWSER_RUNTIME_ROOT,
    log_path: Path | None = None,
) -> dict[str, str | bool]:
    if log_path is None:
        log_path = _visible_auth_log_path_from_env()
    runtime_version = BRAVE_BROWSER_STABLE_VERSION
    platform_key = _brave_browser_platform_key()
    platform_spec = _brave_browser_platform_map()[platform_key]
    runtime_dir = runtime_root / "brave-browser" / runtime_version / platform_key
    executable_path = runtime_dir / str(platform_spec["relative_executable_path"])
    _append_log(
        log_path,
        (
            f"managed brave runtime ensure starting platform={platform_key} "
            f"version={runtime_version} root={runtime_root}"
        ),
    )
    _emit_visible_auth_status(
        "Preparing secure browser runtime. First launch on this device can take a minute."
    )
    if executable_path.exists():
        _append_log(log_path, f"managed brave runtime cache hit executable={executable_path}")
        _emit_visible_auth_status("Verifying secure browser runtime.")
        result = _browser_runtime_result(
            runtime_version=runtime_version,
            platform_key=platform_key,
            platform_spec=platform_spec,
            executable_path=executable_path,
            installed_now=False,
            log_path=log_path,
        )
        _emit_visible_auth_status("Secure browser runtime is ready. Launching secure browser window.")
        return result

    lock_path = runtime_root / ".locks" / f"brave-browser-{runtime_version}-{platform_key}.lock"
    with _runtime_lock(lock_path):
        if executable_path.exists():
            _append_log(log_path, f"managed brave runtime cache hit after lock executable={executable_path}")
            _emit_visible_auth_status("Verifying secure browser runtime.")
            result = _browser_runtime_result(
                runtime_version=runtime_version,
                platform_key=platform_key,
                platform_spec=platform_spec,
                executable_path=executable_path,
                installed_now=False,
                log_path=log_path,
            )
            _emit_visible_auth_status("Secure browser runtime is ready. Launching secure browser window.")
            return result

        if runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)

        temp_root = runtime_root / ".tmp" / f"brave-browser-{runtime_version}-{platform_key}-{os.getpid()}-{time.time_ns()}"
        archive_path = temp_root / str(platform_spec["archive_name"])
        data_archive_path = temp_root / "data-archive"
        extract_root = temp_root / "extract"
        staged_runtime_dir = runtime_dir.parent / f".{runtime_dir.name}.tmp-{os.getpid()}-{time.time_ns()}"

        try:
            _append_log(
                log_path,
                f"managed brave runtime downloading {platform_spec['download_url']}",
            )
            _emit_visible_auth_status(
                "Downloading secure browser runtime. First launch on this device can take a minute."
            )
            _download_file(str(platform_spec["download_url"]), archive_path)
            _append_log(log_path, f"managed brave runtime extracting {archive_path.name}")
            _emit_visible_auth_status("Installing secure browser runtime.")
            _extract_browser_archive(
                archive_path,
                str(platform_spec.get("archive_format") or "deb"),
                data_archive_path,
                extract_root,
            )

            staged_executable_path = extract_root / str(platform_spec["relative_executable_path"])
            if not staged_executable_path.exists():
                raise DesktopBrowserRuntimeError(
                    f"{APP_BRAND_NAME} downloaded Brave, but the extracted browser executable "
                    f"was not found at {staged_executable_path}."
                )

            if os.name != "nt":
                for relative_path in platform_spec.get("relative_executable_paths", []):
                    candidate_path = extract_root / str(relative_path)
                    if not candidate_path.exists():
                        continue
                    current_mode = candidate_path.stat().st_mode
                    candidate_path.chmod(
                        current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                    )

            runtime_dir.parent.mkdir(parents=True, exist_ok=True)
            if staged_runtime_dir.exists():
                shutil.rmtree(staged_runtime_dir, ignore_errors=True)
            shutil.move(str(extract_root), str(staged_runtime_dir))
            os.replace(str(staged_runtime_dir), str(runtime_dir))
            _append_log(log_path, f"managed brave runtime installed executable={executable_path}")

            metadata_path = runtime_dir / "breaktwenty-runtime.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "runtime": "brave_browser",
                        "version": runtime_version,
                        "platform": platform_key,
                        "download_url": platform_spec["download_url"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            shutil.rmtree(staged_runtime_dir, ignore_errors=True)

    if not executable_path.exists():
        raise DesktopBrowserRuntimeError(
            f"{APP_BRAND_NAME} managed Brave runtime was not installed correctly: {executable_path}"
        )

    _emit_visible_auth_status("Verifying secure browser runtime.")
    result = _browser_runtime_result(
        runtime_version=runtime_version,
        platform_key=platform_key,
        platform_spec=platform_spec,
        executable_path=executable_path,
        installed_now=True,
        log_path=log_path,
    )
    _emit_visible_auth_status("Secure browser runtime is ready. Launching secure browser window.")
    return result


def _browser_prewarm_launch_args(platform_key: str) -> list[str]:
    if platform_key == "win64":
        return list(WINDOWS_BRAVE_PREWARM_ARGS)
    return []


def _prewarm_patchright_browser(browser_runtime: dict[str, str | bool], log_path: Path | None) -> bool:
    try:
        from patchright.sync_api import sync_playwright
    except Exception as exc:
        _append_log(log_path, f"patchright browser prewarm skipped: {exc}")
        return False

    try:
        _append_log(log_path, "patchright driver prewarm starting")
        with sync_playwright() as playwright:
            _append_log(log_path, "patchright driver prewarm ready")
            executable_path = str(browser_runtime.get("executable_path") or "").strip()
            if not executable_path:
                _append_log(log_path, "patchright browser prewarm skipped: missing executable path")
                return False
            launch_kwargs: dict[str, object] = {
                "headless": True,
                "executable_path": executable_path,
                "timeout": 45000,
            }
            launch_args = _browser_prewarm_launch_args(str(browser_runtime.get("platform") or ""))
            if launch_args:
                launch_kwargs["args"] = launch_args
            _append_log(log_path, "patchright browser prewarm starting")
            browser = playwright.chromium.launch(**launch_kwargs)
            browser.close()
            _append_log(log_path, "patchright browser prewarm ready")
            return True
    except Exception as exc:
        _append_log(log_path, f"patchright browser prewarm failed: {exc}")
        return False


def _prewarm_patchright_async_browser(browser_runtime: dict[str, str | bool], log_path: Path | None) -> bool:
    if str(browser_runtime.get("platform") or "") != "win64":
        return True

    executable_path = str(browser_runtime.get("executable_path") or "").strip()
    if not executable_path:
        _append_log(log_path, "patchright async browser prewarm skipped: missing executable path")
        return False

    script = """
import asyncio
import sys
from patchright.async_api import async_playwright

WINDOWS_BRAVE_PREWARM_ARGS = (
    "--disable-gpu",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--no-default-browser-check",
    "--no-first-run",
)


async def main() -> None:
    executable_path = sys.argv[1]
    platform_key = sys.argv[2]
    print("patchright async driver prewarm starting", flush=True)
    async with async_playwright() as playwright:
        print("patchright async driver prewarm ready", flush=True)
        launch_kwargs = {
            "headless": True,
            "executable_path": executable_path,
            "timeout": 45000,
        }
        if platform_key == "win64":
            launch_kwargs["args"] = list(WINDOWS_BRAVE_PREWARM_ARGS)
        print("patchright async browser prewarm starting", flush=True)
        browser = await playwright.chromium.launch(**launch_kwargs)
        await browser.close()
        print("patchright async browser prewarm ready", flush=True)


asyncio.run(main())
""".strip()

    subprocess_kwargs: dict[str, object] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                executable_path,
                str(browser_runtime.get("platform") or ""),
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=75,
            **subprocess_kwargs,
        )  # nosec B603
    except subprocess.TimeoutExpired:
        _append_log(log_path, "patchright async browser prewarm timed out")
        return False
    except Exception as exc:
        _append_log(log_path, f"patchright async browser prewarm failed: {exc}")
        return False

    for stream_name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        for line in str(text or "").splitlines():
            if line.strip():
                _append_log(log_path, f"patchright async prewarm {stream_name}: {line.strip()}")
    if result.returncode != 0:
        _append_log(log_path, f"patchright async browser prewarm failed with exit code {result.returncode}")
        return False
    return True


def _prune_obsolete_brave_runtimes(
    runtime_root: Path,
    *,
    current_version: str,
    platform_key: str,
    log_path: Path | None,
) -> list[str]:
    versions_root = runtime_root / "brave-browser"
    if not versions_root.is_dir():
        return []

    removed_versions: list[str] = []
    for version_dir in versions_root.iterdir():
        if not version_dir.is_dir() or version_dir.name == current_version:
            continue
        stale_platform_dir = version_dir / platform_key
        if not stale_platform_dir.exists():
            continue
        try:
            shutil.rmtree(stale_platform_dir)
            if not any(version_dir.iterdir()):
                version_dir.rmdir()
            removed_versions.append(version_dir.name)
            _append_log(log_path, f"removed obsolete Brave runtime {version_dir.name} ({platform_key})")
        except OSError as exc:
            _append_log(log_path, f"could not remove obsolete Brave runtime {version_dir.name}: {exc}")
    return removed_versions


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Prepare the {APP_BRAND_NAME}-managed Brave desktop browser runtime.",
    )
    parser.add_argument(
        "--runtime-root",
        default="",
        help="Override the browser runtime root. Defaults to BREAKTWENTY_BROWSER_RUNTIME_DIR.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the prepared runtime metadata as JSON.",
    )
    parser.add_argument(
        "--log-path",
        default="",
        help="Append prewarm progress to this log file.",
    )
    return parser.parse_args(argv)


def _runtime_root_from_arg(value: str) -> Path:
    if not value:
        return DESKTOP_BROWSER_RUNTIME_ROOT
    return Path(value).expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    runtime_root = _runtime_root_from_arg(args.runtime_root)
    log_path = Path(args.log_path).expanduser().resolve() if args.log_path else None
    _append_log(log_path, f"preparing managed brave runtime under {runtime_root}")

    try:
        result = ensure_brave_browser_runtime(runtime_root=runtime_root, log_path=log_path)
    except DesktopBrowserRuntimeError as exc:
        _append_log(log_path, f"managed brave runtime failed: {exc}")
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        _append_log(log_path, f"managed brave runtime failed unexpectedly: {exc}")
        print(f"{APP_BRAND_NAME} could not prepare its managed Brave runtime. {exc}", file=sys.stderr)
        return 1

    _append_log(
        log_path,
        (
            f"managed {result.get('runtime', 'brave')} runtime ready: "
            f"{result.get('executable_path')} installed_now={result.get('installed_now')}"
        ),
    )
    sync_prewarm_ready = _prewarm_patchright_browser(result, log_path)
    async_prewarm_ready = _prewarm_patchright_async_browser(result, log_path)
    if sync_prewarm_ready and async_prewarm_ready:
        result["removed_versions"] = _prune_obsolete_brave_runtimes(
            runtime_root,
            current_version=str(result["version"]),
            platform_key=str(result["platform"]),
            log_path=log_path,
        )
    else:
        result["removed_versions"] = []
        _append_log(log_path, "retaining prior Brave runtimes because prewarm verification failed")
    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
