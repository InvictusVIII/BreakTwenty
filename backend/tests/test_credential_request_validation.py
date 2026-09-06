from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api.routes import settings as settings_route
from app.api.routes.settings import (
    MAX_SETTING_VALUE_LENGTH,
    _validate_scraper_credential_payload,
)
from app.auth import CurrentUser


class CredentialRequestValidationTests(unittest.TestCase):
    def test_accepts_the_live_credential_request_shape(self) -> None:
        self.assertEqual(
            _validate_scraper_credential_payload(
                {
                    "username": "user@example.test",
                    "password": "provider-password",
                    "institution_id": 17,
                    "preserve_pending_session": True,
                }
            ),
            ("user@example.test", "provider-password", 17, True),
        )
        self.assertEqual(
            _validate_scraper_credential_payload(
                {
                    "username": "user",
                    "password": "password",
                    "institution_id": 18,
                }
            ),
            ("user", "password", 18, False),
        )

    def test_rejects_missing_unknown_and_malformed_fields_with_422(self) -> None:
        invalid_payloads = (
            {},
            {"username": "user", "password": "password"},
            {
                "username": "user",
                "password": "password",
                "institution_id": 1,
                "promote": True,
            },
            {"username": 123, "password": "password", "institution_id": 1},
            {"username": "user", "password": None, "institution_id": 1},
            {"username": "", "password": "password", "institution_id": 1},
            {"username": "user", "password": "", "institution_id": 1},
            {"username": "user", "password": "password", "institution_id": True},
            {"username": "user", "password": "password", "institution_id": "1"},
            {"username": "user", "password": "password", "institution_id": 0},
            {
                "username": "user",
                "password": "password",
                "institution_id": 2_147_483_648,
            },
            {
                "username": "user",
                "password": "password",
                "institution_id": 1,
                "preserve_pending_session": 1,
            },
            {
                "username": "x" * (MAX_SETTING_VALUE_LENGTH + 1),
                "password": "password",
                "institution_id": 1,
            },
            {
                "username": "user",
                "password": "x" * (MAX_SETTING_VALUE_LENGTH + 1),
                "institution_id": 1,
            },
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as caught:
                _validate_scraper_credential_payload(payload)
            self.assertEqual(caught.exception.status_code, 422)


class CredentialRequestRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_body_is_rejected_before_database_work(self) -> None:
        has_institution = AsyncMock()
        with patch.object(
            settings_route,
            "_provider_has_institution",
            new=has_institution,
        ):
            with self.assertRaises(HTTPException) as caught:
                await settings_route.save_credential(
                    "rbc",
                    {
                        "username": "user",
                        "password": "password",
                        "institution_id": "17",
                    },
                    object(),
                    CurrentUser(id=1),
                )

        self.assertEqual(caught.exception.status_code, 422)
        has_institution.assert_not_awaited()

    async def test_live_request_shape_reaches_connection_scoped_storage(self) -> None:
        db = AsyncMock()
        invalidate = AsyncMock()
        upsert = AsyncMock(return_value=True)
        with (
            patch.object(
                settings_route,
                "_provider_has_institution",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                settings_route,
                "get_scraper_credentials",
                new=AsyncMock(return_value=None),
            ),
            patch.object(settings_route, "get_runtime_state_metadata", return_value={}),
            patch.object(
                settings_route,
                "invalidate_provider_auth_state",
                new=invalidate,
            ),
            patch.object(
                settings_route,
                "upsert_scraper_credentials",
                new=upsert,
            ),
        ):
            response = await settings_route.save_credential(
                "rbc",
                {
                    "username": "user",
                    "password": "password",
                    "institution_id": 17,
                    "preserve_pending_session": True,
                },
                db,
                CurrentUser(id=3),
            )

        self.assertEqual(response, {"status": "ok"})
        invalidate.assert_awaited_once_with(
            db,
            3,
            "rbc",
            preserve_pending_session=True,
            quarantine_runtime=False,
            institution_id=17,
        )
        upsert.assert_awaited_once_with(
            db,
            3,
            "rbc",
            "user",
            "password",
            institution_id=17,
        )
        db.commit.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
