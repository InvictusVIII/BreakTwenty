#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("Required executable not found on PATH: npm")
    commands = [
        [npm, "--prefix", "frontend", "run", "sync:provider-catalog:check"],
        [npm, "--prefix", "frontend", "run", "sync:loading-screen:check"],
        [npm, "--prefix", "frontend", "run", "generate:theme:check"],
        [sys.executable, "scripts/generate_seed_taxonomy_mirror.py", "--check"],
        [sys.executable, "scripts/generate_market_strip_catalog_mirror.py", "--check"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)  # nosec B603
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
