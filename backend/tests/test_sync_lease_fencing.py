from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import ProviderSyncLease, User
from app.services import sync_utils


class _FailingSessionContext:
    async def __aenter__(self):
        raise OperationalError("renew provider lease", {}, RuntimeError("database busy"))

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class ProviderSyncLeaseFencingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/sync-lease.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as db:
            db.add(User(id=1))
            await db.commit()

        self._session_patch = patch.object(sync_utils, "async_session", self._sessionmaker)
        self._session_patch.start()

        @asynccontextmanager
        async def local_write_gate():
            yield

        self._write_gate_patch = patch(
            "app.services.sqlite_write_gate.sqlite_write_gate",
            new=local_write_gate,
        )
        self._write_gate_patch.start()

    async def asyncTearDown(self) -> None:
        self._write_gate_patch.stop()
        self._session_patch.stop()
        sync_utils._sync_locks.pop((1, "rbc", 7), None)
        sync_utils._sync_lock_renewal_tasks.pop((1, "rbc", 7), None)
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _store_lease(self, *, owner_token: str, expires_at: datetime) -> None:
        async with self._sessionmaker() as db:
            db.add(
                ProviderSyncLease(
                    user_id=1,
                    provider="rbc",
                    institution_key="7",
                    owner_token=owner_token,
                    acquired_at=datetime.now(timezone.utc),
                    expires_at=expires_at,
                )
            )
            await db.commit()

    @staticmethod
    def _start_owner() -> tuple[asyncio.Task[None], asyncio.Event, asyncio.Event]:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def owner() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        return asyncio.create_task(owner()), started, cancelled

    async def test_transient_renewal_error_retries_without_cancelling_owner(self) -> None:
        initial_expiry = datetime.now(timezone.utc) + timedelta(milliseconds=100)
        await self._store_lease(owner_token="owner-one", expires_at=initial_expiry)
        owner_task, owner_started, owner_cancelled = self._start_owner()
        await owner_started.wait()
        session_attempts = 0

        def flaky_session():
            nonlocal session_attempts
            session_attempts += 1
            if session_attempts == 1:
                return _FailingSessionContext()
            return self._sessionmaker()

        with (
            patch.object(sync_utils, "async_session", new=flaky_session),
            patch.object(sync_utils, "SYNC_LEASE_RENEW_INTERVAL_SECONDS", 0.05),
            patch.object(sync_utils, "SYNC_LEASE_RENEW_ERROR_RETRY_SECONDS", 0.005),
            patch.object(sync_utils, "SYNC_LEASE_DURATION", timedelta(seconds=1)),
        ):
            heartbeat = asyncio.create_task(
                sync_utils._renew_sync_lease(
                    (1, "rbc", 7),
                    "owner-one",
                    owner_task,
                    initial_expiry,
                )
            )
            renewed_expiry = initial_expiry
            try:
                for _ in range(100):
                    async with self._sessionmaker() as db:
                        renewed_expiry = (
                            await db.execute(select(ProviderSyncLease.expires_at))
                        ).scalar_one()
                    if renewed_expiry.tzinfo is None:
                        renewed_expiry = renewed_expiry.replace(tzinfo=timezone.utc)
                    if renewed_expiry > initial_expiry:
                        break
                    await asyncio.sleep(0.005)
                self.assertGreaterEqual(session_attempts, 2)
                self.assertGreater(renewed_expiry, initial_expiry)
            finally:
                heartbeat.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await heartbeat

        self.assertFalse(owner_task.done())
        self.assertFalse(owner_cancelled.is_set())

        owner_task.cancel()
        with suppress(asyncio.CancelledError):
            await owner_task

    async def test_token_takeover_cancels_and_unregisters_old_owner(self) -> None:
        initial_expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
        await self._store_lease(
            owner_token="owner-one",
            expires_at=initial_expiry,
        )
        lock_key = (1, "rbc", 7)
        sync_utils._sync_locks[lock_key] = "owner-one"
        owner_task, owner_started, owner_cancelled = self._start_owner()
        await owner_started.wait()

        async with self._sessionmaker() as db:
            await db.execute(
                update(ProviderSyncLease)
                .where(ProviderSyncLease.user_id == 1)
                .values(owner_token="owner-two")
            )
            await db.commit()

        with (
            patch.object(sync_utils, "SYNC_LEASE_RENEW_INTERVAL_SECONDS", 0.005),
            patch.object(sync_utils, "SYNC_LEASE_RENEW_ERROR_RETRY_SECONDS", 0.005),
            patch.object(sync_utils, "SYNC_LEASE_DURATION", timedelta(seconds=1)),
        ):
            heartbeat = asyncio.create_task(
                sync_utils._renew_sync_lease(
                    lock_key,
                    "owner-one",
                    owner_task,
                    initial_expiry,
                )
            )
            sync_utils._sync_lock_renewal_tasks[lock_key] = heartbeat
            await asyncio.wait_for(heartbeat, timeout=0.5)
            await asyncio.wait_for(owner_cancelled.wait(), timeout=0.5)
            with suppress(asyncio.CancelledError):
                await owner_task

        self.assertTrue(owner_task.cancelled())
        self.assertNotIn(lock_key, sync_utils._sync_locks)
        self.assertNotIn(lock_key, sync_utils._sync_lock_renewal_tasks)
        async with self._sessionmaker() as db:
            token = (
                await db.execute(select(ProviderSyncLease.owner_token))
            ).scalar_one()
        self.assertEqual(token, "owner-two")

    async def test_stale_local_lock_does_not_block_after_durable_expiry(self) -> None:
        await self._store_lease(
            owner_token="stale-owner",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        lock_key = (1, "rbc", 7)
        sync_utils._sync_locks[lock_key] = "stale-owner"

        with patch.object(
            sync_utils,
            "_start_sync_lease_renewal",
            return_value=True,
        ):
            acquired = await sync_utils.acquire_sync_lock(
                1,
                "rbc",
                institution_id=7,
                owner_token="new-owner",
            )

        self.assertTrue(acquired)
        self.assertEqual(sync_utils._sync_locks[lock_key], "new-owner")
        async with self._sessionmaker() as db:
            token = (
                await db.execute(select(ProviderSyncLease.owner_token))
            ).scalar_one()
        self.assertEqual(token, "new-owner")

    async def test_live_local_lock_still_blocks_competing_sync(self) -> None:
        initial_expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
        await self._store_lease(owner_token="owner-one", expires_at=initial_expiry)
        lock_key = (1, "rbc", 7)
        sync_utils._sync_locks[lock_key] = "owner-one"
        renewal_task = asyncio.create_task(asyncio.Event().wait())
        sync_utils._sync_lock_renewal_tasks[lock_key] = renewal_task
        try:
            acquired = await sync_utils.acquire_sync_lock(
                1,
                "rbc",
                institution_id=7,
                owner_token="owner-two",
            )
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task

        self.assertFalse(acquired)
        self.assertEqual(sync_utils._sync_locks[lock_key], "owner-one")

    async def test_controlled_recovery_removes_unexpired_dead_process_lease(self) -> None:
        await self._store_lease(
            owner_token="dead-process-owner",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

        self.assertEqual(await sync_utils.recover_expired_sync_leases(), 0)
        self.assertEqual(await sync_utils.recover_expired_sync_leases(force=True), 1)
        async with self._sessionmaker() as db:
            leases = (await db.execute(select(ProviderSyncLease))).scalars().all()
        self.assertEqual(leases, [])


if __name__ == "__main__":
    unittest.main()
