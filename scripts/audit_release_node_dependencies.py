#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "release-config" / "dependency-audit-policy.json"
GHSA_URL_PREFIX = "https://github.com/advisories/"
RETRYABLE_AUDIT_EXIT = 75


class AuditPolicyError(ValueError):
    pass


class AuditOperationalError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a complete Node dependency graph against the reviewed exception policy."
    )
    parser.add_argument("--project", required=True, choices=("desktop", "frontend"))
    return parser.parse_args()


def load_policy() -> dict[str, object]:
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditPolicyError(f"Could not load {POLICY_PATH}: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schemaVersion") != 1:
        raise AuditPolicyError("Dependency audit policy has an unsupported schema.")
    return policy


def project_policy(policy: dict[str, object], project: str) -> dict[str, object]:
    projects = policy.get("projects")
    selected = projects.get(project) if isinstance(projects, dict) else None
    if not isinstance(selected, dict):
        raise AuditPolicyError(f"Dependency audit policy is missing project {project!r}.")
    return selected


def reported_advisories(report: dict[str, object]) -> dict[str, set[str]]:
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise AuditPolicyError("npm audit did not return an auditReportVersion 2 vulnerability map.")

    advisories: dict[str, set[str]] = {}
    for vulnerability_name, vulnerability in vulnerabilities.items():
        if not isinstance(vulnerability, dict):
            raise AuditPolicyError(f"npm audit returned malformed data for {vulnerability_name}.")
        via = vulnerability.get("via")
        if not isinstance(via, list):
            raise AuditPolicyError(f"npm audit returned malformed advisory data for {vulnerability_name}.")
        for item in via:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith(GHSA_URL_PREFIX):
                raise AuditPolicyError(
                    f"npm audit returned an advisory without a GitHub advisory identifier for {vulnerability_name}."
                )
            advisory_id = url.removeprefix(GHSA_URL_PREFIX).strip()
            package = item.get("dependency") or item.get("name") or vulnerability_name
            if not advisory_id or not isinstance(package, str) or not package:
                raise AuditPolicyError(f"npm audit returned incomplete advisory data for {vulnerability_name}.")
            advisories.setdefault(advisory_id, set()).add(package)

    if vulnerabilities and not advisories:
        raise AuditPolicyError("npm audit reported vulnerabilities without a source advisory.")
    return advisories


def evaluate_audit_report(
    report: dict[str, object],
    selected_policy: dict[str, object],
    *,
    as_of: date,
) -> list[dict[str, str]]:
    advisories = reported_advisories(report)
    configured = selected_policy.get("allowedAdvisories")
    if not isinstance(configured, list):
        raise AuditPolicyError("Project audit policy must define allowedAdvisories as a list.")

    allowed: dict[str, dict[str, str]] = {}
    for raw_entry in configured:
        if not isinstance(raw_entry, dict):
            raise AuditPolicyError("Each allowed advisory must be an object.")
        entry = {
            key: value
            for key in ("id", "package", "expires", "reason")
            if isinstance((value := raw_entry.get(key)), str) and value.strip()
        }
        if set(entry) != {"id", "package", "expires", "reason"}:
            raise AuditPolicyError("Each allowed advisory requires id, package, expires, and reason.")
        if entry["id"] in allowed:
            raise AuditPolicyError(f"Duplicate allowed advisory: {entry['id']}")
        try:
            expires = date.fromisoformat(entry["expires"])
        except ValueError as exc:
            raise AuditPolicyError(f"Invalid expiry date for {entry['id']}: {entry['expires']}") from exc
        if as_of > expires:
            raise AuditPolicyError(
                f"Allowed advisory {entry['id']} expired on {entry['expires']}; review or remove it."
            )
        allowed[entry["id"]] = entry

    unknown = sorted(set(advisories) - set(allowed))
    if unknown:
        raise AuditPolicyError(f"Unapproved npm advisories: {', '.join(unknown)}")

    stale = sorted(set(allowed) - set(advisories))
    if stale:
        raise AuditPolicyError(
            "Stale npm audit exceptions must be removed: " + ", ".join(stale)
        )

    for advisory_id, entry in allowed.items():
        packages = advisories[advisory_id]
        if entry["package"] not in packages:
            raise AuditPolicyError(
                f"Allowed advisory {advisory_id} expected package {entry['package']}, "
                f"but npm reported {', '.join(sorted(packages))}."
            )
    return list(allowed.values())


def run_audit(project: str) -> dict[str, object]:
    npm = shutil.which("npm")
    if not npm:
        raise AuditPolicyError("Required executable not found on PATH: npm")
    completed = subprocess.run(  # nosec B603
        [npm, "--prefix", project, "audit", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AuditOperationalError(
            f"npm audit failed operationally with exit code {completed.returncode}: {detail}"
        )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AuditOperationalError("npm audit did not return valid JSON.") from exc
    if not isinstance(report, dict) or report.get("error"):
        raise AuditOperationalError(
            f"npm audit could not complete: {report.get('error') if isinstance(report, dict) else report}"
        )
    return report


def main() -> int:
    args = parse_args()
    try:
        policy = load_policy()
        report = run_audit(args.project)
        accepted = evaluate_audit_report(
            report,
            project_policy(policy, args.project),
            as_of=datetime.now(timezone.utc).date(),
        )
    except AuditOperationalError as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return RETRYABLE_AUDIT_EXIT
    except AuditPolicyError as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return 1

    if accepted:
        for entry in accepted:
            print(
                f"Accepted temporary npm advisory {entry['id']} for {entry['package']} "
                f"through {entry['expires']}: {entry['reason']}"
            )
    else:
        print(f"{args.project}: npm audit found no vulnerabilities.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
