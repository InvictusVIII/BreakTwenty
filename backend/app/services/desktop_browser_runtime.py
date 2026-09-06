from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from app.runtime_paths import BROWSER_RUNTIME_DIR

DESKTOP_BROWSER_RUNTIME_ROOT = BROWSER_RUNTIME_DIR
BRAVE_BROWSER_RUNTIME_NAME = "brave-browser"
BRAVE_BROWSER_LINUX_RELATIVE_EXECUTABLE = Path("opt/brave.com/brave/brave-browser")
BRAVE_BROWSER_WINDOWS_RELATIVE_EXECUTABLE = Path("brave.exe")


def _runtime_version_sort_key(version: str) -> tuple[Any, ...]:
    return tuple(
        int(token) if token.isdigit() else token.lower()
        for token in re.split(r"(\d+)", str(version or ""))
        if token
    )


def find_managed_brave_browser_runtime(
    *,
    runtime_root: Path = DESKTOP_BROWSER_RUNTIME_ROOT,
) -> dict[str, str | bool] | None:
    return _find_managed_browser_runtime(
        runtime_name=BRAVE_BROWSER_RUNTIME_NAME,
        runtime_label="brave_browser",
        executable_paths=(
            BRAVE_BROWSER_LINUX_RELATIVE_EXECUTABLE,
            BRAVE_BROWSER_WINDOWS_RELATIVE_EXECUTABLE,
        ),
        runtime_root=runtime_root,
    )


def find_managed_desktop_browser_runtime(
    *,
    runtime_root: Path = DESKTOP_BROWSER_RUNTIME_ROOT,
) -> dict[str, str | bool] | None:
    return find_managed_brave_browser_runtime(runtime_root=runtime_root)


def _find_managed_browser_runtime(
    *,
    runtime_name: str,
    runtime_label: str,
    executable_paths: tuple[Path, ...],
    runtime_root: Path,
) -> dict[str, str | bool] | None:
    provider_root = runtime_root / runtime_name
    if not provider_root.is_dir():
        return None

    candidates: list[tuple[str, str, Path]] = []
    for version_dir in provider_root.iterdir():
        if not version_dir.is_dir():
            continue
        for platform_dir in version_dir.iterdir():
            if not platform_dir.is_dir():
                continue
            for relative_executable_path in executable_paths:
                executable_path = platform_dir / relative_executable_path
                if executable_path.is_file():
                    candidates.append((version_dir.name, platform_dir.name, executable_path))
                    break

    if not candidates:
        return None

    preferred_platform = ""
    if sys.platform.startswith("linux"):
        preferred_platform = "linux"
    elif sys.platform.startswith("win"):
        preferred_platform = "win"

    candidates.sort(
        key=lambda item: (
            1 if preferred_platform and item[1].lower().startswith(preferred_platform) else 0,
            _runtime_version_sort_key(item[0]),
            item[1],
            str(item[2]),
        ),
        reverse=True,
    )
    version, platform_key, executable_path = candidates[0]
    return {
        "runtime": runtime_label,
        "mode": "breaktwenty_managed",
        "version": version,
        "platform": platform_key,
        "executable_path": str(executable_path),
        "installed_now": False,
    }
