import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes import sync as sync_route
from app.launch_auth import AuthPrincipal


VISIBLE_AUTH_METADATA = {
    "enabled": True,
    "addFlow": True,
    "manualFlow": True,
}


class VisibleAuthAttemptCleanupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runner_request(*, add_flow: bool = True):
        return SimpleNamespace(
            state=SimpleNamespace(
                auth_principal=AuthPrincipal(
                    kind="runner-session",
                    user_id=1,
                    provider="rbc",
                    attempt_id="attempt-1",
                    add_flow=add_flow,
                    purpose="visible-auth",
                )
            )
        )

    async def test_credentials_reject_non_string_values(self):
        with patch.object(
            sync_route,
            "get_desktop_visible_auth_metadata",
            return_value=VISIBLE_AUTH_METADATA,
        ):
            for field in ("username", "password"):
                body = {
                    "provider": "rbc",
                    "attempt_id": "attempt-1",
                    "username": "alice",
                    "password": "secret",
                }
                body[field] = 123
                with self.subTest(field=field), self.assertRaises(HTTPException) as caught:
                    await sync_route.save_visible_auth_credentials(
                        body,
                        self._runner_request(),
                        current_user=SimpleNamespace(id=1),
                    )
                self.assertEqual(caught.exception.status_code, 422)

    async def test_direct_artifact_promotion_parameter_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            await sync_route.save_visible_auth_artifact(
                {
                    "provider": "rbc",
                    "attempt_id": "attempt-1",
                    "artifact_type": "cookies",
                    "payload": {"cookies": []},
                    "promote": True,
                },
                self._runner_request(),
                current_user=SimpleNamespace(id=1),
            )
        self.assertEqual(caught.exception.status_code, 422)

    async def test_credentials_are_staged_even_when_catalog_flag_is_false(self):
        save_artifact = AsyncMock()
        with patch.object(
            sync_route,
            "get_desktop_visible_auth_metadata",
            return_value={**VISIBLE_AUTH_METADATA, "stageArtifacts": False},
        ), patch.object(
            sync_route,
            "save_visible_auth_attempt_artifact",
            new=save_artifact,
        ):
            response = await sync_route.save_visible_auth_credentials(
                {
                    "provider": "rbc",
                    "attempt_id": "attempt-1",
                    "username": "alice",
                    "password": "secret",
                },
                self._runner_request(),
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(response, {"status": "ok", "attempt_id": "attempt-1", "staged": True})
        save_artifact.assert_awaited_once()

    async def test_connector_handoff_auth_required_deletes_staged_attempt_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_dir = Path(tmpdir) / "attempt"
            attempt_dir.mkdir()
            (attempt_dir / "credentials.json").write_text("{}", encoding="utf-8")
            delete_artifacts = AsyncMock(return_value=True)

            with patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)), \
                patch.object(sync_route, "release_sync_lock", new=AsyncMock()), \
                patch.object(sync_route, "_ensure_request_connection", new=AsyncMock(return_value=7)), \
                patch.object(sync_route, "get_desktop_visible_auth_metadata", return_value=VISIBLE_AUTH_METADATA), \
                patch.object(sync_route, "ensure_provider_sync_attempt", return_value={"sync_id": "sync-1"}), \
                patch.object(sync_route, "_run_connector_sync", new=AsyncMock(return_value={"status": "auth_required"})), \
                patch.object(sync_route, "delete_visible_auth_attempt_artifacts_async", new=delete_artifacts), \
                patch.object(sync_route, "get_provider_visible_auth_attempt_dir", return_value=str(attempt_dir)):
                response = await sync_route._sync_connector_route(
                    object(),
                    1,
                    provider="rbc",
                    body={
                        "sync_id": "sync-1",
                        "attempt_id": "attempt-1",
                        "add_flow": False,
                    },
                )

            self.assertEqual("auth_required", response["status"])
            self.assertFalse(attempt_dir.exists())
            delete_artifacts.assert_awaited_once_with(
                1,
                "rbc",
                "attempt-1",
            )

    async def test_attempt_cleanup_failure_still_releases_sync_lock(self):
        release_lock = AsyncMock(return_value=True)
        with patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)), \
            patch.object(sync_route, "release_sync_lock", new=release_lock), \
            patch.object(sync_route, "_ensure_request_connection", new=AsyncMock(return_value=7)), \
            patch.object(sync_route, "get_desktop_visible_auth_metadata", return_value=VISIBLE_AUTH_METADATA), \
            patch.object(sync_route, "ensure_provider_sync_attempt", return_value={"sync_id": "sync-1"}), \
            patch.object(sync_route, "_run_connector_sync", new=AsyncMock(return_value={"status": "auth_required"})), \
            patch.object(
                sync_route,
                "_delete_visible_auth_attempt_dir_async",
                new=AsyncMock(side_effect=RuntimeError("cleanup failed")),
            ):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                await sync_route._sync_connector_route(
                    object(),
                    1,
                    provider="rbc",
                    body={
                        "sync_id": "sync-1",
                        "attempt_id": "attempt-1",
                        "add_flow": False,
                    },
                )

        release_lock.assert_awaited_once_with(1, "rbc", institution_id=7)

    async def test_cleanup_endpoint_deletes_staged_attempt_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            attempt_dir = Path(tmpdir) / "attempt"
            attempt_dir.mkdir()
            (attempt_dir / "session.json").write_text("{}", encoding="utf-8")
            delete_artifacts = AsyncMock(return_value=True)

            with patch.object(sync_route, "get_desktop_visible_auth_metadata", return_value=VISIBLE_AUTH_METADATA), \
                patch.object(sync_route, "delete_visible_auth_attempt_artifacts_async", new=delete_artifacts), \
                patch.object(sync_route, "get_provider_visible_auth_attempt_dir", return_value=str(attempt_dir)):
                response = await sync_route.cleanup_visible_auth_attempt(
                    {
                        "provider": "rbc",
                        "attempt_id": "attempt-1",
                        "add_flow": False,
                    },
                    current_user=SimpleNamespace(id=1),
                )

            self.assertEqual({"status": "ok", "attempt_id": "attempt-1"}, response)
            self.assertFalse(attempt_dir.exists())
            delete_artifacts.assert_awaited_once_with(
                1,
                "rbc",
                "attempt-1",
            )


if __name__ == "__main__":
    unittest.main()
