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
from unittest.mock import AsyncMock, Mock, patch

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
import amex_visible_auth  # noqa: E402
import scotiabank_visible_auth  # noqa: E402
import td_visible_auth  # noqa: E402


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

    def test_provider_origin_helpers_require_exact_dns_boundaries(self):
        cases = (
            (
                "amex",
                amex_visible_auth._amex_origin_from_url,
                "americanexpress.com",
                "www.americanexpress.com",
                "evilamericanexpress.com",
                amex_visible_auth.AMEX_LOGIN_URL,
                "https://www.americanexpress.com",
            ),
            (
                "scotiabank",
                scotiabank_visible_auth._scotia_origin_from_url,
                "scotiabank.com",
                "secure.scotiabank.com",
                "evilscotiabank.com",
                scotiabank_visible_auth.SCOTIA_ACCOUNTS_URL,
                scotiabank_visible_auth.SCOTIA_SECURE_ORIGIN,
            ),
            (
                "td",
                td_visible_auth._td_origin_from_url,
                "td.com",
                "authentication.td.com",
                "eviltd.com",
                td_visible_auth.TD_AUTH_UI_URL,
                "https://authentication.td.com",
            ),
        )

        for name, origin_from_url, root, subdomain, lookalike, expected_url, expected_origin in cases:
            with self.subTest(provider=name, case="exact root"):
                self.assertEqual(origin_from_url(f"https://{root}/login"), f"https://{root}")
            with self.subTest(provider=name, case="valid subdomain and port"):
                self.assertEqual(
                    origin_from_url(f"http://{subdomain}:8443/login"),
                    f"http://{subdomain}:8443",
                )
            with self.subTest(provider=name, case="deceptive suffix"):
                self.assertIsNone(origin_from_url(f"https://{lookalike}/login"))
            with self.subTest(provider=name, case="unrelated hostname"):
                self.assertIsNone(origin_from_url("https://example.com/login"))
            with self.subTest(provider=name, case="malformed URL"):
                self.assertIsNone(origin_from_url("https://[invalid"))
            with self.subTest(provider=name, case="expected provider URL"):
                self.assertEqual(origin_from_url(expected_url), expected_origin)

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

    def test_observed_rbc_login_post_path_is_classified_as_login(self):
        self.assertTrue(
            rbc_visible_auth._rbc_is_login_request_url(
                "https://www1.royalbank.com/cgi-bin/rbaccess/rbcgi3m01"
            )
        )

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

class RBCVisibleAuthCaptureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_input_scan_groups_selectors_into_one_browser_query(self):
        class FakeInput:
            def __init__(self, value, *, visible):
                self.value = value
                self.visible = visible

            async def is_visible(self, *, timeout):
                del timeout
                return self.visible

            async def input_value(self, *, timeout):
                del timeout
                return self.value

        class FakeLocator:
            def __init__(self):
                self.inputs = [
                    FakeInput("******", visible=True),
                    FakeInput("real-password", visible=False),
                ]

            async def count(self):
                return len(self.inputs)

            def nth(self, index):
                return self.inputs[index]

        class FakePage:
            def __init__(self):
                self.main_frame = object()
                self.frames = []
                self.selector_queries = []

            def locator(self, selector):
                self.selector_queries.append(selector)
                return FakeLocator()

        page = FakePage()

        value = await rbc_visible_auth._rbc_first_input_value(
            page,
            rbc_visible_auth.RBC_PASSWORD_SELECTORS,
            reject_transformed=True,
        )

        self.assertEqual("real-password", value)
        self.assertEqual(1, len(page.selector_queries))
        self.assertIn("input#QQ[name='QQ']", page.selector_queries[0])
        self.assertIn("input#Q1", page.selector_queries[0])

    async def test_hidden_fallback_prioritizes_raw_password_before_username(self):
        credentials = {"username": "", "password": ""}
        calls = []

        async def input_value(_page, selectors, **_kwargs):
            calls.append(selectors)
            if selectors is rbc_visible_auth.RBC_PASSWORD_SELECTORS:
                return "real-password"
            return "client-card"

        with (
            patch.object(rbc_visible_auth, "_rbc_first_input_value", side_effect=input_value),
            patch.object(rbc_visible_auth, "_rbc_first_visible_input_value", new=AsyncMock()) as visible_scan,
        ):
            await rbc_visible_auth._capture_rbc_raw_input_credentials(
                object(),
                credentials,
                include_hidden_fallback=True,
            )

        self.assertEqual(
            [
                rbc_visible_auth.RBC_PASSWORD_SELECTORS,
                rbc_visible_auth.RBC_USERNAME_SELECTORS,
            ],
            calls,
        )
        visible_scan.assert_not_awaited()
        self.assertEqual("client-card", credentials["username"])
        self.assertEqual("real-password", credentials["password"])

    async def test_transformed_password_candidates_do_not_hide_later_raw_value(self):
        class FakeInput:
            def __init__(self, value):
                self.value = value

            async def input_value(self, *, timeout):
                del timeout
                return self.value

        class FakeLocator:
            def __init__(self):
                self.inputs = [
                    FakeInput("******"),
                    FakeInput(self_generated_secret),
                    FakeInput("real-password"),
                ]

            async def count(self):
                return len(self.inputs)

            def nth(self, index):
                return self.inputs[index]

        class FakePage:
            def __init__(self):
                self.main_frame = object()
                self.frames = []

            def locator(self, _selector):
                return FakeLocator()

        self_generated_secret = RBCVisibleAuthCredentialMappingTests.GENERATED_RBC_SECRET
        value = await rbc_visible_auth._rbc_first_input_value(
            FakePage(),
            rbc_visible_auth.RBC_PASSWORD_SELECTORS,
            reject_transformed=True,
        )

        self.assertEqual("real-password", value)

    async def test_windows_login_route_captures_live_fields_before_request_continues(self):
        credentials = {"username": "", "password": ""}
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()
        page = types.SimpleNamespace(is_closed=Mock(return_value=False))

        class FakeContext:
            def __init__(self):
                self.pattern = ""
                self.handler = None

            async def route(self, pattern, handler):
                self.pattern = pattern
                self.handler = handler

        class FakeRoute:
            def __init__(self):
                self.continued = False

            async def continue_(self):
                self.assert_live_capture_complete()
                self.continued = True

            def assert_live_capture_complete(self):
                if credentials.get("password") != "real-password":
                    raise AssertionError("login request continued before live credential capture")

        async def capture_live_fields(_page, captured_credentials, **_kwargs):
            self.assertIs(page, _page)
            rbc_visible_auth._capture_rbc_username(
                captured_credentials,
                "client-card",
                source="raw",
            )
            rbc_visible_auth._capture_rbc_password(
                captured_credentials,
                "real-password",
                source="raw",
            )

        context = FakeContext()
        route = FakeRoute()
        request = types.SimpleNamespace(
            method="POST",
            url="https://www1.royalbank.com/cgi-bin/rbaccess/rbcgi3m01",
            post_data="K1=client-card&QQ=******&Q1=",
            frame=types.SimpleNamespace(page=page),
        )

        with (
            patch.object(rbc_visible_auth, "RBC_WINDOWS_FLOW", True),
            patch.object(
                rbc_visible_auth,
                "_capture_rbc_raw_input_credentials",
                side_effect=capture_live_fields,
            ),
        ):
            await rbc_visible_auth._install_rbc_windows_login_route_capture(
                context,
                credentials,
                capture_state,
            )
            await context.handler(route, request)

        self.assertEqual(rbc_visible_auth.RBC_WINDOWS_LOGIN_ROUTE_PATTERN, context.pattern)
        self.assertTrue(route.continued)
        self.assertEqual("client-card", credentials["username"])
        self.assertEqual("real-password", credentials["password"])
        summary = capture_state.summary()
        self.assertEqual(1, summary["route_install_success_count"])
        self.assertEqual(1, summary["route_login_post_count"])
        self.assertEqual(["masked"], summary["credential_field_observations"]["route"]["QQ"])
        self.assertNotIn("client-card", json.dumps(summary))
        self.assertNotIn("real-password", json.dumps(summary))

    async def test_windows_login_route_keeps_password_captured_before_later_scan_timeout(self):
        credentials = {"username": "", "password": ""}
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()
        page = types.SimpleNamespace(is_closed=Mock(return_value=False))

        class FakeContext:
            def __init__(self):
                self.handler = None

            async def route(self, _pattern, handler):
                self.handler = handler

        class FakeRoute:
            def __init__(self):
                self.continued = False

            async def continue_(self):
                self.continued = True

        async def input_value(_page, selectors, **_kwargs):
            if selectors is rbc_visible_auth.RBC_PASSWORD_SELECTORS:
                return "real-password"
            await asyncio.sleep(1)
            return ""

        context = FakeContext()
        route = FakeRoute()
        request = types.SimpleNamespace(
            method="POST",
            url="https://www1.royalbank.com/cgi-bin/rbaccess/rbcgi3m01",
            post_data="K1=client-card&QQ=******&Q1=",
            frame=types.SimpleNamespace(page=page),
        )

        with (
            patch.object(rbc_visible_auth, "RBC_WINDOWS_FLOW", True),
            patch.object(rbc_visible_auth, "RBC_WINDOWS_LOGIN_ROUTE_CAPTURE_TIMEOUT_SECONDS", 0.01),
            patch.object(rbc_visible_auth, "_rbc_first_input_value", side_effect=input_value),
        ):
            await rbc_visible_auth._install_rbc_windows_login_route_capture(
                context,
                credentials,
                capture_state,
            )
            await context.handler(route, request)

        self.assertTrue(route.continued)
        self.assertEqual(("client-card", "real-password"), rbc_visible_auth.collect_rbc_credentials(credentials))
        self.assertEqual(["TimeoutError"], capture_state.summary()["route_capture_error_types"])
        details = rbc_visible_auth._rbc_credential_capture_details(credentials, capture_state)
        self.assertGreater(details["unmasked_login_value_capture_count"], 0)
        self.assertTrue(details["unmasked_login_value_captured"])
        self.assertNotIn("client-card", json.dumps(details))
        self.assertNotIn("real-password", json.dumps(details))

    async def test_cdp_request_capture_is_installed_then_drained(self):
        class FakePage:
            pass

        class FakeSession:
            def __init__(self):
                self.handlers = {}

            async def send(self, method, params=None):
                del params
                if method == "Network.getRequestPostData":
                    await asyncio.sleep(0)
                    return {"postData": "K1=client-card&QQ=******&Q1=real-password"}
                return {}

            def on(self, event, callback):
                self.handlers[event] = callback

        session = FakeSession()
        context = types.SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
        page = FakePage()
        credentials = {"username": "", "password": ""}
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()

        with patch.object(rbc_visible_auth, "RBC_WINDOWS_FLOW", True):
            await rbc_visible_auth._install_rbc_windows_cdp_request_capture(
                context,
                page,
                credentials,
                capture_state,
            )
            session.handlers["Network.requestWillBeSent"](
                {
                    "requestId": "request-1",
                    "request": {
                        "method": "POST",
                        "url": rbc_visible_auth.RBC_LOGIN_URL,
                        "hasPostData": True,
                    },
                }
            )
            drained = await rbc_visible_auth._drain_rbc_capture_tasks(capture_state)

        self.assertTrue(drained)
        self.assertTrue(getattr(page, "_rbc_windows_cdp_request_capture_installed", False))
        self.assertEqual("client-card", credentials["username"])
        self.assertEqual("real-password", credentials["password"])
        summary = capture_state.summary()
        self.assertEqual(1, summary["cdp_install_success_count"])
        self.assertEqual(1, summary["cdp_login_post_count"])
        self.assertEqual(1, summary["cdp_post_data_retrieved_count"])
        cdp_fields = summary["credential_field_observations"]["cdp_request"]
        self.assertEqual(["raw"], cdp_fields["K1"])
        self.assertEqual(["masked"], cdp_fields["QQ"])
        self.assertEqual(["raw"], cdp_fields["Q1"])
        self.assertEqual(1, summary["capture_task_drain_initial_pending_count"])
        self.assertNotIn("client-card", json.dumps(summary))
        self.assertNotIn("real-password", json.dumps(summary))

    async def test_failed_cdp_install_is_not_marked_installed(self):
        page = types.SimpleNamespace()
        context = types.SimpleNamespace(
            new_cdp_session=AsyncMock(side_effect=RuntimeError("sensitive failure text"))
        )
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()

        with patch.object(rbc_visible_auth, "RBC_WINDOWS_FLOW", True):
            await rbc_visible_auth._install_rbc_windows_cdp_request_capture(
                context,
                page,
                {"username": "", "password": ""},
                capture_state,
            )

        self.assertFalse(getattr(page, "_rbc_windows_cdp_request_capture_installed", False))
        self.assertEqual(1, capture_state.cdp_install_failure_count)
        self.assertEqual(["RuntimeError"], capture_state.summary()["cdp_install_error_types"])
        self.assertNotIn("sensitive failure text", json.dumps(capture_state.summary()))

    async def test_patchright_request_fallback_task_is_tracked_and_classified(self):
        class FakePage:
            def __init__(self):
                self.handlers = {}

            def on(self, event, callback):
                self.handlers[event] = callback

        page = FakePage()
        request = types.SimpleNamespace(
            method="POST",
            url=rbc_visible_auth.RBC_LOGIN_URL,
            post_data="K1=client-card&QQ=******&Q1=",
        )
        credentials = {"username": "", "password": ""}
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()

        with patch.object(
            rbc_visible_auth,
            "_capture_rbc_raw_input_credentials_burst",
            new=AsyncMock(),
        ) as capture_burst:
            rbc_visible_auth._install_rbc_request_capture(
                page,
                credentials,
                capture_state,
            )
            page.handlers["request"](request)
            drained = await rbc_visible_auth._drain_rbc_capture_tasks(capture_state)

        self.assertTrue(drained)
        capture_burst.assert_awaited_once_with(page, credentials)
        summary = capture_state.summary()
        self.assertEqual(1, summary["request_login_post_count"])
        self.assertEqual(1, summary["request_credential_post_count"])
        request_fields = summary["credential_field_observations"]["request"]
        self.assertEqual(["raw"], request_fields["K1"])
        self.assertEqual(["masked"], request_fields["QQ"])
        self.assertEqual(["missing"], request_fields["Q1"])

    async def test_capture_task_drain_cancels_bounded_pending_work(self):
        capture_state = rbc_visible_auth.RBCCredentialCaptureState()
        never_complete = asyncio.Event()

        async def wait_forever():
            await never_complete.wait()

        rbc_visible_auth._schedule_rbc_capture_task(capture_state, wait_forever())
        drained = await rbc_visible_auth._drain_rbc_capture_tasks(
            capture_state,
            timeout_seconds=0.0,
        )

        self.assertFalse(drained)
        self.assertEqual(1, capture_state.task_cancelled_count)
        self.assertEqual(1, capture_state.drain_timeout_count)
        self.assertEqual(0, len(capture_state.tasks))


class ScotiabankVisibleAuthCaptureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_authenticated_route_accepts_current_my_accounts_path(self):
        self.assertTrue(
            scotiabank_visible_auth._scotia_authenticated_accounts_route(
                "https://secure.scotiabank.com/my-accounts?lng=en"
            )
        )
        self.assertTrue(
            scotiabank_visible_auth._scotia_authenticated_accounts_route(
                "https://secure.scotiabank.com/accounts?lng=en"
            )
        )
        self.assertFalse(
            scotiabank_visible_auth._scotia_authenticated_accounts_route(
                "https://auth.scotiaonline.scotiabank.com/my-accounts"
            )
        )

    def test_authentication_request_summary_retains_only_value_presence(self):
        capture_state = scotiabank_visible_auth.ScotiaCredentialCaptureState()
        post_data = json.dumps(
            [
                {"type": "username", "value": "client-card"},
                {"type": "password", "value": "real-password"},
                {"type": "challenge", "value": None},
            ]
        )

        credential_request = scotiabank_visible_auth._record_scotia_authentication_request(
            capture_state,
            post_data=post_data,
            source="request",
        )

        self.assertTrue(credential_request)
        summary = capture_state.summary()
        self.assertEqual(1, summary["request_auth_post_count"])
        self.assertEqual(2, summary["request_value_present_count"])
        self.assertEqual(1, summary["request_value_missing_count"])
        self.assertNotIn("client-card", json.dumps(summary))
        self.assertNotIn("real-password", json.dumps(summary))

    async def test_request_signal_schedules_and_drains_bounded_field_capture(self):
        class FakePage:
            def __init__(self):
                self.handlers = {}

            def on(self, event, callback):
                self.handlers[event] = callback

        page = FakePage()
        credentials = {"username": "", "password": ""}
        capture_state = scotiabank_visible_auth.ScotiaCredentialCaptureState()
        request = types.SimpleNamespace(
            method="POST",
            url="https://auth.scotiaonline.scotiabank.com/v2/authentications/auth-id",
            post_data=json.dumps([{"type": "password", "value": "real-password"}]),
        )

        with patch.object(
            scotiabank_visible_auth,
            "_capture_scotia_raw_input_credentials_burst",
            new=AsyncMock(),
        ) as capture_burst:
            scotiabank_visible_auth._install_scotia_request_capture(
                page,
                credentials,
                capture_state,
            )
            page.handlers["request"](request)
            drained = await scotiabank_visible_auth._drain_scotia_capture_tasks(capture_state)

        self.assertTrue(drained)
        capture_burst.assert_awaited_once_with(page, credentials)
        summary = capture_state.summary()
        self.assertEqual(1, summary["capture_task_scheduled_count"])
        self.assertEqual(1, summary["capture_task_completed_count"])
        self.assertEqual(1, summary["request_auth_post_count"])
        self.assertEqual(1, summary["request_value_present_count"])
        self.assertNotIn("real-password", json.dumps(summary))

    async def test_windows_auth_route_captures_fields_before_request_continues(self):
        credentials = {"username": "", "password": ""}
        capture_state = scotiabank_visible_auth.ScotiaCredentialCaptureState()
        page = types.SimpleNamespace(is_closed=Mock(return_value=False))

        class FakeContext:
            def __init__(self):
                self.pattern = ""
                self.handler = None

            async def route(self, pattern, handler):
                self.pattern = pattern
                self.handler = handler

        class FakeRoute:
            def __init__(self):
                self.continued = False

            async def continue_(self):
                if (
                    credentials.get("username") != "client-card"
                    or credentials.get("password") != "real-password"
                ):
                    raise AssertionError("Scotiabank auth request continued before live-field capture")
                self.continued = True

        async def capture_live_fields(_page, captured_credentials, **_kwargs):
            self.assertIs(page, _page)
            scotiabank_visible_auth._capture_scotia_username(
                captured_credentials,
                "client-card",
                source="raw",
            )
            scotiabank_visible_auth._capture_scotia_password(
                captured_credentials,
                "real-password",
                source="raw",
            )

        context = FakeContext()
        route = FakeRoute()
        request = types.SimpleNamespace(
            method="POST",
            url="https://auth.scotiaonline.scotiabank.com/v2/authentications/auth-id",
            post_data=json.dumps(
                [
                    {"type": "username", "value": "client-card"},
                    {"type": "password", "value": "real-password"},
                ]
            ),
            frame=types.SimpleNamespace(page=page),
        )

        with (
            patch.object(scotiabank_visible_auth, "SCOTIA_WINDOWS_FLOW", True),
            patch.object(
                scotiabank_visible_auth,
                "_capture_scotia_raw_input_credentials",
                side_effect=capture_live_fields,
            ),
        ):
            await scotiabank_visible_auth._install_scotia_windows_auth_route_capture(
                context,
                credentials,
                capture_state,
            )
            await context.handler(route, request)

        self.assertEqual(scotiabank_visible_auth.SCOTIA_WINDOWS_AUTH_ROUTE_PATTERN, context.pattern)
        self.assertTrue(route.continued)
        self.assertEqual(
            ("client-card", "real-password"),
            scotiabank_visible_auth.collect_scotia_credentials(credentials),
        )
        summary = scotiabank_visible_auth._scotia_credential_capture_details(credentials, capture_state)
        self.assertEqual(1, summary["route_install_success_count"])
        self.assertEqual(1, summary["route_auth_post_count"])
        self.assertEqual(2, summary["route_value_present_count"])
        self.assertNotIn("client-card", json.dumps(summary))
        self.assertNotIn("real-password", json.dumps(summary))

    async def test_windows_auth_route_continues_noncredential_auth_request(self):
        capture_state = scotiabank_visible_auth.ScotiaCredentialCaptureState()

        class FakeContext:
            def __init__(self):
                self.handler = None

            async def route(self, _pattern, handler):
                self.handler = handler

        route = types.SimpleNamespace(continue_=AsyncMock())
        request = types.SimpleNamespace(
            method="POST",
            url="https://auth.scotiaonline.scotiabank.com/v2/authentications",
            post_data=json.dumps({"authenticator_key": None, "user_key": None}),
        )
        context = FakeContext()

        with patch.object(scotiabank_visible_auth, "SCOTIA_WINDOWS_FLOW", True):
            await scotiabank_visible_auth._install_scotia_windows_auth_route_capture(
                context,
                {"username": "", "password": ""},
                capture_state,
            )
            await context.handler(route, request)

        route.continue_.assert_awaited_once_with()
        self.assertEqual(1, capture_state.route_auth_post_count)
        self.assertEqual(0, capture_state.route_value_present_count)

    async def test_live_field_capture_prioritizes_ephemeral_password(self):
        credentials = {"username": "", "password": ""}
        calls = []

        async def input_value(_page, selectors, **_kwargs):
            calls.append(selectors)
            if selectors is scotiabank_visible_auth.SCOTIA_PASSWORD_CAPTURE_SELECTORS:
                return "real-password"
            return "client-card"

        with patch.object(
            scotiabank_visible_auth,
            "_scotia_first_input_value",
            side_effect=input_value,
        ):
            await scotiabank_visible_auth._capture_scotia_raw_input_credentials(
                object(),
                credentials,
                include_hidden_fallback=True,
            )

        self.assertEqual(
            [
                scotiabank_visible_auth.SCOTIA_PASSWORD_CAPTURE_SELECTORS,
                scotiabank_visible_auth.SCOTIA_USERNAME_CAPTURE_SELECTORS,
            ],
            calls,
        )
        self.assertEqual(
            ("client-card", "real-password"),
            scotiabank_visible_auth.collect_scotia_credentials(credentials),
        )

    async def test_response_capture_accepts_current_my_accounts_summary_path(self):
        class FakePage:
            def __init__(self):
                self.handlers = {}

            def on(self, event, callback):
                self.handlers[event] = callback

        page = FakePage()
        response = types.SimpleNamespace(
            url="https://secure.scotiabank.com/my-accounts/api/summary?isAccountSummaryPage=true",
            status=200,
            text=AsyncMock(
                return_value=json.dumps(
                    {"data": {"products": [{"key": "account-key"}]}}
                )
            ),
        )

        scotiabank_visible_auth._install_scotia_response_capture(page)
        page.handlers["response"](response)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(
            {"data": {"products": [{"key": "account-key"}]}},
            getattr(page, "_scotia_accounts_payload"),
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
