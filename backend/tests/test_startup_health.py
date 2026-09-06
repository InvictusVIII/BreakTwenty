import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app import main as app_main
from app.api.routes.health import health_check
from app.services.startup_health import (
    STARTUP_COMPONENTS,
    get_startup_health,
    mark_startup_component_failed,
    mark_startup_component_ok,
    reset_startup_health,
)


class StartupHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_startup_health()

    async def test_health_reports_ok_when_startup_components_started(self) -> None:
        for component in STARTUP_COMPONENTS:
            mark_startup_component_ok(component)

        payload = await health_check(object())

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["startup"]["degraded_components"], [])
        self.assertTrue(
            all(
                state["status"] == "ok"
                for state in payload["startup"]["components"].values()
            )
        )

    async def test_health_reports_degraded_when_startup_component_failed(self) -> None:
        mark_startup_component_failed("scheduler", RuntimeError("schedule unavailable"))
        mark_startup_component_ok("transaction_import_resume")
        mark_startup_component_ok("transaction_import_watchdog")

        payload = await health_check(object())

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["startup"]["degraded_components"], ["scheduler"])
        scheduler = payload["startup"]["components"]["scheduler"]
        self.assertEqual(scheduler["status"], "failed")
        self.assertEqual(scheduler["error_type"], "RuntimeError")
        self.assertEqual(scheduler["error"], "schedule unavailable")

    async def test_lifespan_records_degraded_background_startup_and_stays_up(self) -> None:
        startup_order: list[str] = []

        async def unavailable_scheduler() -> None:
            startup_order.append("scheduler")
            raise RuntimeError("scheduler unavailable")

        def start_sampler() -> None:
            startup_order.append("market_strip_sampler")

        patches = (
            patch("app.main.verify_database_connection", new=AsyncMock()),
            patch(
                "app.main.recover_api_credential_replacements",
                new=AsyncMock(return_value=0),
            ),
            patch("app.main.start_api_credential_replacement_watchdog"),
            patch(
                "app.main.stop_api_credential_replacement_watchdog",
                new=AsyncMock(),
            ),
            patch("app.main.start_task_supervisor"),
            patch("app.main.stop_task_supervisor", new=AsyncMock()),
            patch("app.main.recover_expired_sync_leases", new=AsyncMock()),
            patch("app.main.start_sync_lease_watchdog"),
            patch("app.main.recover_expired_runtime_service_leases", new=AsyncMock()),
            patch("app.main.recover_expired_event_stream_leases", new=AsyncMock()),
            patch("app.main.start_market_strip_sampler", side_effect=start_sampler),
            patch("app.main.stop_market_strip_sampler", new=AsyncMock()),
            patch(
                "app.main.start_scheduler",
                new=AsyncMock(side_effect=unavailable_scheduler),
            ),
            patch("app.main.resume_transaction_import_jobs", new=AsyncMock()),
            patch("app.main.resume_sync_batch_jobs", new=AsyncMock()),
            patch("app.main.start_sync_batch_watchdog"),
            patch("app.main.stop_sync_batch_watchdog", new=AsyncMock()),
            patch(
                "app.main.start_transaction_import_watchdog",
                side_effect=RuntimeError("watchdog unavailable"),
            ),
            patch("app.main.stop_transaction_import_watchdog", new=AsyncMock()),
            patch("app.main.stop_scheduler"),
        )
        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            async with app_main.lifespan(object()):
                payload = get_startup_health()

        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(
            payload["degraded_components"],
            ["scheduler", "transaction_import_watchdog"],
        )
        self.assertEqual(
            payload["components"]["transaction_import_resume"]["status"],
            "ok",
        )
        self.assertEqual(startup_order, ["scheduler", "market_strip_sampler"])

    async def test_lifespan_service_stop_has_a_hard_deadline(self) -> None:
        never_stops = asyncio.Event()

        with patch.object(app_main.logger, "error") as log_error:
            await app_main._stop_lifespan_service(
                "stuck_watchdog",
                never_stops.wait,
                timeout_seconds=0.001,
            )

        log_error.assert_called_once_with(
            "backend service shutdown deadline exceeded service=%s",
            "stuck_watchdog",
        )


if __name__ == "__main__":
    unittest.main()
