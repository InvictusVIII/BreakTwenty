#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BreakTwenty pinned Python runtime installer"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def extracted_runtime(root: Path) -> Path:
    for candidate in (root / "python" / "install", root / "install", root / "python"):
        if (candidate / "bin" / "python3").is_file() or (
            candidate / "bin" / "python"
        ).is_file():
            return candidate
    raise RuntimeError("Pinned archive did not contain a Python runtime")


def rewrite_copied_symlinks(runtime: Path, source: Path) -> None:
    for link in runtime.rglob("*"):
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if not target.is_absolute():
            continue
        try:
            relative = target.relative_to(source)
        except ValueError:
            continue
        destination = runtime / relative
        link.unlink()
        link.symlink_to(os.path.relpath(destination, link.parent))


def python_executable(runtime: Path) -> Path:
    for candidate in (runtime / "bin" / "python3", runtime / "bin" / "python"):
        if candidate.is_file():
            return candidate
    raise RuntimeError("Installed runtime has no Python executable")


def runtime_identity(python: Path) -> dict[str, str]:
    probe = subprocess.run(  # nosec B603
        [
            str(python),
            "-s",
            "-c",
            (
                "import json, platform, ssl; "
                "print(json.dumps({'pythonVersion': platform.python_version(), "
                "'opensslVersion': ssl.OPENSSL_VERSION}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
    )
    return json.loads(probe.stdout)


def verify_runtime(python: Path, policy: dict[str, object]) -> dict[str, str]:
    actual = runtime_identity(python)
    python_policy = policy["python"]
    expected = {
        "pythonVersion": python_policy["version"],
        "opensslVersion": python_policy["opensslVersion"],
    }
    if actual != expected:
        raise RuntimeError(f"Pinned runtime identity mismatch: expected {expected}, got {actual}")
    return actual


def main() -> int:
    args = parse_args()
    policy_bytes = args.policy.read_bytes()
    policy = json.loads(policy_bytes)
    asset = policy["python"]["assets"].get(args.target)
    if not asset:
        raise RuntimeError(f"Runtime policy has no asset for {args.target}")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="breaktwenty-python-runtime-", dir=output.parent
    ) as temporary:
        temporary_root = Path(temporary)
        archive = temporary_root / asset["name"]
        extract_root = temporary_root / "extract"
        staged_runtime = temporary_root / "runtime"
        extract_root.mkdir()
        download(asset["url"], archive)
        actual_sha256 = sha256_file(archive)
        if actual_sha256 != asset["sha256"]:
            raise RuntimeError(
                f"Pinned runtime archive hash mismatch: expected {asset['sha256']}, "
                f"got {actual_sha256}"
            )
        with tarfile.open(archive, "r:gz") as handle:
            handle.extractall(extract_root, filter="data")
        source = extracted_runtime(extract_root)
        shutil.copytree(source, staged_runtime, symlinks=True)
        rewrite_copied_symlinks(staged_runtime, source)
        actual = verify_runtime(python_executable(staged_runtime), policy)
        manifest = {
            "runtimeKind": policy["python"]["distribution"],
            "release": policy["python"]["release"],
            "target": args.target,
            "asset": asset["name"],
            "archiveSha256": asset["sha256"],
            "policySha256": hashlib.sha256(policy_bytes).hexdigest(),
            **actual,
        }
        (staged_runtime / "BREAKTWENTY_PORTABLE_PYTHON.json").write_text(
            f"{json.dumps(manifest, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        if output.exists():
            shutil.rmtree(output)
        staged_runtime.rename(output)

    print(
        f"Installed pinned {actual['pythonVersion']} / {actual['opensslVersion']} "
        f"runtime for {args.target} at {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
