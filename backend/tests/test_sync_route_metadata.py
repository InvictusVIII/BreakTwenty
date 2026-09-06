import unittest
from unittest.mock import AsyncMock, patch

from app.api.routes import sync as sync_route
from app.services.network_preflight import SyncNetworkGateResult


class SyncRouteMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def _transaction_import_metadata(self, body: dict) -> tuple[str, str, str | None]:
        enqueue = AsyncMock(return_value={"status": "ok"})
        run_connector = AsyncMock(return_value={"status": "ok", "sync_id": "moomoo-sync-1"})
        with (
            patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_route, "release_sync_lock", new=AsyncMock()),
            patch.object(sync_route, "_ensure_request_connection", new=AsyncMock(return_value=17)),
            patch.object(
                sync_route,
                "ensure_provider_sync_attempt",
                return_value={"sync_id": "moomoo-sync-1"},
            ),
            patch.object(sync_route, "set_provider_activity", return_value=None),
            patch.object(sync_route, "clear_provider_activity"),
            patch.object(sync_route, "_delete_visible_auth_attempt_dir_async", new=AsyncMock()),
            patch.object(
                sync_route,
                "_run_connector_sync",
                new=run_connector,
            ),
            patch.object(sync_route, "_enqueue_transaction_import_after_sync", new=enqueue),
        ):
            await sync_route._sync_connector_route(
                object(),
                1,
                provider="moomoo",
                body={"sync_id": "moomoo-sync-1", "institution_id": 17, **body},
            )

        return (
            enqueue.await_args.kwargs["reason"],
            run_connector.await_args.kwargs["sync_source"],
            enqueue.await_args.kwargs["attempt_id"],
        )

    async def test_autosync_source_is_preserved_across_phases(self):
        reason, connector_source, attempt_id = await self._transaction_import_metadata(
            {"sync_source": "autosync"}
        )

        self.assertEqual("autosync", reason)
        self.assertEqual("autosync", connector_source)
        self.assertIsNone(attempt_id)

    async def test_sync_all_source_is_preserved_for_moomoo_lane(self):
        reason, connector_source, attempt_id = await self._transaction_import_metadata(
            {"sync_source": "sync_all"}
        )

        self.assertEqual("sync_all", reason)
        self.assertEqual("sync_all", connector_source)
        self.assertIsNone(attempt_id)

    async def test_missing_sync_source_defaults_to_individual_sync(self):
        reason, connector_source, attempt_id = await self._transaction_import_metadata({})

        self.assertEqual("individual_sync", reason)
        self.assertEqual("individual_sync", connector_source)
        self.assertIsNone(attempt_id)

    async def test_visible_auth_attempt_is_preserved_for_transaction_phase(self):
        reason, connector_source, attempt_id = await self._transaction_import_metadata({
            "attempt_id": "moomoo-visible-attempt",
        })

        self.assertEqual("manual_reauthentication", reason)
        self.assertEqual("manual_reauthentication", connector_source)
        self.assertEqual("moomoo-visible-attempt", attempt_id)

    async def test_auth_required_sync_does_not_enqueue_transaction_import(self):
        response = {"status": "auth_required", "message": "Sign in again."}

        with patch.object(sync_route, "enqueue_provider_transaction_import", new=AsyncMock()) as enqueue:
            result = await sync_route._enqueue_transaction_import_after_sync(
                1,
                "bmo",
                response,
                add_flow=False,
                reason="manual_sync",
                institution_id=24,
            )

        self.assertEqual(response, result)
        enqueue.assert_not_awaited()

    async def test_confirmed_dns_outage_stops_direct_sync_before_attempt(self):
        run_connector = AsyncMock()
        with (
            patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_route, "release_sync_lock", new=AsyncMock()) as release_lock,
            patch.object(sync_route, "_ensure_request_connection", new=AsyncMock(return_value=23)),
            patch.object(
                sync_route,
                "run_sync_network_gate",
                new=AsyncMock(
                    return_value=SyncNetworkGateResult(
                        status="blocked",
                        attempts=5,
                        probes=(),
                    )
                ),
            ),
            patch.object(sync_route, "_run_connector_sync", new=run_connector),
        ):
            response = await sync_route._sync_connector_route(
                object(),
                1,
                provider="rbc",
                body={"institution_id": 23},
            )

        self.assertEqual("network_blocked", response["status"])
        self.assertEqual(
            "dns_resolution_temporarily_unavailable",
            response["code"],
        )
        run_connector.assert_not_awaited()
        release_lock.assert_awaited_once_with(1, "rbc", institution_id=23)

    async def test_add_connection_failure_releases_acquired_lock(self):
        release_lock = AsyncMock(return_value=True)
        with (
            patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_route, "release_sync_lock", new=release_lock),
            patch.object(
                sync_route,
                "_ensure_request_connection",
                new=AsyncMock(side_effect=RuntimeError("connection failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                await sync_route._sync_connector_route(
                    object(),
                    1,
                    provider="rbc",
                    body={"add_flow": True},
                )

        release_lock.assert_awaited_once_with(1, "rbc", institution_id=None)


if __name__ == "__main__":
    unittest.main()
