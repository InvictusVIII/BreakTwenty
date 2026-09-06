from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, call, patch

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import EventStreamLease, MarketStripSnapshotPoint, RuntimeServiceLease, User
from app.services import event_bus, market_strip, runtime_service_leases, task_supervisor


class RuntimeServiceContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/runtime-contracts.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as db:
            db.add_all(User(id=user_id) for user_id in (1, 2, 3))
            await db.commit()

        self._write_lock = asyncio.Lock()

        @asynccontextmanager
        async def local_write_gate():
            async with self._write_lock:
                yield

        self._patches = [
            patch.object(event_bus, "async_session", self._sessionmaker),
            patch.object(event_bus, "sqlite_write_gate", new=local_write_gate),
            patch.object(runtime_service_leases, "async_session", self._sessionmaker),
            patch.object(runtime_service_leases, "sqlite_write_gate", new=local_write_gate),
            patch.object(market_strip, "async_session", self._sessionmaker),
            patch.object(market_strip, "sqlite_write_gate", new=local_write_gate),
        ]
        for active_patch in self._patches:
            active_patch.start()
        task_supervisor.start_task_supervisor()
        market_strip._market_strip_cache.clear()
        market_strip._market_strip_tile_cache.clear()

    async def asyncTearDown(self) -> None:
        await market_strip.stop_market_strip_sampler()
        await task_supervisor.stop_task_supervisor(timeout_seconds=1)
        event_bus._subscriptions.clear()
        for active_patch in reversed(self._patches):
            active_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _lease_count(self, model) -> int:
        async with self._sessionmaker() as db:
            return int((await db.scalar(select(func.count()).select_from(model))) or 0)

    async def test_runtime_service_lease_has_single_owner_and_expiry_takeover(self) -> None:
        async def attempt():
            return await runtime_service_leases.acquire_runtime_service_lease(
                "fixture-service",
                duration=timedelta(seconds=90),
            )

        @asynccontextmanager
        async def independent_worker_gate():
            yield

        with patch.object(
            runtime_service_leases,
            "sqlite_write_gate",
            new=independent_worker_gate,
        ):
            contenders = await asyncio.gather(*(attempt() for _ in range(8)))
        winners = [ownership for ownership in contenders if ownership is not None]
        self.assertEqual(len(winners), 1)
        first = winners[0]
        self.assertEqual(await self._lease_count(RuntimeServiceLease), 1)

        async with self._sessionmaker() as db, db.begin():
            await db.execute(
                update(RuntimeServiceLease)
                .where(RuntimeServiceLease.service_name == "fixture-service")
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )

        second = await attempt()
        self.assertIsNotNone(second)
        self.assertNotEqual(first.owner_token, second.owner_token)
        self.assertIsNone(
            await runtime_service_leases.renew_runtime_service_lease(
                first,
                duration=timedelta(seconds=90),
            )
        )
        self.assertFalse(await runtime_service_leases.release_runtime_service_lease(first))
        self.assertIsNotNone(
            await runtime_service_leases.renew_runtime_service_lease(
                second,
                duration=timedelta(seconds=90),
            )
        )
        self.assertTrue(await runtime_service_leases.release_runtime_service_lease(second))
        self.assertEqual(await self._lease_count(RuntimeServiceLease), 0)

    async def test_controlled_recovery_clears_unexpired_runtime_lease(self) -> None:
        ownership = await runtime_service_leases.acquire_runtime_service_lease(
            "recovery-fixture-service",
            duration=timedelta(minutes=10),
        )
        self.assertIsNotNone(ownership)
        self.assertEqual(await runtime_service_leases.recover_expired_runtime_service_leases(), 0)
        self.assertEqual(
            await runtime_service_leases.recover_expired_runtime_service_leases(force=True),
            1,
        )
        self.assertEqual(await self._lease_count(RuntimeServiceLease), 0)

    async def test_event_stream_atomic_admission_enforces_global_and_user_slots(self) -> None:
        async def attempt(user_id: int):
            try:
                return user_id, await event_bus._acquire_event_stream_lease(user_id)
            except event_bus.SubscriptionLimitError as exc:
                return user_id, exc

        @asynccontextmanager
        async def independent_worker_gate():
            yield

        with (
            patch.object(event_bus, "SSE_MAX_SUBSCRIPTIONS_PER_USER", 2),
            patch.object(event_bus, "SSE_MAX_SUBSCRIPTIONS_GLOBAL", 3),
            patch.object(
                event_bus,
                "sqlite_write_gate",
                new=independent_worker_gate,
            ),
        ):
            results = await asyncio.gather(
                *(attempt(user_id) for user_id in (1, 1, 1, 2, 2, 3, 3))
            )

        successes = [(user_id, value) for user_id, value in results if isinstance(value, str)]
        failures = [value for _user_id, value in results if isinstance(value, Exception)]
        self.assertEqual(len(successes), 3)
        self.assertGreaterEqual(len(failures), 1)
        self.assertLessEqual(sum(user_id == 1 for user_id, _value in successes), 2)
        async with self._sessionmaker() as db:
            leases = (await db.execute(select(EventStreamLease))).scalars().all()
        self.assertEqual(len(leases), 3)
        self.assertEqual(len({lease.global_slot for lease in leases}), 3)
        self.assertEqual(
            len({(lease.user_id, lease.user_slot) for lease in leases}),
            3,
        )

        async with self._sessionmaker() as db, db.begin():
            await db.execute(
                update(EventStreamLease).values(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                )
            )
        self.assertEqual(await event_bus.recover_expired_event_stream_leases(), 3)
        self.assertEqual(await self._lease_count(EventStreamLease), 0)

    async def test_controlled_recovery_clears_unexpired_event_stream_lease(self) -> None:
        lease_id = await event_bus._acquire_event_stream_lease(1)
        self.assertTrue(lease_id)
        self.assertEqual(await event_bus.recover_expired_event_stream_leases(), 0)
        self.assertEqual(await event_bus.recover_expired_event_stream_leases(force=True), 1)
        self.assertEqual(await self._lease_count(EventStreamLease), 0)

    async def test_event_overflow_wakes_stream_and_promptly_releases_database_slot(self) -> None:
        with patch.object(event_bus, "SSE_QUEUE_MAXSIZE", 1):
            subscription = await event_bus.subscribe(1)
        event_bus.publish_event(1, "first", {})
        event_bus.publish_event(1, "overflow", {})

        self.assertTrue(subscription.closed)
        self.assertIsNotNone(subscription.cleanup_task)
        await asyncio.wait_for(subscription.cleanup_task, timeout=1)
        self.assertTrue(subscription.lease_released)
        self.assertEqual(await self._lease_count(EventStreamLease), 0)
        self.assertEqual((await subscription.queue.get())["_kind"], "closed")
        await event_bus.unsubscribe(subscription)

    async def test_event_renewal_loss_closes_stream_and_releases_slot(self) -> None:
        with (
            patch.object(event_bus, "SSE_LEASE_DURATION", timedelta(seconds=0.2)),
            patch.object(event_bus, "SSE_LEASE_RENEW_INTERVAL_SECONDS", 0.01),
            patch.object(event_bus, "SSE_LEASE_RENEW_ERROR_RETRY_SECONDS", 0.005),
        ):
            subscription = await event_bus.subscribe(1)
            async with self._sessionmaker() as db, db.begin():
                await db.execute(
                    delete(EventStreamLease).where(
                        EventStreamLease.lease_id == subscription.lease_id
                    )
                )
            await asyncio.wait_for(subscription.renewal_task, timeout=1)

        self.assertTrue(subscription.closed)
        self.assertIsNotNone(subscription.cleanup_task)
        await asyncio.wait_for(subscription.cleanup_task, timeout=1)
        self.assertTrue(subscription.lease_released)
        self.assertEqual(await self._lease_count(EventStreamLease), 0)
        self.assertEqual((await subscription.queue.get())["_kind"], "closed")
        await event_bus.unsubscribe(subscription)

    async def test_dirty_notifications_fire_after_commit_but_not_rollback(self) -> None:
        with patch.object(event_bus, "mark_dirty") as mark_dirty:
            async with self._sessionmaker() as db:
                event_bus.mark_dirty_after_commit(db, 1, "accounts", "transactions")
                mark_dirty.assert_not_called()
                await db.commit()

            self.assertEqual(
                mark_dirty.call_args_list,
                [
                    call(1, "accounts"),
                    call(1, "transactions"),
                ],
            )
            mark_dirty.reset_mock()

            async with self._sessionmaker() as db:
                await db.begin()
                event_bus.mark_dirty_after_commit(db, 1, "accounts")
                await db.rollback()
                await db.commit()

            mark_dirty.assert_not_called()

    async def test_market_snapshot_writes_are_token_fenced_across_takeover(self) -> None:
        ownership = await runtime_service_leases.acquire_runtime_service_lease(
            market_strip.MARKET_STRIP_SAMPLER_SERVICE_NAME,
            duration=timedelta(seconds=90),
        )
        self.assertIsNotNone(ownership)
        now = datetime.now(timezone.utc)
        candidate = market_strip.MarketStripSnapshotCandidate(
            user_id=1,
            provider="fixture",
            source="user",
            mode="user_key",
            symbol_key="sp500",
            local_date=date(2026, 8, 13),
            sample_slot="2026-08-13T12:00:00+00:00",
            close=Decimal("100.00"),
            observed_at=now,
        )
        self.assertTrue(
            await market_strip._persist_market_strip_snapshot_candidates(
                ownership,
                [candidate],
            )
        )
        stale = runtime_service_leases.RuntimeLeaseOwnership(
            service_name=ownership.service_name,
            owner_token="stale-owner",
            expires_at=ownership.expires_at,
        )
        self.assertFalse(
            await market_strip._persist_market_strip_snapshot_candidates(stale, [candidate])
        )

        async with self._sessionmaker() as db, db.begin():
            await db.execute(
                update(RuntimeServiceLease)
                .where(RuntimeServiceLease.service_name == ownership.service_name)
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        self.assertFalse(
            await market_strip._persist_market_strip_snapshot_candidates(
                ownership,
                [candidate],
            )
        )
        takeover = await runtime_service_leases.acquire_runtime_service_lease(
            market_strip.MARKET_STRIP_SAMPLER_SERVICE_NAME,
            duration=timedelta(seconds=90),
        )
        self.assertIsNotNone(takeover)
        replacement = market_strip.MarketStripSnapshotCandidate(
            **{**candidate.__dict__, "close": Decimal("200.00")}
        )
        self.assertTrue(
            await market_strip._persist_market_strip_snapshot_candidates(
                takeover,
                [replacement],
            )
        )
        async with self._sessionmaker() as db:
            points = (await db.execute(select(MarketStripSnapshotPoint))).scalars().all()
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].close, Decimal("200.00000000"))

    async def test_market_strip_get_executes_only_read_statements(self) -> None:
        statements: list[str] = []
        commits: list[bool] = []
        flushes: list[bool] = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(str(statement).strip())

        event.listen(self._engine.sync_engine, "before_cursor_execute", capture_statement)
        tile_payload = {
            "id": "sp500",
            "label": "S&P 500",
            "status": "no_data",
            "points": [],
        }
        try:
            with patch.object(
                market_strip,
                "_fetch_tile_cached",
                new=AsyncMock(return_value=tile_payload),
            ):
                async with self._sessionmaker() as db:
                    event.listen(
                        db.sync_session,
                        "after_commit",
                        lambda _session: commits.append(True),
                    )
                    event.listen(
                        db.sync_session,
                        "before_flush",
                        lambda _session, _context, _instances: flushes.append(True),
                    )
                    payload = await market_strip.get_market_strip_payload(db, 1)
        finally:
            event.remove(self._engine.sync_engine, "before_cursor_execute", capture_statement)

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(statements)
        self.assertEqual(commits, [])
        self.assertEqual(flushes, [])
        write_verbs = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}
        self.assertFalse(
            [statement for statement in statements if statement.split(None, 1)[0].upper() in write_verbs]
        )

    async def test_sampler_lease_loss_cancels_inflight_pass_and_releases_token(self) -> None:
        ownership = runtime_service_leases.RuntimeLeaseOwnership(
            service_name=market_strip.MARKET_STRIP_SAMPLER_SERVICE_NAME,
            owner_token="owner-token",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
        )
        sample_started = asyncio.Event()
        sample_cancelled = asyncio.Event()

        async def lose_lease(state) -> None:
            await sample_started.wait()
            state.lost.set()

        async def blocking_sample(_state) -> None:
            sample_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sample_cancelled.set()
                raise

        with (
            patch.object(
                market_strip,
                "_renew_market_strip_sampler_lease",
                new=lose_lease,
            ),
            patch.object(
                market_strip,
                "_sample_market_strip_for_all_users",
                new=blocking_sample,
            ),
            patch.object(
                market_strip,
                "release_runtime_service_lease",
                new=AsyncMock(return_value=True),
            ) as release,
        ):
            await asyncio.wait_for(
                market_strip._run_owned_market_strip_sampler(ownership),
                timeout=1,
            )

        self.assertTrue(sample_cancelled.is_set())
        release.assert_awaited_once_with(ownership)

    async def test_sampler_enumerates_existing_users_read_only_and_does_nothing_when_empty(self) -> None:
        async with self._sessionmaker() as db, db.begin():
            await db.execute(delete(User))
        statements: list[str] = []

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(str(statement).strip())

        event.listen(self._engine.sync_engine, "before_cursor_execute", capture_statement)
        collect = AsyncMock()
        state = market_strip._MarketStripSamplerLeaseState(
            ownership=runtime_service_leases.RuntimeLeaseOwnership(
                service_name=market_strip.MARKET_STRIP_SAMPLER_SERVICE_NAME,
                owner_token="owner-token",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
            ),
            lost=asyncio.Event(),
        )
        try:
            with (
                patch.object(
                    market_strip,
                    "_is_market_strip_provider_fetch_window",
                    return_value=True,
                ),
                patch.object(
                    market_strip,
                    "_collect_market_strip_snapshot_candidates",
                    new=collect,
                ),
            ):
                await market_strip._sample_market_strip_for_all_users(state)
        finally:
            event.remove(self._engine.sync_engine, "before_cursor_execute", capture_statement)

        collect.assert_not_awaited()
        self.assertTrue(statements)
        self.assertTrue(all(statement.upper().startswith("SELECT") for statement in statements))

    async def test_sampler_does_nothing_outside_market_hours(self) -> None:
        collect = AsyncMock()
        state = market_strip._MarketStripSamplerLeaseState(
            ownership=runtime_service_leases.RuntimeLeaseOwnership(
                service_name=market_strip.MARKET_STRIP_SAMPLER_SERVICE_NAME,
                owner_token="owner-token",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=90),
            ),
            lost=asyncio.Event(),
        )

        with (
            patch.object(
                market_strip,
                "_is_market_strip_provider_fetch_window",
                return_value=False,
            ),
            patch.object(
                market_strip,
                "_collect_market_strip_snapshot_candidates",
                new=collect,
            ),
        ):
            await market_strip._sample_market_strip_for_all_users(state)

        collect.assert_not_awaited()

    async def test_sampler_start_and_stop_are_supervised_and_drain_task(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocking_sampler() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch.object(market_strip, "_run_market_strip_sampler", new=blocking_sampler):
            market_strip.start_market_strip_sampler()
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertGreaterEqual(task_supervisor.supervised_task_count(), 1)
            await market_strip.stop_market_strip_sampler()

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(market_strip._market_strip_sampler_task)


if __name__ == "__main__":
    unittest.main()
