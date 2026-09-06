from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
import unittest
from unittest.mock import patch

import app.connectors.sdk_process as sdk_process
from app.connectors.sdk_process import (
    SdkProcessCrashed,
    SdkProcessError,
    SdkProcessTimeout,
    SdkProcessWorker,
)


HANDLER = "tests.sdk_process_test_helpers:TestSdkProcessHandler"


class SdkProcessWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_operation_runs_in_reused_child_process(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        try:
            first = await worker.call("echo", {"value": "first"}, timeout=2)
            second = await worker.call("echo", {"value": "second"}, timeout=2)
        finally:
            await worker.close()

        self.assertNotEqual(first["pid"], 0)
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(second["value"], "second")
        self.assertFalse(worker.is_alive)

    async def test_timeout_terminates_and_reaps_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        with self.assertRaises(SdkProcessTimeout):
            await worker.call("sleep", {"seconds": 30}, timeout=0.05)

        self.assertFalse(worker.is_alive)
        await worker.close()

    async def test_timeout_poisons_worker_instead_of_starting_new_session(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        with self.assertRaises(SdkProcessTimeout):
            await worker.call("sleep", {"seconds": 30}, timeout=0.05)

        with self.assertRaises(SdkProcessCrashed):
            await worker.call("echo", {"value": "new session"}, timeout=2)
        self.assertFalse(worker.is_alive)
        await worker.close()

    @unittest.skipIf(__import__("os").name == "nt", "SIGTERM is not available on Windows")
    async def test_timeout_escalates_from_ignored_terminate_to_kill(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        started = time.monotonic()
        with (
            patch.object(sdk_process, "SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05),
            patch.object(sdk_process, "SDK_PROCESS_KILL_TIMEOUT_SECONDS", 0.25),
        ):
            with self.assertRaises(SdkProcessTimeout):
                await worker.call(
                    "ignore_terminate_and_sleep",
                    {"seconds": 30},
                    timeout=0.05,
                )

        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.is_alive)
        self.assertEqual(worker.exitcode, -signal.SIGKILL)
        await worker.close()

    async def test_startup_handshake_keeps_event_loop_responsive(self) -> None:
        worker = SdkProcessWorker(HANDLER, {"init_delay": 0.15})
        heartbeat_count = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_count
            while not worker.is_alive or heartbeat_count < 3:
                heartbeat_count += 1
                await asyncio.sleep(0.01)

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await worker.start()
            await heartbeat_task
        finally:
            heartbeat_task.cancel()
            await worker.close()

        self.assertGreaterEqual(heartbeat_count, 3)

    async def test_oversized_initialization_is_rejected_before_spawn(self) -> None:
        worker = SdkProcessWorker(
            HANDLER,
            {"value": "x" * sdk_process.SDK_PROCESS_MAX_REQUEST_BYTES},
        )

        with self.assertRaises(SdkProcessError):
            await worker.start()

        self.assertIsNone(worker.pid)
        self.assertFalse(worker.is_alive)
        await worker.close()

    async def test_cancellation_terminates_and_reaps_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        task = asyncio.create_task(
            worker.call("sleep", {"seconds": 30}, timeout=60)
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(worker.is_alive)
        await worker.close()

    async def test_deadline_bounds_receive_after_pipe_becomes_readable(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        loop = asyncio.get_running_loop()

        async def blocking_recv(_socket, _size):
            await asyncio.sleep(30)
            return b""

        with patch.object(loop, "sock_recv", new=blocking_recv):
            started = time.monotonic()
            with self.assertRaises(SdkProcessTimeout):
                await worker.call(
                    "large_result",
                    {"bytes": 4 * 1024 * 1024},
                    timeout=0.05,
                )

        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.is_alive)
        await worker.close()

    async def test_deadline_bounds_blocked_request_send_and_reaps_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        if os.name != "nt":
            await worker.call("ignore_terminate", timeout=2)
        loop = asyncio.get_running_loop()
        heartbeat_count = 0

        async def blocking_send(_socket, _payload):
            await asyncio.sleep(30)

        async def heartbeat() -> None:
            nonlocal heartbeat_count
            while True:
                heartbeat_count += 1
                await asyncio.sleep(0.005)

        heartbeat_task = asyncio.create_task(heartbeat())
        started = time.monotonic()
        try:
            with (
                patch.object(loop, "sock_sendall", new=blocking_send),
                patch.object(sdk_process, "SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05),
                patch.object(sdk_process, "SDK_PROCESS_KILL_TIMEOUT_SECONDS", 0.25),
            ):
                with self.assertRaises(SdkProcessTimeout):
                    await worker.call("echo", {"value": "blocked"}, timeout=0.05)
        finally:
            heartbeat_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await heartbeat_task

        self.assertLess(time.monotonic() - started, 1)
        self.assertGreater(heartbeat_count, 3)
        self.assertFalse(worker.is_alive)
        if os.name != "nt":
            self.assertEqual(worker.exitcode, -signal.SIGKILL)
        await worker.close()

    async def test_crash_is_reported_and_reaped(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        with self.assertRaises(SdkProcessCrashed):
            await worker.call("crash", timeout=2)

        self.assertFalse(worker.is_alive)
        await worker.close()

    async def test_remote_error_does_not_poison_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        try:
            with self.assertRaises(SdkProcessError) as raised:
                await worker.call("error", timeout=2)
            result = await worker.call("echo", {"value": "alive"}, timeout=2)
        finally:
            await worker.close()

        self.assertEqual(raised.exception.remote_type, "ValueError")
        self.assertEqual(result["value"], "alive")

    async def test_oversized_request_is_rejected_without_poisoning_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        try:
            with self.assertRaises(SdkProcessError):
                await worker.call(
                    "echo",
                    {"value": "x" * sdk_process.SDK_PROCESS_MAX_REQUEST_BYTES},
                    timeout=2,
                )
            result = await worker.call("echo", {"value": "alive"}, timeout=2)
        finally:
            await worker.close()

        self.assertEqual(result["value"], "alive")

    async def test_oversized_response_is_rejected_without_poisoning_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        try:
            with self.assertRaises(SdkProcessError) as raised:
                await worker.call(
                    "large_result",
                    {"bytes": sdk_process.SDK_PROCESS_MAX_RESPONSE_BYTES + 1},
                    timeout=5,
                )
            result = await worker.call("echo", {"value": "alive"}, timeout=2)
        finally:
            await worker.close()

        self.assertEqual(raised.exception.remote_type, "_SdkProcessTransportError")
        self.assertEqual(result["value"], "alive")

    async def test_shutdown_timeout_is_bounded_and_reaps_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER, {"hang_close": True})
        await worker.start()
        started = time.monotonic()
        with (
            patch.object(sdk_process, "SDK_PROCESS_CLOSE_TIMEOUT_SECONDS", 0.05),
            patch.object(sdk_process, "SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05),
            patch.object(sdk_process, "SDK_PROCESS_KILL_TIMEOUT_SECONDS", 0.05),
        ):
            await worker.close()

        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.is_alive)

    async def test_shutdown_blocked_send_is_bounded_and_reaps_worker(self) -> None:
        worker = SdkProcessWorker(HANDLER)
        await worker.start()
        loop = asyncio.get_running_loop()

        async def blocking_send(_socket, _payload):
            await asyncio.sleep(30)

        started = time.monotonic()
        with (
            patch.object(loop, "sock_sendall", new=blocking_send),
            patch.object(sdk_process, "SDK_PROCESS_CLOSE_TIMEOUT_SECONDS", 0.05),
            patch.object(sdk_process, "SDK_PROCESS_TERMINATE_TIMEOUT_SECONDS", 0.05),
            patch.object(sdk_process, "SDK_PROCESS_KILL_TIMEOUT_SECONDS", 0.05),
        ):
            await worker.close()

        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(worker.is_alive)

    async def test_shutdown_cancellation_is_propagated_after_worker_reap(self) -> None:
        worker = SdkProcessWorker(HANDLER, {"hang_close": True})
        await worker.start()
        close_task = asyncio.create_task(worker.close())
        await asyncio.sleep(0.05)
        close_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await close_task
        self.assertFalse(worker.is_alive)

    async def test_capacity_rejects_excess_worker_and_recovers_after_close(self) -> None:
        capacity = threading.BoundedSemaphore(1)
        first = SdkProcessWorker(HANDLER)
        second = SdkProcessWorker(HANDLER)
        third = SdkProcessWorker(HANDLER)
        with patch.object(sdk_process, "_SDK_PROCESS_CAPACITY", capacity):
            await first.start()
            with self.assertRaises(SdkProcessError):
                await second.start()
            await first.close()
            await third.start()
            await third.close()
            await second.close()

        self.assertFalse(first.is_alive)
        self.assertFalse(third.is_alive)


if __name__ == "__main__":
    unittest.main()
