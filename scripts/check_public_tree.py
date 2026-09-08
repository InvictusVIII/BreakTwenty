#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO_ROOT / "release-config" / "publication-policy.json"
MAX_BLOB_SIZE = 32 * 1024 * 1024
LICENSE_PATH = "LICENSE.md"
REQUIRED_LICENSE_PHRASES = (
    "BreakTwenty Source-Available License 1.0",
    "Copyright © 2026 Oliver Hanzely.",
    "All rights reserved except as expressly granted by this License.",
    "personal and non-commercial purposes",
    "may not be used for commercial, organizational, business, or revenue-generating purposes",
    "may not be monetized, directly or indirectly",
    "Licensed under the BreakTwenty Source-Available License 1.0.",
)

BLOCKED_PATH_SEGMENTS = {
    ".claude",
    ".codex",
    ".desktop-auth",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-desktop-auth",
    ".vscode",
    "__pycache__",
    "build",
    "cache",
    "caches",
    "capture",
    "captures",
    "coverage",
    "diagnostic",
    "diagnostics",
    "dist",
    "logs",
    "node_modules",
    "runtime-data",
    "screenshots",
    "traces",
}
ALLOWED_BLOCKED_SEGMENT_PATHS = {
    "desktop/build/entitlements.mac.inherit.plist",
    "desktop/build/entitlements.mac.plist",
}
BLOCKED_ROOTS = {"data", "diagnostic"}
BLOCKED_FILENAMES = {
    ".env",
    ".env.save",
    "app-update.yml",
    "credentials.json",
    "credentials_login_providers_backup.sql",
    "cookies.json",
    "launch-auth.token",
    "session.json",
    "storage_state.json",
}
BLOCKED_SUFFIXES = {
    ".7z",
    ".appimage",
    ".bak",
    ".cer",
    ".crt",
    ".db",
    ".deb",
    ".der",
    ".dll",
    ".dmg",
    ".dylib",
    ".env",
    ".exe",
    ".har",
    ".key",
    ".log",
    ".old",
    ".orig",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".so",
    ".sql",
    ".sqlcipher",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tmp",
    ".trace",
    ".webm",
    ".zip",
}
BLOCKED_NAME_FRAGMENTS = {
    "private-updater-token",
    "runtime-key-bundle",
    "wrapped-key-bundle",
}
FORBIDDEN_TIP_PATHS = {
    ".githooks/pre-push",
    ".github/workflows/desktop-package-diagnostic.yml",
    ".github/workflows/macos-notary-smoke.yml",
    ".github/workflows/macos-notary-status.yml",
    ".github/workflows/macos-package-diagnostic.yml",
    ".github/workflows/runtime-maintenance.yml",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "PROJECT_STATE.md",
    "RULES.md",
    "backend/Dockerfile",
    "backend/README.md",
    "desktop/README.md",
    "docker-compose.yml",
    "frontend/README.md",
    "frontend/src/theme/THEME.md",
    "release-notes/README.md",
    "release-config/public-desktop-release.yml",
    "scripts/build_finance_icon_pack.js",
    "scripts/check_provider_state.sh",
    "scripts/check_repo_ownership.py",
    "scripts/generate_app_icons.py",
    "scripts/launch_auth_token.js",
    "scripts/normalize_sidebar_icons.py",
    "scripts/push_private_test.py",
    "scripts/push_public_release.py",
    "scripts/release_git.py",
    "scripts/start_desktop.js",
    "scripts/start_desktop.sh",
    "scripts/start_desktop_build.sh",
    "scripts/start_desktop_packaged_runtime.js",
    "scripts/start_desktop_packaged_runtime.sh",
    "scripts/sync_emoji.sh",
    "scripts/tests/test_git_release_integration.py",
}
FORBIDDEN_TIP_PREFIXES = ("brand/",)
SENSITIVE_CAPTURE_WORDS = {"capture", "diagnostic", "screenshot", "trace"}
CAPTURE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
DEFAULT_FORBIDDEN_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN RSA " + b"PRIVATE KEY-----",
    b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
    b"-----BEGIN EC " + b"PRIVATE KEY-----",
    b"-----BEGIN " + b"CERTIFICATE-----",
    b"BREAKTWENTY_" + b"PRIVATE_ONLY",
)
TOKEN_PATTERNS = (
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"ASIA[0-9A-Z]{16}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
)
OBSOLETE_LEGAL_PATTERNS = (
    re.compile(r"\bprivate and confidential\b", re.IGNORECASE),
    re.compile(r"\bprivate (?:testing )?repository\b", re.IGNORECASE),
    re.compile(r"\bevaluation-only\b", re.IGNORECASE),
)
CONTRADICTORY_README_PATTERNS = (
    re.compile(r"\bBreakTwenty is (?:fully )?open[ -]source\b", re.IGNORECASE),
    re.compile(r"\bBreakTwenty is licensed under (?:the )?(?:MIT|Apache|GPL|AGPL|BSD)\b", re.IGNORECASE),
)
README_LICENSE_TARGETS = (
    "LICENSE.md",
    "https://github.com/InvictusVIII/BreakTwenty?tab=License-1-ov-file#readme",
)
CONTRIBUTION_LICENSE_TARGETS = (
    "LICENSE.md#9-contributions-to-the-official-breaktwenty-project",
    "https://github.com/InvictusVIII/BreakTwenty?tab=License-1-ov-file#9-contributions-to-the-official-breaktwenty-project",
)


class SafetyError(RuntimeError):
    pass


def git(repo: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(  # nosec B603
            ["git", "-C", str(repo), *args],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise SafetyError(f"could not execute Git: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SafetyError(f"Git {' '.join(args)} failed: {detail or 'unknown error'}")
    return completed.stdout


def load_policy(path: Path) -> dict[str, object]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SafetyError(f"publication policy is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SafetyError(f"publication policy is invalid JSON: {path}: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schemaVersion") not in {2, 3}:
        raise SafetyError("publication policy must be a schemaVersion 2 or 3 object")
    if policy["schemaVersion"] == 2:
        roots = policy.get("allowedHistoryRoots")
        if not isinstance(roots, list) or not roots or not all(
            isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40,64}", item) for item in roots
        ):
            raise SafetyError("schemaVersion 2 publication policy must declare allowedHistoryRoots")
    elif policy.get("historyMode") != "single-independent-public-root":
        raise SafetyError(
            "schemaVersion 3 publication policy must require single-independent-public-root history"
        )
    markers = policy.get("forbiddenContentMarkers", [])
    if not isinstance(markers, list) or not all(isinstance(item, str) and item for item in markers):
        raise SafetyError("forbiddenContentMarkers must be a list of non-empty strings")
    legal_files = policy.get("legalPackageFiles")
    if not isinstance(legal_files, list) or not legal_files or not all(
        isinstance(item, str) and item for item in legal_files
    ):
        raise SafetyError("legalPackageFiles must be a non-empty list of repository paths")
    required = policy.get("requiredPublicFiles")
    if not isinstance(required, list) or not set(legal_files).issubset(required):
        raise SafetyError("every legalPackageFiles entry must also be requiredPublicFiles")
    return policy


def resolve_commit(repo: Path, ref: str) -> str:
    output = git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", output):
        raise SafetyError(f"could not resolve a commit for {ref}")
    return output


def verify_history_roots(repo: Path, candidate: str, policy: dict[str, object]) -> list[str]:
    roots = git(repo, ["rev-list", "--max-parents=0", candidate]).decode().splitlines()
    if not roots:
        raise SafetyError("candidate history has no determinable root")
    if policy["schemaVersion"] == 3:
        if len(roots) != 1:
            raise SafetyError(
                "candidate history must have exactly one independent public root; found "
                + ", ".join(roots)
            )
        return roots
    allowed = set(policy["allowedHistoryRoots"])
    unexpected = sorted(set(roots) - allowed)
    if unexpected:
        raise SafetyError(
            "candidate makes non-public ancestry reachable; unexpected history root(s): "
            + ", ".join(unexpected)
        )
    return roots


def verify_baseline(repo: Path, baseline_ref: str, candidate: str) -> None:
    baseline = resolve_commit(repo, baseline_ref)
    completed = subprocess.run(  # nosec B603
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", baseline, candidate],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode == 1:
        raise SafetyError(f"candidate {candidate} does not descend from required baseline {baseline_ref} ({baseline})")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SafetyError(f"could not determine ancestry from {baseline_ref}: {detail or 'unknown error'}")


def validate_path(path: str, mode: str) -> list[str]:
    violations: list[str] = []
    if not path or "\\" in path or path.startswith("/"):
        return ["path is not a canonical repository-relative POSIX path"]
    pure = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in pure.parts):
        return ["path contains an empty, dot, or parent segment"]
    lowered_parts = [part.casefold() for part in pure.parts]
    lowered_name = pure.name.casefold()
    suffixes = {suffix.casefold() for suffix in pure.suffixes}
    if lowered_parts and lowered_parts[0] in BLOCKED_ROOTS:
        violations.append("uses a blocked runtime/private root")
    if path not in ALLOWED_BLOCKED_SEGMENT_PATHS and set(lowered_parts) & BLOCKED_PATH_SEGMENTS:
        violations.append("uses a blocked runtime/build/cache path segment")
    if lowered_name in BLOCKED_FILENAMES or lowered_name.startswith(".env."):
        violations.append("uses a blocked secret/runtime filename")
    if suffixes & BLOCKED_SUFFIXES:
        violations.append("uses a blocked secret/runtime/binary extension")
    if any(fragment in lowered_name for fragment in BLOCKED_NAME_FRAGMENTS):
        violations.append("uses a blocked private-artifact filename")
    if lowered_parts and lowered_parts[0] == "release-config" and "private" in pure.stem.casefold():
        violations.append("uses private-only release configuration")
    stem_words = set(re.split(r"[^a-z0-9]+", pure.stem.casefold()))
    if suffixes & CAPTURE_EXTENSIONS and stem_words & SENSITIVE_CAPTURE_WORDS:
        violations.append("looks like a private screenshot/capture artifact")
    if mode == "120000":
        violations.append("is a symlink; public trees must contain regular files only")
    elif mode == "160000":
        violations.append("is a Git submodule; public source must be self-contained")
    elif not mode.startswith("100"):
        violations.append(f"uses unsupported Git mode {mode}")
    return violations


def content_violations(content: bytes, markers: Iterable[bytes]) -> list[str]:
    violations: list[str] = []
    for marker in markers:
        if marker in content:
            violations.append(f"contains forbidden marker {marker.decode('utf-8', errors='replace')!r}")
    for pattern in TOKEN_PATTERNS:
        if pattern.search(content):
            violations.append(f"contains a high-confidence credential/token pattern {pattern.pattern!r}")
    return violations


def tree_entries(repo: Path, commit: str) -> list[tuple[str, str, str]]:
    raw = git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit])
    entries: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, object_type, oid_bytes = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SafetyError(f"could not safely decode tree entry in {commit}") from exc
        if object_type not in {b"blob", b"commit"}:
            raise SafetyError(f"unsupported Git object type in {commit}: {object_type!r}")
        entries.append((mode_bytes.decode(), oid_bytes.decode(), path))
    return entries


def scan_history(repo: Path, candidate: str, policy: dict[str, object]) -> list[str]:
    commits = git(repo, ["rev-list", "--reverse", candidate]).decode().splitlines()
    if not commits:
        raise SafetyError("candidate history could not be enumerated")
    markers = [*DEFAULT_FORBIDDEN_MARKERS]
    markers.extend(item.encode("utf-8") for item in policy.get("forbiddenContentMarkers", []))
    violations: list[str] = []
    scanned: set[tuple[str, str, str]] = set()
    for commit in commits:
        for mode, oid, path in tree_entries(repo, commit):
            key = (mode, oid, path)
            if key in scanned:
                continue
            scanned.add(key)
            for detail in validate_path(path, mode):
                violations.append(f"{commit[:12]}:{path}: {detail}")
            if mode == "160000":
                continue
            size_raw = git(repo, ["cat-file", "-s", oid]).decode().strip()
            try:
                size = int(size_raw)
            except ValueError as exc:
                raise SafetyError(f"could not determine blob size for {commit}:{path}") from exc
            if size > MAX_BLOB_SIZE:
                violations.append(
                    f"{commit[:12]}:{path}: blob is {size} bytes, above the fail-closed scan limit"
                )
                continue
            content = git(repo, ["cat-file", "blob", oid])
            for detail in content_violations(content, markers):
                violations.append(f"{commit[:12]}:{path}: {detail}")
    return violations


def verify_required_files(repo: Path, candidate: str, policy: dict[str, object]) -> list[str]:
    required = policy.get("requiredPublicFiles", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
        raise SafetyError("requiredPublicFiles must be a list of non-empty strings")
    available = {path for _mode, _oid, path in tree_entries(repo, candidate)}
    return [f"{candidate[:12]}:{path}: required public file is missing" for path in required if path not in available]


def tip_blobs(repo: Path, candidate: str) -> dict[str, bytes]:
    return {
        path: git(repo, ["cat-file", "blob", oid])
        for mode, oid, path in tree_entries(repo, candidate)
        if mode != "160000"
    }


def verify_legal_package(repo: Path, candidate: str, policy: dict[str, object]) -> list[str]:
    blobs = tip_blobs(repo, candidate)
    legal_files = policy["legalPackageFiles"]
    violations: list[str] = []
    decoded: dict[str, str] = {}
    for path in legal_files:
        content = blobs.get(path)
        if content is None:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"{candidate[:12]}:{path}: legal/publication file is not UTF-8 text")
            continue
        decoded[path] = text
        for pattern in OBSOLETE_LEGAL_PATTERNS:
            if pattern.search(text):
                violations.append(
                    f"{candidate[:12]}:{path}: contains obsolete private-testing wording matching {pattern.pattern!r}"
                )

    license_bytes = blobs.get(LICENSE_PATH)
    if license_bytes is not None:
        expected_hash = policy.get("expectedLicenseSha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            violations.append(f"{candidate[:12]}:{LICENSE_PATH}: expected publication checksum is missing")
        elif hashlib.sha256(license_bytes).hexdigest() != expected_hash:
            violations.append(f"{candidate[:12]}:{LICENSE_PATH}: custom BreakTwenty license text is missing or modified")
        license_text = license_bytes.decode("utf-8", errors="replace")
        for phrase in REQUIRED_LICENSE_PHRASES:
            if phrase not in license_text:
                violations.append(f"{candidate[:12]}:{LICENSE_PATH}: required license phrase is missing: {phrase!r}")

    readme = decoded.get("README.md", "")
    for phrase in (
        "Source Available — Not Open Source",
        "BreakTwenty Source-Available License 1.0",
        "Commercial use, organizational use, business use, revenue-generating use, and monetization",
    ):
        if phrase not in readme:
            violations.append(f"{candidate[:12]}:README.md: required source-available statement is missing: {phrase!r}")
    if not any(
        f"The exact terms in [LICENSE.md]({target}) control." in readme
        for target in README_LICENSE_TARGETS
    ):
        violations.append(
            f"{candidate[:12]}:README.md: required canonical LICENSE.md control statement is missing"
        )
    for pattern in CONTRADICTORY_README_PATTERNS:
        if pattern.search(readme):
            violations.append(
                f"{candidate[:12]}:README.md: contradicts the source-available licence using {pattern.pattern!r}"
            )

    contributing = decoded.get("CONTRIBUTING.md", "")
    if "source-available, not open source" not in contributing:
        violations.append(f"{candidate[:12]}:CONTRIBUTING.md: source-available status is missing or contradictory")
    if not any(
        f"Section 9 of [LICENSE.md]({target})" in contributing
        for target in CONTRIBUTION_LICENSE_TARGETS
    ):
        violations.append(f"{candidate[:12]}:CONTRIBUTING.md: contribution licence terms are missing")
    if "not required to accept, merge, publish, or continue using any contribution" not in contributing:
        violations.append(f"{candidate[:12]}:CONTRIBUTING.md: contribution acceptance discretion is missing")

    security = decoded.get(".github/SECURITY.md", "")
    for phrase in ("GitHub Private Vulnerability Reporting", "security@breaktwenty.com"):
        if phrase not in security:
            violations.append(f"{candidate[:12]}:.github/SECURITY.md: private reporting path is missing: {phrase!r}")

    notices = decoded.get("desktop/THIRD_PARTY_NOTICES.md", "")
    if "third-party-licenses" not in notices or "not covered by BreakTwenty's" not in notices:
        violations.append(f"{candidate[:12]}:desktop/THIRD_PARTY_NOTICES.md: third-party licence boundary is incomplete")
    package_text = decoded.get("desktop/package.json", "")
    try:
        package = json.loads(package_text)
    except json.JSONDecodeError:
        package = None
    if not isinstance(package, dict) or package.get("private") is not True:
        violations.append(f"{candidate[:12]}:desktop/package.json: npm publication must remain disabled")
    if not isinstance(package, dict) or package.get("license") != "SEE LICENSE IN ../LICENSE.md":
        violations.append(f"{candidate[:12]}:desktop/package.json: licence metadata does not point to the root LICENSE.md")
    return violations


def verify_tip_layout(
    repo: Path, candidate: str, *, allow_private_release_files: bool = False
) -> list[str]:
    paths = {path for _mode, _oid, path in tree_entries(repo, candidate)}
    return [
        f"{candidate[:12]}:{path}: private development material is forbidden in the current public source tree"
        for path in sorted(paths)
        if (path in FORBIDDEN_TIP_PATHS or path.startswith(FORBIDDEN_TIP_PREFIXES))
        and not (
            allow_private_release_files
            and path in {
                ".githooks/pre-push",
                ".github/workflows/desktop-package-diagnostic.yml",
                ".github/workflows/macos-notary-smoke.yml",
                ".github/workflows/macos-notary-status.yml",
                ".github/workflows/macos-package-diagnostic.yml",
                ".github/workflows/runtime-maintenance.yml",
                "release-config/public-desktop-release.yml",
            }
        )
    ]


def verify_clean_worktree(repo: Path, candidate: str) -> list[str]:
    head = resolve_commit(repo, "HEAD")
    if head != candidate:
        raise SafetyError("--check-worktree requires the candidate to be HEAD")
    raw = git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if not raw:
        return []
    entries = [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
    return [f"working tree is not a committed candidate: {entry}" for entry in entries]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail closed if a candidate Git history is unsafe to make public.",
    )
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--baseline-ref", default="")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--check-worktree", action="store_true")
    parser.add_argument("--allow-private-release-files", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).expanduser().resolve()
    policy_path = Path(args.policy).expanduser().resolve()
    try:
        policy = load_policy(policy_path)
        candidate = resolve_commit(repo, args.candidate)
        roots = verify_history_roots(repo, candidate, policy)
        if args.baseline_ref:
            verify_baseline(repo, args.baseline_ref, candidate)
        violations = scan_history(repo, candidate, policy)
        violations.extend(
            verify_tip_layout(
                repo,
                candidate,
                allow_private_release_files=args.allow_private_release_files,
            )
        )
        violations.extend(verify_required_files(repo, candidate, policy))
        violations.extend(verify_legal_package(repo, candidate, policy))
        if args.check_worktree:
            violations.extend(verify_clean_worktree(repo, candidate))
    except SafetyError as exc:
        print(f"Public-tree safety check failed closed: {exc}", file=sys.stderr)
        return 2

    if violations:
        print("Public-tree safety check rejected the candidate:", file=sys.stderr)
        for violation in violations[:100]:
            print(f"  {violation}", file=sys.stderr)
        if len(violations) > 100:
            print(f"  ... {len(violations) - 100} more", file=sys.stderr)
        return 1

    print(
        f"Public-tree safety passed for {candidate} across {len(roots)} allowed history root(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
