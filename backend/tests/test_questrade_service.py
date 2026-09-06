from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.questrade as questrade_service


class QuestradeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotated_credentials_are_persisted_in_one_transaction(self) -> None:
        class AsyncContext:
            def __init__(self, value=None):
                self.value = value

            async def __aenter__(self):
                return self.value

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        session = MagicMock()
        session.begin.return_value = AsyncContext()
        upsert = AsyncMock(return_value=True)

        with (
            patch.object(
                questrade_service,
                "async_session",
                return_value=AsyncContext(session),
            ),
            patch.object(
                questrade_service,
                "sqlite_write_gate",
                return_value=AsyncContext(),
            ),
            patch.object(questrade_service, "upsert_api_credentials", upsert),
        ):
            await questrade_service.save_questrade_credentials(
                7,
                "fresh-access",
                "fresh-refresh",
                "https://fresh.example/",
            )

        upsert.assert_awaited_once_with(
            session,
            7,
            questrade_service.QUESTRADE_PROVIDER,
            {
                "questrade_access_token": "fresh-access",
                "questrade_refresh_token": "fresh-refresh",
                "questrade_api_server": "https://fresh.example/",
            },
            ensure_institution=True,
        )
        session.begin.assert_called_once_with()
        session.commit.assert_not_called()

    async def asyncSetUp(self) -> None:
        self._original_get_questrade_credentials = questrade_service.get_questrade_credentials
        self._original_exchange_token_once = questrade_service._exchange_token_once
        self._original_health_check_api_server = questrade_service._health_check_api_server
        self._original_retry_delays = questrade_service.QUESTRADE_AUTH_RETRY_DELAYS_SECONDS
        questrade_service.QUESTRADE_AUTH_RETRY_DELAYS_SECONDS = (0.0, 0.0)

    async def asyncTearDown(self) -> None:
        questrade_service.get_questrade_credentials = self._original_get_questrade_credentials
        questrade_service._exchange_token_once = self._original_exchange_token_once
        questrade_service._health_check_api_server = self._original_health_check_api_server
        questrade_service.QUESTRADE_AUTH_RETRY_DELAYS_SECONDS = self._original_retry_delays

    async def test_auth_exchange_500_is_retried_before_sync_fails(self) -> None:
        calls = 0

        async def fake_get_questrade_credentials(user_id: int):
            return "refresh-token", None, None

        async def fake_exchange_token_once(user_id: int, refresh_token: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise questrade_service.QuestradeTemporaryError(
                    "Connection failed - Questrade auth returned 500"
                )
            return "access-token", "new-refresh-token", "https://api03.iq.questrade.com/"

        async def fake_health_check_api_server(
            access_token: str,
            api_server: str,
        ) -> questrade_service.QuestradeApiServerHealthResult:
            return questrade_service.QuestradeApiServerHealthResult(ok=True, duration_ms=12, status_code=200)

        questrade_service.get_questrade_credentials = fake_get_questrade_credentials
        questrade_service._exchange_token_once = fake_exchange_token_once
        questrade_service._health_check_api_server = fake_health_check_api_server

        access_token, api_server = await questrade_service.get_authenticated_client(1)

        self.assertEqual("access-token", access_token)
        self.assertEqual("https://api03.iq.questrade.com/", api_server)
        self.assertEqual(2, calls)

    async def test_api_server_probe_failure_logs_sanitized_details(self) -> None:
        exchanges = 0
        health_checks = 0

        async def fake_get_questrade_credentials(user_id: int):
            return "refresh-token", None, None

        async def fake_exchange_token_once(user_id: int, refresh_token: str):
            nonlocal exchanges
            exchanges += 1
            return f"access-token-{exchanges}", f"refresh-token-{exchanges}", "https://api07.iq.questrade.com/"

        async def fake_health_check_api_server(
            access_token: str,
            api_server: str,
        ) -> questrade_service.QuestradeApiServerHealthResult:
            nonlocal health_checks
            health_checks += 1
            if health_checks == 1:
                return questrade_service.QuestradeApiServerHealthResult(
                    ok=False,
                    duration_ms=5002,
                    error="ReadTimeout",
                    message="timed out",
                )
            return questrade_service.QuestradeApiServerHealthResult(
                ok=True,
                duration_ms=41,
                status_code=200,
                content_type="application/json",
            )

        questrade_service.get_questrade_credentials = fake_get_questrade_credentials
        questrade_service._exchange_token_once = fake_exchange_token_once
        questrade_service._health_check_api_server = fake_health_check_api_server

        with self.assertLogs("breaktwenty.connectors.questrade", level="WARNING") as logs:
            access_token, api_server = await questrade_service.get_authenticated_client(1)

        self.assertEqual("access-token-2", access_token)
        self.assertEqual("https://api07.iq.questrade.com/", api_server)
        self.assertEqual(2, exchanges)
        self.assertEqual(2, health_checks)
        rendered = "\n".join(logs.output)
        self.assertIn("api server probe failed", rendered)
        self.assertIn("duration_ms=5002", rendered)
        self.assertIn("error=ReadTimeout", rendered)
        self.assertIn("message=timed out", rendered)

    async def test_auth_required_is_not_retried(self) -> None:
        calls = 0

        async def fake_get_questrade_credentials(user_id: int):
            return "refresh-token", None, None

        async def fake_exchange_token_once(user_id: int, refresh_token: str):
            nonlocal calls
            calls += 1
            raise questrade_service.QuestradeAuthRequiredError("Invalid token")

        questrade_service.get_questrade_credentials = fake_get_questrade_credentials
        questrade_service._exchange_token_once = fake_exchange_token_once

        with self.assertRaises(questrade_service.QuestradeAuthRequiredError):
            await questrade_service.get_authenticated_client(1)

        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
