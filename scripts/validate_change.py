#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
from pathlib import Path

from change_validation import (
    ValidationMappingError,
    fingerprint_paths,
    infer_validation_plan,
    normalize_paths,
    receipt_base,
    run_check,
    verify_receipt,
    write_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECEIPT = ROOT / ".breaktwenty-validation" / "task-receipt.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the diff-aware BreakTwenty task validation gate.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--paths", nargs="+", help="Exact repository paths changed by this task.")
    scope.add_argument("--base", help="Validate files changed from this Git revision through the working tree.")
    parser.add_argument("--plan", action="store_true", help="Print the inferred plan without executing checks.")
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--verify-receipt", type=Path)
    return parser.parse_args()


def git_lines(command: list[str]) -> list[str]:
    completed = subprocess.run(  # nosec B603
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line]


def discover_paths(base: str | None) -> tuple[str, ...]:
    if base:
        committed = git_lines(["git", "diff", "--name-only", f"{base}...HEAD"])
    else:
        committed = []
    working = git_lines(["git", "diff", "--name-only", "HEAD"])
    untracked = git_lines(["git", "ls-files", "--others", "--exclude-standard"])
    return normalize_paths(ROOT, [*committed, *working, *untracked])


def plan_payload(plan) -> dict[str, object]:
    return {
        "changed_paths": list(plan.paths),
        "boundaries": list(plan.boundaries),
        "checks": [
            {
                "id": check.check_id,
                "title": check.title,
                "command": list(check.command),
                "cwd": check.cwd,
                "evidence": list(check.evidence),
            }
            for check in plan.checks
        ],
        "skipped_boundaries": list(plan.skipped_boundaries),
    }


def main() -> int:
    args = parse_args()
    if args.verify_receipt:
        receipt = json.loads(args.verify_receipt.read_text(encoding="utf-8"))
        verify_receipt(ROOT, receipt)
        print(f"Validation receipt verified: {args.verify_receipt}")
        return 0

    paths = normalize_paths(ROOT, args.paths) if args.paths else discover_paths(args.base)
    plan = infer_validation_plan(ROOT, paths)
    if args.plan:
        print(json.dumps(plan_payload(plan), indent=2, sort_keys=True))
        return 0

    initial_fingerprint = fingerprint_paths(ROOT, plan.paths)
    receipt = receipt_base(
        ROOT,
        mode="task",
        paths=plan.paths,
        boundaries=plan.boundaries,
        skipped_boundaries=plan.skipped_boundaries,
        fingerprint=initial_fingerprint,
    )
    write_receipt(args.receipt, receipt)
    for check in plan.checks:
        print(f"\n==> {check.title}", flush=True)
        record = run_check(ROOT, check)
        receipt["checks"].append(record)
        if record["status"] != "passed":
            receipt["status"] = "failed"
            write_receipt(args.receipt, receipt)
            print(f"Validation failed; receipt: {args.receipt}", file=sys.stderr)
            return 1
    if fingerprint_paths(ROOT, plan.paths)["value"] != initial_fingerprint["value"]:
        receipt["status"] = "failed"
        receipt["error"] = "Changed task files were modified while validation was running."
        write_receipt(args.receipt, receipt)
        print(receipt["error"], file=sys.stderr)
        return 1
    receipt["status"] = "passed"
    write_receipt(args.receipt, receipt)
    verify_receipt(ROOT, receipt)
    print(f"\nValidation passed; receipt: {args.receipt}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationMappingError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
