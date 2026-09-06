import asyncio
import base64
import builtins
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.connection_auth_storage import is_scraper_username_placeholder


SCRIPT_CANDIDATES = [
    path
    for path in (
        Path(os.environ["BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR"])
        if os.environ.get("BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR")
        else None,
        Path("/repo/scripts"),
        Path("/scripts"),
        *(parent / "scripts" for parent in Path(__file__).resolve().parents),
    )
    if path and (path / "visible_auth_common.py").is_file()
]
if not SCRIPT_CANDIDATES:
    raise RuntimeError("The release test layout must include the visible-auth scripts")
SCRIPTS_DIR = SCRIPT_CANDIDATES[0]
sys.path.insert(0, str(SCRIPTS_DIR))
import visible_auth_common as visible_auth  # noqa: E402
patchright_module = sys.modules.setdefault("patchright", types.ModuleType("patchright"))
patchright_async_module = sys.modules.setdefault("patchright.async_api", types.ModuleType("patchright.async_api"))
if not hasattr(patchright_async_module, "async_playwright"):
    patchright_async_module.async_playwright = None
if not hasattr(patchright_module, "async_api"):
    patchright_module.async_api = patchright_async_module
import national_visible_auth  # noqa: E402
import rbc_visible_auth  # noqa: E402
import bmo_visible_auth  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class VisibleAuthCredentialLoadTests(unittest.TestCase):
    RUNNER_GRANT = base64.b64encode(b"g" * 32).decode("ascii")
    RUNNER_SESSION = base64.b64encode(b"s" * 32).decode("ascii")

    def setUp(self):
        visible_auth._runner_session_token = None

    def tearDown(self):
        visible_auth._runner_session_token = None

    def test_username_placeholder_contract_matches_backend(self):
        samples = (
            "",
            "$USERNAME",
            "${LOGIN_USER}",
            "%EMAIL%",
            "{{ username }}",
            "<user id>",
            "alice@example.com",
            "alice_user",
        )
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(
                    visible_auth.username_looks_like_placeholder(value),
                    is_scraper_username_placeholder(value),
                )

    def test_diagnostic_scope_suffix_is_filename_safe_and_identity_explicit(self):
        self.assertEqual(
            "sync-bmo-abc_attempt-popup-123",
            visible_auth.diagnostic_scope_suffix("bmo-abc", "popup 123"),
        )

    def test_terminal_failure_emits_diagnostics_ready_only_after_runner_returns(self):
        client = visible_auth.VisibleAuthBackendClient(
            backend_api_base_url="http://127.0.0.1:8000/api",
            provider="eqbank",
            attempt_id="attempt-final",
            add_flow=False,
        )
        client.sync_id = "eqbank-sync-final"
        with patch.object(visible_auth, "backend_post_json", return_value={"sync_id": "eqbank-sync-final"}):
            asyncio.run(
                client.log_support_event(
                    stage="sync result",
                    result="failed",
                    message="upstream login failed",
                    last_output="upstream login failed",
                )
            )

        with (
            patch.object(visible_auth, "_visible_auth_backend_clients", [client]),
            patch.object(builtins, "print") as print_mock,
        ):
            visible_auth.print_visible_auth_diagnostics_ready(
                fallback_result="failed",
                fallback_message="fallback",
            )

        printed = print_mock.call_args.args[0]
        self.assertTrue(printed.startswith(visible_auth.VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX))
        payload = json.loads(printed.removeprefix(visible_auth.VISIBLE_AUTH_DIAGNOSTICS_READY_PREFIX))
        self.assertEqual("eqbank-sync-final", payload["sync_id"])
        self.assertEqual("attempt-final", payload["attempt_id"])
        self.assertEqual("failed", payload["result"])
        self.assertEqual("upstream login failed", payload["message"])
        self.assertIsNone(client.terminal_support_event)

    def test_every_catalog_visible_auth_runner_uses_shared_terminal_main(self):
        catalog_candidates = [
            Path(os.environ["PROVIDER_CATALOG_PATH"])
            if os.environ.get("PROVIDER_CATALOG_PATH")
            else None,
            SCRIPTS_DIR.parent / "config" / "provider_catalog.json",
            Path("/app/config/provider_catalog.json"),
        ]
        catalog_path = next(path for path in catalog_candidates if path and path.is_file())
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        runner_scripts = []
        for metadata in catalog.values():
            if not isinstance(metadata, dict):
                continue
            visible_auth_config = (
                ((metadata.get("frontend") or {}).get("scraperAuth") or {}).get("desktopVisibleAuth") or {}
            )
            if visible_auth_config.get("enabled") is not True:
                continue
            command = visible_auth_config.get("runnerCommand") or []
            script_name = next((Path(part).name for part in command if str(part).endswith("_visible_auth.py")), "")
            self.assertTrue(script_name)
            runner_scripts.append(script_name)

        self.assertTrue(runner_scripts)
        for script_name in runner_scripts:
            with self.subTest(script=script_name):
                script_text = (SCRIPTS_DIR / script_name).read_text(encoding="utf-8")
                self.assertIn("return visible_auth.run_visible_auth_main(", script_text)

    def test_resolves_provider_platform_from_current_os(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(visible_auth.platform, "system", return_value="Linux"):
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("rbc"), "linux")
                self.assertEqual(visible_auth.provider_visible_auth_flow("rbc", "linux"), "rbc:linux")
            with patch.object(visible_auth.platform, "system", return_value="Windows"):
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("rbc"), "windows")
                self.assertEqual(visible_auth.provider_visible_auth_flow("rbc", "windows"), "rbc:windows")
            with patch.object(visible_auth.platform, "system", return_value="Darwin"):
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("rbc"), "mac")
                self.assertEqual(visible_auth.provider_visible_auth_flow("rbc", "mac"), "rbc:mac")

    def test_provider_platform_override_wins_over_global(self):
        with patch.dict(
            os.environ,
            {
                "BREAKTWENTY_VISIBLE_AUTH_PLATFORM": "linux",
                "BREAKTWENTY_RBC_VISIBLE_AUTH_PLATFORM": "rbc:windows",
            },
            clear=True,
        ):
            with patch.object(visible_auth.platform, "system", return_value="Darwin"):
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("rbc"), "windows")
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("tangerine"), "linux")

    def test_managed_browser_launch_args_are_platform_neutral(self):
        with patch.object(visible_auth.platform, "system", return_value="Linux"):
            self.assertEqual(visible_auth.managed_browser_launch_args(), [])
        with patch.object(visible_auth.platform, "system", return_value="Windows"):
            self.assertEqual(visible_auth.managed_browser_launch_args(), [])

    def test_unknown_provider_platform_falls_back_to_linux_baseline(self):
        with patch.dict(os.environ, {"BREAKTWENTY_RBC_VISIBLE_AUTH_PLATFORM": "rbc:future-os"}, clear=True):
            with patch.object(visible_auth.platform, "system", return_value="Windows"):
                self.assertEqual(visible_auth.resolve_provider_visible_auth_platform("rbc"), "linux")
                self.assertEqual(visible_auth.provider_visible_auth_flow("rbc", "rbc:future-os"), "rbc:linux")

    def test_loads_credentials_from_backend_endpoint(self):
        seen_requests = []

        def fake_urlopen(request, *, timeout):
            seen_requests.append((request.full_url, timeout))
            return FakeResponse({"status": "ok", "username": "client", "password": "secret"})

        with patch.dict(
            os.environ,
            {
                "BREAKTWENTY_BACKEND_API_URL": "http://127.0.0.1:8000/api",
                "BREAKTWENTY_VISIBLE_AUTH_PROVIDER": "tangerine",
            },
        ):
            with (
                patch.object(visible_auth, "_runner_session_token", self.RUNNER_SESSION),
                patch.object(visible_auth.urllib_request, "urlopen", side_effect=fake_urlopen),
            ):
                self.assertEqual(
                    visible_auth.load_provider_credentials(provider="tangerine"),
                    ("client", "secret"),
                )

        self.assertEqual(seen_requests, [("http://127.0.0.1:8000/api/auth/runner-credentials", 10)])

    def test_loads_credentials_from_connection_scoped_backend_endpoint(self):
        seen_requests = []

        def fake_urlopen(request, *, timeout):
            seen_requests.append((request.full_url, timeout))
            return FakeResponse({"status": "ok", "username": "client", "password": "secret"})

        with patch.dict(
            os.environ,
            {
                "BREAKTWENTY_BACKEND_API_URL": "http://127.0.0.1:8000/api",
                "BREAKTWENTY_VISIBLE_AUTH_INSTITUTION_ID": "8",
                "BREAKTWENTY_VISIBLE_AUTH_PROVIDER": "tangerine",
            },
        ):
            with (
                patch.object(visible_auth, "_runner_session_token", self.RUNNER_SESSION),
                patch.object(visible_auth.urllib_request, "urlopen", side_effect=fake_urlopen),
            ):
                self.assertEqual(
                    visible_auth.load_provider_credentials(provider="tangerine"),
                    ("client", "secret"),
                )

        self.assertEqual(seen_requests, [("http://127.0.0.1:8000/api/auth/runner-credentials", 10)])

    def test_runner_grant_is_exchanged_once_from_stdin_and_cached_only_in_memory(self):
        seen_requests = []

        def fake_urlopen(request, *, timeout):
            seen_requests.append((request.full_url, request.get_header("Authorization"), timeout))
            return FakeResponse({
                "status": "ok",
                "access_token": self.RUNNER_SESSION,
                "token_type": "bearer",
            })

        stdin = types.SimpleNamespace(
            buffer=io.BytesIO(
                f'{{"version":1,"grant":"{self.RUNNER_GRANT}"}}\n'.encode("ascii")
            )
        )
        with (
            patch.dict(os.environ, {"BREAKTWENTY_BACKEND_API_URL": "http://127.0.0.1:8000/api"}),
            patch.object(visible_auth.sys, "stdin", stdin),
            patch.object(visible_auth.urllib_request, "urlopen", side_effect=fake_urlopen),
        ):
            self.assertEqual(
                visible_auth._runner_authorization_header(),
                f"Bearer {self.RUNNER_SESSION}",
            )
            self.assertEqual(
                visible_auth._runner_authorization_header(),
                f"Bearer {self.RUNNER_SESSION}",
            )

        self.assertEqual(
            seen_requests,
            [(
                "http://127.0.0.1:8000/api/auth/runner-session",
                f"Bearer {self.RUNNER_GRANT}",
                30,
            )],
        )

    def test_runner_rejects_hostile_backend_urls_before_sending_any_secret(self):
        rejected = (
            "https://127.0.0.1:8000/api",
            "http://backend.test/api",
            "http://localhost.example/api",
            "http://127.0.0.2/api",
            "http://user:password@localhost:8000/api",
            "http://localhost:8000/api?next=evil",
            "http://localhost:8000/api#fragment",
            "http://localhost:8000/api/v1",
            "http://localhost\\@evil.test/api",
        )
        for backend_api_url in rejected:
            with self.subTest(backend_api_url=backend_api_url):
                urlopen = Mock()
                with (
                    patch.dict(
                        os.environ,
                        {
                            "BREAKTWENTY_BACKEND_API_URL": backend_api_url,
                            "BREAKTWENTY_VISIBLE_AUTH_PROVIDER": "rbc",
                        },
                        clear=True,
                    ),
                    patch.object(visible_auth, "_runner_session_token", self.RUNNER_SESSION),
                    patch.object(visible_auth.urllib_request, "urlopen", urlopen),
                ):
                    self.assertEqual(visible_auth._backend_api_base_url_from_env(), "")
                    self.assertIsNone(visible_auth.load_provider_credentials(provider="rbc"))
                urlopen.assert_not_called()

    def test_runner_post_rejects_cross_origin_url_before_sending_bearer(self):
        urlopen = Mock()
        with patch.object(visible_auth.urllib_request, "urlopen", urlopen):
            with self.assertRaisesRegex(RuntimeError, "unsupported BreakTwenty backend URL"):
                visible_auth._post_json_with_bearer(
                    "http://evil.test/api/auth/runner-session",
                    {},
                    backend_api_base_url="http://127.0.0.1:8000/api",
                    authorization=f"Bearer {self.RUNNER_GRANT}",
                )
        urlopen.assert_not_called()

    def test_visible_auth_client_rejects_nonlocal_backend_at_construction(self):
        with self.assertRaisesRegex(ValueError, "private local API boundary"):
            visible_auth.VisibleAuthBackendClient(
                backend_api_base_url="http://backend.test/api",
                provider="rbc",
                attempt_id="attempt-1",
                add_flow=False,
            )

    def test_persists_credentials_as_attempt_scoped_staging(self):
        seen_payloads = []

        def fake_backend_post_json(url, payload, *, backend_api_base_url):
            seen_payloads.append((url, payload, backend_api_base_url))
            return {"status": "ok"}

        client = visible_auth.VisibleAuthBackendClient(
            backend_api_base_url="http://127.0.0.1:8000/api",
            provider="ibkr",
            attempt_id="attempt-1",
            add_flow=False,
        )

        with patch.object(visible_auth, "backend_post_json", side_effect=fake_backend_post_json):
            response = asyncio.run(client.persist_credentials("client", "secret"))

        self.assertEqual(response, {"status": "ok"})
        self.assertEqual(
            seen_payloads,
            [(
                "http://127.0.0.1:8000/api/sync/visible-auth-credentials",
                {
                    "provider": "ibkr",
                    "attempt_id": "attempt-1",
                    "add_flow": False,
                    "username": "client",
                    "password": "secret",
                },
                "http://127.0.0.1:8000/api",
            )],
        )

    def test_persists_visible_auth_artifact_as_staging_only(self):
        seen_payloads = []

        def fake_backend_post_json(url, payload, *, backend_api_base_url):
            seen_payloads.append((url, payload, backend_api_base_url))
            return {"status": "ok", "staged": True}

        client = visible_auth.VisibleAuthBackendClient(
            backend_api_base_url="http://127.0.0.1:8000/api",
            provider="rbc",
            attempt_id="attempt-1",
            add_flow=False,
        )

        with patch.object(visible_auth, "backend_post_json", side_effect=fake_backend_post_json):
            response = asyncio.run(
                client.persist_artifact(
                    artifact_type="storage_state",
                    payload={"cookies": [], "origins": []},
                )
            )

        self.assertEqual(response, {"status": "ok", "staged": True})
        self.assertEqual(
            seen_payloads,
            [(
                "http://127.0.0.1:8000/api/sync/visible-auth-artifact",
                {
                    "provider": "rbc",
                    "attempt_id": "attempt-1",
                    "artifact_type": "storage_state",
                    "payload": {"cookies": [], "origins": []},
                    "add_flow": False,
                },
                "http://127.0.0.1:8000/api",
            )],
        )

    def test_handoff_result_exposes_only_staging_state(self):
        result = visible_auth.build_visible_auth_handoff_result(
            provider="rbc",
            attempt_id="attempt-1",
            sync_id="sync-1",
            storage_result={"staged": True},
            session_result={"staged": True},
            credentials_result={"staged": True},
        )

        self.assertTrue(result["storageStateStaged"])
        self.assertTrue(result["sessionArtifactStaged"])
        self.assertTrue(result["credentialsStaged"])
        self.assertNotIn("storageStatePromoted", result)
        self.assertNotIn("sessionArtifactPromoted", result)
        self.assertNotIn("credentialsSaved", result)

    def test_developer_local_har_preserves_non_secret_request_and_response_details(self):
        entry = {
            "request": {
                "url": "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi?REQUEST=AcctTransactionInquiry",
                "headers": [{"name": "Cookie", "value": "SESSIONID=secret"}],
                "cookies": [{"name": "SESSIONID", "value": "secret"}],
                "queryString": [{"name": "REQUEST", "value": "AcctTransactionInquiry"}],
                "postData": {
                    "mimeType": "application/x-www-form-urlencoded",
                    "params": [
                        {"name": "REQUEST", "value": "AcctTransactionInquiry"},
                        {"name": "ACCOUNT_TYPE", "value": "R"},
                        {"name": "ACCOUNT_NUMBER", "value": "1234567890"},
                        {"name": "QQ", "value": "raw-password"},
                    ],
                    "text": (
                        "REQUEST=AcctTransactionInquiry&ACCOUNT_TYPE=R&"
                        "ACCOUNT_NUMBER=1234567890&QQ=raw-password"
                    ),
                },
            },
            "response": {
                "status": 200,
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {
                    "mimeType": "application/json",
                    "text": json.dumps(
                        {
                            "accountNumber": "1234567890",
                            "nickname": "LOC",
                            "token": "secret-token",
                        }
                    ),
                },
            },
        }

        sanitized = visible_auth.sanitize_har_entry(
            entry,
            should_keep_response_body=lambda _url, _status, _content_type: True,
            safe_response_keys=frozenset(),
            capture_level=visible_auth.CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        self.assertIn("REQUEST=AcctTransactionInquiry", sanitized["request"]["url"])
        self.assertEqual(sanitized["request"]["queryString"][0]["value"], "AcctTransactionInquiry")
        params = sanitized["request"]["postData"]["params"]
        self.assertEqual(params[0]["value"], "AcctTransactionInquiry")
        self.assertEqual(params[1]["value"], "R")
        self.assertEqual(params[2]["value"], "1234567890")
        self.assertEqual(params[3]["value"], visible_auth.REDACTED_VALUE)
        form_body = sanitized["request"]["postData"]["text"]
        self.assertIn("ACCOUNT_NUMBER=1234567890", form_body)
        self.assertIn("QQ=%3Credacted%3E", form_body)
        response_body = json.loads(sanitized["response"]["content"]["text"])
        self.assertEqual(response_body["accountNumber"], "1234567890")
        self.assertEqual(response_body["nickname"], "LOC")
        self.assertEqual(response_body["token"], visible_auth.REDACTED_VALUE)

    def test_developer_local_har_keeps_text_response_even_without_provider_allowlist(self):
        entry = {
            "request": {
                "url": "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi",
                "headers": [],
                "cookies": [],
                "queryString": [],
            },
            "response": {
                "status": 200,
                "headers": [{"name": "Content-Type", "value": "text/html"}],
                "cookies": [],
                "content": {"mimeType": "text/html", "text": "<html>LOC activity page</html>"},
            },
        }

        sanitized = visible_auth.sanitize_har_entry(
            entry,
            should_keep_response_body=lambda _url, _status, _content_type: False,
            safe_response_keys=frozenset(),
            capture_level=visible_auth.CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )

        self.assertEqual(
            sanitized["response"]["content"]["text"],
            "<html>LOC activity page</html>",
        )

    def test_redacted_har_keeps_existing_request_body_redaction(self):
        entry = {
            "request": {
                "url": "https://www1.royalbank.com/cgi-bin/rbaccess/rbunxcgi?ACCOUNT_NUMBER=1234567890",
                "headers": [],
                "cookies": [],
                "queryString": [],
                "postData": {
                    "mimeType": "application/x-www-form-urlencoded",
                    "params": [{"name": "ACCOUNT_NUMBER", "value": "1234567890"}],
                    "text": "ACCOUNT_NUMBER=1234567890",
                },
            },
            "response": {"status": 200, "headers": [], "cookies": [], "content": {"text": ""}},
        }

        sanitized = visible_auth.sanitize_har_entry(
            entry,
            should_keep_response_body=lambda _url, _status, _content_type: True,
            safe_response_keys=frozenset(),
            capture_level=visible_auth.CAPTURE_LEVEL_REDACTED_RICH,
        )

        self.assertNotIn("1234567890", sanitized["request"]["url"])
        self.assertEqual(
            sanitized["request"]["postData"]["params"][0]["value"],
            visible_auth.REDACTED_VALUE,
        )
        self.assertEqual(
            sanitized["request"]["postData"]["text"],
            "ACCOUNT_NUMBER=%3Credacted%3E",
        )

    def test_returns_none_when_backend_credentials_are_absent(self):
        with patch.dict(os.environ, {"BREAKTWENTY_BACKEND_API_URL": "http://127.0.0.1:8000/api"}):
            with patch.object(
                visible_auth.urllib_request,
                "urlopen",
                return_value=FakeResponse({"status": "not_found"}),
            ):
                self.assertIsNone(
                    visible_auth.load_provider_credentials(provider="ibkr")
                )

    def test_loads_runtime_artifact_from_backend_endpoint(self):
        seen_requests = []

        def fake_urlopen(request, *, timeout):
            seen_requests.append((request.full_url, timeout))
            return FakeResponse({
                "status": "ok",
                "slot": "quarantine",
                "payload": {"cookies": [], "origins": []},
            })

        with patch.dict(os.environ, {"BREAKTWENTY_BACKEND_API_URL": "http://127.0.0.1:8000/api"}):
            with (
                patch.object(visible_auth, "_runner_session_token", self.RUNNER_SESSION),
                patch.object(visible_auth.urllib_request, "urlopen", side_effect=fake_urlopen),
            ):
                payload, slot = visible_auth.load_provider_artifact(
                    provider="rbc",
                    artifact_kind="storage_state",
                    slots=("quarantine", "active"),
                )

        self.assertEqual(payload, {"cookies": [], "origins": []})
        self.assertEqual(slot, "quarantine")
        self.assertEqual(
            seen_requests,
            [(
                "http://127.0.0.1:8000/api/sync/runtime-artifact/rbc/storage_state?slots=quarantine&slots=active",
                10,
            )],
        )

    def test_visible_auth_sources_never_install_page_init_scripts(self):
        source_paths = [
            SCRIPTS_DIR / "visible_auth_common.py",
            *sorted(SCRIPTS_DIR.glob("*_visible_auth.py")),
        ]
        forbidden = (
            "Page.addScriptToEvaluateOnNewDocument",
            ".add_init_script(",
            "patchright-init-script-inject.internal",
        )
        for source_path in source_paths:
            source = source_path.read_text(encoding="utf-8")
            for marker in forbidden:
                self.assertNotIn(marker, source, f"{source_path.name} must not install early page scripts")

    def test_national_credentials_do_not_use_cdp_init_capture(self):
        source = (SCRIPTS_DIR / "national_visible_auth.py").read_text(encoding="utf-8")
        forbidden = (
            "build_cdp_credential_capture_init_script",
            "install_cdp_credential_capture_init_script",
            "capture_cdp_credential_snapshot",
            "NATIONAL_CREDENTIAL_CAPTURE_INIT_SCRIPT",
            "_national_credential_capture_installed",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)

    def test_national_chrome_identity_uses_the_managed_runtime_version(self):
        identity = national_visible_auth._national_chrome_compat_identity(
            {"chromium_major": "150"}
        )

        self.assertIn("Chrome/150.0.0.0", identity["user_agent"])
        self.assertEqual(
            identity["metadata"]["brands"],
            [
                {"brand": "Not;A=Brand", "version": "8"},
                {"brand": "Chromium", "version": "150"},
                {"brand": "Google Chrome", "version": "150"},
            ],
        )
        self.assertEqual(identity["metadata"]["fullVersionList"][1]["version"], "150.0.0.0")

    def test_national_user_agent_override_uses_cdp_emulation_only(self):
        class FakeSession:
            def __init__(self):
                self.sent = []

            async def send(self, method, params=None):
                self.sent.append((method, params))

        class FakeContext:
            def __init__(self):
                self.session = FakeSession()

            async def new_cdp_session(self, _page):
                return self.session

        class FakePage:
            pass

        context = FakeContext()
        page = FakePage()
        identity = national_visible_auth._national_chrome_compat_identity({"chromium_major": "150"})

        applied = asyncio.run(
            national_visible_auth._apply_national_user_agent_override(
                context,
                page,
                identity,
                phase="test",
            )
        )

        self.assertTrue(applied)
        self.assertEqual([method for method, _params in context.session.sent], ["Emulation.setUserAgentOverride"])
        self.assertTrue(getattr(page, "_national_user_agent_override_applied", False))


class RBCVisibleAuthCredentialMappingTests(unittest.TestCase):
    GENERATED_RBC_SECRET = "8,eyJ" + ("A" * 120)

    def test_request_mapping_uses_q1_when_qq_is_masked(self):
        self.assertEqual(
            rbc_visible_auth._credentials_from_mapping(
                {
                    "K1": "client-card",
                    "QQ": "******",
                    "Q1": "real-password",
                }
            ),
            ("client-card", "real-password"),
        )

    def test_request_mapping_accepts_q1_password_without_qq(self):
        self.assertEqual(
            rbc_visible_auth._credentials_from_mapping(
                {
                    "K1": "client-card",
                    "Q1": "real-password",
                }
            ),
            ("client-card", "real-password"),
        )

    def test_request_mapping_uses_q1_when_qq_is_generated_payload(self):
        self.assertEqual(
            rbc_visible_auth._credentials_from_mapping(
                {
                    "K1": "client-card",
                    "QQ": self.GENERATED_RBC_SECRET,
                    "Q1": "real-password",
                }
            ),
            ("client-card", "real-password"),
        )

    def test_request_mapping_rejects_generated_password_payload(self):
        self.assertIsNone(
            rbc_visible_auth._credentials_from_mapping(
                {
                    "K1": "client-card",
                    "QQ": self.GENERATED_RBC_SECRET,
                }
            )
        )


class BMOVisibleAuthSessionArtifactTests(unittest.TestCase):
    def test_sanitized_har_filename_carries_sync_and_attempt_identity(self):
        with (
            patch.object(bmo_visible_auth.VISIBLE_AUTH_CLIENT, "sync_id", "bmo-attempt-a"),
            patch.object(bmo_visible_auth, "ATTEMPT_ID", "popup-a"),
        ):
            path = bmo_visible_auth._bmo_sanitized_har_path(
                1,
                "20260826T200000Z",
                "failed",
            )

        self.assertIn("_sync-bmo-attempt-a_attempt-popup-a_failed.har", path.name)

    def test_cdb_init_session_summary_is_saved_in_session_artifact(self):
        summary = {"categories": [{"categoryName": "BA", "products": []}]}
        auth_signals = {
            "summary": bmo_visible_auth._extract_summary(
                {"InitCDBSessionRs": {"BodyRs": {"mySummary": summary}}}
            )
        }

        artifact = bmo_visible_auth.build_bmo_session_artifact(
            auth_signals,
            user_agent="test-agent",
        )

        self.assertEqual(summary, artifact["summary"])


if __name__ == "__main__":
    unittest.main()
