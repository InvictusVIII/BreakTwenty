#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_INPUT = REPO_ROOT / "backend" / "requirements-runtime.in"
RUNTIME_LOCK = REPO_ROOT / "backend" / "requirements.lock"
MAC_BUILD_INPUT = REPO_ROOT / "backend" / "requirements-macos-x64-build.in"
MAC_BUILD_LOCK = REPO_ROOT / "backend" / "requirements-macos-x64-build.lock"
PIN_RE = re.compile(r"^cryptography==(?P<version>\d+\.\d+\.\d+)$", re.MULTILINE)
HASH_RE = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")
ABI3_RE = re.compile(r"-cp(?P<major>\d)(?P<minor>\d+)-abi3-")
EXACT_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^;\s]+)$")


def _cryptography_pin() -> str:
    match = PIN_RE.search(RUNTIME_INPUT.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError("requirements-runtime.in has no exact cryptography pin")
    version = match.group("version")
    if tuple(map(int, version.split("."))) < (50, 0, 0):
        raise RuntimeError(f"cryptography {version} is below the audited safe floor 50.0.0")
    return version


def _lock_hashes(lock_path: Path, name: str, version: str) -> set[str]:
    lines = lock_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{name}=={version} "):
            hashes: set[str] = set()
            for continuation in lines[index + 1 :]:
                digest = HASH_RE.search(continuation)
                if digest is None:
                    break
                hashes.add(digest.group("digest"))
            if hashes:
                return hashes
    raise RuntimeError(f"{lock_path.name} has no hashed {name}=={version} entry")


def _release(name: str, version: str) -> list[dict[str, object]]:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{name}/{version}/json",
        headers={"User-Agent": "BreakTwenty cryptography platform probe"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return list(json.load(response).get("urls") or [])


def _verify_complete_release_hashes(
    name: str,
    version: str,
    artifacts: list[dict[str, object]],
    lock_path: Path,
) -> set[str]:
    published_hashes = {
        str(item.get("digests", {}).get("sha256") or "").lower()
        for item in artifacts
    }
    lock_hashes = _lock_hashes(lock_path, name, version)
    if published_hashes != lock_hashes:
        raise RuntimeError(f"{name} lock hashes do not match the complete PyPI release")
    return lock_hashes


def _mac_x64_build_pins() -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for raw_line in MAC_BUILD_INPUT.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXACT_PIN_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"macOS x64 build prerequisite is not exact: {line}")
        pins.append((match.group("name"), match.group("version")))
    return pins


def _has_macos_x64_wheel(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    try:
        _prefix, python_tag, abi_tag, platform_tag = filename[:-4].rsplit("-", 3)
    except ValueError:
        return False
    python_compatible = python_tag in {"py3", "py2.py3", "cp312"} or (
        python_tag.startswith("cp")
        and abi_tag == "abi3"
        and int(python_tag.removeprefix("cp")) <= 312
    )
    platform_compatible = platform_tag == "any" or (
        "macosx_" in platform_tag
        and (
            platform_tag.endswith("_x86_64")
            or platform_tag.endswith("_universal2")
        )
    )
    return python_compatible and platform_compatible


def _abi3_supports_python312(filename: str) -> bool:
    match = ABI3_RE.search(filename)
    if match is None:
        return False
    return (int(match.group("major")), int(match.group("minor"))) <= (3, 12)


def main() -> int:
    version = _cryptography_pin()
    artifacts = _release("cryptography", version)
    lock_hashes = _verify_complete_release_hashes(
        "cryptography", version, artifacts, RUNTIME_LOCK
    )

    compatible_wheels = {
        "Linux x64": lambda name: "manylinux" in name and name.endswith("_x86_64.whl"),
        "Windows x64": lambda name: name.endswith("-win_amd64.whl"),
        "macOS arm64": lambda name: "-macosx_" in name and name.endswith("_arm64.whl"),
    }
    resolved: dict[str, str] = {}
    for label, predicate in compatible_wheels.items():
        candidates = sorted(
            str(item.get("filename") or "")
            for item in artifacts
            if predicate(str(item.get("filename") or ""))
            and _abi3_supports_python312(str(item.get("filename") or ""))
        )
        if not candidates:
            raise RuntimeError(f"cryptography {version} has no CPython 3.12-compatible {label} wheel")
        resolved[label] = candidates[0]

    source_name = f"cryptography-{version}.tar.gz"
    source = next(
        (item for item in artifacts if item.get("filename") == source_name),
        None,
    )
    if source is None:
        raise RuntimeError(f"cryptography {version} has no source archive for macOS x64")
    source_hash = str(source.get("digests", {}).get("sha256") or "").lower()
    if source_hash not in lock_hashes:
        raise RuntimeError("cryptography macOS x64 source archive is not hash-locked")
    resolved["macOS x64"] = source_name

    prerequisites: list[str] = []
    for name, prerequisite_version in _mac_x64_build_pins():
        prerequisite_artifacts = _release(name, prerequisite_version)
        _verify_complete_release_hashes(
            name,
            prerequisite_version,
            prerequisite_artifacts,
            MAC_BUILD_LOCK,
        )
        wheels = sorted(
            str(item.get("filename") or "")
            for item in prerequisite_artifacts
            if _has_macos_x64_wheel(str(item.get("filename") or ""))
        )
        if not wheels:
            raise RuntimeError(
                f"{name}=={prerequisite_version} has no macOS x64-compatible wheel"
            )
        prerequisites.append(f"{name}=={prerequisite_version}: {wheels[0]}")

    print(f"cryptography=={version} distribution matrix:")
    for label, filename in resolved.items():
        mode = "source (hash-locked)" if label == "macOS x64" else "wheel (hash-locked)"
        print(f"  {label}: {filename} [{mode}]")
    print("macOS x64 hash-locked build prerequisites:")
    for prerequisite in prerequisites:
        print(f"  {prerequisite}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
