from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes import _parse_csv_strings
from app.api.routes import categories as category_routes
from app.api.routes import sync as sync_routes
from app.api.routes import settings as settings_routes
from app.api.routes.transactions import router as transactions_router
from app.services.support_auto_archive import SupportArchive, SupportArchiveLimitError


def _connection(provider: object = "rbc", institution_id: object = 1) -> dict:
    return {"provider": provider, "institution_id": institution_id}


class BackendRouteContractTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_scalar_payloads_require_native_json_types(self) -> None:
        self.assertEqual(sync_routes._payload_institution_id({"institution_id": 7}), 7)
        self.assertTrue(sync_routes._payload_bool({"add_flow": True}, "add_flow"))
        self.assertFalse(sync_routes._payload_bool({}, "add_flow"))

        for value in (True, "7", 7.0, 0, -1):
            with self.subTest(institution_id=value), self.assertRaises(HTTPException) as caught:
                sync_routes._payload_institution_id({"institution_id": value})
            self.assertEqual(caught.exception.status_code, 422)

        for value in (1, 0, "true", "false", None):
            with self.subTest(add_flow=value), self.assertRaises(HTTPException) as caught:
                sync_routes._payload_bool({"add_flow": value}, "add_flow")
            self.assertEqual(caught.exception.status_code, 422)

    async def test_sync_batch_accepts_only_canonical_body(self) -> None:
        starter = AsyncMock(return_value={"status": "started", "batch_id": "batch-1"})
        body = {
            "connections": [_connection("rbc", 7), _connection("td", 8)],
            "mode": "manual",
        }
        with patch("app.services.sync_batch.start_sync_batch", new=starter):
            result = await sync_routes.start_batch_sync(
                body,
                current_user=SimpleNamespace(id=11),
            )

        self.assertEqual(result["batch_id"], "batch-1")
        starter.assert_awaited_once_with(11, body["connections"], mode="manual")

    async def test_active_sync_batches_returns_owner_scoped_canonical_envelope(self) -> None:
        batches = [
            {"batch_id": "batch-1", "status": "queued"},
            {"batch_id": "batch-2", "status": "running"},
        ]
        getter = AsyncMock(return_value=batches)
        with patch("app.services.sync_batch.get_user_active_sync_batches", new=getter):
            result = await sync_routes.get_active_batch_syncs(
                current_user=SimpleNamespace(id=11),
            )

        self.assertEqual(result, {"batches": batches})
        getter.assert_awaited_once_with(11)

    async def test_support_archive_creation_and_download_never_expose_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "opaque.zip"
            archive_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            archive = SupportArchive(
                archive_id="11111111-1111-4111-8111-111111111111",
                filename="support.zip",
                path=archive_path,
            )
            with (
                patch.object(settings_routes, "get_run", return_value={"sync_id": "sync-1"}),
                patch.object(
                    settings_routes,
                    "_active_support_sync_ids",
                    new=AsyncMock(return_value=set()),
                ),
                patch.object(settings_routes, "package_run_zip", return_value=archive) as package,
            ):
                created = await settings_routes.zip_support_run(
                    "rbc",
                    "run-1",
                    db=SimpleNamespace(),
                    current_user=SimpleNamespace(id=11),
                )

            self.assertEqual(
                created,
                {
                    "status": "ok",
                    "export_scope": "sync_attempt",
                    "archive_id": archive.archive_id,
                    "archive_filename": archive.filename,
                },
            )
            package.assert_called_once_with("rbc", "run-1", user_id=11)

            with patch.object(
                settings_routes,
                "resolve_support_archive",
                return_value=archive,
            ) as resolve:
                response = await settings_routes.download_support_archive(
                    archive.archive_id,
                    current_user=SimpleNamespace(id=11),
                )

            resolve.assert_called_once_with(archive.archive_id, user_id=11)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("support.zip", response.headers["content-disposition"])
            self.assertNotIn(str(archive_path), str(created))

    async def test_support_archive_download_hides_invalid_or_foreign_ids(self) -> None:
        with (
            patch.object(settings_routes, "resolve_support_archive", return_value=None),
            self.assertRaises(HTTPException) as caught,
        ):
            await settings_routes.download_support_archive(
                "not-owned",
                current_user=SimpleNamespace(id=11),
            )
        self.assertEqual(caught.exception.status_code, 404)

    async def test_attempt_export_reports_limit_instead_of_returning_partial_zip(self) -> None:
        with (
            patch.object(settings_routes, "get_run", return_value={"sync_id": "sync-1"}),
            patch.object(
                settings_routes,
                "_active_support_sync_ids",
                new=AsyncMock(return_value=set()),
            ),
            patch.object(
                settings_routes,
                "package_run_zip",
                side_effect=SupportArchiveLimitError(
                    reason="attempt_file_count_limit",
                    message="No partial ZIP was created.",
                    required_file_count=300,
                    limit=256,
                ),
            ),
        ):
            result = await settings_routes.zip_support_run(
                "rbc",
                "run-1",
                db=SimpleNamespace(),
                current_user=SimpleNamespace(id=11),
            )

        self.assertEqual("too_large", result["status"])
        self.assertEqual("attempt_file_count_limit", result["reason"])
        self.assertEqual(300, result["required_file_count"])
        self.assertEqual(256, result["limit"])

    async def test_all_recent_support_export_fails_clearly_instead_of_omitting_attempts(self) -> None:
        package = AsyncMock(
            side_effect=SupportArchiveLimitError(
                reason="export_file_count_limit",
                message=(
                    "The complete 45-minute export needs 4,100 files. "
                    "No partial ZIP was created; use a shorter window."
                ),
                required_file_count=4_100,
                limit=4_096,
            )
        )
        with (
            patch.object(settings_routes, "package_all_recent_runs", new=package),
            patch.object(
                settings_routes,
                "_active_support_sync_ids",
                new=AsyncMock(return_value=set()),
            ),
        ):
            result = await settings_routes.export_recent_support_runs(
                {"minutes": 45},
                db=SimpleNamespace(),
                current_user=SimpleNamespace(id=11),
            )

        self.assertEqual("too_large", result["status"])
        self.assertEqual("export_file_count_limit", result["reason"])
        self.assertEqual(4_100, result["required_file_count"])
        self.assertEqual(4_096, result["limit"])
        self.assertIn("No partial ZIP was created", result["message"])
        self.assertEqual(45, result["minutes"])
        package.assert_awaited_once_with(user_id=11, since_minutes=45, excluded_sync_ids=set())

    async def test_sync_batch_rejects_malformed_modes_connections_and_duplicates(self) -> None:
        malformed = (
            None,
            [],
            {},
            {"connections": [_connection()]},
            {"mode": "auto"},
            {"connections": [_connection()], "mode": "auto", "extra": True},
            {"connections": [], "mode": "auto"},
            {"connections": "rbc", "mode": "auto"},
            {"connections": [_connection()], "mode": "nightly"},
            {"connections": [_connection()], "mode": 1},
            {"connections": ["rbc"], "mode": "auto"},
            {"connections": [{"provider": "rbc"}], "mode": "auto"},
            {"connections": [{**_connection(), "extra": True}], "mode": "auto"},
            {"connections": [_connection("RBC", 1)], "mode": "auto"},
            {"connections": [_connection("unknown", 1)], "mode": "auto"},
            {"connections": [_connection("manual", 1)], "mode": "auto"},
            {"connections": [_connection(7, 1)], "mode": "auto"},
            {"connections": [_connection("rbc", True)], "mode": "auto"},
            {"connections": [_connection("rbc", "1")], "mode": "auto"},
            {"connections": [_connection("rbc", 1.0)], "mode": "auto"},
            {"connections": [_connection("rbc", 0)], "mode": "auto"},
            {
                "connections": [_connection("rbc", 1), _connection("rbc", 2)],
                "mode": "auto",
            },
            {
                "connections": [_connection("rbc", 1), _connection("td", 1)],
                "mode": "auto",
            },
        )
        starter = AsyncMock()
        with patch("app.services.sync_batch.start_sync_batch", new=starter):
            for body in malformed:
                with self.subTest(body=body), self.assertRaises(HTTPException) as caught:
                    await sync_routes.start_batch_sync(
                        body,
                        current_user=SimpleNamespace(id=11),
                    )
                self.assertEqual(caught.exception.status_code, 422)
        starter.assert_not_awaited()

    async def test_sync_batch_rejects_excess_connection_count(self) -> None:
        starter = AsyncMock()
        body = {
            "connections": [{} for _ in range(sync_routes.MAX_SYNC_BATCH_CONNECTIONS + 1)],
            "mode": "auto",
        }
        with (
            patch("app.services.sync_batch.start_sync_batch", new=starter),
            self.assertRaises(HTTPException) as caught,
        ):
            await sync_routes.start_batch_sync(body, current_user=SimpleNamespace(id=11))
        self.assertEqual(caught.exception.status_code, 413)
        starter.assert_not_awaited()

    async def test_visible_auth_credentials_reject_institution_compatibility_field(self) -> None:
        save_artifact = AsyncMock()
        with (
            patch.object(sync_routes, "save_visible_auth_attempt_artifact", new=save_artifact),
            self.assertRaises(HTTPException) as caught,
        ):
            await sync_routes.save_visible_auth_credentials(
                {
                    "provider": "rbc",
                    "attempt_id": "attempt-1",
                    "add_flow": True,
                    "institution_id": 7,
                    "username": "user",
                    "password": "secret",
                },
                None,
                current_user=SimpleNamespace(id=11),
            )
        self.assertEqual(caught.exception.status_code, 422)
        save_artifact.assert_not_awaited()

    async def test_api_credential_replacement_conflict_is_a_controlled_response(self) -> None:
        db = SimpleNamespace(rollback=AsyncMock())
        with (
            patch.object(
                settings_routes,
                "recover_expired_api_credential_replacements",
                new=AsyncMock(return_value=0),
            ),
            patch.object(
                settings_routes,
                "ensure_api_credentials_institution",
                new=AsyncMock(return_value=7),
            ),
            patch.object(
                settings_routes,
                "get_api_credentials",
                new=AsyncMock(return_value={"wise_api_token": "old-token"}),
            ),
            patch.object(
                settings_routes,
                "begin_api_credential_replacement",
                new=AsyncMock(side_effect=ValueError("replacement already pending")),
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            await settings_routes.save_settings(
                {"wise_api_token": "new-token", "institution_id": 7},
                db=db,
                current_user=SimpleNamespace(id=11),
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "replacement already pending")
        db.rollback.assert_awaited_once()

    async def test_categories_get_is_read_only(self) -> None:
        db = SimpleNamespace(commit=AsyncMock())
        list_categories = AsyncMock(return_value=[])
        with patch.object(
            category_routes,
            "list_categories_for_user",
            new=list_categories,
        ):
            result = await category_routes.list_categories(
                db=db,
                current_user=SimpleNamespace(id=11),
            )

        self.assertEqual(result["categories"], [])
        list_categories.assert_awaited_once_with(db, 11)
        db.commit.assert_not_awaited()

    def test_transaction_string_filters_are_bounded(self) -> None:
        self.assertEqual(
            _parse_csv_strings("buy, dividend", field_name="type"),
            ["buy", "dividend"],
        )
        for raw in (
            ",buy",
            "buy,,sell",
            ",".join("buy" for _ in range(51)),
            "x" * 65,
        ):
            with self.subTest(raw=raw), self.assertRaises(HTTPException) as caught:
                _parse_csv_strings(raw, field_name="type")
            self.assertEqual(caught.exception.status_code, 422)

        route = next(
            item
            for item in transactions_router.routes
            if item.path == "/transactions" and "GET" in item.methods
        )
        max_lengths = {}
        for field in route.dependant.query_params:
            for constraint in field.field_info.metadata:
                if hasattr(constraint, "max_length"):
                    max_lengths[field.name] = constraint.max_length
        self.assertEqual(max_lengths["type"], 1_024)
        self.assertEqual(max_lengths["symbol"], 64)
        self.assertEqual(max_lengths["search"], 200)


if __name__ == "__main__":
    unittest.main()
