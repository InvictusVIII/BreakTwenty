#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "backend" / "runtime-policy.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check BreakTwenty runtime maintenance policy.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--check-upstream", action="store_true")
    parser.add_argument("--quiet-healthy", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    return parser.parse_args()


def load_policy(path: Path) -> dict[str, object]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schemaVersion") != 1:
        raise ValueError("runtime policy schemaVersion must be 1")
    reviewed_at = date.fromisoformat(policy["reviewedAt"])
    review_after_days = policy["reviewAfterDays"]
    if not isinstance(review_after_days, int) or review_after_days < 1:
        raise ValueError("reviewAfterDays must be a positive integer")
    python = policy["python"]
    release = python["release"]
    version = python["version"]
    assets = python["assets"]
    if not isinstance(assets, dict) or not assets:
        raise ValueError("runtime policy must contain pinned assets")
    for target, asset in assets.items():
        name = asset["name"]
        digest = asset["sha256"]
        url = urllib.parse.urlparse(asset["url"])
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 for {target}")
        if url.scheme != "https" or url.hostname != "github.com":
            raise ValueError(f"runtime asset URL must use https://github.com for {target}")
        if urllib.parse.unquote(Path(url.path).name) != name:
            raise ValueError(f"runtime asset URL/name mismatch for {target}")
        for required in (version, release, target, "install_only_stripped.tar.gz"):
            if required not in name:
                raise ValueError(f"runtime asset name for {target} is missing {required}")
    docker = policy["developmentDocker"]
    if "@" in docker["baseImage"] or not DIGEST_RE.fullmatch(docker["baseImageDigest"]):
        raise ValueError("development Docker base image/digest is invalid")
    policy["_reviewedDate"] = reviewed_at
    return policy


def request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "BreakTwenty runtime maintenance checker",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        parsed = json.loads(response.read())
    if not isinstance(parsed, dict):
        raise ValueError(f"unexpected JSON response from {url}")
    return parsed


def github_latest_release(repository: str) -> dict[str, object]:
    return request_json(f"https://api.github.com/repos/{repository}/releases/latest")


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    return tuple(int(part) for part in match.groups(default="0")) if match else ()


def latest_runtime_assets(policy: dict[str, object]) -> dict[str, object]:
    python = policy["python"]
    repository = policy["maintenance"]["pythonBuildStandaloneRepository"]
    release = github_latest_release(repository)
    branch = ".".join(python["version"].split(".")[:2])
    results: dict[str, object] = {
        "release": release.get("tag_name", ""),
        "pythonVersion": "",
        "missingTargets": [],
    }
    versions: list[tuple[int, ...]] = []
    names = [asset.get("name", "") for asset in release.get("assets", [])]
    for target in python["assets"]:
        pattern = re.compile(
            rf"^cpython-({re.escape(branch)}\.\d+)(?:[.+].*)-{re.escape(target)}-"
            r"install_only_stripped\.tar\.gz$"
        )
        matches = [pattern.fullmatch(name) for name in names]
        target_versions = [version_tuple(match.group(1)) for match in matches if match]
        if not target_versions:
            results["missingTargets"].append(target)
        else:
            versions.append(max(target_versions))
    if versions:
        newest = max(versions)
        results["pythonVersion"] = ".".join(str(part) for part in newest)
    return results


def remote_docker_digest(image: str) -> str:
    completed = subprocess.run(  # nosec B603
        ["docker", "buildx", "imagetools", "inspect", image],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})$", completed.stdout, re.MULTILINE)
    if not match:
        raise ValueError(f"could not read remote digest for {image}")
    return match.group(1)


def osv_advisories(name: str, version: str) -> list[dict[str, str]]:
    payload = {
        "package": {"name": name, "ecosystem": "OSS-Fuzz"},
        "version": version,
    }
    response = request_json("https://api.osv.dev/v1/query", payload)
    return [
        {
            "id": str(item.get("id", "unknown")),
            "summary": str(item.get("summary", "No summary supplied")),
        }
        for item in response.get("vulns", [])
        if isinstance(item, dict)
    ]


def build_report(
    policy: dict[str, object], today: date, check_upstream: bool
) -> dict[str, object]:
    reviewed_at = policy.pop("_reviewedDate")
    age_days = (today - reviewed_at).days
    stale = age_days > policy["reviewAfterDays"]
    report: dict[str, object] = {
        "schemaVersion": 1,
        "checkedAt": today.isoformat(),
        "reviewedAt": reviewed_at.isoformat(),
        "ageDays": age_days,
        "reviewAfterDays": policy["reviewAfterDays"],
        "stale": stale,
        "attentionRequired": stale,
        "messages": [],
        "checkErrors": [],
    }
    if stale:
        report["messages"].append(
            f"Runtime policy was last reviewed {age_days} days ago "
            f"(threshold: {policy['reviewAfterDays']} days)."
        )
    if not check_upstream:
        return report

    python = policy["python"]
    try:
        latest = latest_runtime_assets(policy)
        report["latestPortableRuntime"] = latest
        if latest["release"] != python["release"]:
            report["messages"].append(
                f"python-build-standalone release {latest['release']} is available; "
                f"policy pins {python['release']}."
            )
        if version_tuple(latest["pythonVersion"]) > version_tuple(python["version"]):
            report["messages"].append(
                f"CPython {latest['pythonVersion']} is available for the pinned branch; "
                f"policy pins {python['version']}."
            )
        if latest["missingTargets"]:
            report["messages"].append(
                "The latest portable release lacks targets: "
                + ", ".join(latest["missingTargets"])
            )
    except Exception as exc:  # noqa: BLE001
        report["checkErrors"].append(f"Portable runtime release check failed: {exc}")

    docker = policy["developmentDocker"]
    try:
        digest = remote_docker_digest(docker["baseImage"])
        report["remoteDockerDigest"] = digest
        if digest != docker["baseImageDigest"]:
            report["messages"].append(
                f"The pinned Playwright tag now resolves to {digest}; policy pins "
                f"{docker['baseImageDigest']}."
            )
    except Exception as exc:  # noqa: BLE001
        report["checkErrors"].append(f"Docker digest check failed: {exc}")

    try:
        playwright = github_latest_release(policy["maintenance"]["playwrightRepository"])
        latest_playwright = str(playwright.get("tag_name", ""))
        report["latestPlaywrightRelease"] = latest_playwright
        latest_train = version_tuple(latest_playwright)[:2]
        pinned_train = version_tuple(docker["baseImage"])[:2]
        if latest_train > pinned_train:
            report["messages"].append(
                f"Playwright {latest_playwright} is available; policy pins "
                f"{docker['baseImage']}."
            )
    except Exception as exc:  # noqa: BLE001
        report["checkErrors"].append(f"Playwright release check failed: {exc}")

    advisory_version = python["opensslVersion"].split()[1]
    advisories: dict[str, list[dict[str, str]]] = {}
    for name, version in (
        ("python", python["version"]),
        ("cpython", python["version"]),
        ("openssl", advisory_version),
    ):
        try:
            advisories[name] = osv_advisories(name, version)
        except Exception as exc:  # noqa: BLE001
            report["checkErrors"].append(f"OSV advisory check failed for {name}: {exc}")
    report["osvAdvisories"] = advisories
    advisory_ids = sorted(
        {item["id"] for values in advisories.values() for item in values}
    )
    if advisory_ids:
        report["messages"].append(
            "OSV reports runtime advisories requiring review: " + ", ".join(advisory_ids)
        )

    if report["messages"] or report["checkErrors"]:
        report["attentionRequired"] = True
    return report


def print_report(report: dict[str, object]) -> None:
    if report["stale"]:
        print("Runtime maintenance warning:")
        print(
            f"CPython/OpenSSL policy was last reviewed {report['ageDays']} days ago."
        )
        print("Run the runtime maintenance checker before publishing this release.")
    elif not report["messages"] and not report["checkErrors"]:
        print(
            f"Runtime policy age is healthy ({report['ageDays']} days; "
            f"review after {report['reviewAfterDays']} days)."
        )
    for message in report["messages"]:
        if not (report["stale"] and message.startswith("Runtime policy was last reviewed")):
            print(f"Runtime maintenance notice: {message}")
    for error in report["checkErrors"]:
        print(f"Runtime maintenance check error: {error}")


def main() -> int:
    args = parse_args()
    report = build_report(load_policy(args.policy), args.today, args.check_upstream)
    if not args.quiet_healthy or report["attentionRequired"]:
        print_report(report)
    if args.output:
        args.output.write_text(
            f"{json.dumps(report, indent=2, sort_keys=True)}\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
