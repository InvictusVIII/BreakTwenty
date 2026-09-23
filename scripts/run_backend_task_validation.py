#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess  # nosec B404
import uuid
from pathlib import PurePosixPath


CONTAINER = "breaktwenty-backend-1"
TEMP_ROOT_PATTERN = re.compile(r"^/tmp/breaktwenty-task-validation-[0-9a-f]{32}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one backend task-gate phase with isolated container runtime state."
    )
    parser.add_argument("--phase", choices=("lint", "tests", "import"), required=True)
    return parser.parse_args()


def required_docker() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise SystemExit("Required executable not found on PATH: docker")
    return executable


def isolated_environment(root: str) -> dict[str, str]:
    data = str(PurePosixPath(root) / "data")
    logs = str(PurePosixPath(root) / "logs")
    desktop_auth = str(PurePosixPath(root) / "desktop-auth")
    return {
        "BREAKTWENTY_APP_ENCRYPTION_KEY": base64.b64encode(bytes([1]) * 32).decode("ascii"),
        "BREAKTWENTY_BROWSER_RUNTIME_DIR": str(PurePosixPath(root) / "browser-runtime"),
        "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": base64.b64encode(bytes(32)).decode("ascii"),
        "BREAKTWENTY_DATA_DIR": data,
        "BREAKTWENTY_DB_PATH": str(PurePosixPath(data) / "task-validation.db"),
        "BREAKTWENTY_DESKTOP_AUTH_DIR": desktop_auth,
        "BREAKTWENTY_DIAGNOSTIC_DIR": str(PurePosixPath(logs) / "providers"),
        "BREAKTWENTY_KEY_BOOTSTRAP": "",
        "BREAKTWENTY_LAUNCH_TOKEN_FILE": str(PurePosixPath(desktop_auth) / "launch-auth.token"),
        "BREAKTWENTY_LOG_DIR": logs,
        "BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR": "/repo/scripts",
        "DATABASE_URL": f"sqlite+sqlcipher_aiosqlite:///{PurePosixPath(data) / 'task-validation.db'}",
        "HOME": str(PurePosixPath(root) / "home"),
        "PROVIDER_CATALOG_PATH": "/provider-config/provider_catalog.json",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(PurePosixPath(root) / "pycache"),
        "TMPDIR": str(PurePosixPath(root) / "tmp"),
        "XDG_CACHE_HOME": str(PurePosixPath(root) / "home" / ".cache"),
    }


def docker_command(
    docker: str,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> list[str]:
    result = [docker, "exec"]
    result.extend((
        "-w",
        "/app",
        CONTAINER,
        "env",
        "-i",
        "PATH=/opt/breaktwenty/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG=C.UTF-8",
    ))
    result.extend(f"{key}={value}" for key, value in sorted((environment or {}).items()))
    result.extend(command)
    return result


def run_phase(phase: str) -> int:
    docker = required_docker()
    if phase == "lint":
        subprocess.run(  # nosec B603
            docker_command(
                docker,
                ["python3", "-m", "ruff", "check", "--no-cache", "app", "alembic", "tests"],
            ),
            check=True,
        )
        return 0

    root = f"/tmp/breaktwenty-task-validation-{uuid.uuid4().hex}"
    if not TEMP_ROOT_PATTERN.fullmatch(root):
        raise RuntimeError("Generated backend validation root is unsafe.")
    environment = isolated_environment(root)
    directories = sorted({
        environment["BREAKTWENTY_DATA_DIR"],
        environment["BREAKTWENTY_LOG_DIR"],
        environment["BREAKTWENTY_DIAGNOSTIC_DIR"],
        environment["BREAKTWENTY_DESKTOP_AUTH_DIR"],
        environment["BREAKTWENTY_BROWSER_RUNTIME_DIR"],
        environment["HOME"],
        environment["PYTHONPYCACHEPREFIX"],
        environment["TMPDIR"],
    })
    subprocess.run(
        [docker, "exec", CONTAINER, "mkdir", "-p", *directories],
        check=True,
    )  # nosec B603
    try:
        command = (
            ["python3", "-m", "unittest", "discover", "-s", "tests"]
            if phase == "tests"
            else ["python3", "-c", "import app.main"]
        )
        subprocess.run(  # nosec B603
            docker_command(docker, command, environment=environment),
            check=True,
        )
        return 0
    finally:
        subprocess.run(  # nosec B603
            [docker, "exec", CONTAINER, "rm", "-rf", "--", root],
            check=False,
        )


def main() -> int:
    return run_phase(parse_args().phase)


if __name__ == "__main__":
    raise SystemExit(main())
