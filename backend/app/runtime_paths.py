from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "BREAKTWENTY_DATA_DIR"
DESKTOP_AUTH_DIR_ENV = "BREAKTWENTY_DESKTOP_AUTH_DIR"
DIAGNOSTIC_DIR_ENV = "BREAKTWENTY_DIAGNOSTIC_DIR"
BROWSER_RUNTIME_DIR_ENV = "BREAKTWENTY_BROWSER_RUNTIME_DIR"
LOG_DIR_ENV = "BREAKTWENTY_LOG_DIR"
DB_PATH_ENV = "BREAKTWENTY_DB_PATH"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent if BACKEND_ROOT.name == "backend" else BACKEND_ROOT
APP_ROOT = BACKEND_ROOT


def _resolve_runtime_path(env_name: str, default: Path) -> Path:
    configured = str(os.getenv(env_name) or "").strip()
    path = Path(configured).expanduser() if configured else default
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _default_data_dir() -> Path:
    if BACKEND_ROOT.name == "backend":
        return REPO_ROOT / "data"
    return BACKEND_ROOT / "data"


def sqlite_database_url(path: Path) -> str:
    return f"sqlite+sqlcipher_aiosqlite:///{path.as_posix()}"


DATA_DIR = _resolve_runtime_path(DATA_DIR_ENV, _default_data_dir())
DESKTOP_AUTH_DIR = _resolve_runtime_path(DESKTOP_AUTH_DIR_ENV, REPO_ROOT / ".desktop-auth")
LOG_DIR = _resolve_runtime_path(LOG_DIR_ENV, DATA_DIR / "logs")
# Per-provider automatic diagnostic logs (phase-1 HAR + phase-2 replay JSONL).
# Defaults under the shared log root (next to data/logs/exports) so all
# automatic provider logs live in one place; env-overridable and cross-platform.
DIAGNOSTIC_DIR = _resolve_runtime_path(DIAGNOSTIC_DIR_ENV, LOG_DIR / "providers")
BROWSER_RUNTIME_DIR = _resolve_runtime_path(
    BROWSER_RUNTIME_DIR_ENV,
    DESKTOP_AUTH_DIR / "browser-runtimes",
)
DATABASE_PATH = _resolve_runtime_path(DB_PATH_ENV, DATA_DIR / "breaktwenty.db")


def runtime_display_path(path: str | Path) -> str:
    target = Path(path)
    if not target.is_absolute():
        return str(target)
    resolved = target.resolve()
    roots = (
        (DATA_DIR, Path("data")),
        (DESKTOP_AUTH_DIR, Path(".desktop-auth")),
    )
    for root, label in roots:
        try:
            return str(label / resolved.relative_to(root.resolve()))
        except ValueError:
            continue
    try:
        return os.path.relpath(resolved, start=APP_ROOT)
    except ValueError:
        return str(resolved)
