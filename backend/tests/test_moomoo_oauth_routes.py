from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request

from app.api.routes import moomoo_oauth
from app.auth import CurrentUser
from app.services.moomoo_cloud import MoomooCloudTokens


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _Database:
    def begin(self):
        return _Transaction()

    async def rollback(self):
        return None


@asynccontextmanager
async def _write_gate():
    yield


def _request() -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/moomoo/oauth/start",
        "raw_path": b"/api/auth/moomoo/oauth/start",
        "query_string": b"",
        "headers": [(b"host", b"localhost:8000")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    })


class MoomooOAuthRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        async with moomoo_oauth._flow_lock:
            moomoo_oauth._flows.clear()
            moomoo_oauth._flow_ids_by_state.clear()
        async with moomoo_oauth._client_registration_lock:
            moomoo_oauth._client_ids_by_redirect.clear()
        moomoo_oauth.finalize_provider_sync_attempt(
            1,
            "moomoo",
            sync_id="moomoo-route-test",
            status="error",
        )

    async def test_pkce_callback_and_encrypted_artifact_handoff_expose_no_tokens(self) -> None:
        with (
            patch.object(
                moomoo_oauth,
                "register_public_client",
                AsyncMock(return_value=("public-client", "trade:read accid:*")),
            ),
            patch.object(moomoo_oauth, "log_connector_event"),
        ):
            started = await moomoo_oauth.start_moomoo_oauth(
                {"add_flow": True, "sync_id": "moomoo-route-test"},
                _request(),
                CurrentUser(id=1),
            )

        self.assertEqual(started["status"], "waiting")
        self.assertRegex(started["attempt_id"], r"^[0-9a-f]{32}$")
        self.assertNotIn("verifier", started)
        self.assertNotIn("token", str(started).lower())
        authorization = urlparse(started["authorization_url"])
        query = parse_qs(authorization.query)
        self.assertEqual(authorization.netloc, "webapi.moomoo.com")
        self.assertEqual(query["redirect_uri"], [
            "http://localhost:8000/api/auth/moomoo/oauth/callback"
        ])
        self.assertEqual(query["code_challenge_method"], ["S256"])

        tokens = MoomooCloudTokens(
            access_token="one-use-access",
            refresh_token="stored-refresh",
            scope="trade:read accid:123456",
            expires_in=7200,
        )
        with (
            patch.object(
                moomoo_oauth,
                "exchange_authorization_code",
                AsyncMock(return_value=tokens),
            ) as exchange,
            patch.object(moomoo_oauth, "log_connector_event"),
        ):
            callback = await moomoo_oauth.complete_moomoo_oauth_callback(
                state=query["state"][0],
                code="one-time-code",
            )
        self.assertEqual(callback.status_code, 200)
        self.assertNotIn(b"one-time-code", callback.body)
        self.assertNotIn(b"stored-refresh", callback.body)
        exchange.assert_awaited_once()

        status = await moomoo_oauth.moomoo_oauth_status(
            started["flow_id"],
            CurrentUser(id=1),
        )
        self.assertEqual(status, {
            "status": "authorized",
            "sync_id": "moomoo-route-test",
            "attempt_id": started["attempt_id"],
        })

        saved_payload = {}

        async def save_artifact(_db, _user_id, _provider, _kind, payload, **_kwargs):
            saved_payload.update(payload)
            return True

        with (
            patch.object(moomoo_oauth, "sqlite_write_gate", _write_gate),
            patch.object(moomoo_oauth, "ensure_connection", AsyncMock(return_value=17)),
            patch.object(moomoo_oauth, "upsert_connection_artifact", AsyncMock(side_effect=save_artifact)),
            patch.object(moomoo_oauth, "archive_run_fire_and_forget") as archive_run,
            patch.object(moomoo_oauth, "log_connector_event"),
        ):
            completed = await moomoo_oauth.persist_moomoo_oauth(
                started["flow_id"],
                _Database(),
                CurrentUser(id=1),
            )
        self.assertEqual(completed, {
            "status": "ok",
            "sync_id": "moomoo-route-test",
            "attempt_id": started["attempt_id"],
            "institution_id": 17,
        })
        self.assertEqual(saved_payload["refresh_token"], "stored-refresh")
        self.assertNotIn("access_token", saved_payload)
        self.assertNotIn("refresh", str(completed).lower())
        self.assertEqual(started["attempt_id"], archive_run.call_args.kwargs["attempt_id"])

    async def test_callback_state_is_required_and_unknown_attempt_leaks_no_detail(self) -> None:
        response = await moomoo_oauth.complete_moomoo_oauth_callback(
            state="unknown",
            code="secret-code",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"secret-code", response.body)
        self.assertIn(b"could not be matched", response.body)

    async def test_duplicate_callback_cannot_exchange_the_code_twice(self) -> None:
        with (
            patch.object(
                moomoo_oauth,
                "register_public_client",
                AsyncMock(return_value=("public-client", "trade:read accid:*")),
            ),
            patch.object(moomoo_oauth, "log_connector_event"),
        ):
            started = await moomoo_oauth.start_moomoo_oauth(
                {"add_flow": True, "sync_id": "moomoo-route-test"},
                _request(),
                CurrentUser(id=1),
            )
        flow = moomoo_oauth._flows[started["flow_id"]]
        flow.status = "exchanging"
        with patch.object(
            moomoo_oauth,
            "exchange_authorization_code",
            AsyncMock(),
        ) as exchange:
            duplicate = await moomoo_oauth.complete_moomoo_oauth_callback(
                state=flow.state,
                code="already-claimed-code",
            )
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn(b"already being processed", duplicate.body)
        exchange.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
