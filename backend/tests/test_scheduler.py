import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services import scheduler as scheduler_service


class FakeScheduler:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.jobs = []
        self.removed_all = False
        self.started = False

    def remove_all_jobs(self) -> None:
        self.jobs.clear()
        self.removed_all = True

    def add_job(self, func, **kwargs) -> None:
        self.jobs.append({"func": func, **kwargs})

    def start(self) -> None:
        self.started = True
        self.running = True


class SchedulerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def _start_with_fake_scheduler(self, *, nightly_enabled: bool) -> FakeScheduler:
        fake_scheduler = FakeScheduler(running=True)
        env = {"BREAKTWENTY_ENABLE_NIGHTLY_SYNC": "1" if nightly_enabled else ""}
        with (
            patch.dict("os.environ", env),
            patch("app.services.scheduler.scheduler", fake_scheduler),
            patch("app.services.scheduler.ensure_default_dev_user_exists", new=AsyncMock()),
            patch(
                "app.services.scheduler.list_scheduler_users",
                new=AsyncMock(return_value=[(1, "America/Toronto")]),
            ),
            patch(
                "app.services.categories.ensure_categorization_ruleset_current",
                new=AsyncMock(return_value=False),
            ),
            patch("app.services.scheduler.run_fx_daily_refresh", Mock(return_value=object())),
            patch("app.services.scheduler.create_tracked_task"),
        ):
            await scheduler_service.start_scheduler()
        return fake_scheduler

    async def test_start_scheduler_leaves_provider_nightly_sync_disabled_by_default(self) -> None:
        fake_scheduler = await self._start_with_fake_scheduler(nightly_enabled=False)

        job_ids = {job["id"] for job in fake_scheduler.jobs}

        self.assertTrue(fake_scheduler.removed_all)
        self.assertEqual(job_ids, {"fx_rates_daily_refresh"})

    async def test_start_scheduler_can_enable_provider_nightly_sync_for_saas(self) -> None:
        fake_scheduler = await self._start_with_fake_scheduler(nightly_enabled=True)

        job_ids = {job["id"] for job in fake_scheduler.jobs}

        self.assertIn("nightly_sync_user_1", job_ids)
        self.assertIn("fx_rates_daily_refresh", job_ids)


if __name__ == "__main__":
    unittest.main()
