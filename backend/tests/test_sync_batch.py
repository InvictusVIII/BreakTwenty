import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Account, BalanceHistory, Institution, SyncBatchJob, User
from app.services import sync_batch
from app.services.network_preflight import SyncNetworkGateResult


def _gate_result(status: str) -> SyncNetworkGateResult:
    return SyncNetworkGateResult(status=status, attempts=1, probes=())


class SyncBatchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/sync-batch.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as db:
            db.add_all((User(id=1), User(id=2)))
            db.add_all(
                Institution(
                    id=index,
                    user_id=1,
                    name=f"Institution {index}",
                    type="api",
                    provider=f"provider_{index}",
                )
                for index in range(1, 25)
            )
            await db.commit()
        self._session_patch = patch.object(sync_batch, "async_session", self._sessionmaker)
        self._session_patch.start()
        self._write_lock = asyncio.Lock()

        @asynccontextmanager
        async def local_write_gate():
            async with self._write_lock:
                yield

        self._write_gate_patch = patch.object(
            sync_batch,
            "sqlite_write_gate",
            new=local_write_gate,
        )
        self._write_gate_patch.start()
        self._task_patch = patch.object(
            sync_batch,
            "create_tracked_task",
            side_effect=lambda coroutine, **_kwargs: asyncio.create_task(coroutine),
        )
        self._task_patch.start()

    async def asyncTearDown(self) -> None:
        self._task_patch.stop()
        self._write_gate_patch.stop()
        self._session_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _store_job(
        self,
        user_id: int,
        batch_id: str,
        targets_by_key: dict[str, dict],
        *,
        mode: str,
    ) -> None:
        now = sync_batch._utc_now_string()
        job = {
            "batch_id": batch_id,
            "status": "queued",
            "mode": mode,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "results": {
                key: sync_batch._initial_connection_state(
                    target["provider"],
                    target["institution_id"],
                    target["route_provider"],
                )
                for key, target in targets_by_key.items()
            },
        }
        now_dt = sync_batch._utc_now()
        async with self._sessionmaker() as db:
            db.add(
                SyncBatchJob(
                    user_id=user_id,
                    batch_id=batch_id,
                    mode=mode,
                    status="queued",
                    connection_signature=sync_batch._connection_signature(targets_by_key),
                    state_json=sync_batch._encode_job_state(job),
                    created_at=now_dt,
                    updated_at=now_dt,
                )
            )
            await db.commit()

    async def test_active_batches_are_owner_scoped_status_filtered_and_capped(self) -> None:
        now = sync_batch._utc_now()
        async with self._sessionmaker() as db:
            for index in range(103):
                status = "queued" if index < 101 and index % 2 == 0 else "running" if index < 101 else "completed"
                db.add(
                    SyncBatchJob(
                        user_id=1,
                        batch_id=f"owner-one-{index}",
                        mode="manual",
                        status=status,
                        connection_signature=f"signature-{index}",
                        state_json=sync_batch._encode_job_state({"sentinel": index}),
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                )
            db.add(
                SyncBatchJob(
                    user_id=2,
                    batch_id="owner-two-active",
                    mode="manual",
                    status="queued",
                    connection_signature="owner-two-signature",
                    state_json=sync_batch._encode_job_state({"sentinel": "other"}),
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()

        batches = await sync_batch.get_user_active_sync_batches(1)

        self.assertEqual(len(batches), 100)
        self.assertTrue(all(batch["status"] in {"queued", "running"} for batch in batches))
        self.assertTrue(all(batch["batch_id"].startswith("owner-one-") for batch in batches))
        self.assertNotIn("owner-one-101", {batch["batch_id"] for batch in batches})
        self.assertNotIn("owner-one-102", {batch["batch_id"] for batch in batches})

    async def test_connection_accounts_are_safe_for_durable_batch_json(self) -> None:
        async with self._sessionmaker() as db:
            db.add(
                Account(
                    id=101,
                    user_id=1,
                    institution_id=1,
                    external_id="account-101",
                    name="Investment account",
                    account_type="investment",
                    currency="CAD",
                )
            )
            db.add(
                BalanceHistory(
                    user_id=1,
                    account_id=101,
                    balance=Decimal("123.45"),
                    date=date(2026, 8, 22),
                )
            )
            await db.commit()

        accounts = await sync_batch._load_connection_accounts(1, 1)

        self.assertEqual(accounts[0]["balance"], 123.45)
        self.assertIsInstance(accounts[0]["balance"], float)
        json.dumps(accounts)

    async def test_provider_history_purge_cancels_active_batch_and_keeps_unrelated_batch(self) -> None:
        ibkr_targets = {
            "connection:1": {
                "provider": "ibkr",
                "institution_id": 1,
                "route_provider": "ibkr_flex",
            }
        }
        rbc_targets = {
            "connection:2": {
                "provider": "rbc",
                "institution_id": 2,
                "route_provider": "rbc",
            }
        }
        await self._store_job(1, "ibkr-active", ibkr_targets, mode="manual")
        await self._store_job(1, "rbc-retained", rbc_targets, mode="manual")

        task = asyncio.create_task(asyncio.Event().wait())
        identity = (1, "ibkr-active")
        sync_batch._sync_batch_tasks[identity] = task
        try:
            async with self._sessionmaker() as db, db.begin():
                removed = await sync_batch.purge_provider_sync_batch_history(
                    db,
                    user_id=1,
                    provider="ibkr",
                    institution_id=1,
                )
            with self.assertRaises(asyncio.CancelledError):
                await task
            async with self._sessionmaker() as db:
                remaining = (
                    await db.execute(select(SyncBatchJob).order_by(SyncBatchJob.batch_id))
                ).scalars().all()
        finally:
            sync_batch._sync_batch_tasks.pop(identity, None)

        self.assertEqual(removed, 1)
        self.assertEqual([row.batch_id for row in remaining], ["rbc-retained"])

    async def test_deduped_active_batch_is_rescheduled(self) -> None:
        async with self._sessionmaker() as db:
            db.add(
                Institution(
                    id=30,
                    user_id=1,
                    name="Coinbase",
                    type="api",
                    provider="coinbase",
                )
            )
            await db.commit()

        targets_by_key = {
            "connection:30": {
                "provider": "coinbase",
                "institution_id": 30,
                "route_provider": "coinbase",
            }
        }
        await self._store_job(1, "coinbase-active", targets_by_key, mode="manual")

        with patch.object(sync_batch, "_schedule_sync_batch") as schedule:
            result = await sync_batch.start_sync_batch(
                1,
                [{"provider": "coinbase", "institution_id": 30}],
                mode="manual",
            )

        self.assertEqual(
            result,
            {"status": "started", "batch_id": "coinbase-active", "deduped": True},
        )
        schedule.assert_called_once_with(1, "coinbase-active")

    async def test_repeated_batch_scheduling_cannot_start_concurrent_duplicate_work(self) -> None:
        identity = (1, "single-worker-batch")
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()
        starts = 0

        async def run_batch(_user_id, _batch_id, _targets):
            nonlocal starts
            starts += 1
            worker_started.set()
            await release_worker.wait()

        with patch.object(sync_batch, "_run_sync_batch_job", side_effect=run_batch):
            sync_batch._schedule_sync_batch(*identity, {})
            await worker_started.wait()
            first_task = sync_batch._sync_batch_tasks[identity]
            sync_batch._schedule_sync_batch(*identity, {})
            self.assertIs(sync_batch._sync_batch_tasks[identity], first_task)
            self.assertEqual(starts, 1)
            release_worker.set()
            await first_task
            await asyncio.sleep(0)

        self.assertNotIn(identity, sync_batch._sync_batch_tasks)

    async def test_batch_applies_gate_and_concurrency_caps(self) -> None:
        user_id = 1
        batch_id = "test-batch"
        route_providers = {
            **{f"api_{index}": f"api_route_{index}" for index in range(8)},
            **{f"scraper_{index}": f"scraper_route_{index}" for index in range(12)},
        }
        targets_by_key = {
            f"connection:{index}": {
                "provider": provider,
                "institution_id": index,
                "route_provider": route_provider,
            }
            for index, (provider, route_provider) in enumerate(route_providers.items(), start=1)
        }
        await self._store_job(
            user_id,
            batch_id,
            targets_by_key,
            mode="sync_all",
        )

        active_total = 0
        active_by_type = {"api": 0, "scraper": 0}
        max_total = 0
        max_by_type = {"api": 0, "scraper": 0}
        observed_modes: list[str] = []
        counter_lock = asyncio.Lock()

        def provider_type(route_provider: str) -> str:
            return "scraper" if route_provider.startswith("scraper_") else "api"

        async def run_single_connection(_user_id, _batch_id, _key, target, **_kwargs):
            nonlocal active_total, max_total
            observed_modes.append(_kwargs["mode"])
            kind = provider_type(target["route_provider"])
            async with counter_lock:
                active_total += 1
                active_by_type[kind] += 1
                max_total = max(max_total, active_total)
                max_by_type[kind] = max(max_by_type[kind], active_by_type[kind])
            await asyncio.sleep(0.01)
            async with counter_lock:
                active_total -= 1
                active_by_type[kind] -= 1

        gate = AsyncMock(return_value=_gate_result("ready"))
        with (
            patch.object(
                sync_batch,
                "_order_connections_for_batch",
                return_value=list(targets_by_key.items()),
            ),
            patch.object(sync_batch, "_provider_type", side_effect=provider_type),
            patch.object(sync_batch, "_run_single_connection", side_effect=run_single_connection),
            patch.object(sync_batch, "run_sync_network_gate", new=gate),
            patch.object(sync_batch, "publish_event"),
        ):
            await sync_batch._run_sync_batch_job(user_id, batch_id, targets_by_key)

        self.assertLessEqual(max_total, sync_batch.SYNC_BATCH_TOTAL_CONCURRENCY)
        self.assertLessEqual(max_by_type["api"], sync_batch.SYNC_BATCH_API_CONCURRENCY)
        self.assertLessEqual(max_by_type["scraper"], sync_batch.SYNC_BATCH_SCRAPER_CONCURRENCY)
        gate.assert_awaited_once()
        self.assertEqual("sync_all", gate.await_args.kwargs["mode"])
        self.assertEqual({"sync_all"}, set(observed_modes))

    def test_batch_modes_map_to_explicit_attempt_sources(self) -> None:
        self.assertEqual("autosync", sync_batch._sync_source_for_batch_mode("auto"))
        self.assertEqual("sync_all", sync_batch._sync_source_for_batch_mode("manual"))
        self.assertEqual("sync_all", sync_batch._sync_source_for_batch_mode("sync_all"))
        self.assertEqual("scheduled_sync", sync_batch._sync_source_for_batch_mode("nightly"))

    async def test_manual_batch_preserves_sync_all_source_through_transaction_enqueue(self) -> None:
        run_connector = AsyncMock(
            return_value=(object(), {"status": "ok", "sync_id": "rbc-sync-1"})
        )
        enqueue = AsyncMock(return_value={"job_id": "tx-job-1"})
        target = {"provider": "rbc", "institution_id": 1, "route_provider": "rbc"}

        with (
            patch.object(sync_batch, "_update_job", new=AsyncMock(return_value=True)),
            patch.object(sync_batch, "_finish_connection", new=AsyncMock()),
            patch.object(sync_batch, "set_provider_activity"),
            patch.object(sync_batch, "clear_provider_activity"),
            patch.object(sync_batch, "acquire_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_batch, "release_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_batch, "run_connector_sync", new=run_connector),
            patch.object(
                sync_batch,
                "provider_uses_background_transaction_import",
                return_value=True,
            ),
            patch.object(sync_batch, "enqueue_provider_transaction_import", new=enqueue),
        ):
            await sync_batch._run_single_connection(
                1,
                "batch-1",
                "connection:1",
                target,
                lease_token="lease-1",
                mode="manual",
            )

        self.assertEqual("sync_all", run_connector.await_args.kwargs["sync_source"])
        self.assertEqual("sync_all", enqueue.await_args.kwargs["reason"])

    async def test_batch_gate_runs_before_provider_work(self) -> None:
        user_id = 1
        batch_id = "test-auto-batch"
        targets_by_key = {
            "connection:1": {"provider": "rbc", "institution_id": 1, "route_provider": "rbc"},
            "connection:2": {"provider": "td", "institution_id": 2, "route_provider": "td"},
        }
        await self._store_job(user_id, batch_id, targets_by_key, mode="auto")
        call_order: list[str] = []

        async def run_gate(*_args, **_kwargs):
            call_order.append("dns")
            return _gate_result("ready")

        async def run_single_provider(*_args, **_kwargs):
            call_order.append("provider")

        with (
            patch.object(
                sync_batch,
                "_order_connections_for_batch",
                return_value=list(targets_by_key.items()),
            ),
            patch.object(sync_batch, "run_sync_network_gate", side_effect=run_gate),
            patch.object(sync_batch, "_run_single_connection", side_effect=run_single_provider),
            patch.object(sync_batch, "publish_event"),
        ):
            await sync_batch._run_sync_batch_job(user_id, batch_id, targets_by_key)

        self.assertEqual("dns", call_order[0])
        self.assertEqual(2, call_order.count("provider"))

    async def test_blocked_gate_finishes_batch_without_provider_attempts(self) -> None:
        user_id = 1
        batch_id = "test-blocked-batch"
        targets_by_key = {
            "connection:1": {"provider": "rbc", "institution_id": 1, "route_provider": "rbc"},
            "connection:2": {"provider": "td", "institution_id": 2, "route_provider": "td"},
        }
        await self._store_job(user_id, batch_id, targets_by_key, mode="auto")
        run_provider = AsyncMock()

        with (
            patch.object(
                sync_batch,
                "_order_connections_for_batch",
                return_value=list(targets_by_key.items()),
            ),
            patch.object(
                sync_batch,
                "run_sync_network_gate",
                new=AsyncMock(return_value=_gate_result("blocked")),
            ),
            patch.object(sync_batch, "_run_single_connection", new=run_provider),
            patch.object(sync_batch, "clear_provider_activity"),
            patch.object(sync_batch, "publish_event"),
        ):
            await sync_batch._run_sync_batch_job(user_id, batch_id, targets_by_key)

        job = await sync_batch.get_sync_batch(user_id, batch_id)
        self.assertEqual("blocked", job["status"])
        self.assertIsNone(job["started_at"])
        self.assertEqual(
            "dns_resolution_temporarily_unavailable",
            job["blocker"]["code"],
        )
        self.assertTrue(all(
            result["status"] == "network_blocked"
            for result in job["results"].values()
        ))
        run_provider.assert_not_awaited()

    async def test_restart_recovery_only_replays_unfinished_connections(self) -> None:
        targets = sync_batch._targets_from_job(
            {
                "results": {
                    "connection:1": {
                        "provider": "rbc",
                        "institution_id": 1,
                        "route_provider": "rbc",
                        "status": "ok",
                    },
                    "connection:2": {
                        "provider": "td",
                        "institution_id": 2,
                        "route_provider": "td",
                        "status": "pending",
                    },
                    "connection:3": {
                        "provider": "bmo",
                        "institution_id": 3,
                        "route_provider": "bmo",
                        "status": "syncing",
                    },
                    "connection:4": {
                        "provider": "cibc",
                        "institution_id": 4,
                        "route_provider": "cibc",
                        "status": "error",
                    },
                }
            }
        )

        self.assertEqual(set(targets), {"connection:2", "connection:3"})

    async def test_resume_scanner_schedules_running_batch_only_after_lease_expiry(self) -> None:
        targets = {
            "connection:1": {
                "provider": "provider_1",
                "institution_id": 1,
                "route_provider": "provider_1",
            }
        }
        await self._store_job(1, "leased-batch", targets, mode="auto")
        async with self._sessionmaker() as db:
            await db.execute(
                update(SyncBatchJob)
                .where(SyncBatchJob.batch_id == "leased-batch")
                .values(
                    status="running",
                    lease_token="owner-one",
                    lease_expires_at=sync_batch._utc_now() + timedelta(minutes=1),
                )
            )
            await db.commit()

        schedule = Mock()
        with patch.object(sync_batch, "_schedule_sync_batch", new=schedule):
            await sync_batch.resume_sync_batch_jobs()
            schedule.assert_not_called()
            async with self._sessionmaker() as db:
                await db.execute(
                    update(SyncBatchJob)
                    .where(SyncBatchJob.batch_id == "leased-batch")
                    .values(lease_expires_at=sync_batch._utc_now() - timedelta(seconds=1))
                )
                await db.commit()
            await sync_batch.resume_sync_batch_jobs()

        schedule.assert_called_once_with(1, "leased-batch")

    async def test_controlled_recovery_requeues_running_batch_before_lease_expiry(self) -> None:
        targets = {
            "connection:1": {
                "provider": "provider_1",
                "institution_id": 1,
                "route_provider": "provider_1",
            }
        }
        await self._store_job(1, "recovery-batch", targets, mode="auto")
        async with self._sessionmaker() as db:
            await db.execute(
                update(SyncBatchJob)
                .where(SyncBatchJob.batch_id == "recovery-batch")
                .values(
                    status="running",
                    lease_token="dead-process-owner",
                    lease_expires_at=sync_batch._utc_now() + timedelta(minutes=10),
                )
            )
            await db.commit()

        schedule = Mock()
        with patch.object(sync_batch, "_schedule_sync_batch", new=schedule):
            await sync_batch.resume_sync_batch_jobs(force_running=True)

        schedule.assert_called_once_with(1, "recovery-batch")
        async with self._sessionmaker() as db:
            row = (
                await db.execute(
                    select(SyncBatchJob).where(SyncBatchJob.batch_id == "recovery-batch")
                )
            ).scalar_one()
        self.assertEqual(row.status, "queued")
        self.assertIsNone(row.lease_token)
        self.assertIsNone(row.lease_expires_at)

    async def test_batch_lease_heartbeat_prevents_a_second_claim(self) -> None:
        targets = {
            "connection:1": {
                "provider": "provider_1",
                "institution_id": 1,
                "route_provider": "provider_1",
            }
        }
        await self._store_job(1, "heartbeat-batch", targets, mode="auto")
        owner_task = asyncio.current_task()
        self.assertIsNotNone(owner_task)
        with (
            patch.object(sync_batch, "SYNC_BATCH_LEASE_RENEW_INTERVAL_SECONDS", 0.005),
            patch.object(sync_batch, "SYNC_BATCH_LEASE_DURATION", timedelta(milliseconds=20)),
        ):
            first_claim = await sync_batch._claim_sync_batch(
                1,
                "heartbeat-batch",
                "owner-one",
            )
            self.assertIsNotNone(first_claim)
            async with self._sessionmaker() as db:
                initial_lease_expires_at = (
                    await db.execute(
                        select(SyncBatchJob.lease_expires_at).where(
                            SyncBatchJob.batch_id == "heartbeat-batch"
                        )
                    )
                ).scalar_one()
            heartbeat = asyncio.create_task(
                sync_batch._renew_sync_batch_lease(
                    1,
                    "heartbeat-batch",
                    "owner-one",
                    owner_task,
                )
            )
            try:
                for _ in range(100):
                    async with self._sessionmaker() as db:
                        renewed_lease_expires_at = (
                            await db.execute(
                                select(SyncBatchJob.lease_expires_at).where(
                                    SyncBatchJob.batch_id == "heartbeat-batch"
                                )
                            )
                        ).scalar_one()
                    if renewed_lease_expires_at > initial_lease_expires_at:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("sync batch lease heartbeat did not renew the lease")
                second_claim = await sync_batch._claim_sync_batch(
                    1,
                    "heartbeat-batch",
                    "owner-two",
                )
            finally:
                heartbeat.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await heartbeat

        self.assertIsNone(second_claim)

    async def test_batch_lease_heartbeat_is_paced_after_successful_renewals(self) -> None:
        targets = {
            "connection:1": {
                "provider": "provider_1",
                "institution_id": 1,
                "route_provider": "provider_1",
            }
        }
        await self._store_job(1, "paced-heartbeat-batch", targets, mode="auto")
        owner_task = asyncio.current_task()
        self.assertIsNotNone(owner_task)
        first_claim = await sync_batch._claim_sync_batch(
            1,
            "paced-heartbeat-batch",
            "owner-one",
        )
        self.assertIsNotNone(first_claim)

        session_calls = 0

        def counted_session():
            nonlocal session_calls
            session_calls += 1
            return self._sessionmaker()

        with (
            patch.object(sync_batch, "SYNC_BATCH_LEASE_RENEW_INTERVAL_SECONDS", 0.02),
            patch.object(sync_batch, "async_session", new=counted_session),
        ):
            heartbeat = asyncio.create_task(
                sync_batch._renew_sync_batch_lease(
                    1,
                    "paced-heartbeat-batch",
                    "owner-one",
                    owner_task,
                )
            )
            try:
                await asyncio.sleep(0.075)
            finally:
                heartbeat.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await heartbeat

        self.assertGreaterEqual(session_calls, 2)
        self.assertLessEqual(session_calls, 4)

    async def test_batch_target_validation_precedes_cleanup_and_scheduling(self) -> None:
        async with self._sessionmaker() as db:
            institution = await db.get(Institution, 2)
            institution.enabled = False
            await db.commit()
        cleanup = AsyncMock()
        schedule = Mock()
        publish = Mock()
        with (
            patch.object(sync_batch, "_cleanup_old_jobs", new=cleanup),
            patch.object(sync_batch, "_schedule_sync_batch", new=schedule),
            patch.object(sync_batch, "publish_event", new=publish),
        ):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                await sync_batch.start_sync_batch(
                    1,
                    [{"provider": "not-provider-1", "institution_id": 1}],
                )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                await sync_batch.start_sync_batch(
                    2,
                    [{"provider": "provider_1", "institution_id": 1}],
                )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                await sync_batch.start_sync_batch(
                    1,
                    [{"provider": "provider_2", "institution_id": 2}],
                )

        cleanup.assert_not_awaited()
        schedule.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
