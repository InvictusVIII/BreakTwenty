from __future__ import annotations

import contextlib
import hashlib
import io
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.quiet_validation import run_quiet_command
from scripts import run_release_health


class QuietValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logs = self.root / ".breaktwenty-validation" / "logs"

    def tearDown(self) -> None:
        run_release_health.ACTIVE_RELEASE_RECEIPT = None
        self.temporary.cleanup()

    def capture_run(self, source: str, **kwargs):
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            result = run_quiet_command(
                [sys.executable, "-c", source],
                cwd=self.root,
                label=kwargs.pop("label", "Focused validation"),
                log_directory=self.logs,
                receipt_root=self.root,
                **kwargs,
            )
        return result, console.getvalue()

    def test_success_records_output_evidence_without_printing_or_retaining_it(self) -> None:
        payload = "successful but deliberately noisy output"
        result, console = self.capture_run(f"print({payload!r})")

        self.assertTrue(result.passed)
        self.assertEqual(result.output_sha256, hashlib.sha256(f"{payload}\n".encode()).hexdigest())
        self.assertEqual(result.output_bytes, len(payload) + 1)
        self.assertIsNone(result.retained_log)
        self.assertNotIn(payload, console)
        self.assertIn("captured and discarded", console)
        self.assertEqual(list(self.logs.glob("*.log")), [])

    def test_failure_prints_only_a_bounded_tail_and_retains_the_complete_log(self) -> None:
        source = "print('first-line'); print('middle-line'); print('last-line'); raise SystemExit(7)"
        result, console = self.capture_run(source, failure_tail_lines=2)

        self.assertFalse(result.passed)
        self.assertEqual(result.exit_code, 7)
        self.assertIsNotNone(result.retained_log)
        self.assertNotIn("first-line", console)
        self.assertIn("last-line", console)
        retained = self.root / str(result.retained_log)
        self.assertEqual(stat.S_IMODE(retained.stat().st_mode), 0o600)
        complete = retained.read_text(encoding="utf-8")
        self.assertIn("first-line", complete)
        self.assertIn("middle-line", complete)
        self.assertIn("last-line", complete)
        self.assertEqual(result.receipt_output()["log_path"], result.retained_log)

    def test_long_running_command_emits_elapsed_heartbeat_without_streaming_output(self) -> None:
        result, console = self.capture_run(
            "import time; print('hidden-progress'); time.sleep(0.08)",
            heartbeat_seconds=0.02,
        )

        self.assertTrue(result.passed)
        self.assertIn("still running", console)
        self.assertNotIn("hidden-progress", console)

    def test_complete_release_attempt_receipt_uses_the_shared_output_metadata(self) -> None:
        run_release_health.ACTIVE_RELEASE_RECEIPT = {"checks": []}
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            run_release_health.run_step(
                "Quiet release probe",
                [sys.executable, "-c", "print('release-noise')"],
                cwd=self.root,
            )

        record = run_release_health.ACTIVE_RELEASE_RECEIPT["checks"][0]
        attempt = record["attempts"][0]
        self.assertEqual(record["status"], "passed")
        self.assertEqual(attempt["output"]["status"], "passed")
        self.assertFalse(attempt["output"]["retained"])
        self.assertNotIn("release-noise", console.getvalue())


if __name__ == "__main__":
    unittest.main()
