import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.routes import settings as settings_route
from app.launch_auth import AuthPrincipal
from app.services import support_auto_archive
from app.services.sync_tracking import (
    finalize_provider_sync_attempt,
    list_user_sync_attempt_ids,
    start_provider_sync_attempt,
)


class VisibleAuthSupportArchiveTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _principal(*, provider: str = "amex", attempt_id: str = "attempt-1"):
        return AuthPrincipal(
            kind="runner-session",
            user_id=1,
            provider=provider,
            attempt_id=attempt_id,
            purpose="visible-auth",
        )

    @staticmethod
    def _request(principal):
        return SimpleNamespace(state=SimpleNamespace(auth_principal=principal))

    def test_terminal_connector_phase_clears_unscoped_visible_auth_attempt(self):
        user_id = 981_001
        sync_id = "eqbank-visible-auth-scope-handoff"
        start_provider_sync_attempt(user_id, "eqbank", sync_id=sync_id)
        start_provider_sync_attempt(
            user_id,
            "eqbank",
            sync_id=sync_id,
            institution_id=12,
        )

        self.assertIn(sync_id, list_user_sync_attempt_ids(user_id))
        finalized = finalize_provider_sync_attempt(
            user_id,
            "eqbank",
            sync_id=sync_id,
            status="ok",
            institution_id=12,
        )

        self.assertEqual(sync_id, finalized)
        self.assertNotIn(sync_id, list_user_sync_attempt_ids(user_id))

    async def test_terminal_visible_auth_failure_waits_for_runner_finalization(self):
        principal = self._principal()
        with patch.object(settings_route, "ensure_provider_sync_attempt", return_value={"sync_id": "sync-1"}), \
            patch.object(settings_route, "log_scraper_event"), \
            patch.object(settings_route, "finalize_provider_sync_attempt") as finalize_attempt, \
            patch.object(settings_route, "archive_run_fire_and_forget") as archive_run:
            response = await settings_route.log_support_client_event(
                {
                    "source": "desktop_visible_auth",
                    "provider": "amex",
                    "sync_id": "sync-1",
                    "attempt_id": "attempt-1",
                    "stage": "sync result",
                    "result": "failed",
                    "message": "Login window closed",
                },
                self._request(principal),
                current_user=SimpleNamespace(id=1),
                principal=principal,
            )

        self.assertEqual({"status": "ok", "sync_id": "sync-1"}, response)
        finalize_attempt.assert_not_called()
        archive_run.assert_not_called()

    async def test_finalized_visible_auth_failure_creates_archive_request(self):
        principal = AuthPrincipal(kind="desktop", user_id=1)
        with patch.object(settings_route, "ensure_provider_sync_attempt", return_value={"sync_id": "sync-1"}), \
            patch.object(settings_route, "log_scraper_event"), \
            patch.object(settings_route, "finalize_provider_sync_attempt") as finalize_attempt, \
            patch.object(settings_route, "archive_run_fire_and_forget") as archive_run:
            response = await settings_route.log_support_client_event(
                {
                    "source": "desktop_visible_auth",
                    "provider": "amex",
                    "sync_id": "sync-1",
                    "attempt_id": "attempt-1",
                    "stage": "runner finalized",
                    "result": "failed",
                    "message": "Login window closed",
                },
                self._request(principal),
                current_user=SimpleNamespace(id=1),
                principal=principal,
            )

        self.assertEqual({"status": "ok", "sync_id": "sync-1"}, response)
        finalize_attempt.assert_called_once_with(1, "amex", sync_id="sync-1", status="error")
        archive_run.assert_called_once_with(
            user_id=1,
            provider="amex",
            sync_id="sync-1",
            attempt_id="attempt-1",
            trigger="visible_auth_failed",
            error="Login window closed",
            extra_fields={
                "source": "desktop_visible_auth",
                "flow": "existing",
                "attempt_id": "attempt-1",
                "result": "failed",
                "result_status": "error",
                "sync_source": "manual_reauthentication",
            },
            diagnostics_settle_seconds=0,
        )

    async def test_visible_auth_success_does_not_create_pre_handoff_archive(self):
        principal = self._principal()
        with patch.object(settings_route, "ensure_provider_sync_attempt", return_value={"sync_id": "sync-1"}), \
            patch.object(settings_route, "log_scraper_event"), \
            patch.object(settings_route, "finalize_provider_sync_attempt") as finalize_attempt, \
            patch.object(settings_route, "archive_run_fire_and_forget") as archive_run:
            await settings_route.log_support_client_event(
                {
                    "source": "desktop_visible_auth",
                    "provider": "amex",
                    "sync_id": "sync-1",
                    "attempt_id": "attempt-1",
                    "stage": "sync result",
                    "result": "succeeded",
                },
                self._request(principal),
                current_user=SimpleNamespace(id=1),
                principal=principal,
            )

        finalize_attempt.assert_not_called()
        archive_run.assert_not_called()

    async def test_run_listing_hides_active_sync_and_transaction_phases(self):
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [("job-sync", "source-sync")])
            )
        )
        completed_runs = [
            {"run_id": "ready", "sync_id": "ready-sync"},
            {"run_id": "connector-active", "sync_id": "connector-sync"},
            {"run_id": "job-active", "sync_id": "job-sync"},
            {"run_id": "source-active", "sync_id": "source-sync"},
        ]
        with (
            patch.object(settings_route, "list_runs", return_value=completed_runs),
            patch.object(settings_route, "list_user_sync_attempt_ids", return_value={"connector-sync"}),
        ):
            response = await settings_route.list_support_runs(
                provider=None,
                db=db,
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual({"status": "ok", "runs": [{"run_id": "ready", "sync_id": "ready-sync"}]}, response)

    def test_runner_log_copy_is_sanitized_and_attempt_scoped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            desktop_auth_dir = Path(tmpdir) / "desktop-auth"
            runner_log = desktop_auth_dir / "logs" / "visible-auth" / "amex-attempt-1.log"
            runner_log.parent.mkdir(parents=True)
            runner_log.write_text("network_error=net_ERR_FAILED token=secret-value\n", encoding="utf-8")
            run_dir = Path(tmpdir) / "run"

            with patch.object(support_auto_archive, "DESKTOP_AUTH_DIR", desktop_auth_dir):
                support_auto_archive._copy_visible_auth_runner_log_into_run(
                    run_dir=run_dir,
                    provider="amex",
                    attempt_id="attempt-1",
                )

            copied = (run_dir / "diagnostics" / "visible-auth-runner.log").read_text(encoding="utf-8")
            self.assertIn("network_error=net_ERR_FAILED", copied)
            self.assertNotIn("secret-value", copied)

if __name__ == "__main__":
    unittest.main()
