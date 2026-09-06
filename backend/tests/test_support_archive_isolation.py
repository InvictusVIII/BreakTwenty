from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services import support_auto_archive


class SupportArchiveIsolationTests(unittest.TestCase):
    def _make_run(
        self,
        runs_dir: Path,
        run_id: str,
        *,
        user_id: int,
        sync_id: str,
        payloads: tuple[bytes, ...] = (),
        provider: str = "rbc",
    ) -> Path:
        run_dir = runs_dir / provider / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "trigger.json").write_text(
            json.dumps({"user_id": user_id, "sync_id": sync_id}),
            encoding="utf-8",
        )
        (run_dir / support_auto_archive.RUN_COMPLETION_FILENAME).write_text(
            json.dumps({"status": "complete", "evidence_collection_finished": True}),
            encoding="utf-8",
        )
        for index, payload in enumerate(payloads):
            (run_dir / f"payload-{index}.log").write_bytes(payload)
        return run_dir

    def test_windows_extended_paths_cover_drive_and_unc_roots(self) -> None:
        self.assertEqual(
            r"\\?\C:\Users\Alice\AppData\Roaming\BreakTwenty\data\logs",
            support_auto_archive._windows_extended_path_text(
                r"C:\Users\Alice\AppData\Roaming\BreakTwenty\data\logs"
            ),
        )
        self.assertEqual(
            r"\\?\UNC\server\share\BreakTwenty\data\logs",
            support_auto_archive._windows_extended_path_text(
                r"\\server\share\BreakTwenty\data\logs"
            ),
        )
        already_extended = r"\\?\C:\BreakTwenty\data\logs"
        self.assertEqual(
            already_extended,
            support_auto_archive._windows_extended_path_text(already_extended),
        )

    def test_attempt_collector_keeps_evidence_beyond_legacy_windows_path_length(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging_dir = root / "s"
            staging_dir.mkdir()
            evidence_name = f"visible-auth-{'x' * 175}.har"
            evidence = staging_dir / evidence_name
            evidence.write_bytes(b"complete HAR evidence")

            run_dir = (
                root
                / "runs"
                / "amex"
                / f"2026-09-02_23-19-02_EDT_amex-sync_result_ok_{'y' * 80}"
            )
            run_dir.parent.mkdir(parents=True)
            os.replace(staging_dir, run_dir)
            retained_evidence = run_dir / evidence_name
            self.assertGreater(len(str(retained_evidence)), 260)

            collected = support_auto_archive._collect_attempt_archive_files(
                [(run_dir, "amex/attempt")],
                attempt_key="sync:amex-long-path",
                tree_scan_limit=support_auto_archive.SUPPORT_DIRECTORY_SCAN_LIMIT,
            )

            self.assertEqual(
                [
                    (
                        support_auto_archive._filesystem_access_path(retained_evidence),
                        f"amex/attempt/{evidence_name}",
                    )
                ],
                list(collected.entries),
            )
            zip_path = root / "long-path-attempt.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                for child, archive_name in collected.entries:
                    archive.write(child, arcname=archive_name)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    b"complete HAR evidence",
                    archive.read(f"amex/attempt/{evidence_name}"),
                )
            summary = support_auto_archive._summarize_run(run_dir.parent, run_dir)
            self.assertEqual(
                [evidence_name],
                [item["path"] for item in summary["files"]],
            )

    def test_completed_siblings_stay_hidden_while_same_attempt_archive_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            self._make_run(
                runs_dir,
                "accounts-phase",
                user_id=1,
                sync_id="shared-attempt",
            )
            pending_key = support_auto_archive._archive_identity_key(
                user_id=1,
                sync_id="shared-attempt",
                attempt_id=None,
            )
            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                support_auto_archive._mark_archive_pending(pending_key)
                try:
                    self.assertEqual([], support_auto_archive.list_runs(provider="rbc", user_id=1))
                    self.assertIsNone(
                        support_auto_archive.get_run("rbc", "accounts-phase", user_id=1)
                    )
                finally:
                    support_auto_archive._clear_archive_pending(pending_key)

                self.assertEqual(
                    ["accounts-phase"],
                    [run["run_id"] for run in support_auto_archive.list_runs(provider="rbc", user_id=1)],
                )

    def test_list_get_package_and_prune_are_user_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            user_one_old = self._make_run(
                runs_dir,
                "user-one-old",
                user_id=1,
                sync_id="user-one-old-sync",
                payloads=(b"owner-one-old",),
            )
            user_one_new = self._make_run(
                runs_dir,
                "user-one-new",
                user_id=1,
                sync_id="user-one-new-sync",
                payloads=(b"owner-one-new",),
            )
            user_ten_old = self._make_run(
                runs_dir,
                "user-ten-old",
                user_id=10,
                sync_id="shared-sync",
                payloads=(b"owner-ten-old",),
            )
            user_ten_new = self._make_run(
                runs_dir,
                "user-ten-new",
                user_id=10,
                sync_id="shared-sync",
                payloads=(b"owner-ten-new",),
            )
            now = time.time()
            os.utime(user_one_old, (now - 20, now - 20))
            os.utime(user_one_new, (now - 10, now - 10))
            os.utime(user_ten_old, (now - 20, now - 20))
            os.utime(user_ten_new, (now - 10, now - 10))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                listed = support_auto_archive.list_runs(provider="rbc", user_id=1)
                self.assertEqual(
                    {"user-one-old", "user-one-new"},
                    {run["run_id"] for run in listed},
                )
                self.assertIsNone(
                    support_auto_archive.get_run("rbc", "user-ten-new", user_id=1)
                )
                self.assertIsNone(
                    support_auto_archive.package_run_zip(
                        "rbc",
                        "user-ten-new",
                        user_id=1,
                    )
                )

                support_archive = support_auto_archive.package_run_zip(
                    "rbc",
                    "user-one-new",
                    user_id=1,
                )
                self.assertIsNotNone(support_archive)
                self.assertEqual(
                    support_archive.filename,
                    "BreakTwenty_rbc_attempt_diagnostics_user-one-new.zip",
                )
                self.assertNotIn("user-1", support_archive.path.name)
                metadata_path = support_archive.path.with_suffix(".json")
                if os.name == "posix":
                    self.assertEqual(support_archive.path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)
                self.assertIsNone(
                    support_auto_archive.resolve_support_archive(
                        support_archive.archive_id,
                        user_id=10,
                    )
                )
                resolved = support_auto_archive.resolve_support_archive(
                    support_archive.archive_id,
                    user_id=1,
                )
                self.assertIsNotNone(resolved)
                with zipfile.ZipFile(support_archive.path) as archive:
                    archived = b"\n".join(archive.read(name) for name in archive.namelist())
                self.assertIn(b"owner-one-new", archived)
                self.assertNotIn(b"owner-ten", archived)

                with patch.object(support_auto_archive, "ATTEMPTS_PER_PROVIDER_LIMIT", 1):
                    support_auto_archive._prune_runs(runs_dir / "rbc", user_id=1)

            self.assertFalse(user_one_old.exists())
            self.assertTrue(user_one_new.exists())
            self.assertTrue(user_ten_old.exists())
            self.assertTrue(user_ten_new.exists())

    def test_prune_keeps_three_attempt_units_and_removes_fourth_oldest_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            attempt_runs = []
            phase_counts = (3, 1, 3, 1)
            now = time.time()
            for attempt_index, phase_count in enumerate(phase_counts):
                runs = []
                for phase_index in range(phase_count):
                    run_dir = self._make_run(
                        runs_dir,
                        f"attempt-{attempt_index}-phase-{phase_index}",
                        user_id=1,
                        sync_id=f"sync-{attempt_index}",
                        provider="tangerine",
                    )
                    modified_at = now - ((len(phase_counts) - attempt_index) * 60) + phase_index
                    os.utime(run_dir, (modified_at, modified_at))
                    runs.append(run_dir)
                attempt_runs.append(runs)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                support_auto_archive._prune_runs(runs_dir / "tangerine", user_id=1)

            self.assertTrue(all(not run.exists() for run in attempt_runs[0]))
            self.assertTrue(all(run.exists() for runs in attempt_runs[1:] for run in runs))

    def test_prune_attempt_retention_does_not_depend_on_run_directory_scan_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            now = time.time()
            attempt_runs = []
            for attempt_index in range(4):
                runs = []
                for phase_index in range(3):
                    run_dir = self._make_run(
                        runs_dir,
                        f"attempt-{attempt_index}-phase-{phase_index}",
                        user_id=1,
                        sync_id=f"sync-{attempt_index}",
                        provider="tangerine",
                    )
                    modified_at = now - ((4 - attempt_index) * 60) + phase_index
                    os.utime(run_dir, (modified_at, modified_at))
                    runs.append(run_dir)
                attempt_runs.append(runs)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "SUPPORT_RUN_SCAN_LIMIT", 2),
            ):
                support_auto_archive._prune_runs(runs_dir / "tangerine", user_id=1)

            self.assertTrue(all(not run.exists() for run in attempt_runs[0]))
            self.assertTrue(all(run.exists() for runs in attempt_runs[1:] for run in runs))

    def test_prune_keeps_related_route_provider_phases_in_same_attempt_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            now = time.time()
            oldest_account = self._make_run(
                runs_dir,
                "oldest-account",
                user_id=1,
                sync_id="oldest-sync",
                provider="ibkr",
            )
            oldest_transactions = self._make_run(
                runs_dir,
                "oldest-transactions",
                user_id=1,
                sync_id="oldest-sync",
                provider="ibkr_flex",
            )
            os.utime(oldest_account, (now - 240, now - 240))
            os.utime(oldest_transactions, (now - 239, now - 239))
            for index in range(3):
                run_dir = self._make_run(
                    runs_dir,
                    f"newer-{index}",
                    user_id=1,
                    sync_id=f"newer-sync-{index}",
                    provider="ibkr" if index % 2 == 0 else "ibkr_flex",
                )
                modified_at = now - ((3 - index) * 30)
                os.utime(run_dir, (modified_at, modified_at))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(
                    support_auto_archive,
                    "_provider_family",
                    return_value=("ibkr", "ibkr_flex"),
                ),
            ):
                support_auto_archive._prune_runs(runs_dir / "ibkr", user_id=1)

            self.assertFalse(oldest_account.exists())
            self.assertFalse(oldest_transactions.exists())

    def test_all_export_uses_local_identified_name_and_deterministic_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            rbc_run = self._make_run(
                runs_dir,
                "2026-08-20_08-24-00_EDT_rbc-sync_accounts",
                user_id=1,
                sync_id="rbc-sync",
                payloads=(b"rbc",),
                provider="rbc",
            )
            bmo_run = self._make_run(
                runs_dir,
                "2026-08-20_08-23-00_EDT_bmo-sync_accounts",
                user_id=1,
                sync_id="bmo-sync",
                payloads=(b"bmo",),
                provider="bmo",
            )
            export_time = datetime(2026, 8, 20, 12, 25, 25, tzinfo=timezone.utc)
            modified_at = export_time.timestamp() - 60
            os.utime(rbc_run, (modified_at, modified_at))
            os.utime(bmo_run, (modified_at, modified_at))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=export_time),
            ):
                support_archive, included, export_summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=60,
                        user_timezone=ZoneInfo("America/Toronto"),
                    )
                )

            self.assertIsNotNone(support_archive)
            self.assertEqual(
                support_archive.filename,
                "BreakTwenty_all_providers_recent_diagnostics_2026-08-20_08-25-25_EDT.zip",
            )
            self.assertEqual({row["provider"] for row in included}, {"bmo", "rbc"})
            self.assertFalse(export_summary["truncated"])
            with zipfile.ZipFile(support_archive.path) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertEqual("export_manifest.json", names[0])
            self.assertEqual(names[1:], sorted(names[1:]))
            self.assertTrue(
                all(
                    name == "export_manifest.json" or name.startswith(("bmo/", "rbc/"))
                    for name in names
                )
            )
            self.assertEqual("all_providers_recent", manifest["export_scope"])
            self.assertIn("provider-wide", manifest["scope_label"].lower())

    def test_all_export_includes_more_than_legacy_256_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            export_time = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
            runs = []
            for provider_index in range(20):
                provider = f"provider-{provider_index:02d}"
                run_dir = self._make_run(
                    runs_dir,
                    f"run-{provider_index:02d}",
                    user_id=1,
                    sync_id=f"sync-{provider_index:02d}",
                    payloads=tuple(
                        f"{provider}-payload-{payload_index}".encode()
                        for payload_index in range(14)
                    ),
                    provider=provider,
                )
                runs.append(run_dir)
            for run_dir in runs:
                modified_at = export_time.timestamp() - 60
                os.utime(run_dir, (modified_at, modified_at))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=export_time),
            ):
                support_archive, included, export_summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=60,
                    )
                )

            self.assertIsNotNone(support_archive)
            self.assertEqual(20, len(included))
            self.assertFalse(export_summary["truncated"])
            self.assertEqual(300, export_summary["included_file_count"])
            with zipfile.ZipFile(support_archive.path) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertEqual(301, len(names))
            self.assertTrue(manifest["complete"])
            self.assertEqual(300, manifest["included_file_count"])
            self.assertEqual([], manifest["omitted_attempts"])

    def test_all_export_never_partially_includes_same_sync_sibling_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            account_run = self._make_run(
                runs_dir,
                "same-sync-accounts",
                user_id=1,
                sync_id="same-sync",
                payloads=(b"account phase",),
                provider="bmo",
            )
            transaction_run = self._make_run(
                runs_dir,
                "same-sync-transactions",
                user_id=1,
                sync_id="same-sync",
                payloads=(b"transaction phase",),
                provider="bmo",
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT", 4),
            ):
                complete_archive, complete_runs, complete_summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=60,
                    )
                )
            self.assertEqual(2, len(complete_runs))
            self.assertFalse(complete_summary["truncated"])
            with zipfile.ZipFile(complete_archive.path) as archive:
                complete_names = archive.namelist()
            self.assertTrue(any(account_run.name in name for name in complete_names))
            self.assertTrue(any(transaction_run.name in name for name in complete_names))
            registered_before = set((log_dir / "exports" / "archives").glob("*.zip"))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT", 3),
            ):
                with self.assertRaises(support_auto_archive.SupportArchiveLimitError) as caught:
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=60,
                    )
            self.assertEqual("newest_attempt_file_count_limit", caught.exception.reason)
            self.assertIn("No partial attempt was exported", caught.exception.message)
            self.assertEqual(
                registered_before,
                set((log_dir / "exports" / "archives").glob("*.zip")),
            )

    def test_all_export_keeps_newest_whole_attempts_and_reports_older_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            export_time = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
            oldest = self._make_run(
                runs_dir,
                "oldest-attempt",
                user_id=1,
                sync_id="oldest-sync",
                payloads=(b"oldest evidence",),
                provider="bmo",
            )
            newest = self._make_run(
                runs_dir,
                "newest-attempt",
                user_id=1,
                sync_id="newest-sync",
                payloads=(b"newest evidence",),
                provider="rbc",
            )
            os.utime(oldest, (export_time.timestamp() - 120,) * 2)
            os.utime(newest, (export_time.timestamp() - 60,) * 2)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=export_time),
                patch.object(support_auto_archive, "ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT", 2),
            ):
                support_archive, included, summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=15,
                    )
                )

            self.assertEqual(["newest-sync"], [row["sync_id"] for row in included])
            self.assertTrue(summary["truncated"])
            self.assertEqual(1, summary["included_attempt_count"])
            self.assertEqual(1, summary["omitted_attempt_count"])
            self.assertEqual(["bmo"], summary["providers_with_omitted_attempts"])
            with zipfile.ZipFile(support_archive.path) as archive:
                content = b"\n".join(archive.read(name) for name in archive.namelist())
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertIn(b"newest evidence", content)
            self.assertNotIn(b"oldest evidence", content)
            self.assertFalse(manifest["complete"])
            self.assertEqual("oldest-sync", manifest["omitted_attempts"][0]["sync_id"])
            self.assertEqual(
                "export_file_count_limit",
                manifest["omitted_attempts"][0]["reason"],
            )

    def test_all_export_aggregate_cap_keeps_newest_whole_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            export_time = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
            oldest = self._make_run(
                runs_dir,
                "oldest-attempt",
                user_id=1,
                sync_id="oldest-sync",
                payloads=(b"oldest evidence",),
                provider="bmo",
            )
            newest = self._make_run(
                runs_dir,
                "newest-attempt",
                user_id=1,
                sync_id="newest-sync",
                payloads=(b"newest evidence",),
                provider="rbc",
            )
            os.utime(oldest, (export_time.timestamp() - 120,) * 2)
            os.utime(newest, (export_time.timestamp() - 60,) * 2)
            newest_bytes = sum(
                path.stat().st_size
                for path in newest.rglob("*")
                if path.is_file() and path.name != support_auto_archive.RUN_COMPLETION_FILENAME
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=export_time),
                patch.object(
                    support_auto_archive,
                    "SUPPORT_ZIP_AGGREGATE_MAX_BYTES",
                    newest_bytes,
                ),
            ):
                support_archive, included, summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=15,
                    )
                )

            self.assertEqual(["newest-sync"], [row["sync_id"] for row in included])
            self.assertTrue(summary["truncated"])
            with zipfile.ZipFile(support_archive.path) as archive:
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertEqual(
                "export_aggregate_bytes_limit",
                manifest["omitted_attempts"][0]["reason"],
            )

    def test_all_export_deduplicates_identical_phase_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            account_run = self._make_run(
                runs_dir,
                "same-sync-accounts",
                user_id=1,
                sync_id="failed-sync",
                provider="eqbank",
            )
            transaction_run = self._make_run(
                runs_dir,
                "same-sync-transactions",
                user_id=1,
                sync_id="failed-sync",
                provider="eqbank",
            )
            har_bytes = b'{"log":{"entries":[{"response":{"status":500}}]}}'
            for run_dir in (account_run, transaction_run):
                (run_dir / "trigger.json").write_text(
                    json.dumps(
                        {
                            "user_id": 1,
                            "sync_id": "failed-sync",
                            "trigger": "sync_result_error",
                            "error": "provider rejected request",
                        }
                    ),
                    encoding="utf-8",
                )
                diagnostics_dir = run_dir / "diagnostics"
                diagnostics_dir.mkdir()
                (diagnostics_dir / "failed-sync.har").write_bytes(har_bytes)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                support_archive, included, export_summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=60,
                    )
                )

            self.assertEqual(2, len(included))
            self.assertEqual({"sync_result_error"}, {row["trigger"] for row in included})
            self.assertEqual(1, export_summary["deduplicated_file_count"])
            with zipfile.ZipFile(support_archive.path) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertEqual(1, len([name for name in names if name.endswith("failed-sync.har")]))
            self.assertEqual(1, manifest["deduplicated_file_count"])
            self.assertEqual(len(har_bytes), manifest["deduplicated_bytes"])
            self.assertEqual(
                manifest["deduplicated_files"][0]["canonical_path"],
                next(name for name in names if name.endswith("failed-sync.har")),
            )

    def test_all_export_includes_older_phase_when_attempt_finishes_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            account_run = self._make_run(
                runs_dir,
                "boundary-accounts",
                user_id=1,
                sync_id="boundary-sync",
                payloads=(b"account failure context",),
                provider="eqbank",
            )
            transaction_run = self._make_run(
                runs_dir,
                "boundary-transactions",
                user_id=1,
                sync_id="boundary-sync",
                payloads=(b"terminal failure context",),
                provider="eqbank",
            )
            export_time = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
            os.utime(account_run, (export_time.timestamp() - 16 * 60,) * 2)
            os.utime(transaction_run, (export_time.timestamp() - 60,) * 2)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=export_time),
            ):
                support_archive, included, _summary = (
                    support_auto_archive._package_all_recent_runs_sync(
                        user_id=1,
                        since_minutes=15,
                    )
                )

            self.assertEqual(
                {account_run.name, transaction_run.name},
                {row["run_id"] for row in included},
            )
            with zipfile.ZipFile(support_archive.path) as archive:
                content = b"\n".join(archive.read(name) for name in archive.namelist())
            self.assertIn(b"account failure context", content)
            self.assertIn(b"terminal failure context", content)

    def test_same_sync_account_and_transaction_runs_export_together_without_other_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            account_run = self._make_run(
                runs_dir,
                "2026-08-26_15-00-00_EDT_bmo-attempt-a_sync_result_ok_accounts",
                user_id=1,
                sync_id="bmo-attempt-a",
                payloads=(b"account phase A",),
                provider="bmo",
            )
            transaction_run = self._make_run(
                runs_dir,
                "2026-08-26_15-01-00_EDT_bmo-attempt-a_sync_result_ok_transactions",
                user_id=1,
                sync_id="bmo-attempt-a",
                payloads=(b"transaction phase A",),
                provider="bmo",
            )
            self._make_run(
                runs_dir,
                "2026-08-26_14-00-00_EDT_bmo-attempt-b_sync_result_error_transactions",
                user_id=1,
                sync_id="bmo-attempt-b",
                payloads=(b"older attempt B",),
                provider="bmo",
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                archive = support_auto_archive.package_run_zip(
                    "bmo",
                    account_run.name,
                    user_id=1,
                )

            self.assertIsNotNone(archive)
            with zipfile.ZipFile(archive.path) as bundle:
                content = b"\n".join(bundle.read(name) for name in bundle.namelist())
                names = bundle.namelist()
                manifest = json.loads(bundle.read("export_manifest.json"))

        self.assertTrue(any(account_run.name in name for name in names))
        self.assertTrue(any(transaction_run.name in name for name in names))
        self.assertIn(b"account phase A", content)
        self.assertIn(b"transaction phase A", content)
        self.assertNotIn(b"older attempt B", content)
        self.assertTrue(manifest["complete"])
        self.assertFalse(manifest["truncated"])
        self.assertEqual("bmo-attempt-a", manifest["sync_id"])
        self.assertEqual({account_run.name, transaction_run.name}, set(manifest["run_ids"]))

    def test_same_sync_related_route_provider_runs_export_as_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            account_run = self._make_run(
                runs_dir,
                "2026-08-26_15-00-00_EDT_ibkr-attempt-a_sync_result_ok_accounts",
                user_id=1,
                sync_id="ibkr-attempt-a",
                payloads=(b"ibkr account phase",),
                provider="ibkr",
            )
            transaction_run = self._make_run(
                runs_dir,
                "2026-08-26_15-01-00_EDT_ibkr-attempt-a_sync_result_ok_transactions",
                user_id=1,
                sync_id="ibkr-attempt-a",
                payloads=(b"ibkr flex transaction phase",),
                provider="ibkr_flex",
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(
                    support_auto_archive,
                    "_provider_family",
                    return_value=("ibkr", "ibkr_flex"),
                ),
            ):
                archive = support_auto_archive.package_run_zip(
                    "ibkr",
                    account_run.name,
                    user_id=1,
                )

            self.assertIsNotNone(archive)
            with zipfile.ZipFile(archive.path) as bundle:
                names = bundle.namelist()

        self.assertTrue(any(name.startswith(f"ibkr/{account_run.name}/") for name in names))
        self.assertTrue(
            any(name.startswith(f"ibkr_flex/{transaction_run.name}/") for name in names)
        )

    def test_bmo_auth_required_run_lists_and_packages_with_string_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            run_dir = self._make_run(
                runs_dir,
                "2026-08-22_19-20-37_EDT_bmo-42926185d4_sync_result_auth_required_accounts",
                user_id=1,
                sync_id="bmo-42926185d4",
                payloads=(b"bmo auth required",),
                provider="bmo",
            )
            trigger_path = run_dir / "trigger.json"
            trigger_path.write_text(
                json.dumps(
                    {
                        "user_id": "1",
                        "sync_id": "bmo-42926185d4",
                        "provider": "bmo",
                        "trigger": "sync_result_auth_required",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                listed = support_auto_archive.list_runs(provider="bmo", user_id=1)
                self.assertEqual([run_dir.name], [run["run_id"] for run in listed])
                archive = support_auto_archive.package_run_zip("bmo", run_dir.name, user_id=1)

            self.assertIsNotNone(archive)

    def test_list_runs_keeps_attempt_within_seven_day_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            fresh_time = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)
            stale_run = self._make_run(
                runs_dir,
                "2026-08-22_16-12-26_EDT_ibkr-72f387858b_sync_result_ok_full",
                user_id=1,
                sync_id="ibkr-72f387858b",
                payloads=(b"stale",),
                provider="ibkr",
            )
            fresh_run = self._make_run(
                runs_dir,
                "2026-08-26_18-29-30_EDT_ibkr_flex-88c892d464_sync_result_ok_transactions",
                user_id=1,
                sync_id="ibkr_flex-88c892d464",
                payloads=(b"fresh",),
                provider="ibkr_flex",
            )
            stale_timestamp = fresh_time.timestamp() - 72 * 60 * 60
            os.utime(stale_run, (stale_timestamp, stale_timestamp))
            os.utime(fresh_run, (fresh_time.timestamp() - 60, fresh_time.timestamp() - 60))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=fresh_time),
                patch.object(
                    support_auto_archive,
                    "_provider_family",
                    return_value=("ibkr", "ibkr_flex"),
                ),
            ):
                listed = support_auto_archive.list_runs(provider="ibkr", user_id=1)

            self.assertEqual(
                [fresh_run.name, stale_run.name],
                [run["run_id"] for run in listed],
            )
            self.assertTrue(stale_run.exists())
            self.assertTrue(fresh_run.exists())

    def test_list_runs_expires_entire_attempt_after_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            now = datetime(2026, 8, 26, 22, 0, tzinfo=timezone.utc)
            expired_phases = [
                self._make_run(
                    runs_dir,
                    f"expired-phase-{phase}",
                    user_id=1,
                    sync_id="expired-sync",
                    provider="rbc",
                )
                for phase in ("accounts", "transactions", "finalization")
            ]
            expired_timestamp = now.timestamp() - 8 * 24 * 60 * 60
            for run_dir in expired_phases:
                os.utime(run_dir, (expired_timestamp, expired_timestamp))
            fresh_run = self._make_run(
                runs_dir,
                "fresh-attempt",
                user_id=1,
                sync_id="fresh-sync",
                provider="rbc",
            )
            os.utime(fresh_run, (now.timestamp() - 60, now.timestamp() - 60))

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "_utc_now", return_value=now),
            ):
                listed = support_auto_archive.list_runs(provider="rbc", user_id=1)

            self.assertEqual([fresh_run.name], [run["run_id"] for run in listed])
            self.assertTrue(all(not run.exists() for run in expired_phases))
            self.assertTrue(fresh_run.exists())

    def test_packaged_zip_fails_instead_of_silently_truncating_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            self._make_run(
                runs_dir,
                "bounded-run",
                user_id=7,
                sync_id="bounded-sync",
                payloads=(b"abc", b"def", b"ghi"),
            )
            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "SUPPORT_ZIP_FILE_COUNT_LIMIT", 2),
            ):
                with self.assertRaises(support_auto_archive.SupportArchiveLimitError) as caught:
                    support_auto_archive.package_run_zip(
                        "rbc",
                        "bounded-run",
                        user_id=7,
                    )

            self.assertEqual("attempt_file_count_limit", caught.exception.reason)
            self.assertIn("No partial ZIP was created", caught.exception.message)

    def test_packaged_zip_fails_when_full_attempt_exceeds_aggregate_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            self._make_run(
                runs_dir,
                "bounded-run",
                user_id=7,
                sync_id="bounded-sync",
                payloads=(b"abc",),
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "SUPPORT_ZIP_AGGREGATE_MAX_BYTES", 1),
            ):
                with self.assertRaises(support_auto_archive.SupportArchiveLimitError) as caught:
                    support_auto_archive.package_run_zip(
                        "rbc",
                        "bounded-run",
                        user_id=7,
                    )

            self.assertEqual("attempt_aggregate_bytes_limit", caught.exception.reason)
            self.assertIn("No partial ZIP was created", caught.exception.message)

    def test_packaged_zip_fails_on_symlinks_outside_run_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            run_dir = self._make_run(
                runs_dir,
                "symlink-run",
                user_id=7,
                sync_id="symlink-sync",
                payloads=(b"safe-content",),
            )
            outside = log_dir / "outside-secret.txt"
            outside.write_bytes(b"must-not-be-archived")
            (run_dir / "escaped.log").symlink_to(outside)

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
            ):
                with self.assertRaises(support_auto_archive.SupportArchiveLimitError) as caught:
                    support_auto_archive.package_run_zip(
                        "rbc",
                        "symlink-run",
                        user_id=7,
                    )

            self.assertEqual("unsafe_evidence_path", caught.exception.reason)
            self.assertIn("No partial ZIP was created", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
