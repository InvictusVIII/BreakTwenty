from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.services import task_supervisor


class TaskSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await task_supervisor.stop_task_supervisor(timeout_seconds=0)
        task_supervisor._tasks.clear()

    async def asyncTearDown(self) -> None:
        await task_supervisor.stop_task_supervisor(timeout_seconds=0.1)
        task_supervisor._tasks.clear()

    async def test_closed_admission_closes_rejected_coroutine(self) -> None:
        async def fixture() -> None:
            return None

        coroutine = fixture()
        with self.assertRaisesRegex(RuntimeError, "admission is closed"):
            task_supervisor.create_tracked_task(coroutine, name="rejected")
        self.assertIsNone(coroutine.cr_frame)

    async def test_failed_task_is_observed_logged_and_removed(self) -> None:
        async def fail() -> None:
            raise ValueError("supervised failure")

        task_supervisor.start_task_supervisor()
        with patch.object(task_supervisor.logger, "error") as log_error:
            task = task_supervisor.create_tracked_task(fail(), name="failed-fixture")
            with self.assertRaisesRegex(ValueError, "supervised failure"):
                await task
            await asyncio.sleep(0)

        self.assertEqual(task_supervisor.supervised_task_count(), 0)
        log_error.assert_called_once()

    async def test_stop_cancels_and_drains_tasks_then_closes_admission(self) -> None:
        cancelled = asyncio.Event()

        async def wait_forever() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task_supervisor.start_task_supervisor()
        task_supervisor.create_tracked_task(wait_forever(), name="cancelled-fixture")
        await asyncio.sleep(0)
        await task_supervisor.stop_task_supervisor(timeout_seconds=1)

        self.assertTrue(cancelled.is_set())
        self.assertEqual(task_supervisor.supervised_task_count(), 0)
        coroutine = wait_forever()
        with self.assertRaisesRegex(RuntimeError, "admission is closed"):
            task_supervisor.create_tracked_task(coroutine)
        self.assertIsNone(coroutine.cr_frame)

    async def test_stop_returns_at_deadline_for_cancellation_resistant_task(self) -> None:
        release = asyncio.Event()

        async def resist_cancellation() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task_supervisor.start_task_supervisor()
        task = task_supervisor.create_tracked_task(
            resist_cancellation(),
            name="deadline-fixture",
        )
        await asyncio.sleep(0)
        with patch.object(task_supervisor.logger, "error") as log_error:
            await task_supervisor.stop_task_supervisor(timeout_seconds=0.001)

        self.assertEqual(task_supervisor.supervised_task_count(), 1)
        log_error.assert_called_once()
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertEqual(task_supervisor.supervised_task_count(), 0)


if __name__ == "__main__":
    unittest.main()
