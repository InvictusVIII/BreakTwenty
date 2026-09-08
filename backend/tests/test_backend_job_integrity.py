import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Account,
    Institution,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
    User,
)
from app.services import transaction_import_jobs


class TransactionImportLeaseHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/transaction-job.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        now = transaction_import_jobs._utcnow()
        async with self._sessionmaker() as db:
            db.add(User(id=1))
            db.add(
                Institution(
                    id=1,
                    user_id=1,
                    name="RBC",
                    type="scraper",
                    provider="rbc",
                )
            )
            db.add(
                Account(
                    id=1,
                    user_id=1,
                    institution_id=1,
                    external_id="rbc-account",
                    name="RBC Account",
                    currency="CAD",
                )
            )
            db.add(
                TransactionImportAccountState(
                    user_id=1,
                    institution_id=1,
                    account_id=1,
                    provider="rbc",
                    account_external_id="rbc-account",
                    account_name="RBC Account",
                    backfill_status="running",
                    created_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TransactionImportWindow(
                    user_id=1,
                    institution_id=1,
                    account_id=1,
                    provider="rbc",
                    account_external_id="rbc-account",
                    mode="backfill",
                    window_start_date=date(2026, 1, 1),
                    window_end_date=date(2026, 1, 31),
                    status="running",
                    started_at=now,
                    updated_at=now,
                )
            )
            db.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=1,
                    provider="rbc",
                    job_id="rbc-heartbeat-job",
                    status=transaction_import_jobs.JOB_STATUS_RUNNING,
                    lease_token="owner-one",
                    lease_expires_at=now + timedelta(milliseconds=10),
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()
        self._session_patch = patch.object(
            transaction_import_jobs,
            "async_session",
            self._sessionmaker,
        )
        self._session_patch.start()
        self._write_lock = asyncio.Lock()

        @asynccontextmanager
        async def local_write_gate():
            async with self._write_lock:
                yield

        self._write_gate_patch = patch.object(
            transaction_import_jobs,
            "sqlite_write_gate",
            new=local_write_gate,
        )
        self._write_gate_patch.start()

    async def asyncTearDown(self) -> None:
        self._write_gate_patch.stop()
        self._session_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_heartbeat_keeps_long_provider_phase_out_of_stale_recovery(self) -> None:
        owner_task = asyncio.current_task()
        self.assertIsNotNone(owner_task)
        with (
            patch.object(
                transaction_import_jobs,
                "TRANSACTION_JOB_LEASE_RENEW_INTERVAL_SECONDS",
                0.005,
            ),
            patch.object(
                transaction_import_jobs,
                "TRANSACTION_JOB_LEASE_DURATION",
                timedelta(milliseconds=100),
            ),
        ):
            heartbeat = asyncio.create_task(
                transaction_import_jobs._renew_transaction_import_job_lease(
                    "rbc-heartbeat-job",
                    "owner-one",
                    owner_task,
                )
            )
            try:
                await asyncio.sleep(0.03)
                recovered = (
                    await transaction_import_jobs.recover_stale_transaction_import_jobs()
                )
            finally:
                heartbeat.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await heartbeat

        self.assertEqual(recovered, 0)
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
        self.assertEqual(job.status, transaction_import_jobs.JOB_STATUS_RUNNING)
        self.assertEqual(job.lease_token, "owner-one")

    async def test_restart_requeues_resumable_job_and_marks_window_retryable(self) -> None:
        async with self._sessionmaker() as db, db.begin():
            job = await db.get(TransactionImportJob, 1)
            job.reason = "manual_sync"
            job.lease_expires_at = transaction_import_jobs._utcnow() - timedelta(seconds=1)

        with (
            patch.object(transaction_import_jobs, "_schedule_job") as schedule_job,
            patch.object(transaction_import_jobs, "mark_dirty"),
        ):
            await transaction_import_jobs.resume_transaction_import_jobs()

        schedule_job.assert_called_once_with("rbc-heartbeat-job", delay_seconds=0.1)
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
            window = await db.get(TransactionImportWindow, 1)
            account_state = await db.get(TransactionImportAccountState, 1)
        self.assertEqual(job.status, transaction_import_jobs.JOB_STATUS_QUEUED)
        self.assertIsNone(job.lease_token)
        self.assertIsNone(job.lease_expires_at)
        self.assertIsNone(job.finished_at)
        self.assertEqual(window.status, "error")
        self.assertEqual(account_state.backfill_status, "error")

    async def test_controlled_recovery_requeues_resumable_job_before_lease_expiry(self) -> None:
        async with self._sessionmaker() as db, db.begin():
            job = await db.get(TransactionImportJob, 1)
            job.reason = "manual_sync"
            job.lease_expires_at = transaction_import_jobs._utcnow() + timedelta(minutes=10)

        with (
            patch.object(transaction_import_jobs, "_schedule_job") as schedule_job,
            patch.object(transaction_import_jobs, "mark_dirty"),
        ):
            await transaction_import_jobs.resume_transaction_import_jobs(force_running=True)

        schedule_job.assert_called_once_with("rbc-heartbeat-job", delay_seconds=0.1)
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
        self.assertEqual(job.status, transaction_import_jobs.JOB_STATUS_QUEUED)
        self.assertIsNone(job.lease_token)
        self.assertIsNone(job.lease_expires_at)

    async def test_restart_fails_nonrevivable_add_connection_job(self) -> None:
        async with self._sessionmaker() as db, db.begin():
            job = await db.get(TransactionImportJob, 1)
            job.reason = "add_connection"
            job.lease_expires_at = transaction_import_jobs._utcnow() - timedelta(seconds=1)

        with (
            patch.object(transaction_import_jobs, "_schedule_job") as schedule_job,
            patch.object(transaction_import_jobs, "mark_dirty"),
        ):
            await transaction_import_jobs.resume_transaction_import_jobs()

        schedule_job.assert_not_called()
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
            window = await db.get(TransactionImportWindow, 1)
        self.assertEqual(job.status, transaction_import_jobs.JOB_STATUS_FAILED)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.lease_token)
        self.assertEqual(window.status, "error")

    async def test_task_crash_callback_durably_fails_job_and_clears_lease(self) -> None:
        async def crash() -> None:
            raise RuntimeError("fixture crash")

        task = asyncio.create_task(crash())
        with self.assertRaisesRegex(RuntimeError, "fixture crash"):
            await task
        transaction_import_jobs._tasks["rbc-heartbeat-job"] = task
        captured_coroutines = []

        def capture_task(coroutine, *, name=None):
            captured_coroutines.append(coroutine)
            return Mock(name=name)

        with (
            patch.object(
                transaction_import_jobs,
                "create_tracked_task",
                side_effect=capture_task,
            ),
            patch.object(transaction_import_jobs, "_archive_crashed_job"),
            patch("app.services.sync_utils._schedule_drain_pending_status"),
        ):
            transaction_import_jobs._on_job_task_done("rbc-heartbeat-job", task)
            self.assertEqual(len(captured_coroutines), 1)
            await captured_coroutines[0]

        self.assertNotIn("rbc-heartbeat-job", transaction_import_jobs._tasks)
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
        self.assertEqual(job.status, transaction_import_jobs.JOB_STATUS_FAILED)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNone(job.lease_token)
        self.assertIsNone(job.lease_expires_at)
        self.assertIn("RuntimeError: fixture crash", job.last_error)

    async def test_terminal_worker_archive_is_scheduled_after_claimed_flow_returns(self) -> None:
        claimed_job = SimpleNamespace(
            user_id=1,
            provider="rbc",
            source_sync_id="rbc-full-attempt",
            sync_id="rbc-transaction-phase",
            attempt_id="visible-auth-attempt",
        )
        finalized_job = SimpleNamespace(
            user_id=1,
            institution_id=1,
            provider="rbc",
            source_sync_id="rbc-full-attempt",
            sync_id="rbc-transaction-phase",
            attempt_id="visible-auth-attempt",
            status=transaction_import_jobs.JOB_STATUS_COMPLETE,
            reason="manual_sync",
            last_error=None,
        )
        flow_finished = False

        async def run_claimed(*_args):
            nonlocal flow_finished
            flow_finished = True

        async def heartbeat(*_args):
            await asyncio.Event().wait()

        def create_task(coroutine, *, name=None):
            return asyncio.create_task(coroutine, name=name)

        def archive_after_flow(**_kwargs):
            self.assertTrue(flow_finished)

        with (
            patch.object(
                transaction_import_jobs,
                "_claim_queued_job",
                new=AsyncMock(return_value=claimed_job),
            ),
            patch.object(
                transaction_import_jobs,
                "_run_claimed_transaction_import_job",
                side_effect=run_claimed,
            ),
            patch.object(
                transaction_import_jobs,
                "_renew_transaction_import_job_lease",
                side_effect=heartbeat,
            ),
            patch.object(
                transaction_import_jobs,
                "_load_job",
                new=AsyncMock(return_value=finalized_job),
            ),
            patch.object(transaction_import_jobs, "create_tracked_task", side_effect=create_task),
            patch.object(transaction_import_jobs, "_log_provider_job_event") as log_event,
            patch(
                "app.services.support_auto_archive.archive_run_fire_and_forget",
                side_effect=archive_after_flow,
            ) as archive_run,
        ):
            await transaction_import_jobs._run_transaction_import_job("rbc-final-job")

        log_event.assert_called_once()
        self.assertEqual("transaction import job finalized", log_event.call_args.args[1])
        archive_run.assert_called_once_with(
            user_id=1,
            provider="rbc",
            sync_id="rbc-full-attempt",
            attempt_id="visible-auth-attempt",
            trigger="tximport_job_finalized_complete",
            error=None,
            extra_fields={
                "job_id": "rbc-final-job",
                "job_status": transaction_import_jobs.JOB_STATUS_COMPLETE,
                "terminal_worker_snapshot": True,
                "sync_source": "manual_sync",
                "institution_id": 1,
                "result_status": "ok",
            },
        )

    async def test_sync_all_transaction_job_retries_once_after_temporary_dns_recovery(self) -> None:
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
            job.reason = "sync_all"
            job.source_sync_id = "rbc-sync-all"
            await db.commit()

        run_connector = AsyncMock(
            side_effect=(
                (
                    object(),
                    {
                        "status": "network_error",
                        "sync_id": "rbc-sync-all",
                        transaction_import_jobs.TEMPORARY_DNS_FAILURE_MARKER: True,
                    },
                ),
                (object(), {"status": "ok", "sync_id": "rbc-sync-all"}),
            )
        )
        mark_job = AsyncMock()
        with (
            patch.object(
                transaction_import_jobs,
                "_background_import_enabled",
                return_value=True,
            ),
            patch.object(
                transaction_import_jobs,
                "acquire_sync_lock",
                new=AsyncMock(return_value=True),
            ),
            patch.object(transaction_import_jobs, "release_sync_lock", new=AsyncMock()),
            patch.object(transaction_import_jobs, "_touch_job_progress", new=AsyncMock()),
            patch.object(transaction_import_jobs, "_mark_job", new=mark_job),
            patch.object(
                transaction_import_jobs,
                "run_provider_dns_resolution",
                new=AsyncMock(
                    return_value=SimpleNamespace(status="resolved", error_name=None)
                ),
            ),
            patch.object(
                transaction_import_jobs,
                "run_sync_network_gate",
                new=AsyncMock(return_value=SimpleNamespace(status="ready")),
            ) as recovery_gate,
            patch(
                "app.connectors.orchestration.run_connector_sync",
                new=run_connector,
            ),
            patch(
                "app.services.recurrence.run_detection_for_user",
                new=AsyncMock(),
            ),
        ):
            await transaction_import_jobs._run_claimed_transaction_import_job(
                "rbc-heartbeat-job",
                job,
                "owner-one",
            )

        self.assertEqual(2, run_connector.await_count)
        self.assertTrue(
            run_connector.await_args_list[0].kwargs["include_network_recovery_hint"]
        )
        self.assertFalse(
            run_connector.await_args_list[1].kwargs["include_network_recovery_hint"]
        )
        recovery_gate.assert_awaited_once()
        self.assertEqual(
            "transaction_import_recovery",
            recovery_gate.await_args.kwargs["flow"],
        )
        self.assertEqual(
            transaction_import_jobs.JOB_STATUS_COMPLETE,
            mark_job.await_args_list[-1].args[1],
        )

    async def test_individual_transaction_job_does_not_retry_network_error(self) -> None:
        async with self._sessionmaker() as db:
            job = await db.get(TransactionImportJob, 1)
            job.reason = "individual_sync"
            job.source_sync_id = "rbc-individual"
            await db.commit()

        run_connector = AsyncMock(
            return_value=(
                object(),
                {"status": "network_error", "sync_id": "rbc-individual"},
            )
        )
        mark_job = AsyncMock()
        dns_resolution = AsyncMock()
        recovery_gate = AsyncMock()
        with (
            patch.object(
                transaction_import_jobs,
                "_background_import_enabled",
                return_value=True,
            ),
            patch.object(
                transaction_import_jobs,
                "acquire_sync_lock",
                new=AsyncMock(return_value=True),
            ),
            patch.object(transaction_import_jobs, "release_sync_lock", new=AsyncMock()),
            patch.object(transaction_import_jobs, "_touch_job_progress", new=AsyncMock()),
            patch.object(transaction_import_jobs, "_mark_job", new=mark_job),
            patch.object(
                transaction_import_jobs,
                "run_provider_dns_resolution",
                new=dns_resolution,
            ),
            patch.object(
                transaction_import_jobs,
                "run_sync_network_gate",
                new=recovery_gate,
            ),
            patch(
                "app.connectors.orchestration.run_connector_sync",
                new=run_connector,
            ),
        ):
            await transaction_import_jobs._run_claimed_transaction_import_job(
                "rbc-heartbeat-job",
                job,
                "owner-one",
            )

        run_connector.assert_awaited_once()
        self.assertFalse(run_connector.await_args.kwargs["include_network_recovery_hint"])
        dns_resolution.assert_not_awaited()
        recovery_gate.assert_not_awaited()
        self.assertEqual(
            transaction_import_jobs.JOB_STATUS_FAILED,
            mark_job.await_args_list[-1].args[1],
        )


if __name__ == "__main__":
    unittest.main()
