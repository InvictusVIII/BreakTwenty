from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.services import support_logging
from app.services.support_logging import (
    DEFAULT_SUPPORT_CAPTURE_LEVEL,
    SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
)


class SupportLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self._capture_path_patch = patch.object(
            support_logging,
            "SUPPORT_CAPTURE_LEVEL_PATH",
            Path(self._tmpdir.name) / "support_capture_levels.json",
        )
        self._capture_path_patch.start()
        self._clear_state()

    def tearDown(self) -> None:
        self._clear_state()
        self._capture_path_patch.stop()
        self._tmpdir.cleanup()

    def _clear_state(self) -> None:
        with support_logging._support_logging_lock:
            support_logging._support_logging_state.clear()
            support_logging._support_logging_state_loaded = False

    def test_developer_capture_stays_on_until_explicitly_cleared(self) -> None:
        enabled = support_logging.set_support_capture_level(
            11,
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        self.assertEqual(enabled["capture_level"], SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL)
        self.assertNotIn("seconds_remaining", enabled)
        self.assertNotIn("expires_at", enabled)
        self.assertEqual(
            support_logging.get_support_capture_level("rbc", user_id=11),
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        cleared = support_logging.set_support_capture_level(11, DEFAULT_SUPPORT_CAPTURE_LEVEL)

        self.assertEqual(cleared["capture_level"], DEFAULT_SUPPORT_CAPTURE_LEVEL)
        self.assertEqual(
            support_logging.get_support_capture_level("rbc", user_id=11),
            DEFAULT_SUPPORT_CAPTURE_LEVEL,
        )

    def test_provider_capture_falls_back_to_user_wildcard(self) -> None:
        support_logging.set_support_capture_level(
            11,
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        self.assertEqual(
            support_logging.get_user_capture_level(11, provider="td")["capture_level"],
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

    def test_developer_capture_survives_in_memory_state_reload(self) -> None:
        support_logging.set_support_capture_level(
            11,
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        with support_logging._support_logging_lock:
            support_logging._support_logging_state.clear()
            support_logging._support_logging_state_loaded = False

        self.assertEqual(
            support_logging.get_support_capture_level("rbc", user_id=11),
            SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        support_logging.set_support_capture_level(11, DEFAULT_SUPPORT_CAPTURE_LEVEL)
        with support_logging._support_logging_lock:
            support_logging._support_logging_state.clear()
            support_logging._support_logging_state_loaded = False

        self.assertEqual(
            support_logging.get_support_capture_level("rbc", user_id=11),
            DEFAULT_SUPPORT_CAPTURE_LEVEL,
        )
