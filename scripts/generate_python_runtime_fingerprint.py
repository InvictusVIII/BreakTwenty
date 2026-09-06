from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import ssl
import sys
from pathlib import Path


DEPENDENCIES = (
    "certifi",
    "cryptography",
    "fastapi",
    "httpcore",
    "httpx",
    "patchright",
    "pydantic",
    "pydantic-core",
    "sqlcipher3",
    "uvicorn",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def portable_manifest(resource_root: Path) -> dict[str, object]:
    path = resource_root / "python" / "BREAKTWENTY_PORTABLE_PYTHON.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed = (
        "runtimeKind",
        "release",
        "target",
        "asset",
        "archiveSha256",
        "policySha256",
        "pythonVersion",
        "opensslVersion",
        "pipOnlyBinary",
        "pipNoBinary",
    )
    return {key: payload[key] for key in allowed if key in payload}


def build_fingerprint(resource_root: Path) -> dict[str, object]:
    executable = Path(sys.executable).resolve()
    python_root = (resource_root / "python").resolve()
    if not executable.is_relative_to(python_root):
        raise RuntimeError(f"Fingerprint interpreter {executable} is outside {python_root}")

    import _ssl

    policy_path = resource_root / "backend" / "runtime-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    expected_python = policy["python"]["version"]
    expected_openssl = policy["python"]["opensslVersion"]
    actual_python = platform.python_version()
    actual_openssl = ssl.OPENSSL_VERSION
    if actual_python != expected_python or actual_openssl != expected_openssl:
        raise RuntimeError(
            "Packaged runtime identity does not match policy: "
            f"expected Python {expected_python} / {expected_openssl}, "
            f"got Python {actual_python} / {actual_openssl}"
        )

    dependencies = {}
    for name in DEPENDENCIES:
        distribution = importlib.metadata.distribution(name)
        distribution_root = Path(distribution.locate_file("")).resolve()
        if not distribution_root.is_relative_to(python_root):
            raise RuntimeError(
                f"Fingerprint dependency {name} is outside the packaged runtime"
            )
        dependencies[name] = distribution.version
    ssl_module = {"kind": "builtin"}
    ssl_extension_path = getattr(_ssl, "__file__", None)
    if ssl_extension_path:
        ssl_extension = Path(ssl_extension_path).resolve()
        if not ssl_extension.is_relative_to(python_root):
            raise RuntimeError("Fingerprint SSL extension is outside the packaged runtime")
        ssl_module["kind"] = "extension"
    components = {
        "backendSha256": sha256_tree(resource_root / "backend"),
        "providerCatalogSha256": sha256_file(
            resource_root / "config" / "provider_catalog.json"
        ),
        "pythonExecutableSha256": sha256_file(executable),
        "requirementsLockSha256": sha256_file(
            resource_root / "backend" / "requirements.lock"
        ),
        "runtimePolicySha256": sha256_file(policy_path),
    }
    if ssl_extension_path:
        components["sslExtensionSha256"] = sha256_file(ssl_extension)
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "python": {
            "implementation": platform.python_implementation(),
            "version": actual_python,
            "compiler": platform.python_compiler(),
            "byteOrder": sys.byteorder,
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "architecture": platform.architecture()[0],
        },
        "opensslVersion": actual_openssl,
        "sslModule": ssl_module,
        "portableRuntime": portable_manifest(resource_root),
        "dependencies": dependencies,
        "components": components,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["fingerprintSha256"] = hashlib.sha256(canonical).hexdigest()
    return payload


def main() -> int:
    args = parse_args()
    payload = build_fingerprint(args.resource_root.resolve())
    args.output.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")
    print(f"Packaged Python runtime fingerprint: {payload['fingerprintSha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
