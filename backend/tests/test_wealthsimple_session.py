from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services import wealthsimple


class FakeSession:
    client_id = "client"
    access_token = "access"
    refresh_token = "refresh"
    session_id = "session"
    wssdi = "device"
    token_info = {"identity_canonical_id": "identity"}


class WealthsimpleSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._session_path = Path(self._tmpdir.name) / "session.json"
        self._path_patch = patch.object(
            wealthsimple,
            "get_provider_session_path",
            return_value=str(self._session_path),
        )
        self._save_patch = patch.object(
            wealthsimple,
            "save_json_artifact",
            side_effect=lambda path, payload: Path(path).write_text(json.dumps(payload)),
        )
        self._path_patch.start()
        self._save_patch.start()

    def tearDown(self) -> None:
        self._save_patch.stop()
        self._path_patch.stop()
        self._tmpdir.cleanup()

    def _saved_session(self) -> dict:
        return json.loads(self._session_path.read_text())

    def test_save_authenticated_session_accepts_session_objects(self) -> None:
        wealthsimple.save_authenticated_session(1, FakeSession())

        self.assertEqual(
            self._saved_session(),
            {
                "client_id": "client",
                "access_token": "access",
                "refresh_token": "refresh",
                "session_id": "session",
                "wssdi": "device",
                "token_info": {"identity_canonical_id": "identity"},
            },
        )

    def test_save_authenticated_session_accepts_ws_api_json_callbacks(self) -> None:
        payload = {
            "client_id": "client",
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "session_id": "session",
            "wssdi": "device",
            "token_info": None,
        }

        wealthsimple.save_authenticated_session(1, json.dumps(payload))

        self.assertEqual(self._saved_session(), payload)

    def test_graphql_errors_array_unauthorized_refreshes_saved_session(self) -> None:
        from ws_api import WSApiException

        class FakeWealthsimpleAPI:
            OAUTH_BASE_URL = "https://oauth.example"
            instance = None

            def __init__(self, session) -> None:
                self.session = session
                self.refresh_payload = None
                FakeWealthsimpleAPI.instance = self

            def search_security(self, query: str):
                raise WSApiException(
                    "GraphQL query failed: FetchSecuritySearchResult",
                    {
                        "errors": [
                            {
                                "message": "Not Authorized.",
                                "extensions": {"code": "UNAUTHENTICATED"},
                            }
                        ]
                    },
                )

            def send_post(self, url: str, data: dict, headers: dict):
                self.refresh_payload = {"url": url, "data": data, "headers": headers}
                return {
                    "access_token": "refreshed-access",
                    "refresh_token": "refreshed-refresh",
                }

        with patch("ws_api.WealthsimpleAPI", FakeWealthsimpleAPI):
            client = wealthsimple.get_ws_client(
                1,
                {
                    "client_id": "client",
                    "access_token": "expired-access",
                    "refresh_token": "old-refresh",
                    "session_id": "session",
                    "wssdi": "device",
                    "token_info": None,
                },
            )

        self.assertIs(client, FakeWealthsimpleAPI.instance)
        self.assertEqual(client.refresh_payload["data"]["refresh_token"], "old-refresh")
        self.assertEqual(self._saved_session()["access_token"], "refreshed-access")
        self.assertEqual(self._saved_session()["refresh_token"], "refreshed-refresh")

    def test_refreshed_session_updates_live_ws_client(self) -> None:
        from ws_api import WSApiException, WSAPISession

        class CopyingFakeWealthsimpleAPI:
            OAUTH_BASE_URL = "https://oauth.example"
            instance = None

            def __init__(self, session) -> None:
                self.session = WSAPISession(
                    client_id=session.client_id,
                    access_token=session.access_token,
                    refresh_token=session.refresh_token,
                    session_id=session.session_id,
                    wssdi=session.wssdi,
                    token_info=session.token_info,
                )
                CopyingFakeWealthsimpleAPI.instance = self

            def search_security(self, query: str):
                if self.session.access_token == "expired-access":
                    raise WSApiException(
                        "GraphQL query failed: FetchSecuritySearchResult",
                        {
                            "errors": [
                                {
                                    "message": "Not Authorized.",
                                    "extensions": {"code": "UNAUTHENTICATED"},
                                }
                            ]
                        },
                    )
                return []

            def send_post(self, url: str, data: dict, headers: dict):
                return {
                    "access_token": "refreshed-access",
                    "refresh_token": "refreshed-refresh",
                }

        with patch("ws_api.WealthsimpleAPI", CopyingFakeWealthsimpleAPI):
            client = wealthsimple.get_ws_client(
                1,
                {
                    "client_id": "client",
                    "access_token": "expired-access",
                    "refresh_token": "old-refresh",
                    "session_id": "session",
                    "wssdi": "device",
                    "token_info": {"identity_canonical_id": "identity"},
                },
            )

        self.assertIs(client, CopyingFakeWealthsimpleAPI.instance)
        self.assertEqual(client.session.access_token, "refreshed-access")
        self.assertEqual(client.session.refresh_token, "refreshed-refresh")
        self.assertIsNone(client.session.token_info)
        self.assertEqual(self._saved_session()["access_token"], "refreshed-access")

    def test_cached_token_info_is_restored_after_ws_api_session_copy(self) -> None:
        from ws_api import WSAPISession

        class CopyingFakeWealthsimpleAPI:
            instance = None

            def __init__(self, session) -> None:
                self.session = WSAPISession(
                    client_id=session.client_id,
                    access_token=session.access_token,
                    refresh_token=session.refresh_token,
                    session_id=session.session_id,
                    wssdi=session.wssdi,
                    token_info=None,
                )
                CopyingFakeWealthsimpleAPI.instance = self

            def search_security(self, query: str):
                return []

        with patch("ws_api.WealthsimpleAPI", CopyingFakeWealthsimpleAPI):
            client = wealthsimple.get_ws_client(
                1,
                {
                    "client_id": "client",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "session_id": "session",
                    "wssdi": "device",
                    "token_info": {"identity_canonical_id": "identity"},
                },
            )

        self.assertIs(client, CopyingFakeWealthsimpleAPI.instance)
        self.assertEqual(client.session.token_info, {"identity_canonical_id": "identity"})


class WealthsimpleApiSessionOtpAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.user_id = 4242

    async def test_login_failed_returns_invalid_attempt_instead_of_raw_exception(self) -> None:
        from ws_api import LoginFailedException

        response = {"error": "invalid_grant", "error_description": "credentials rejected"}
        with (
            patch.object(
                wealthsimple,
                "run_wealthsimple_login",
                new=AsyncMock(side_effect=LoginFailedException("Login failed", response)),
            ),
            patch.object(wealthsimple, "clear_otp_challenge", new=AsyncMock()),
        ):
            result = await wealthsimple.WealthsimpleApiSessionOtpAuth().login(
                self.user_id,
                "person@example.com",
                "bad-password",
            )

        self.assertEqual(result.status.value, "error")
        self.assertEqual(result.message, wealthsimple.INVALID_LOGIN_MESSAGE)

    async def test_2fa_login_failed_with_connection_word_is_not_network_error(self) -> None:
        from ws_api import LoginFailedException

        response = {
            "error": "invalid_grant",
            "error_description": "No connected account or verification code incorrect",
        }
        with (
            patch.object(
                wealthsimple,
                "run_wealthsimple_login",
                new=AsyncMock(side_effect=LoginFailedException("Login failed", response)),
            ),
            patch.object(
                wealthsimple,
                "consume_otp_challenge",
                new=AsyncMock(
                    return_value={"email": "person@example.com", "password": "password"}
                ),
            ),
        ):
            result = await wealthsimple.WealthsimpleApiSessionOtpAuth().complete_2fa(self.user_id, "123456")

        self.assertEqual(result.status.value, "error")
        self.assertEqual(result.message, wealthsimple.INVALID_LOGIN_MESSAGE)

    async def test_2fa_curl_exception_remains_network_error_and_restores_challenge(self) -> None:
        from ws_api import CurlException

        save_challenge = AsyncMock()
        with (
            patch.object(
                wealthsimple,
                "run_wealthsimple_login",
                new=AsyncMock(
                    side_effect=CurlException("HTTP request failed: Failed to establish a new connection")
                ),
            ),
            patch.object(
                wealthsimple,
                "consume_otp_challenge",
                new=AsyncMock(
                    return_value={"email": "person@example.com", "password": "password"}
                ),
            ),
            patch.object(wealthsimple, "save_otp_challenge", new=save_challenge),
        ):
            result = await wealthsimple.WealthsimpleApiSessionOtpAuth().complete_2fa(self.user_id, "123456")

        self.assertEqual(result.status.value, "network_error")
        self.assertEqual(result.message, "Connection failed")
        save_challenge.assert_awaited_once_with(
            self.user_id,
            "person@example.com",
            "password",
        )

    async def test_2fa_session_save_failure_is_not_reported_as_invalid_code(self) -> None:
        with (
            patch.object(
                wealthsimple,
                "run_wealthsimple_login",
                new=AsyncMock(return_value=wealthsimple._session_payload(FakeSession())),
            ),
            patch.object(
                wealthsimple,
                "consume_otp_challenge",
                new=AsyncMock(
                    return_value={"email": "person@example.com", "password": "password"}
                ),
            ),
            patch.object(
                wealthsimple,
                "save_authenticated_session_async",
                new=AsyncMock(
                side_effect=RuntimeError("Failed to save provider runtime artifact"),
                ),
            ),
        ):
            result = await wealthsimple.WealthsimpleApiSessionOtpAuth().complete_2fa(self.user_id, "123456")

        self.assertEqual(result.status.value, "error")
        self.assertEqual(result.message, wealthsimple.SESSION_SAVE_FAILED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
