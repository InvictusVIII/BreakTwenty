import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import HTTPException
from starlette.requests import Request

TEST_KEY = base64.b64encode(b"k" * 32).decode("ascii")

from app import key_material  # noqa: E402
from app.launch_auth import (  # noqa: E402
    LaunchAuthMiddleware,
    generate_token,
    issue_runner_session,
    register_runner_grant,
    reset_auth_state_for_tests,
    revoke_runner_session,
)
from app.main import app, get_cors_origins  # noqa: E402
from app.api.routes.auth_sessions import create_runner_grant, get_runner_credentials  # noqa: E402
from app.api.routes import sync as sync_routes  # noqa: E402
from app.auth import CurrentUser  # noqa: E402
from app.launch_auth import AuthPrincipal, LaunchTokenBundle  # noqa: E402


def write_token_bundle(path: Path, desktop_token: str, renderer_token: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "desktopToken": desktop_token,
                "rendererToken": renderer_token,
            }
        ),
        encoding="ascii",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


class LaunchAuthIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.token_path = Path(self.tempdir.name) / "launch-auth.token"
        self.desktop_token = generate_token()
        self.renderer_token = generate_token()
        write_token_bundle(self.token_path, self.desktop_token, self.renderer_token)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": TEST_KEY,
                "BREAKTWENTY_APP_ENCRYPTION_KEY": TEST_KEY,
                "BREAKTWENTY_LAUNCH_TOKEN_FILE": str(self.token_path),
                "BREAKTWENTY_KEY_BOOTSTRAP": "",
            },
        )
        self.environment.start()
        with key_material._key_material_lock:
            key_material._key_material = None
        reset_auth_state_for_tests()

    def tearDown(self) -> None:
        reset_auth_state_for_tests()
        with key_material._key_material_lock:
            key_material._key_material = None
        self.environment.stop()
        self.tempdir.cleanup()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    async def test_health_logo_sse_and_ordinary_api_all_reject_missing_bearer(self) -> None:
        for path in (
            "/api/health",
            "/api/health/ownership?challenge=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "/api/institutions/1/logo",
            "/api/events/stream",
            "/api/accounts",
        ):
            with self.subTest(path=path):
                response = await self.request("GET", path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    async def test_api_cache_policy_wraps_authorized_and_rejected_responses(self) -> None:
        rejected = await self.request("GET", "/api/health")
        authorized = await self.request(
            "GET",
            "/api/not-a-route",
            headers={"Authorization": f"Bearer {self.renderer_token}"},
        )
        preflight = await self.request(
            "OPTIONS",
            "/api/accounts",
            headers={
                "Origin": "http://127.0.0.1:32100",
                "Access-Control-Request-Method": "GET",
            },
        )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.headers["cache-control"], "no-store")
        self.assertEqual(authorized.status_code, 404)
        self.assertEqual(authorized.headers["cache-control"], "no-store")
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["cache-control"], "no-store")
        self.assertEqual(
            preflight.headers["access-control-allow-origin"],
            "http://127.0.0.1:32100",
        )

    async def test_embedded_backend_ownership_proof_authenticates_without_bearer(self) -> None:
        key_material.get_key_material()
        challenge = base64.urlsafe_b64encode(b"c" * 32).decode("ascii").rstrip("=")
        expected = base64.urlsafe_b64encode(
            hmac.new(
                base64.b64decode(self.desktop_token),
                (
                    "breaktwenty-backend-ownership-v1:"
                    f"http://127.0.0.1:8765:{challenge}"
                ).encode("ascii"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii").rstrip("=")
        with mock.patch.dict(
            os.environ,
            {
                "BREAKTWENTY_KEY_BOOTSTRAP": "stdin-v2",
                "BREAKTWENTY_EMBEDDED_BACKEND_OWNERSHIP_ENDPOINT": (
                    "http://127.0.0.1:8765"
                ),
            },
        ):
            response = await self.request(
                "GET",
                f"/api/health/ownership?challenge={challenge}",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"proof": expected})

    async def test_embedded_backend_ownership_rejects_invalid_challenge(self) -> None:
        key_material.get_key_material()
        with mock.patch.dict(
            os.environ,
            {
                "BREAKTWENTY_KEY_BOOTSTRAP": "stdin-v2",
                "BREAKTWENTY_EMBEDDED_BACKEND_OWNERSHIP_ENDPOINT": (
                    "http://127.0.0.1:8765"
                ),
            },
        ):
            response = await self.request(
                "GET",
                "/api/health/ownership?challenge=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA!",
            )
        self.assertEqual(response.status_code, 400)

    async def test_only_moomoo_oauth_get_callback_crosses_launch_auth_boundary(self) -> None:
        callback = await self.request(
            "GET",
            "/api/auth/moomoo/oauth/callback?state=unknown&code=unused",
        )
        self.assertEqual(callback.status_code, 400)
        self.assertIn("could not be matched", callback.text)
        self.assertEqual(
            (await self.request("POST", "/api/auth/moomoo/oauth/callback")).status_code,
            401,
        )
        self.assertEqual(
            (await self.request("GET", "/api/auth/moomoo/oauth/unknown/status")).status_code,
            401,
        )

    def test_every_registered_http_route_is_inside_authenticated_api_boundary(self) -> None:
        route_paths = set(app.openapi()["paths"])
        self.assertTrue(route_paths)
        self.assertTrue(all(path == "/api" or path.startswith("/api/") for path in route_paths))

    async def test_disabled_framework_schema_and_docs_have_no_public_operation(self) -> None:
        for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            with self.subTest(path=path):
                self.assertEqual((await self.request("GET", path)).status_code, 404)
                self.assertEqual(
                    (
                        await self.request(
                            "GET",
                            path,
                            headers={"Authorization": f"Bearer {self.renderer_token}"},
                        )
                    ).status_code,
                    404,
                )

    async def test_cors_preflight_passes_but_actual_401_keeps_cors_headers(self) -> None:
        origin = "http://127.0.0.1:3000"
        preflight = await self.request(
            "OPTIONS",
            "/api/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers.get("access-control-allow-origin"), origin)
        denied = await self.request("GET", "/api/health", headers={"Origin": origin})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers.get("access-control-allow-origin"), origin)

    def test_configured_cors_origins_reject_remote_or_credentialed_frontends(self) -> None:
        for origin in (
            "https://127.0.0.1:3000",
            "http://attacker.test:3000",
            "http://localhost.example:3000",
            "http://user:password@localhost:3000",
            "http://localhost:3000/path",
            "http://localhost:3000?next=evil",
        ):
            with self.subTest(origin=origin):
                with mock.patch.dict(
                    os.environ,
                    {"BREAKTWENTY_CORS_ALLOW_ORIGINS": origin},
                ):
                    with self.assertRaisesRegex(RuntimeError, "private local frontend origins"):
                        get_cors_origins()

    async def test_renderer_role_cannot_mint_exchange_or_use_runner_secrets(self) -> None:
        headers = {"Authorization": f"Bearer {self.renderer_token}"}
        for method, path in (
            ("POST", "/api/auth/runner-grants"),
            ("POST", "/api/auth/runner-sessions/revoke"),
            ("POST", "/api/auth/runner-session"),
            ("GET", "/api/auth/runner-credentials"),
            ("GET", "/api/sync/runtime-artifact/rbc/storage_state"),
        ):
            with self.subTest(path=path):
                response = await self.request(method, path, headers=headers)
                self.assertEqual(response.status_code, 401)

        authenticated_not_found = await self.request(
            "GET",
            "/api/not-a-route",
            headers=headers,
        )
        self.assertEqual(authenticated_not_found.status_code, 404)

    async def test_atomic_file_rotation_invalidates_old_roles_without_restart(self) -> None:
        old_headers = {"Authorization": f"Bearer {self.renderer_token}"}
        self.assertEqual(
            (await self.request("GET", "/api/not-a-route", headers=old_headers)).status_code,
            404,
        )
        next_desktop = generate_token()
        next_renderer = generate_token()
        write_token_bundle(self.token_path, next_desktop, next_renderer)
        self.assertEqual(
            (await self.request("GET", "/api/not-a-route", headers=old_headers)).status_code,
            401,
        )
        self.assertEqual(
            (
                await self.request(
                    "GET",
                    "/api/not-a-route",
                    headers={"Authorization": f"Bearer {next_renderer}"},
                )
            ).status_code,
            404,
        )

    async def test_symlink_token_file_is_rejected_without_disclosing_its_path(self) -> None:
        real_path = self.token_path.with_name("real-launch-auth.token")
        linked_path = self.token_path.with_name("linked-launch-auth.token")
        write_token_bundle(real_path, self.desktop_token, self.renderer_token)
        linked_path.symlink_to(real_path)
        os.environ["BREAKTWENTY_LAUNCH_TOKEN_FILE"] = str(linked_path)
        reset_auth_state_for_tests()

        response = await self.request(
            "GET",
            "/api/health",
            headers={"Authorization": f"Bearer {self.renderer_token}"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Launch authentication is unavailable"})
        self.assertNotIn(str(linked_path), response.text)

    async def test_file_rotation_revokes_pre_rotation_runner_sessions(self) -> None:
        # Establish the initial generation before registering a grant.
        await self.request(
            "GET",
            "/api/not-a-route",
            headers={"Authorization": f"Bearer {self.renderer_token}"},
        )
        grant = generate_token()
        register_runner_grant(
            token=grant,
            user_id=1,
            provider="rbc",
            attempt_id="attempt-rotation",
            institution_id=7,
            add_flow=False,
        )
        from app.launch_auth import _authenticate

        grant_principal = _authenticate(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/runner-session",
                "headers": [(b"authorization", f"Bearer {grant}".encode("ascii"))],
            }
        )
        session_token, _ = issue_runner_session(grant_principal)

        write_token_bundle(self.token_path, generate_token(), generate_token())
        denied = await self.request(
            "GET",
            "/api/auth/runner-credentials",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        self.assertEqual(denied.status_code, 401)

    async def test_owner_exit_revokes_launch_roles_and_runner_sessions_immediately(self) -> None:
        from app.launch_auth import _authenticate

        await self.request(
            "GET",
            "/api/health",
            headers={"Authorization": f"Bearer {self.desktop_token}"},
        )
        grant = generate_token()
        register_runner_grant(
            token=grant,
            user_id=1,
            provider="rbc",
            attempt_id="attempt-owner-exit",
            institution_id=7,
            add_flow=False,
            purpose="visible-auth",
        )
        grant_principal = _authenticate(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/runner-session",
                "headers": [(b"authorization", f"Bearer {grant}".encode("ascii"))],
            }
        )
        runner_token, _ = issue_runner_session(grant_principal)

        revoked = await self.request(
            "POST",
            "/api/auth/launch/revoke",
            headers={"Authorization": f"Bearer {self.desktop_token}"},
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json(), {"status": "ok"})

        for method, path, token in (
            ("GET", "/api/health", self.desktop_token),
            ("GET", "/api/accounts", self.renderer_token),
            ("GET", "/api/auth/runner-credentials", runner_token),
        ):
            with self.subTest(path=path):
                denied = await self.request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(denied.status_code, 401)

    async def test_in_flight_old_desktop_principal_cannot_mint_into_new_launch(self) -> None:
        from app.launch_auth import _authenticate

        old_principal = _authenticate(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/health",
                "headers": [
                    (b"authorization", f"Bearer {self.desktop_token}".encode("ascii"))
                ],
            }
        )
        write_token_bundle(self.token_path, generate_token(), generate_token())

        with self.assertRaisesRegex(ValueError, "rotated"):
            register_runner_grant(
                token=generate_token(),
                user_id=1,
                provider="rbc",
                attempt_id="attempt-stale-principal",
                institution_id=7,
                add_flow=False,
                launch_generation=old_principal.launch_generation,
            )

    async def test_desktop_process_exit_scope_revokes_runner_session(self) -> None:
        from app.launch_auth import _authenticate

        await self.request(
            "GET",
            "/api/health",
            headers={"Authorization": f"Bearer {self.desktop_token}"},
        )
        grant = generate_token()
        register_runner_grant(
            token=grant,
            user_id=1,
            provider="rbc",
            attempt_id="attempt-process-exit",
            institution_id=7,
            add_flow=False,
            purpose="visible-auth",
        )
        principal = _authenticate(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/runner-session",
                "headers": [(b"authorization", f"Bearer {grant}".encode("ascii"))],
            }
        )
        session_token, _ = issue_runner_session(principal)

        revoked = await self.request(
            "POST",
            "/api/auth/runner-sessions/revoke",
            headers={"Authorization": f"Bearer {self.desktop_token}"},
            json={
                "provider": "rbc",
                "attempt_id": "attempt-process-exit",
                "purpose": "visible-auth",
            },
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json(), {"status": "ok", "revoked": 1})
        denied = await self.request(
            "GET",
            "/api/auth/runner-credentials",
            headers={"Authorization": f"Bearer {session_token}"},
        )
        self.assertEqual(denied.status_code, 401)

    async def test_grant_is_single_use_and_session_is_runner_route_only(self) -> None:
        grant = generate_token()
        register_runner_grant(
            token=grant,
            user_id=1,
            provider="rbc",
            attempt_id="attempt-1",
            institution_id=7,
            add_flow=False,
        )

        async def inner(scope, _receive, send):
            principal = scope.get("state", {}).get("auth_principal")
            body = b""
            if scope.get("path") == "/api/auth/runner-session":
                session_token, _ = issue_runner_session(principal)
                body = session_token.encode("ascii")
            elif scope.get("path") == "/api/auth/runner-session/revoke":
                revoke_runner_session(principal)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body})

        scoped_app = LaunchAuthMiddleware(inner)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=scoped_app),
            base_url="http://testserver",
        ) as client:
            exchanged = await client.post(
                "/api/auth/runner-session",
                headers={"Authorization": f"Bearer {grant}"},
            )
            self.assertEqual(exchanged.status_code, 200)
            session_token = exchanged.text
            repeated = await client.post(
                "/api/auth/runner-session",
                headers={"Authorization": f"Bearer {grant}"},
            )
            self.assertEqual(repeated.status_code, 401)
            accepted = await client.get(
                "/api/auth/runner-credentials",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            self.assertEqual(accepted.status_code, 200)
            revoked = await client.post(
                "/api/auth/runner-session/revoke",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            self.assertEqual(revoked.status_code, 200)
            after_revoke = await client.get(
                "/api/auth/runner-credentials",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            self.assertEqual(after_revoke.status_code, 401)
            rejected = await client.get(
                "/api/health",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            self.assertEqual(rejected.status_code, 401)

class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDb:
    def __init__(self, values):
        self.values = list(values)
        self.execute_count = 0
        self.statements = []

    async def execute(self, statement):
        self.execute_count += 1
        self.statements.append(statement)
        if not self.values:
            raise AssertionError("Unexpected database lookup")
        return _ScalarResult(self.values.pop(0))


class RunnerGrantScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_auth_state_for_tests()
        self.launch_source = mock.patch(
            "app.launch_auth._launch_token_source.read",
            return_value=LaunchTokenBundle(
                desktop=generate_token(),
                renderer=generate_token(),
                generation=1,
            ),
        )
        self.launch_source.start()
        self.user = CurrentUser(id=1)
        self.desktop = AuthPrincipal(kind="desktop", user_id=1, launch_generation=1)

    def tearDown(self) -> None:
        reset_auth_state_for_tests()
        self.launch_source.stop()

    async def test_exact_grant_registration_retry_is_idempotent_and_binding_safe(self) -> None:
        from app.launch_auth import _authenticate

        grant = generate_token()
        registration = {
            "token": grant,
            "user_id": 1,
            "provider": "tangerine",
            "attempt_id": "attempt-retry",
            "institution_id": 91,
            "add_flow": False,
            "launch_generation": 1,
        }
        register_runner_grant(**registration)
        register_runner_grant(**registration)
        different_binding = {**registration, "attempt_id": "attempt-other"}
        with self.assertRaisesRegex(ValueError, "already registered"):
            register_runner_grant(**different_binding)

        principal = _authenticate(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/runner-session",
                "headers": [(b"authorization", f"Bearer {grant}".encode("ascii"))],
            }
        )
        issue_runner_session(principal)

    async def test_existing_flow_rejects_foreign_or_provider_mismatched_connection(self) -> None:
        grant = generate_token()
        with self.assertRaises(HTTPException) as raised:
            await create_runner_grant(
                {
                    "grant": grant,
                    "provider": "rbc",
                    "attempt_id": "attempt-foreign",
                    "institution_id": 91,
                    "add_flow": False,
                },
                self.user,
                self.desktop,
                _FakeDb([None]),
            )
        self.assertEqual(raised.exception.status_code, 404)
        # A failed ownership check must not mutate the one-time registry.
        register_runner_grant(
            token=grant,
            user_id=1,
            provider="rbc",
            attempt_id="attempt-direct",
            institution_id=91,
            add_flow=False,
        )

    async def test_visible_auth_uses_catalog_institution_type_for_existing_connection(self) -> None:
        for provider, expected_type in (("ibkr", "api"), ("rbc", "scraper")):
            with self.subTest(provider=provider):
                db = _FakeDb([SimpleNamespace(enabled=True)])
                response = await create_runner_grant(
                    {
                        "grant": generate_token(),
                        "provider": provider,
                        "attempt_id": f"attempt-{provider}",
                        "institution_id": 91,
                        "add_flow": False,
                    },
                    self.user,
                    self.desktop,
                    db,
                )

                self.assertEqual(response, {"status": "ok"})
                self.assertIn(expected_type, db.statements[0].compile().params.values())

    async def test_add_flow_requires_null_connection_and_rejects_existing_provider(self) -> None:
        with self.assertRaises(HTTPException) as non_null:
            await create_runner_grant(
                {
                    "grant": generate_token(),
                    "provider": "rbc",
                    "attempt_id": "attempt-add-id",
                    "institution_id": 10,
                    "add_flow": True,
                },
                self.user,
                self.desktop,
                _FakeDb([]),
            )
        self.assertEqual(non_null.exception.status_code, 422)

        with self.assertRaises(HTTPException) as existing:
            await create_runner_grant(
                {
                    "grant": generate_token(),
                    "provider": "rbc",
                    "attempt_id": "attempt-add-existing",
                    "institution_id": None,
                    "add_flow": True,
                },
                self.user,
                self.desktop,
                _FakeDb([44]),
            )
        self.assertEqual(existing.exception.status_code, 409)

    async def test_add_flow_runner_never_falls_back_to_existing_credentials(self) -> None:
        principal = AuthPrincipal(
            kind="runner-session",
            user_id=1,
            provider="rbc",
            attempt_id="attempt-add",
            institution_id=None,
            add_flow=True,
            purpose="visible-auth",
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/auth/runner-credentials",
                "headers": [],
                "state": {"auth_principal": principal},
            }
        )
        response = await get_runner_credentials(request, _FakeDb([]))
        self.assertEqual(response, {"status": "not_found"})

    async def test_add_flow_runner_never_reads_existing_runtime_artifacts(self) -> None:
        principal = AuthPrincipal(
            kind="runner-session",
            user_id=1,
            provider="rbc",
            attempt_id="attempt-add-artifact",
            institution_id=None,
            add_flow=True,
            purpose="visible-auth",
            launch_generation=1,
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/sync/runtime-artifact/rbc/storage_state",
                "headers": [],
                "state": {"auth_principal": principal},
            }
        )
        loader = mock.AsyncMock()
        with mock.patch.object(
            sync_routes,
            "load_provider_runtime_artifact",
            new=loader,
        ):
            response = await sync_routes.get_runtime_artifact_for_visible_auth(
                "rbc",
                "storage_state",
                request,
                slots=["active"],
                current_user=self.user,
            )
        self.assertEqual(response, {"status": "not_found"})
        loader.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
