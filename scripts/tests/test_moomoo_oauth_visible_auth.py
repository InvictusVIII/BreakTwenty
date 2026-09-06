from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


RUNNER_PATH = Path(__file__).resolve().parents[1] / "moomoo_oauth_visible_auth.py"
sys.path.insert(0, str(RUNNER_PATH.parent))
RUNNER_SPEC = importlib.util.spec_from_file_location("moomoo_oauth_visible_auth_tested", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


class MoomooOAuthVisibleAuthRunnerTests(unittest.TestCase):
    def test_passport_redirect_is_forced_to_english_without_changing_oauth_target(self) -> None:
        target = "https://webapi.moomoo.com/oauth2/authorize/confirm?state=state-value"
        localized = RUNNER._english_moomoo_passport_url(
            "https://passport.moomoo.com/?target="
            "https%3A%2F%2Fwebapi.moomoo.com%2Foauth2%2Fauthorize%2Fconfirm%3Fstate%3Dstate-value"
            "&lang=zh-cn#/"
        )
        parsed = urlsplit(localized)
        query = parse_qs(parsed.query)

        self.assertEqual(query["lang"], ["en-us"])
        self.assertEqual(query["target"], [target])
        self.assertEqual(parsed.fragment, "/")
        self.assertEqual(
            RUNNER._english_moomoo_passport_url("https://example.com/?lang=zh-cn"),
            "https://example.com/?lang=zh-cn",
        )

    def test_passport_navigation_route_rewrites_language_before_loading(self) -> None:
        class FakeRequest:
            url = "https://passport.moomoo.com/?target=oauth&lang=zh-cn"

            @staticmethod
            def is_navigation_request() -> bool:
                return True

        class FakeRoute:
            request = FakeRequest()

            def __init__(self) -> None:
                self.continued_with = None

            async def continue_(self, **kwargs) -> None:
                self.continued_with = kwargs

        route = FakeRoute()
        asyncio.run(RUNNER._enforce_english_moomoo_passport_route(route))

        self.assertIsNotNone(route.continued_with)
        rewritten = urlsplit(route.continued_with["url"])
        self.assertEqual(parse_qs(rewritten.query), {
            "lang": ["en-us"],
            "target": ["oauth"],
        })

    def test_managed_context_uses_english_locale_and_real_window_viewport(self) -> None:
        source = RUNNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        new_context_call = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "new_context"
        )
        self.assertTrue(any(keyword.arg is None for keyword in new_context_call.keywords))
        self.assertIn('"locale": MOOMOO_ENGLISH_LOCALE', source)
        self.assertIn('"extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"}', source)
        self.assertIn('"no_viewport": True', source)
        self.assertIn('"record_har_path": str(raw_har_path)', source)

    def test_managed_browser_requests_a_desktop_layout_window_size(self) -> None:
        class FakeCdpSession:
            def __init__(self) -> None:
                self.calls = []
                self.detached = False

            async def send(self, method, params=None):
                self.calls.append((method, params))
                if method == "Browser.getWindowForTarget":
                    return {"windowId": 17}
                if method == "Browser.getWindowBounds":
                    return {
                        "bounds": {
                            "left": 0,
                            "top": 0,
                            "width": 1598,
                            "height": 998,
                            "windowState": "normal",
                        }
                    }
                return {}

            async def detach(self) -> None:
                self.detached = True

        class FakeContext:
            def __init__(self, session) -> None:
                self.session = session

            async def new_cdp_session(self, _page):
                return self.session

        class FakePage:
            async def evaluate(self, _script):
                return {
                    "width": 1598,
                    "height": 911,
                    "devicePixelRatio": 1,
                    "screenWidth": 1920,
                    "screenHeight": 1080,
                    "availableWidth": 1920,
                    "availableHeight": 1040,
                }

        session = FakeCdpSession()
        details = asyncio.run(
            RUNNER._size_moomoo_browser_window(FakeContext(session), FakePage())
        )

        self.assertTrue(details["window_sized"])
        self.assertEqual(details["actual_window_width"], 1598)
        self.assertEqual(details["actual_window_height"], 998)
        self.assertEqual(details["actual_viewport_width"], 1598)
        self.assertEqual(details["actual_viewport_height"], 911)
        self.assertIn("--window-size=1600,1000", RUNNER.MOOMOO_MANAGED_BROWSER_LAUNCH_ARGS)
        self.assertEqual(session.calls, [
            ("Browser.getWindowForTarget", None),
            (
                "Browser.setWindowBounds",
                {
                    "windowId": 17,
                    "bounds": {
                        "windowState": "normal",
                        "left": 0,
                        "top": 0,
                        "width": 1600,
                        "height": 1000,
                    },
                },
            ),
            ("Browser.getWindowBounds", {"windowId": 17}),
        ])
        self.assertTrue(session.detached)

    def test_har_finalization_is_scoped_and_redacts_oauth_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_diag = RUNNER.MOOMOO_DIAGNOSTIC_DIR
            original_sync = RUNNER.SYNC_ID
            original_attempt = RUNNER.ATTEMPT_ID
            RUNNER.MOOMOO_DIAGNOSTIC_DIR = root / "diagnostics"
            RUNNER.SYNC_ID = "sync-safe"
            RUNNER.ATTEMPT_ID = "attempt-safe"
            original_capture_level = os.environ.get("BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL")
            os.environ["BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL"] = "developer_local"
            raw_path = root / "raw.har"
            raw_path.write_text(json.dumps({
                "log": {
                    "entries": [
                        {
                            "request": {
                                "url": "https://webapi.moomoo.com/oauth2/authorize/confirm?code=secret&state=state-secret&phone_no=4165550199",
                                "headers": [{"name": "Authorization", "value": "Bearer secret"}],
                                "cookies": [],
                                "queryString": [
                                    {"name": "code", "value": "secret"},
                                    {"name": "state", "value": "state-secret"},
                                    {"name": "phone_no", "value": "4165550199"},
                                ],
                            },
                            "response": {
                                "status": 200,
                                "headers": [],
                                "cookies": [],
                                "content": {"mimeType": "text/html", "text": "private login page"},
                                "redirectURL": "",
                            },
                        },
                        {
                            "request": {
                                "url": "https://webapi.moomoo.com/api/v1.0/accounts/authorized_trd_accs",
                                "headers": [],
                                "cookies": [],
                                "queryString": [],
                            },
                            "response": {
                                "status": 200,
                                "headers": [],
                                "cookies": [],
                                "content": {
                                    "mimeType": "application/json",
                                    "text": json.dumps({
                                        "uid": "private-user-id",
                                        "displayName": "Private Person",
                                        "univsAccountId": "private-account-id",
                                    }),
                                },
                                "redirectURL": "",
                            },
                        },
                    ],
                },
            }), encoding="utf-8")
            try:
                saved = RUNNER.finalize_moomoo_har_capture(
                    raw_path,
                    user_id=1,
                    timestamp_slug="20260903T200000Z",
                    result="succeeded",
                )
                self.assertIsNotNone(saved)
                self.assertIn("sync-sync-safe_attempt-attempt-safe", saved.name)
                text = saved.read_text(encoding="utf-8")
                self.assertNotIn("state-secret", text)
                self.assertNotIn("Bearer secret", text)
                self.assertNotIn("private login page", text)
                self.assertNotIn("4165550199", text)
                self.assertNotIn("private-user-id", text)
                self.assertNotIn("Private Person", text)
                self.assertNotIn("private-account-id", text)
                self.assertIn("/api/v1.0/accounts/authorized_trd_accs", text)
                self.assertFalse(raw_path.exists())
            finally:
                RUNNER.MOOMOO_DIAGNOSTIC_DIR = original_diag
                RUNNER.SYNC_ID = original_sync
                RUNNER.ATTEMPT_ID = original_attempt
                if original_capture_level is None:
                    os.environ.pop("BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL", None)
                else:
                    os.environ["BREAKTWENTY_VISIBLE_AUTH_CAPTURE_LEVEL"] = original_capture_level

    def test_handoff_result_is_emitted_after_har_finalization(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
        )
        print_call = next(
            node
            for node in ast.walk(run_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "print_visible_auth_result"
        )
        finalize_reference = next(
            node
            for node in ast.walk(run_function)
            if isinstance(node, ast.Name)
            and node.id == "finalize_moomoo_har_capture"
        )

        self.assertGreater(print_call.lineno, finalize_reference.lineno)

    def test_managed_browser_is_closed_from_the_runner_finally_boundary(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        run_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
        )
        close_calls = [
            node
            for try_node in ast.walk(run_function)
            if isinstance(try_node, ast.Try)
            for statement in try_node.finalbody
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close_visible_auth_context_quietly"
        ]

        self.assertEqual(len(close_calls), 1)
        self.assertEqual(len(close_calls[0].args), 2)

    def test_callback_handler_signals_completion_from_its_finally_boundary(self) -> None:
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        callback_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_callback"
        )
        completion_calls = [
            node
            for try_node in ast.walk(callback_function)
            if isinstance(try_node, ast.Try)
            for statement in try_node.finalbody
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set"
        ]

        self.assertEqual(len(completion_calls), 1)


if __name__ == "__main__":
    unittest.main()
