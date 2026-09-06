from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.pool import AsyncAdaptedQueuePool

from app import database


class DatabaseObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        database._last_pool_state_log.clear()

    def test_sqlcipher_open_duration_and_pool_events_are_observed(self) -> None:
        record = SimpleNamespace(info={})
        with (
            patch.object(database.logger, "info") as info,
            patch.object(database, "_pool_status", return_value="checked out=1"),
        ):
            database._observe_sqlcipher_connection_open_started(None, record, (), {})
            record.info["breaktwenty_connection_open_started"] = time.perf_counter() - 0.01
            database._observe_sqlcipher_connection_open_completed(None, record)
            database._observe_sqlalchemy_pool_checkout(None, record, None)
            database._observe_sqlalchemy_pool_checkin(None, record)

        formats = [call.args[0] for call in info.call_args_list]
        self.assertIn("sqlcipher connection open started connection_id=%s", formats)
        self.assertIn(
            "sqlcipher connection open completed connection_id=%s duration=%.6fs",
            formats,
        )
        pool_events = [call.args[1] for call in info.call_args_list if call.args[0].startswith("sqlalchemy pool")]
        self.assertEqual(pool_events, ["connect", "checkout", "checkin"])

    def test_runtime_reuses_sqlcipher_connections_without_a_checkout_cap(self) -> None:
        pool = database.engine.sync_engine.pool

        self.assertIsInstance(pool, AsyncAdaptedQueuePool)
        self.assertEqual(pool.size(), database.SQLITE_RETAINED_POOL_CONNECTIONS)
        self.assertEqual(pool._max_overflow, -1)
