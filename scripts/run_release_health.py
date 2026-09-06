#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
ANY_WORKTREE_IGNORE_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
ROOT_WORKTREE_IGNORE_NAMES = {
    ".claude",
    ".codex",
    ".desktop-auth",
    ".venv",
    ".venv-desktop-auth",
    "Diagnostic",
    "data",
}
BACKEND_RUNTIME_ENV_KEYS = {
    "DATABASE_URL",
    "BREAKTWENTY_APP_ENCRYPTION_KEY",
    "BREAKTWENTY_BROWSER_RUNTIME_DIR",
    "BREAKTWENTY_DATABASE_ENCRYPTION_KEY",
    "BREAKTWENTY_DATA_DIR",
    "BREAKTWENTY_DB_PATH",
    "BREAKTWENTY_DESKTOP_AUTH_DIR",
    "BREAKTWENTY_DIAGNOSTIC_DIR",
    "BREAKTWENTY_KEY_BOOTSTRAP",
    "BREAKTWENTY_LAUNCH_TOKEN_FILE",
    "BREAKTWENTY_LOG_DIR",
    "BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR",
    "PROVIDER_CATALOG_PATH",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the BreakTwenty release health gate.")
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Reuse existing Node dependencies instead of running npm ci.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Run against the current checkout instead of a temporary clean worktree.",
    )
    return parser.parse_args()


def required_tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise SystemExit(f"Required executable not found on PATH: {name}")
    return executable


def run_step(
    title: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    attempts: int = 1,
    retry_delay_seconds: float = 5.0,
) -> None:
    print(f"\n==> {title}", flush=True)
    # Release checks must never inherit a developer's live runtime targets.
    # Backend import/test steps receive an explicit temporary environment below.
    step_env = {
        key: value
        for key, value in os.environ.items()
        if key not in BACKEND_RUNTIME_ENV_KEYS
    }
    if env:
        step_env.update(env)
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            subprocess.run(command, cwd=cwd, env=step_env, check=True)  # nosec B603
            return
        except subprocess.CalledProcessError:
            if attempt >= max(attempts, 1):
                raise
            print(
                f"{title} failed on attempt {attempt}/{attempts}; retrying in "
                f"{retry_delay_seconds:g}s...",
                flush=True,
            )
            time.sleep(retry_delay_seconds)


def frontend_cache_dirs(root: Path) -> list[Path]:
    node_modules = root / "frontend" / "node_modules"
    return [node_modules / ".cache", node_modules / ".vite"]


def ensure_frontend_cache_writable(root: Path) -> None:
    for cache_dir in frontend_cache_dirs(root):
        ensure_cache_dir_writable(cache_dir)


def ensure_cache_dir_writable(cache_dir: Path) -> None:
    if not cache_dir.exists():
        return
    probe = cache_dir / ".breaktwenty-release-health-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return
    except OSError:
        for index in range(1, 100):
            relocated = cache_dir.with_name(
                f"{cache_dir.name}.release-health-unwritable-{os.getpid()}-{index}"
            )
            if not relocated.exists():
                break
        else:
            raise SystemExit("Could not find a relocation path for an unwritable frontend cache.")

        try:
            cache_dir.rename(relocated)
        except OSError as exc:
            raise SystemExit(
                "Frontend build cache is not writable and could not be moved. "
                f"Cache path: {cache_dir}. Error: {exc}"
            ) from exc
        print(f"Moved unwritable frontend build cache aside: {relocated}", flush=True)


def ignore_worktree_entries(directory: str, names: list[str]) -> set[str]:
    current = Path(directory)
    ignored = set(names) & ANY_WORKTREE_IGNORE_NAMES
    try:
        is_root = current.resolve() == SOURCE_ROOT
    except OSError:
        is_root = False
    if is_root:
        ignored |= set(names) & ROOT_WORKTREE_IGNORE_NAMES
    if current.name == "frontend":
        ignored.add("build")
    if current.name == "desktop":
        ignored.add("dist")
    return ignored


def copy_release_health_worktree(destination: Path) -> None:
    print(f"Preparing temporary release-health worktree: {destination}", flush=True)
    shutil.copytree(
        SOURCE_ROOT,
        destination,
        ignore=ignore_worktree_entries,
        symlinks=True,
    )


def venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run_backend_health(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="breaktwenty_release_health_") as tmp_dir:
        temporary_root = Path(tmp_dir)
        data_dir = temporary_root / "data"
        log_dir = temporary_root / "logs"
        diagnostic_dir = log_dir / "providers"
        desktop_auth_dir = temporary_root / "desktop-auth"
        browser_runtime_dir = temporary_root / "browser-runtime"
        home_dir = temporary_root / "home"
        process_tmp_dir = temporary_root / "tmp"
        pycache_dir = temporary_root / "pycache"
        for runtime_dir in (
            data_dir,
            log_dir,
            diagnostic_dir,
            desktop_auth_dir,
            browser_runtime_dir,
            home_dir,
            process_tmp_dir,
            pycache_dir,
        ):
            runtime_dir.mkdir(parents=True, exist_ok=True)
        run_step(
            "Verify Python dependency lock freshness",
            [sys.executable, "backend/scripts/generate_python_hash_lock.py", "--check"],
            cwd=root,
        )
        run_step(
            "Verify cryptography platform distribution matrix",
            [sys.executable, "scripts/check_cryptography_distribution_matrix.py"],
            cwd=root,
        )
        venv_dir = temporary_root / "venv"
        run_step(
            "Create Python audit environment",
            [sys.executable, "-m", "venv", str(venv_dir)],
            cwd=root,
        )
        venv_python = venv_python_path(venv_dir)
        run_step(
            "Install hash-verified Python bootstrap tooling",
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--require-hashes",
                "--no-deps",
                "--only-binary=:all:",
                "-r",
                "backend/requirements-bootstrap.lock",
            ],
            cwd=root,
        )
        run_step(
            "Install hash-verified backend runtime and audit tooling",
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--no-build-isolation",
                "--only-binary=:all:",
                "-r",
                "backend/requirements-audit.lock",
            ],
            cwd=root,
        )
        run_step(
            "Backend dependency consistency",
            [str(venv_python), "-m", "pip", "check"],
            cwd=root,
        )
        run_step(
            "Backend dependency audit",
            [str(venv_python), "-m", "pip_audit", "-r", "backend/requirements.lock"],
            cwd=root,
        )
        run_step(
            "Backend runtime license and SBOM inventory",
            [
                str(venv_python),
                "backend/scripts/generate_python_runtime_notices.py",
                "--output-dir",
                str(temporary_root / "python-runtime-notices"),
                "--fail-on-missing-license",
            ],
            cwd=root,
        )
        run_step(
            "Backend Python lint",
            [
                str(venv_python),
                "-m",
                "ruff",
                "check",
                "backend/app",
                "backend/alembic",
                "backend/tests",
            ],
            cwd=root,
        )
        release_database_path = data_dir / "release-health.db"
        backend_env = {
            "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "BREAKTWENTY_APP_ENCRYPTION_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
            "BREAKTWENTY_BROWSER_RUNTIME_DIR": str(browser_runtime_dir),
            "BREAKTWENTY_DATA_DIR": str(data_dir),
            "BREAKTWENTY_DB_PATH": str(release_database_path),
            "BREAKTWENTY_DESKTOP_AUTH_DIR": str(desktop_auth_dir),
            "BREAKTWENTY_DIAGNOSTIC_DIR": str(diagnostic_dir),
            "BREAKTWENTY_KEY_BOOTSTRAP": "",
            "BREAKTWENTY_LAUNCH_TOKEN_FILE": str(desktop_auth_dir / "launch-auth.token"),
            "BREAKTWENTY_LOG_DIR": str(log_dir),
            "BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR": str(root / "scripts"),
            "DATABASE_URL": f"sqlite+sqlcipher_aiosqlite:///{release_database_path.as_posix()}",
            "HOME": str(home_dir),
            "PROVIDER_CATALOG_PATH": str(root / "config" / "provider_catalog.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(pycache_dir),
            "TMPDIR": str(process_tmp_dir),
            "XDG_CACHE_HOME": str(home_dir / ".cache"),
        }
        run_step(
            "Backend application import",
            [str(venv_python), "-c", "import app.main"],
            cwd=root / "backend",
            env=backend_env,
        )
        run_step(
            "Backend tests and schema contracts",
            [str(venv_python), "-m", "unittest", "discover", "-s", "tests"],
            cwd=root / "backend",
            env=backend_env,
        )


def run_release_health(root: Path, args: argparse.Namespace) -> int:
    npm = required_tool("npm")

    public_safety_test = root / "scripts" / "tests" / "test_public_tree_safety.py"
    if public_safety_test.is_file():
        run_step(
            "Git release and public-history safety tests",
            [sys.executable, "-m", "unittest", "scripts.tests.test_public_tree_safety"],
            cwd=root,
        )
    if not args.skip_install:
        run_step("Install frontend dependencies", [npm, "--prefix", "frontend", "ci"], cwd=root)
        run_step("Install desktop dependencies", [npm, "--prefix", "desktop", "ci"], cwd=root)

    run_step(
        "Node runtime licence notice freshness",
        [required_tool("node"), "scripts/generate_node_runtime_notices.js", "--check"],
        cwd=root,
    )

    run_step(
        "Frontend dependency audit",
        [npm, "--prefix", "frontend", "audit"],
        cwd=root,
        attempts=3,
        retry_delay_seconds=10.0,
    )
    run_step(
        "Frontend provider catalog check",
        [npm, "--prefix", "frontend", "run", "sync:provider-catalog:check"],
        cwd=root,
    )
    run_step(
        "Frontend seed category taxonomy check",
        [sys.executable, "scripts/generate_seed_taxonomy_mirror.py", "--check"],
        cwd=root,
    )
    run_step(
        "Frontend market strip catalog check",
        [sys.executable, "scripts/generate_market_strip_catalog_mirror.py", "--check"],
        cwd=root,
    )
    run_step(
        "Frontend theme token check",
        [npm, "--prefix", "frontend", "run", "generate:theme:check"],
        cwd=root,
    )
    run_step("Frontend JS lint", [npm, "--prefix", "frontend", "run", "lint:js"], cwd=root)
    ensure_frontend_cache_writable(root)
    run_step("Frontend build", [npm, "--prefix", "frontend", "run", "build"], cwd=root, env={"CI": "false"})
    run_step(
        "Frontend tests",
        [npm, "--prefix", "frontend", "test", "--", "--run"],
        cwd=root,
        env={"CI": "true"},
    )
    run_step("Frontend CSS lint", [npm, "--prefix", "frontend", "run", "--if-present", "lint:css"], cwd=root)
    run_step(
        "Desktop production audit",
        [npm, "--prefix", "desktop", "audit", "--omit=dev"],
        cwd=root,
        attempts=3,
        retry_delay_seconds=10.0,
    )
    release_test_modules = ["scripts.tests.test_packaged_runtime_parity"]
    for module, relative in (
        ("scripts.tests.test_desktop_signing_release", "scripts/tests/test_desktop_signing_release.py"),
        ("scripts.tests.test_runtime_policy", "scripts/tests/test_runtime_policy.py"),
    ):
        if (root / relative).is_file():
            release_test_modules.append(module)
    run_step(
        "Desktop release and packaged-runtime policy tests",
        [sys.executable, "-m", "unittest", *release_test_modules],
        cwd=root,
    )
    run_step("Desktop check", [npm, "--prefix", "desktop", "run", "check"], cwd=root)
    run_step(
        "SQLCipher CPython 3.12 wheel matrix",
        [sys.executable, "scripts/check_sqlcipher_wheel_matrix.py"],
        cwd=root,
    )
    run_backend_health(root)
    print("\nRelease health passed.", flush=True)
    return 0


def main() -> int:
    args = parse_args()
    if args.skip_install and not args.in_place:
        raise SystemExit("--skip-install can only be used with --in-place.")

    if args.in_place:
        return run_release_health(SOURCE_ROOT, args)

    with tempfile.TemporaryDirectory(prefix="breaktwenty_release_health_worktree_") as tmp_dir:
        worktree = Path(tmp_dir) / "BreakTwenty"
        copy_release_health_worktree(worktree)
        return run_release_health(worktree, args)


if __name__ == "__main__":
    raise SystemExit(main())
