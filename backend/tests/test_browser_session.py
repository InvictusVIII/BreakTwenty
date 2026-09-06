from __future__ import annotations

import tempfile
import unittest
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.scrapers.browser_session import (
    DEFAULT_SCRAPER_USER_AGENT,
    SavedArtifactHttpClient,
    SavedArtifactReplaySession,
    default_scraper_user_agent,
    refresh_session_artifact,
    scraper_session_user_agent,
)
from app.scrapers.scraper_logging import (
    EMAIL_LOG_REDACTION_PATTERN,
    JWT_LOG_REDACTION_PATTERN,
    sanitize_scraper_log_text,
    sanitize_scraper_log_url,
)
from app.scrapers.results import scraper_ok
from app.services.desktop_browser_runtime import find_managed_desktop_browser_runtime


class SavedArtifactHttpClientTests(unittest.TestCase):
    def test_headers_add_user_agent_and_matching_cookie(self) -> None:
        storage_state = {
            "cookies": [
                {"name": "session", "value": "abc", "domain": ".example.com", "path": "/"},
                {"name": "other", "value": "skip", "domain": ".other.com", "path": "/"},
            ]
        }
        replay = SavedArtifactHttpClient(storage_state, user_agent="ua")

        headers = replay.headers("https://secure.example.com/accounts", {"Accept": "application/json"})

        self.assertEqual(headers["User-Agent"], "ua")
        self.assertEqual(headers["Cookie"], "session=abc")
        self.assertEqual(headers["Accept"], "application/json")

    def test_refresh_session_artifact_preserves_captured_at_and_sets_reuse_fields(self) -> None:
        refreshed = refresh_session_artifact(
            {"captured_at": "existing"},
            user_agent="ua",
            extra={"provider": "test"},
        )

        self.assertEqual(refreshed["captured_at"], "existing")
        self.assertEqual(refreshed["user_agent"], "ua")
        self.assertEqual(refreshed["provider"], "test")
        self.assertIn("last_reused_at", refreshed)


class ScraperReplayHelperTests(unittest.TestCase):
    def test_default_scraper_user_agent_uses_shared_fallback(self) -> None:
        self.assertEqual(default_scraper_user_agent(" custom "), "custom")
        self.assertEqual(default_scraper_user_agent(""), DEFAULT_SCRAPER_USER_AGENT)
        self.assertIn("Chrome/147.0.0.0", DEFAULT_SCRAPER_USER_AGENT)

    def test_scraper_session_user_agent_reads_session_or_fallback(self) -> None:
        self.assertEqual(scraper_session_user_agent({"user_agent": " session "}), "session")
        self.assertEqual(scraper_session_user_agent({"user_agent": ""}, "fallback"), "fallback")
        self.assertEqual(scraper_session_user_agent(None, "fallback"), "fallback")

    def test_sanitize_scraper_log_text_redacts_and_truncates(self) -> None:
        sanitized = sanitize_scraper_log_text(
            "  card 1234567\nabc.def.ghi user@example.com  ",
            limit=32,
            redactions=(
                (JWT_LOG_REDACTION_PATTERN, "<jwt>", 0),
                (EMAIL_LOG_REDACTION_PATTERN, "<email>", re.IGNORECASE),
            ),
        )

        self.assertEqual(sanitized, "card <number> <jwt> <email>")

    def test_sanitize_scraper_log_text_can_collapse_before_redaction(self) -> None:
        sanitized = sanitize_scraper_log_text(
            'password:   "secret"  token: "abc"',
            redactions=((r'(password:\s*)["\'][^"\']+["\']', r"\1<redacted>", 0),),
            collapse_first=True,
        )

        self.assertEqual(sanitized, "password: <redacted> token: \"abc\"")

    def test_sanitize_scraper_log_url_strips_or_redacts_query_values(self) -> None:
        self.assertEqual(
            sanitize_scraper_log_url("https://example.test/path?token=secret"),
            "https://example.test/path",
        )
        self.assertEqual(
            sanitize_scraper_log_url(
                "https://example.test/path?fromBookingDate=2026-01-01&token=secret",
                safe_query_keys={"fromBookingDate"},
            ),
            "https://example.test/path?fromBookingDate=2026-01-01&token=<redacted>",
        )
        self.assertEqual(sanitize_scraper_log_url("/relative/path"), "")


class SavedArtifactReplaySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_uses_visible_attempt_namespace_and_resolves_user_agent(self) -> None:
        def fake_path(user_id, provider, artifact_kind, *, visible_auth_attempt_namespace=None):
            self.assertEqual(user_id, 7)
            self.assertEqual(provider, "demo")
            self.assertEqual(visible_auth_attempt_namespace, "attempt:demo")
            return artifact_kind

        with (
            patch("app.scrapers.browser_session.provider_runtime_artifact_path", side_effect=fake_path),
            patch(
                "app.scrapers.browser_session.load_json_artifact_async",
                new=AsyncMock(side_effect=[{"cookies": []}, {"user_agent": "session-ua"}]),
            ) as load_artifact,
        ):
            replay = await SavedArtifactReplaySession(
                "demo",
                7,
                visible_auth_attempt_namespace="attempt:demo",
                fallback_user_agent="fallback",
            ).load_async()

        self.assertEqual(replay.storage_state, {"cookies": []})
        self.assertEqual(replay.session_artifact, {"user_agent": "session-ua"})
        self.assertEqual(replay.user_agent, "session-ua")
        self.assertEqual([call.args[0] for call in load_artifact.await_args_list], ["storage_state", "session"])

    def test_common_result_helpers_return_standard_scraper_envelopes(self) -> None:
        replay = SavedArtifactReplaySession("demo", 7)

        self.assertEqual(replay.missing_artifact_result()["status"], "auth_required")
        self.assertEqual(
            scraper_ok(
                [{"id": "1"}],
                transactions={"account:1": []},
                transaction_fetch_succeeded_accounts=["account:1"],
            ),
            {
                "status": "ok",
                "accounts": [{"id": "1"}],
                "holdings": {},
                "transactions": {"account:1": []},
                "message": None,
                "transaction_fetch_succeeded_accounts": ["account:1"],
            },
        )

    def test_save_session_artifact_refreshes_and_persists(self) -> None:
        saved = {}

        def fake_save(user_id, provider, artifact_kind, artifact, *, visible_auth_attempt_namespace=None):
            saved.update(
                {
                    "user_id": user_id,
                    "provider": provider,
                    "artifact_kind": artifact_kind,
                    "artifact": artifact,
                    "namespace": visible_auth_attempt_namespace,
                }
            )

        replay = SavedArtifactReplaySession(
            "demo",
            7,
            visible_auth_attempt_namespace="attempt:demo",
            user_agent="ua",
        )
        replay.session_artifact = {"captured_at": "original"}

        with patch("app.scrapers.browser_session.save_provider_runtime_artifact", side_effect=fake_save):
            artifact = replay.save_session_artifact(extra={"token": "fresh"})

        self.assertEqual(saved["user_id"], 7)
        self.assertEqual(saved["provider"], "demo")
        self.assertEqual(saved["artifact_kind"], "session")
        self.assertEqual(saved["namespace"], "attempt:demo")
        self.assertEqual(saved["artifact"], artifact)
        self.assertEqual(artifact["captured_at"], "original")
        self.assertEqual(artifact["user_agent"], "ua")
        self.assertEqual(artifact["token"], "fresh")


class DesktopBrowserRuntimeTests(unittest.TestCase):
    def test_finds_packaged_windows_brave_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            executable_path = runtime_root / "brave-browser" / "1.89.145" / "win64" / "brave.exe"
            executable_path.parent.mkdir(parents=True)
            executable_path.touch()

            with patch("app.services.desktop_browser_runtime.sys.platform", "win32"):
                runtime = find_managed_desktop_browser_runtime(runtime_root=runtime_root)

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime["platform"], "win64")
        self.assertEqual(runtime["executable_path"], str(executable_path))

    def test_prefers_current_platform_when_multiple_runtimes_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_root = Path(temp_dir)
            linux_executable = (
                runtime_root
                / "brave-browser"
                / "1.89.145"
                / "linux64"
                / "opt"
                / "brave.com"
                / "brave"
                / "brave-browser"
            )
            windows_executable = runtime_root / "brave-browser" / "1.89.145" / "win64" / "brave.exe"
            linux_executable.parent.mkdir(parents=True)
            windows_executable.parent.mkdir(parents=True)
            linux_executable.touch()
            windows_executable.touch()

            with patch("app.services.desktop_browser_runtime.sys.platform", "linux"):
                runtime = find_managed_desktop_browser_runtime(runtime_root=runtime_root)

        self.assertIsNotNone(runtime)
        self.assertEqual(runtime["platform"], "linux64")
        self.assertEqual(runtime["executable_path"], str(linux_executable))


if __name__ == "__main__":
    unittest.main()
