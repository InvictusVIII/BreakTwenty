#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import tarfile
import tempfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "backend" / "runtime-policy.json"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maintenance = load_module(ROOT / "scripts" / "check_runtime_maintenance.py")
installer = load_module(ROOT / "backend" / "scripts" / "install_pinned_python_runtime.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a reviewable update of BreakTwenty's pinned runtime policy."
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def select_assets(
    policy: dict[str, object], release: dict[str, object]
) -> tuple[str, dict[str, dict[str, str]]]:
    branch = ".".join(policy["python"]["version"].split(".")[:2])
    release_assets = release.get("assets", [])
    candidates: dict[str, dict[str, dict[str, str]]] = {}
    for target in policy["python"]["assets"]:
        pattern = re.compile(
            rf"^cpython-({re.escape(branch)}\.\d+)(?:[.+].*)-{re.escape(target)}-"
            r"install_only_stripped\.tar\.gz$"
        )
        target_candidates: dict[str, dict[str, str]] = {}
        for asset in release_assets:
            match = pattern.fullmatch(str(asset.get("name", "")))
            digest = str(asset.get("digest", "")).removeprefix("sha256:")
            if not match or not maintenance.SHA256_RE.fullmatch(digest):
                continue
            target_candidates[match.group(1)] = {
                "name": asset["name"],
                "sha256": digest,
                "url": asset["browser_download_url"],
            }
        if not target_candidates:
            raise RuntimeError(f"Latest portable release has no pinned-style asset for {target}")
        candidates[target] = target_candidates
    common_versions = set.intersection(
        *(set(target_candidates) for target_candidates in candidates.values())
    )
    if not common_versions:
        raise RuntimeError("Latest portable release has no common Python version for all targets")
    selected_version = max(common_versions, key=maintenance.version_tuple)
    return selected_version, {
        target: target_candidates[selected_version]
        for target, target_candidates in candidates.items()
    }


def probe_linux_identity(asset: dict[str, str]) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="breaktwenty-runtime-proposal-") as temporary:
        root = Path(temporary)
        archive = root / asset["name"]
        extract_root = root / "extract"
        runtime = root / "runtime"
        extract_root.mkdir()
        installer.download(asset["url"], archive)
        actual_sha256 = installer.sha256_file(archive)
        if actual_sha256 != asset["sha256"]:
            raise RuntimeError(
                f"Downloaded Linux runtime hash mismatch: expected {asset['sha256']}, "
                f"got {actual_sha256}"
            )
        with tarfile.open(archive, "r:gz") as handle:
            handle.extractall(extract_root, filter="data")
        source = installer.extracted_runtime(extract_root)
        shutil.copytree(source, runtime, symlinks=True)
        installer.rewrite_copied_symlinks(runtime, source)
        return installer.runtime_identity(installer.python_executable(runtime))


def proposed_policy(policy: dict[str, object]) -> dict[str, object]:
    repository = policy["maintenance"]["pythonBuildStandaloneRepository"]
    release = maintenance.github_latest_release(repository)
    version, assets = select_assets(policy, release)
    identity = probe_linux_identity(assets["x86_64-unknown-linux-gnu"])
    if identity["pythonVersion"] != version:
        raise RuntimeError(
            f"Linux runtime probe returned Python {identity['pythonVersion']}, expected {version}"
        )
    policy["reviewedAt"] = date.today().isoformat()
    policy["python"]["version"] = version
    policy["python"]["opensslVersion"] = identity["opensslVersion"]
    policy["python"]["release"] = release["tag_name"]
    policy["python"]["assets"] = assets
    return policy


def main() -> int:
    args = parse_args()
    if args.apply and args.output:
        raise SystemExit("Use either --apply or --output, not both.")
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    proposed = proposed_policy(policy)
    destination = (
        args.policy
        if args.apply
        else args.output
        if args.output
        else args.policy.with_suffix(".proposed.json")
    )
    destination.write_text(
        f"{json.dumps(proposed, indent=2, sort_keys=False)}\n", encoding="utf-8"
    )
    print(f"Wrote proposed runtime policy to {destination}")
    if not args.apply:
        print("Review the proposal, then rerun with --apply when ready to rebuild and test an RC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
