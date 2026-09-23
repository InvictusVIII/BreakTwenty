from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from quiet_validation import run_quiet_command
except ModuleNotFoundError:
    from scripts.quiet_validation import run_quiet_command


RECEIPT_SCHEMA_VERSION = 1
SOURCE_SUFFIXES = {
    ".c",
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".json",
    ".mjs",
    ".py",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
TREE_IGNORE_NAMES = {
    ".breaktwenty-validation",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}


class ValidationMappingError(ValueError):
    pass


@dataclass(frozen=True)
class CheckSpec:
    check_id: str
    title: str
    command: tuple[str, ...]
    cwd: str = "."
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationPlan:
    paths: tuple[str, ...]
    boundaries: tuple[str, ...]
    checks: tuple[CheckSpec, ...]
    skipped_boundaries: tuple[dict[str, str], ...]


def _normalized_relative_path(root: Path, raw_path: str | Path) -> str:
    value = Path(raw_path)
    candidate = value.resolve() if value.is_absolute() else (root / value).resolve()
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationMappingError(f"Changed path escapes the repository: {raw_path}") from exc
    if not relative.parts or ".git" in relative.parts:
        raise ValidationMappingError(f"Changed path is not a repository file: {raw_path}")
    return relative.as_posix()


def normalize_paths(root: Path, paths: Iterable[str | Path]) -> tuple[str, ...]:
    return tuple(sorted({_normalized_relative_path(root, path) for path in paths}))


def _check_catalog() -> dict[str, CheckSpec]:
    python = sys.executable
    return {
        "validation_gate_tests": CheckSpec(
            "validation_gate_tests",
            "Diff-aware validation gate tests",
            (
                python,
                "-m",
                "unittest",
                "scripts.tests.test_change_validation",
                "scripts.tests.test_quiet_validation",
            ),
            evidence=("mapping", "fingerprint", "receipt-verification"),
        ),
        "release_tooling_tests": CheckSpec(
            "release_tooling_tests",
            "Release and packaged-runtime integration tests",
            (
                python,
                "-m",
                "unittest",
                "scripts.tests.test_desktop_signing_release",
                "scripts.tests.test_packaged_runtime_parity",
            ),
            evidence=("release-health-wiring", "packaged-runtime-policy"),
        ),
        "frontend_contracts": CheckSpec(
            "frontend_contracts",
            "Frontend generated-contract checks",
            (python, "scripts/validate_frontend_contracts.py"),
            evidence=("provider-catalog", "loading-screen", "taxonomy", "market-strip", "theme"),
        ),
        "frontend_js_lint": CheckSpec(
            "frontend_js_lint",
            "Frontend semantic lint",
            ("npm", "--prefix", "frontend", "run", "lint:js"),
            evidence=("frontend-static-semantics",),
        ),
        "frontend_css_lint": CheckSpec(
            "frontend_css_lint",
            "Frontend CSS and theme lint",
            ("npm", "--prefix", "frontend", "run", "lint:css"),
            evidence=("frontend-css", "theme-law"),
        ),
        "frontend_tests": CheckSpec(
            "frontend_tests",
            "Frontend component and behavior tests",
            ("npm", "--prefix", "frontend", "test", "--", "--run"),
            evidence=("frontend-behavior", "neighboring-consumers"),
        ),
        "frontend_build": CheckSpec(
            "frontend_build",
            "Frontend production build",
            ("npm", "--prefix", "frontend", "run", "build"),
            evidence=("vite-import-resolution", "production-bundle"),
        ),
        "desktop_check": CheckSpec(
            "desktop_check",
            "Complete desktop check and dry startup",
            ("npm", "--prefix", "desktop", "run", "check"),
            evidence=("desktop-lint", "desktop-unit-tests", "dry-electron-startup"),
        ),
        "electron_e2e": CheckSpec(
            "electron_e2e",
            "Real isolated Electron route, geometry, and visual journey",
            ("npm", "--prefix", "desktop", "run", "test:e2e"),
            evidence=("electron-main", "chromium-renderer", "all-routes", "geometry", "visual-baselines"),
        ),
        "packaged_native_smoke": CheckSpec(
            "packaged_native_smoke",
            "Current-OS packaged native executable smoke",
            ("npm", "--prefix", "desktop", "run", "test:packaged"),
            evidence=(
                "packaged-executable",
                "production-frontend",
                "embedded-backend",
                "fresh-database-migrations",
            ),
        ),
        "backend_lint": CheckSpec(
            "backend_lint",
            "Backend semantic lint",
            (python, "scripts/run_backend_task_validation.py", "--phase", "lint"),
            evidence=("backend-static-semantics",),
        ),
        "backend_tests": CheckSpec(
            "backend_tests",
            "Backend tests and schema contracts",
            (python, "scripts/run_backend_task_validation.py", "--phase", "tests"),
            evidence=("backend-behavior", "persistence-contracts", "schema-contracts"),
        ),
        "backend_import": CheckSpec(
            "backend_import",
            "Backend application import in authoritative runtime",
            (python, "scripts/run_backend_task_validation.py", "--phase", "import"),
            evidence=("backend-import", "top-level-initialization"),
        ),
    }


def infer_validation_plan(root: Path, paths: Iterable[str | Path]) -> ValidationPlan:
    normalized = normalize_paths(root, paths)
    if not normalized:
        raise ValidationMappingError("No changed paths were supplied or discovered.")

    boundaries: set[str] = set()
    required_checks: set[str] = set()
    mapped_paths: set[str] = set()
    skipped: list[dict[str, str]] = []
    packaged_native_required = False

    for path in normalized:
        path_checks: set[str] = set()
        suffix = Path(path).suffix.lower()
        name = Path(path).name
        is_documentation = suffix in {".md", ".txt"} or path.startswith("docs/")

        if is_documentation:
            boundaries.add("documentation")
            mapped_paths.add(path)
            continue

        if path.startswith("frontend/"):
            boundaries.update(("frontend", "renderer"))
            path_checks.update(
                ("frontend_contracts", "frontend_js_lint", "frontend_tests", "frontend_build", "electron_e2e")
            )
            if suffix == ".css" or "/theme/" in path:
                boundaries.add("frontend_style")
                path_checks.add("frontend_css_lint")
            mapped_paths.add(path)

        if path.startswith("desktop/"):
            boundaries.add("desktop")
            path_checks.add("desktop_check")
            if suffix in {".js", ".mjs", ".json"} or name in {"package.json", "package-lock.json"}:
                boundaries.add("electron_runtime")
                path_checks.add("electron_e2e")
            mapped_paths.add(path)
            if (
                path in {
                    "desktop/electron-builder.yml",
                    "desktop/electron-builder.release.cjs",
                    "desktop/package.json",
                    "desktop/package-lock.json",
                }
                or path.startswith("desktop/src/") and name in {
                    "appIdentity.js",
                    "backendManager.js",
                    "bootstrap.js",
                    "isolatedElectronJourney.js",
                    "isolatedJourneyRoutes.js",
                    "isolatedTestContract.js",
                    "keyManager.js",
                    "main.js",
                    "migrationSafety.js",
                    "preload.js",
                    "secureStorage.js",
                }
                or path.startswith("desktop/test/visual-baselines/")
            ):
                packaged_native_required = True

        if path.startswith("backend/"):
            boundaries.add("backend")
            path_checks.update(("backend_lint", "backend_tests", "backend_import"))
            if path.startswith("backend/alembic/") or path == "backend/app/models.py":
                boundaries.add("database_schema")
                packaged_native_required = True
            if name.startswith("requirements") or path == "backend/runtime-policy.json":
                packaged_native_required = True
            mapped_paths.add(path)

        if path.startswith("config/"):
            boundaries.add("shared_contract")
            path_checks.update(
                (
                    "frontend_contracts",
                    "frontend_js_lint",
                    "frontend_tests",
                    "frontend_build",
                    "desktop_check",
                    "electron_e2e",
                    "backend_lint",
                    "backend_tests",
                    "backend_import",
                )
            )
            mapped_paths.add(path)

        if path.startswith(".github/") or path.startswith(".githooks/"):
            boundaries.add("release_tooling")
            path_checks.add("release_tooling_tests")
            mapped_paths.add(path)

        if path.startswith("release-config/"):
            boundaries.add("release_tooling")
            path_checks.add("release_tooling_tests")
            mapped_paths.add(path)

        if path.startswith("scripts/"):
            boundaries.add("tooling")
            if name in {
                "change_validation.py",
                "quiet_validation.py",
                "run_backend_task_validation.py",
                "validate_change.py",
                "validate_frontend_contracts.py",
            }:
                boundaries.add("validation_gate")
                path_checks.add("validation_gate_tests")
            if name == "validate_frontend_contracts.py":
                path_checks.add("frontend_contracts")
            if name == "run_release_health.py" or "release" in name or "public_tree" in name:
                boundaries.add("release_tooling")
                path_checks.add("release_tooling_tests")
            if name == "run_release_health.py":
                boundaries.update(("desktop", "electron_runtime"))
                path_checks.update(("validation_gate_tests", "desktop_check", "electron_e2e"))
                packaged_native_required = True
            if name.startswith("run_desktop_isolated_"):
                boundaries.update(("desktop", "electron_runtime"))
                path_checks.update(("desktop_check", "electron_e2e"))
            if name in {"run_packaged_native_smoke.js", "run_packaged_native_smoke.test.js"}:
                boundaries.update(("desktop", "electron_runtime"))
                path_checks.update(("desktop_check", "electron_e2e"))
                packaged_native_required = True
            if name in {
                "build_desktop_packaged_runtime.js",
                "prepare_portable_python_runtime.js",
                "python_runtime_install_policy.js",
                "run_packaged_safe_storage_smoke.js",
            }:
                boundaries.update(("desktop", "electron_runtime"))
                path_checks.add("desktop_check")
                packaged_native_required = True
            if name.endswith("_visible_auth.py") or name == "visible_auth_common.py":
                boundaries.update(("backend", "desktop"))
                path_checks.update(("backend_lint", "backend_tests", "backend_import", "desktop_check"))
            if path.startswith("scripts/tests/") and name in {
                "test_change_validation.py",
                "test_quiet_validation.py",
            }:
                path_checks.add("validation_gate_tests")
            if suffix in SOURCE_SUFFIXES and not path_checks.intersection(
                {"validation_gate_tests", "release_tooling_tests", "desktop_check", "backend_tests"}
            ):
                raise ValidationMappingError(
                    f"Source file has no executable validation mapping: {path}"
                )
            mapped_paths.add(path)

        if path in {".gitignore", "LICENSE.md", "NOTICE.md", "SECURITY.md"}:
            boundaries.add("repository_metadata")
            mapped_paths.add(path)

        if path not in mapped_paths and suffix in SOURCE_SUFFIXES:
            raise ValidationMappingError(f"Source file has no validation mapping: {path}")
        if path not in mapped_paths:
            boundaries.add("repository_asset")
            mapped_paths.add(path)
        required_checks.update(path_checks)

    if packaged_native_required:
        boundaries.add("packaged_native_renderer")
        required_checks.add("packaged_native_smoke")

    if {"renderer", "electron_runtime"}.intersection(boundaries) and not packaged_native_required:
        skipped.append({
            "boundary": "packaged_native_renderer",
            "reason": "The task gate exercises development Electron; packaged-native smoke remains a separate release tier.",
        })
    if "backend" in boundaries:
        skipped.append({
            "boundary": "live_provider_networks",
            "reason": "Automated validation does not use credentials or contact live providers.",
        })
    if boundaries == {"documentation"}:
        skipped.append({
            "boundary": "runtime_execution",
            "reason": "Documentation-only changes have no executable runtime boundary.",
        })

    catalog = _check_catalog()
    ordered_checks = tuple(catalog[check_id] for check_id in catalog if check_id in required_checks)
    return ValidationPlan(
        paths=normalized,
        boundaries=tuple(sorted(boundaries)),
        checks=ordered_checks,
        skipped_boundaries=tuple(skipped),
    )


def _path_record(root: Path, relative_path: str) -> dict[str, object]:
    path = root / relative_path
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": relative_path, "kind": "missing", "sha256": None}
    if stat.S_ISLNK(metadata.st_mode):
        value = os.readlink(path)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return {"path": relative_path, "kind": "symlink", "sha256": digest}
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationMappingError(f"Validation paths must be regular files or symlinks: {relative_path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": relative_path, "kind": "file", "sha256": digest}


def fingerprint_paths(root: Path, paths: Iterable[str]) -> dict[str, object]:
    records = [_path_record(root, path) for path in sorted(paths)]
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "algorithm": "sha256",
        "value": hashlib.sha256(canonical).hexdigest(),
        "files": records,
    }


def fingerprint_tree(root: Path) -> dict[str, object]:
    paths: list[str] = []
    for directory, names, filenames in os.walk(root):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        names[:] = sorted(
            name
            for name in names
            if name not in TREE_IGNORE_NAMES
            and not (relative_directory == Path("frontend") and name == "build")
            and not (relative_directory == Path("desktop") and name == "dist")
        )
        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(root).as_posix()
            if any(part in TREE_IGNORE_NAMES for part in Path(relative).parts):
                continue
            paths.append(relative)
    result = fingerprint_paths(root, paths)
    result["scope"] = "complete-tree"
    result["file_count"] = len(paths)
    result.pop("files", None)
    return result


def git_head(root: Path) -> str | None:
    completed = subprocess.run(  # nosec B603
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def receipt_base(
    root: Path,
    *,
    mode: str,
    paths: Iterable[str],
    boundaries: Iterable[str],
    skipped_boundaries: Iterable[dict[str, str]],
    fingerprint: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_paths = tuple(paths)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "running",
        "mode": mode,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "git_head": git_head(root),
            "fingerprint": fingerprint or fingerprint_paths(root, normalized_paths),
        },
        "changed_paths": list(normalized_paths),
        "boundaries": sorted(set(boundaries)),
        "checks": [],
        "skipped_boundaries": list(skipped_boundaries),
    }


def run_check(root: Path, check: CheckSpec) -> dict[str, object]:
    record: dict[str, object] = {
        "id": check.check_id,
        "title": check.title,
        "command": list(check.command),
        "cwd": check.cwd,
        "evidence": list(check.evidence),
        "status": "running",
    }
    result = run_quiet_command(
        check.command,
        cwd=root / check.cwd,
        label=check.title,
        log_directory=root / ".breaktwenty-validation" / "logs",
        receipt_root=root,
    )
    record["exit_code"] = result.exit_code
    record["status"] = "passed" if result.passed else "failed"
    record["duration_ms"] = result.duration_ms
    record["output"] = result.receipt_output()
    if result.error:
        record["error"] = result.error
    return record


def write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def verify_receipt(root: Path, receipt: dict[str, object]) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Validation receipt schema is unsupported.")
    if receipt.get("status") != "passed":
        raise ValueError("Validation receipt does not record a passed gate.")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or any(check.get("status") != "passed" for check in checks):
        raise ValueError("Validation receipt contains an incomplete or failed check.")
    paths = receipt.get("changed_paths")
    repository = receipt.get("repository")
    if not isinstance(paths, list) or not isinstance(repository, dict):
        raise ValueError("Validation receipt scope is malformed.")
    expected = repository.get("fingerprint")
    if receipt.get("mode") == "complete-release":
        actual = fingerprint_tree(root)
    else:
        actual = fingerprint_paths(root, normalize_paths(root, paths))
    if not isinstance(expected, dict) or expected.get("value") != actual.get("value"):
        raise ValueError("Validation receipt fingerprint no longer matches the validated files.")


def check_id_from_title(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return value or "unnamed_check"
