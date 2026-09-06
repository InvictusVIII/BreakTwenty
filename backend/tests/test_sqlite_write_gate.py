from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app.services import sqlite_write_gate as gate


class SQLiteWriteGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gate._sqlite_write_condition = asyncio.Condition()
        gate._sqlite_write_active = False
        gate._sqlite_waiting_background = 0
        gate._sqlite_waiting_interactive = 0
        gate._sqlite_write_owner = None
        gate._sqlite_write_waiters = {}
        gate._sqlite_last_state_sample_at = 0.0

    def tearDown(self) -> None:
        gate._sqlite_write_condition = asyncio.Condition()
        gate._sqlite_write_active = False
        gate._sqlite_waiting_background = 0
        gate._sqlite_waiting_interactive = 0
        gate._sqlite_write_owner = None
        gate._sqlite_write_waiters = {}
        gate._sqlite_last_state_sample_at = 0.0

    async def test_write_gate_is_enabled_by_default_for_sqlite(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(gate.database, "DATABASE_URL", "sqlite+sqlcipher_aiosqlite:///test.db"),
        ):
            os.environ.pop("BREAKTWENTY_SQLITE_WRITE_GATE", None)
            self.assertTrue(gate.sqlite_write_gate_enabled())

    async def test_write_gate_can_be_explicitly_disabled(self) -> None:
        events: list[str] = []
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def holder() -> None:
            async with gate.sqlite_write_gate(label="holder"):
                events.append("holder-enter")
                holder_entered.set()
                await release_holder.wait()
                events.append("holder-exit")

        async def concurrent_writer() -> None:
            await holder_entered.wait()
            async with gate.sqlite_write_gate(label="concurrent"):
                events.append("concurrent-enter")

        with (
            patch.dict(os.environ, {"BREAKTWENTY_SQLITE_WRITE_GATE": "off"}),
            patch.object(gate.database, "DATABASE_URL", "sqlite+sqlcipher_aiosqlite:///test.db"),
        ):
            holder_task = asyncio.create_task(holder(), name="request:holder")
            concurrent_task = asyncio.create_task(concurrent_writer(), name="request:concurrent")
            await holder_entered.wait()
            await asyncio.wait_for(concurrent_task, timeout=1)
            release_holder.set()
            await holder_task

        self.assertEqual(events, ["holder-enter", "concurrent-enter", "holder-exit"])

    async def test_interactive_write_runs_before_waiting_background_write(self) -> None:
        events: list[str] = []
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        background_attempting = asyncio.Event()

        async def holder() -> None:
            async with gate.sqlite_write_gate(label="holder"):
                events.append("holder-enter")
                holder_entered.set()
                await release_holder.wait()

        async def background_waiter() -> None:
            background_attempting.set()
            async with gate.sqlite_write_gate(label="background"):
                events.append("background-enter")

        async def interactive_waiter() -> None:
            async with gate.sqlite_write_gate(label="interactive"):
                events.append("interactive-enter")

        with patch.object(gate, "sqlite_write_gate_enabled", return_value=True):
            holder_task = asyncio.create_task(holder(), name="request:holder")
            await holder_entered.wait()

            background_task = asyncio.create_task(
                background_waiter(),
                name="sync-batch:test",
            )
            await background_attempting.wait()
            await asyncio.sleep(0)

            interactive_task = asyncio.create_task(
                interactive_waiter(),
                name="request:interactive",
            )
            await asyncio.sleep(0)

            release_holder.set()
            await asyncio.gather(holder_task, background_task, interactive_task)

        self.assertEqual(
            events,
            ["holder-enter", "interactive-enter", "background-enter"],
        )

    async def test_write_gate_reports_owner_and_waiter_without_changing_admission(self) -> None:
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        waiter_started = asyncio.Event()

        async def holder() -> None:
            async with gate.sqlite_write_gate(label="diagnostic-holder"):
                holder_entered.set()
                await release_holder.wait()

        async def waiter() -> None:
            waiter_started.set()
            async with gate.sqlite_write_gate(label="diagnostic-waiter"):
                pass

        with (
            patch.object(gate, "sqlite_write_gate_enabled", return_value=True),
            patch.object(gate.logger, "info") as info,
        ):
            holder_task = asyncio.create_task(holder(), name="request:holder")
            await holder_entered.wait()
            waiter_task = asyncio.create_task(waiter(), name="sync-batch:waiter")
            await waiter_started.wait()
            await asyncio.sleep(0)

            state = gate.sqlite_write_gate_state()
            self.assertEqual(state["owner"]["label"], "diagnostic-holder")
            self.assertEqual(
                [item["label"] for item in state["waiters"]],
                ["diagnostic-waiter"],
            )
            self.assertEqual(state["waiting_background"], 1)
            self.assertTrue(any(call.args[1] == "waiter" for call in info.call_args_list))

            release_holder.set()
            await asyncio.gather(holder_task, waiter_task)

        self.assertFalse(gate.sqlite_write_gate_state()["active"])

    async def test_write_gate_is_reentrant_for_nested_writes(self) -> None:
        events: list[str] = []

        with patch.object(gate, "sqlite_write_gate_enabled", return_value=True):
            async with gate.sqlite_write_gate(label="outer"):
                events.append("outer")
                async with gate.sqlite_write_gate(label="inner"):
                    events.append("inner")

        self.assertEqual(events, ["outer", "inner"])

    async def test_retry_sqlite_busy_retries_transient_locked_error(self) -> None:
        attempts = 0
        rollbacks = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("commit", {}, RuntimeError("database is locked"))
            return "ok"

        async def rollback() -> None:
            nonlocal rollbacks
            rollbacks += 1

        with patch.object(gate.logger, "warning"):
            result = await gate.retry_sqlite_busy(
                operation,
                label="test",
                rollback=rollback,
                attempts=2,
                base_delay_seconds=0,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(rollbacks, 1)

    async def test_retry_sqlite_busy_does_not_retry_other_operational_errors(self) -> None:
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise OperationalError("select", {}, RuntimeError("no such table: proof"))

        with self.assertRaises(OperationalError):
            await gate.retry_sqlite_busy(
                operation,
                label="test",
                attempts=3,
                base_delay_seconds=0,
            )

        self.assertEqual(attempts, 1)
