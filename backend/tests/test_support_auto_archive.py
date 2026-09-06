from __future__ import annotations

import asyncio
import builtins
import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Account, Institution, TransactionImportJob, TransactionImportWindow, User
from app.services import support_auto_archive, support_diagnostics
from app.services.log_buffer import ProviderLogRingBuffer


class SupportAutoArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_state_excludes_older_provider_jobs_and_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(f"sqlite+aiosqlite:///{temp_dir}/support-state.db")
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            now = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
            async with sessionmaker() as db:
                db.add(User(id=1))
                db.add(
                    Institution(
                        id=1,
                        user_id=1,
                        name="BMO",
                        type="scraper",
                        provider="bmo",
                        sync_status="ok",
                    )
                )
                db.add(
                    Account(
                        id=1,
                        user_id=1,
                        institution_id=1,
                        external_id="bmo-account",
                        name="BMO Account",
                        currency="CAD",
                    )
                )
                db.add_all(
                    [
                        TransactionImportJob(
                            user_id=1,
                            institution_id=1,
                            provider="bmo",
                            job_id="bmo-current-job",
                            status="running",
                            sync_id="bmo-transaction-phase",
                            source_sync_id="bmo-attempt-a",
                            created_at=now,
                            updated_at=now,
                        ),
                        TransactionImportJob(
                            user_id=1,
                            institution_id=1,
                            provider="bmo",
                            job_id="bmo-older-failed-job",
                            status="failed",
                            sync_id="bmo-attempt-b",
                            source_sync_id="bmo-attempt-b",
                            last_error="older failure",
                            created_at=now - timedelta(hours=1),
                            updated_at=now - timedelta(hours=1),
                        ),
                        TransactionImportWindow(
                            user_id=1,
                            institution_id=1,
                            account_id=1,
                            provider="bmo",
                            account_external_id="bmo-account",
                            mode="incremental",
                            window_start_date=date(2026, 8, 1),
                            window_end_date=date(2026, 8, 15),
                            status="complete",
                            sync_id="bmo-attempt-a",
                            updated_at=now,
                        ),
                        TransactionImportWindow(
                            user_id=1,
                            institution_id=1,
                            account_id=1,
                            provider="bmo",
                            account_external_id="bmo-account",
                            mode="incremental",
                            window_start_date=date(2026, 7, 1),
                            window_end_date=date(2026, 7, 15),
                            status="failed",
                            sync_id="bmo-attempt-b",
                            last_error="older failure",
                            updated_at=now - timedelta(hours=1),
                        ),
                    ]
                )
                await db.commit()

            try:
                def active_attempt(_user_id, _provider, *, institution_id=None):
                    return {
                        "sync_id": "bmo-attempt-a" if institution_id == 1 else "bmo-attempt-b",
                        "status": "started",
                    }

                with (
                    patch.object(support_diagnostics, "async_session", sessionmaker),
                    patch.object(
                        support_diagnostics,
                        "get_provider_sync_attempt",
                        side_effect=active_attempt,
                    ),
                    patch.object(
                        support_diagnostics,
                        "sync_lock_state_snapshot",
                        return_value={
                            "1:bmo:1": {
                                "user_id": "1",
                                "provider": "bmo",
                                "institution_id": "1",
                            },
                            "1:bmo:2": {
                                "user_id": "1",
                                "provider": "bmo",
                                "institution_id": "2",
                            },
                        },
                    ),
                    patch.object(
                        support_diagnostics,
                        "transaction_import_task_snapshot",
                        return_value={
                            "bmo-current-job": {"done": False},
                            "bmo-older-failed-job": {"done": True},
                        },
                    ),
                ):
                    snapshots = await support_diagnostics._collect_state_snapshots(
                        1,
                        "bmo",
                        sync_id="bmo-attempt-a",
                        attempt_id="popup-a",
                        job_id="bmo-older-failed-job",
                        institution_id=1,
                        result_status="auth_required",
                        initiation_source="sync_all",
                        since=now - timedelta(minutes=10),
                        until=now + timedelta(minutes=2),
                    )
            finally:
                await engine.dispose()

        self.assertEqual(
            ["bmo-current-job"],
            [row["job_id"] for row in snapshots["jobs.json"]["jobs"]],
        )
        self.assertEqual(
            ["bmo-attempt-a"],
            [row["sync_id"] for row in snapshots["windows.json"]["windows"]],
        )
        self.assertEqual("sync_attempt", snapshots["jobs.json"]["scope_kind"])
        self.assertEqual("bmo-attempt-a", snapshots["jobs.json"]["attempt_sync_id"])
        self.assertFalse(snapshots["jobs.json"]["provider_history_included"])
        self.assertNotIn("account_states", snapshots["windows.json"])
        self.assertEqual(
            "current_app_state",
            snapshots["institution_snapshot.json"]["scope_kind"],
        )
        self.assertFalse(
            snapshots["institution_snapshot.json"]["historical_attempt_evidence"]
        )
        institution_snapshot = snapshots["institution_snapshot.json"]
        self.assertEqual("popup-a", institution_snapshot["attempt_id"])
        self.assertEqual("auth_required", institution_snapshot["attempt_result_status"])
        self.assertEqual("sync_all", institution_snapshot["initiation_source"])
        self.assertEqual("auth_required", institution_snapshot["institutions"][0]["sync_status"])
        self.assertEqual(
            "terminal_attempt_result",
            institution_snapshot["institutions"][0]["sync_status_source"],
        )
        self.assertEqual(
            "ok",
            institution_snapshot["institutions"][0]["persisted_sync_status_at_capture"],
        )
        self.assertEqual(
            ["bmo-attempt-a"],
            [
                row["sync_id"]
                for row in snapshots["attempts.json"]["in_memory_sync_attempts"]
            ],
        )
        self.assertEqual(
            ["1:bmo:1"],
            list(snapshots["attempts.json"]["sync_locks"]),
        )
        self.assertEqual(
            ["bmo-current-job"],
            list(snapshots["attempts.json"]["live_tximport_tasks"]),
        )

    async def test_current_state_merges_ibkr_flex_into_ibkr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_async_engine(f"sqlite+aiosqlite:///{temp_dir}/support-state.db")
            sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            now = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)
            async with sessionmaker() as db:
                db.add(User(id=1))
                db.add(
                    Institution(
                        id=1,
                        user_id=1,
                        name="IBKR",
                        type="scraper",
                        provider="ibkr",
                        sync_status="ok",
                    )
                )
                db.add(
                    TransactionImportJob(
                        user_id=1,
                        institution_id=1,
                        provider="ibkr_flex",
                        job_id="ibkr-flex-job",
                        status="complete",
                        reason="autosync",
                        sync_id="ibkr-attempt-a",
                        source_sync_id="ibkr-attempt-a",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await db.commit()

            try:
                with patch("app.database.async_session", sessionmaker):
                    snapshot = await support_auto_archive._collect_current_state_at_export(1)
            finally:
                await engine.dispose()

        self.assertIn("ibkr", snapshot["providers"])
        self.assertNotIn("ibkr_flex", snapshot["providers"])
        self.assertEqual(
            {"complete": 1},
            snapshot["providers"]["ibkr"]["transaction_import_job_status_counts"],
        )
        self.assertEqual(
            "autosync",
            snapshot["providers"]["ibkr"]["latest_transaction_import_job"]["reason"],
        )

    def test_log_buffer_snapshot_requires_matching_user_and_sync_id(self) -> None:
        buffer = ProviderLogRingBuffer()
        buffer.append("bmo", 1, "attempt A", user_id=1, sync_id="bmo-attempt-a")
        buffer.append("bmo", 2, "attempt B", user_id=1, sync_id="bmo-attempt-b")
        buffer.append("bmo", 3, "other user A", user_id=2, sync_id="bmo-attempt-a")

        rows = buffer.snapshot("bmo", user_id=1, sync_id="bmo-attempt-a")

        self.assertEqual([(1, "attempt A")], rows)

    def test_provider_diagnostics_copy_requires_exact_attempt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diagnostic_dir = root / "providers"
            source_dir = diagnostic_dir / "BMO"
            source_dir.mkdir(parents=True)
            run_dir = root / "run"
            run_dir.mkdir()
            now = datetime.now(timezone.utc)
            paths = {
                "current.har": source_dir / "bmo_visible_auth_user-1_20260826T200000Z_sync-bmo-attempt-a_attempt-popup-a_failed.har",
                "older.har": source_dir / "bmo_visible_auth_user-1_20260826T195900Z_sync-bmo-attempt-b_attempt-popup-b_failed.har",
                "unscoped.har": source_dir / "bmo_visible_auth_user-1_20260826T195800Z_failed.har",
                "current.jsonl": source_dir / "bmo_phase2_replay_user1_current.jsonl",
                "older.jsonl": source_dir / "bmo_phase2_replay_user1_older.jsonl",
                "active.jsonl": source_dir / "bmo_phase2_replay_user1_in_progress.jsonl",
            }
            for label, path in paths.items():
                if path.suffix == ".jsonl":
                    sync_id = (
                        "bmo-attempt-a"
                        if label in {"current.jsonl", "active.jsonl"}
                        else "bmo-attempt-b"
                    )
                    path.write_text(json.dumps({"sync_id": sync_id}) + "\n", encoding="utf-8")
                else:
                    path.write_text(label, encoding="utf-8")

            with patch.object(support_auto_archive, "DIAGNOSTIC_DIR", diagnostic_dir):
                support_auto_archive._copy_provider_diagnostics_into_run(
                    run_dir=run_dir,
                    provider="bmo",
                    user_id=1,
                    sync_id="bmo-attempt-a",
                    attempt_id="popup-a",
                    since=now - timedelta(minutes=10),
                    until=now + timedelta(minutes=2),
                )

            copied = {path.name for path in (run_dir / "diagnostics").iterdir()}
            if os.name == "posix":
                for label in ("current.har", "current.jsonl"):
                    copied_path = run_dir / "diagnostics" / paths[label].name
                    self.assertEqual(paths[label].stat().st_ino, copied_path.stat().st_ino)

        self.assertEqual({paths["current.har"].name, paths["current.jsonl"].name}, copied)

    def test_attempt_export_deduplicates_identical_diagnostics_across_phase_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            (first / "diagnostics").mkdir(parents=True)
            (second / "diagnostics").mkdir(parents=True)
            (first / "diagnostics" / "phase2_original_ok.jsonl").write_text(
                '{"status":"ok"}\n',
                encoding="utf-8",
            )
            (second / "diagnostics" / "phase2_renamed_ok.jsonl").write_text(
                '{"status":"ok"}\n',
                encoding="utf-8",
            )

            result = support_auto_archive._collect_attempt_archive_files(
                [(first, "first"), (second, "second")],
                attempt_key="sync:one-attempt",
                tree_scan_limit=100,
            )

        self.assertEqual(1, len(result.entries))
        self.assertEqual(1, len(result.deduplicated_files))
        self.assertNotEqual(
            result.deduplicated_files[0]["canonical_path"],
            result.deduplicated_files[0]["duplicate_path"],
        )

    def test_provider_diagnostics_preserve_har_larger_than_generic_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            diagnostic_dir = root / "providers"
            source_dir = diagnostic_dir / "BMO"
            source_dir.mkdir(parents=True)
            run_dir = root / "run"
            run_dir.mkdir()
            har_path = source_dir / (
                "bmo_visible_auth_user-1_20260902T010000Z_"
                "sync-bmo-large-har_attempt-popup-large_succeeded.har"
            )
            har_size = support_auto_archive.DIAGNOSTIC_FILE_MAX_BYTES + 1
            with har_path.open("wb") as handle:
                handle.truncate(har_size)
            now = datetime.now(timezone.utc)

            with patch.object(support_auto_archive, "DIAGNOSTIC_DIR", diagnostic_dir):
                support_auto_archive._copy_provider_diagnostics_into_run(
                    run_dir=run_dir,
                    provider="bmo",
                    user_id=1,
                    sync_id="bmo-large-har",
                    attempt_id="popup-large",
                    since=now - timedelta(minutes=10),
                    until=now + timedelta(minutes=2),
                )

            copied_har = run_dir / "diagnostics" / har_path.name
            self.assertTrue(copied_har.is_file())
            self.assertEqual(har_size, copied_har.stat().st_size)
            self.assertEqual(
                [(copied_har, f"diagnostics/{har_path.name}")],
                list(
                    support_auto_archive._collect_attempt_archive_files(
                        [(run_dir, "")],
                        attempt_key="sync:bmo-large-har",
                        tree_scan_limit=support_auto_archive.SUPPORT_DIRECTORY_SCAN_LIMIT,
                    ).entries
                ),
            )

    async def test_archive_trigger_and_state_declare_attempt_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            scoped_state = {
                "jobs.json": {
                    "schema_version": 2,
                    "scope_kind": "sync_attempt",
                    "attempt_sync_id": "bmo-attempt-a",
                    "provider_history_included": False,
                    "jobs": [],
                }
            }
            collector = AsyncMock(return_value=scoped_state)
            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "DIAGNOSTIC_DIR", log_dir / "providers"),
                patch.object(
                    support_auto_archive,
                    "_user_timezone_info",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(support_diagnostics, "_collect_state_snapshots", collector),
                patch(
                    "app.services.support_logging.get_support_capture_level",
                    return_value="developer_local",
                ),
            ):
                run_dir = await support_auto_archive.archive_run(
                    user_id=1,
                    provider="bmo",
                    sync_id="bmo-attempt-a",
                    attempt_id="popup-a",
                    trigger="sync_result_ok",
                    extra_fields={
                        "sync_scope": "accounts",
                        "sync_source": "sync_all",
                        "institution_id": 17,
                        "result_status": "auth_required",
                    },
                )
                packaged = support_auto_archive.package_run_zip(
                    "bmo",
                    run_dir.name,
                    user_id=1,
                )
                with zipfile.ZipFile(packaged.path) as archive:
                    export_manifest = json.loads(archive.read("export_manifest.json"))

            self.assertIsNotNone(run_dir)
            trigger = json.loads((run_dir / "trigger.json").read_text(encoding="utf-8"))
            state = json.loads((run_dir / "state" / "jobs.json").read_text(encoding="utf-8"))
            completion = json.loads(
                (run_dir / support_auto_archive.RUN_COMPLETION_FILENAME).read_text(encoding="utf-8")
            )
            context = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))

        self.assertEqual("sync_attempt", trigger["export_scope"]["kind"])
        self.assertEqual("developer_local", trigger["capture_level"])
        self.assertEqual("developer_local", context["capture_level"])
        self.assertEqual("bmo-attempt-a", context["sync_id"])
        self.assertEqual("popup-a", context["attempt_id"])
        self.assertEqual("developer_local", export_manifest["capture_level"])
        self.assertEqual(["developer_local"], export_manifest["capture_levels"])
        self.assertEqual("bmo-attempt-a", trigger["export_scope"]["attempt_sync_id"])
        self.assertFalse(trigger["export_scope"]["provider_history_included"])
        self.assertEqual("sync_attempt", trigger["buffer_metadata"]["scope_kind"])
        self.assertEqual("sync_all", trigger["initiation_source"])
        self.assertEqual("sync_attempt", state["scope_kind"])
        self.assertEqual("complete", completion["status"])
        self.assertTrue(completion["evidence_collection_finished"])
        collector.assert_awaited_once()
        self.assertEqual(
            "bmo-attempt-a",
            collector.await_args.kwargs["sync_id"],
        )
        self.assertEqual("sync_all", collector.await_args.kwargs["initiation_source"])
        self.assertEqual("auth_required", collector.await_args.kwargs["result_status"])
        self.assertEqual(17, collector.await_args.kwargs["institution_id"])

    async def test_run_is_not_listed_until_evidence_collection_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            collection_started = asyncio.Event()
            release_collection = asyncio.Event()

            async def delayed_collector(*_args, **_kwargs):
                collection_started.set()
                await release_collection.wait()
                return {"attempts.json": {"scope_kind": "sync_attempt"}}

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "DIAGNOSTIC_DIR", log_dir / "providers"),
                patch.object(
                    support_auto_archive,
                    "_user_timezone_info",
                    new=AsyncMock(return_value=None),
                ),
                patch.object(support_diagnostics, "_collect_state_snapshots", side_effect=delayed_collector),
            ):
                archive_task = asyncio.create_task(
                    support_auto_archive.archive_run(
                        user_id=1,
                        provider="bmo",
                        sync_id="bmo-attempt-final",
                        attempt_id="popup-final",
                        trigger="visible_auth_failed",
                    )
                )
                await collection_started.wait()

                self.assertEqual([], support_auto_archive.list_runs(provider="bmo", user_id=1))
                self.assertFalse((runs_dir / "bmo").exists())
                self.assertTrue((log_dir / ".runs-staging" / "bmo").exists())

                release_collection.set()
                run_dir = await archive_task
                listed = support_auto_archive.list_runs(provider="bmo", user_id=1)

            self.assertIsNotNone(run_dir)
            self.assertEqual(1, len(listed))
            self.assertEqual(run_dir.name, listed[0]["run_id"])
            self.assertIn(
                support_auto_archive.RUN_COMPLETION_FILENAME,
                {entry["path"] for entry in listed[0]["files"]},
            )

    def test_regular_state_snapshot_redacts_account_names_and_identifier_fragments(self) -> None:
        account = SimpleNamespace(
            id=8,
            institution_id=3,
            external_id="ABC123456789",
            name="Personal Savings 6789",
            account_type="savings",
            currency="CAD",
            is_liability=False,
            hidden=False,
            last_synced=None,
        )
        institution = SimpleNamespace(
            id=3,
            name="My Custom Bank Name",
            type="scraper",
            provider="rbc",
            enabled=True,
            hidden=False,
            sync_status="ok",
            created_at=None,
        )

        account_payload = support_diagnostics._account_row_to_dict(
            account,
            capture_level="redacted_rich",
        )
        institution_payload = support_diagnostics._institution_row_to_dict(
            institution,
            capture_level="redacted_rich",
        )

        self.assertEqual(account_payload["name"], "<redacted>")
        self.assertTrue(account_payload["external_id_mask"].startswith("<redacted:"))
        self.assertNotIn("ABC", account_payload["external_id_mask"])
        self.assertNotIn("789", account_payload["external_id_mask"])
        self.assertEqual(institution_payload["name"], "<redacted>")

    def test_developer_local_state_snapshot_preserves_existing_rich_values(self) -> None:
        account = SimpleNamespace(
            id=8,
            institution_id=3,
            external_id="ABC123456789",
            name="Personal Savings 6789",
            account_type="savings",
            currency="CAD",
            is_liability=False,
            hidden=False,
            last_synced=None,
        )
        institution = SimpleNamespace(
            id=3,
            name="My Custom Bank Name",
            type="scraper",
            provider="rbc",
            enabled=True,
            hidden=False,
            sync_status="ok",
            created_at=None,
        )

        account_payload = support_diagnostics._account_row_to_dict(
            account,
            capture_level="developer_local",
        )
        institution_payload = support_diagnostics._institution_row_to_dict(
            institution,
            capture_level="developer_local",
        )

        self.assertEqual(account_payload["name"], "Personal Savings 6789")
        self.assertEqual(account_payload["external_id_mask"], "ABC…789")
        self.assertEqual(institution_payload["name"], "My Custom Bank Name")

    def test_log_sanitization_fails_closed_when_sanitizer_is_unavailable(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "app.services.support_diagnostics":
                raise ImportError("unavailable")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            sanitized = support_auto_archive._sanitize_log_blob("password=do-not-archive")

        self.assertEqual(sanitized, support_auto_archive.SANITIZER_FAILURE_TEXT)
        self.assertNotIn("do-not-archive", sanitized)

    def test_diagnostic_owner_matching_does_not_confuse_user_one_and_ten(self) -> None:
        self.assertTrue(support_auto_archive._diagnostic_filename_owned_by("rbc_user1_capture.har", 1))
        self.assertTrue(support_auto_archive._diagnostic_filename_owned_by("rbc_user-1_capture.har", 1))
        self.assertFalse(support_auto_archive._diagnostic_filename_owned_by("rbc_user10_capture.har", 1))

    def test_run_summary_stops_at_the_directory_scan_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider_root = Path(temp_dir) / "rbc"
            run_dir = provider_root / "run"
            run_dir.mkdir(parents=True)
            for index in range(6):
                (run_dir / f"entry-{index}.log").write_text("ok", encoding="utf-8")

            with patch.object(support_auto_archive, "SUPPORT_DIRECTORY_SCAN_LIMIT", 3):
                summary = support_auto_archive._summarize_run(provider_root, run_dir)

            self.assertTrue(summary["files_truncated"])
            self.assertLessEqual(summary["file_count"], 3)
            self.assertNotIn("relative_path", summary)

    def test_provider_purge_removes_owner_registered_materialized_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            diagnostic_dir = log_dir / "providers"
            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(support_auto_archive, "DIAGNOSTIC_DIR", diagnostic_dir),
            ):
                archive = support_auto_archive._register_support_archive(
                    user_id=7,
                    filename="BreakTwenty_ibkr_flex_diagnostics_test.zip",
                    kind="provider_run",
                    provider="ibkr_flex",
                )
                archive.path.write_bytes(b"owned")
                materialized = log_dir / "exports" / "ibkr_flex" / archive.filename
                materialized.parent.mkdir(parents=True)
                materialized.write_bytes(b"owned")
                _zip_path, metadata_path = support_auto_archive._archive_paths(archive.archive_id)
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["materialized_relative_path"] = f"ibkr_flex/{archive.filename}"
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                other_archive = support_auto_archive._register_support_archive(
                    user_id=8,
                    filename="BreakTwenty_ibkr_flex_diagnostics_other.zip",
                    kind="provider_run",
                    provider="ibkr_flex",
                )
                other_archive.path.write_bytes(b"other")
                other_materialized = (
                    log_dir / "exports" / "ibkr_flex" / other_archive.filename
                )
                other_materialized.write_bytes(b"other")
                _other_zip, other_metadata_path = support_auto_archive._archive_paths(
                    other_archive.archive_id
                )
                other_metadata = json.loads(other_metadata_path.read_text(encoding="utf-8"))
                other_metadata["materialized_relative_path"] = (
                    f"ibkr_flex/{other_archive.filename}"
                )
                other_metadata_path.write_text(json.dumps(other_metadata), encoding="utf-8")

                counts = support_auto_archive.purge_provider_diagnostic_artifacts(
                    user_id=7,
                    provider="ibkr",
                )

                self.assertEqual(counts["exports_removed"], 1)
                self.assertFalse(materialized.exists())
                self.assertFalse(archive.path.exists())
                self.assertFalse(metadata_path.exists())
                self.assertTrue(other_materialized.exists())
                self.assertTrue(other_archive.path.exists())
                self.assertTrue(other_metadata_path.exists())

    async def test_export_recent_includes_global_network_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            runs_dir = log_dir / "runs"
            run_dir = runs_dir / "network" / "2026-07-28_13-51-09_EDT_dns-gate_sync-dns-gate-blocked"
            run_dir.mkdir(parents=True)
            trigger = {
                "schema_version": 1,
                "generated_at": support_auto_archive._utc_now().isoformat(),
                "provider": "network",
                "user_id": 7,
                "sync_id": "dns-gate-test",
                "trigger": "sync_dns_gate_blocked",
                "extra": {
                    "network_preflight": {
                        "attempt_count": 5,
                        "resolver_facts": {
                            "runtime_mode": "embedded",
                            "resolver_source": "systemd_resolved",
                        },
                    },
                },
            }
            (run_dir / "trigger.json").write_text(
                json.dumps(trigger),
                encoding="utf-8",
            )
            (run_dir / support_auto_archive.RUN_COMPLETION_FILENAME).write_text(
                json.dumps({"status": "complete", "evidence_collection_finished": True}),
                encoding="utf-8",
            )

            with (
                patch.object(support_auto_archive, "LOG_DIR", log_dir),
                patch.object(support_auto_archive, "RUNS_DIR", runs_dir),
                patch.object(
                    support_auto_archive,
                    "_user_timezone_info",
                    new=AsyncMock(return_value=None),
                ),
            ):
                support_archive, included, export_summary = (
                    await support_auto_archive.package_all_recent_runs(
                        user_id=7,
                        since_minutes=15,
                    )
                )

            self.assertIsNotNone(support_archive)
            self.assertEqual(1, len(included))
            self.assertEqual("network", included[0]["provider"])
            self.assertEqual("sync_dns_gate_blocked", included[0]["trigger"])
            self.assertFalse(export_summary["truncated"])
            with zipfile.ZipFile(support_archive.path) as archive:
                self.assertEqual(
                    [
                        "current_state_at_export.json",
                        "export_manifest.json",
                        f"network/{run_dir.name}/trigger.json",
                    ],
                    archive.namelist(),
                )
                export_state = json.loads(
                    archive.read("current_state_at_export.json").decode("utf-8")
                )
                manifest = json.loads(archive.read("export_manifest.json"))
            self.assertEqual(2, export_state["schema_version"])
            self.assertEqual(7, export_state["user_id"])
            self.assertEqual("all_providers_current_app_state", export_state["scope_kind"])
            self.assertIn("provider-wide", export_state["purpose"])
            self.assertEqual("all_providers_recent", manifest["export_scope"])


if __name__ == "__main__":
    unittest.main()
