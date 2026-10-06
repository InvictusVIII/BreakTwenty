import os
import sys
import types
import unittest
from pathlib import Path


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
    if path and (path / "ibkr_visible_auth.py").is_file()
]
if not SCRIPT_CANDIDATES:
    raise RuntimeError("The release test layout must include the IBKR visible-auth runner")
SCRIPTS_DIR = SCRIPT_CANDIDATES[0]
sys.path.insert(0, str(SCRIPTS_DIR))
patchright_module = sys.modules.setdefault("patchright", types.ModuleType("patchright"))
patchright_async_module = sys.modules.setdefault("patchright.async_api", types.ModuleType("patchright.async_api"))
if not hasattr(patchright_async_module, "async_playwright"):
    patchright_async_module.async_playwright = None
if not hasattr(patchright_module, "async_api"):
    patchright_module.async_api = patchright_async_module
import ibkr_visible_auth as ibkr_visible_auth  # noqa: E402


class FakeResponse:
    def __init__(self, url: str, payload: str, *, status: int = 200):
        self.url = url
        self.status = status
        self.payload = payload

    async def text(self) -> str:
        return self.payload


class FakeLocator:
    def __init__(self, selector: str, fills: list[tuple[str, str]]):
        self.selector = selector
        self.fills = fills

    async def count(self) -> int:
        return 1

    def nth(self, _index: int):
        return self

    async def is_visible(self) -> bool:
        return True

    async def fill(self, value: str) -> None:
        self.fills.append((self.selector, value))


class FakePrefillPage:
    def __init__(self):
        self.fills: list[tuple[str, str]] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(selector, self.fills)


class FakeStatePage:
    def __init__(self):
        self.arguments = None

    async def evaluate(self, _script: str, arguments: dict) -> dict:
        self.arguments = arguments
        return {
            "current_url": ibkr_visible_auth.IBKR_LOGIN_URL,
            "username_visible": True,
            "password_visible": True,
            "code_visible": False,
            "invalid_credentials": True,
            "authenticated_route": False,
            "authenticated": False,
        }


class IBKRVisibleAuthStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_shell_error_class_cannot_block_saved_credential_prefill(self) -> None:
        auth_signals: dict[str, object] = {}
        await ibkr_visible_auth._capture_ibkr_response(
            FakeResponse(
                ibkr_visible_auth.IBKR_LOGIN_URL,
                '<noscript><div class="alert alert-danger">JavaScript must be enabled</div></noscript>',
            ),
            auth_signals,
        )
        page = FakeStatePage()

        state = await ibkr_visible_auth.get_ibkr_page_state(page, auth_signals)

        self.assertFalse(state["invalid_credentials"])
        self.assertNotIn("errorSelectors", page.arguments)

    async def test_authenticator_response_is_authoritative_for_credential_errors(self) -> None:
        auth_signals: dict[str, object] = {}
        authenticator_url = "https://ndcdyn.interactivebrokers.com/sso/Authenticator"

        await ibkr_visible_auth._capture_ibkr_response(
            FakeResponse(authenticator_url, '{"error":"Invalid username or password"}'),
            auth_signals,
        )
        self.assertTrue(auth_signals["invalid_credentials"])

        await ibkr_visible_auth._capture_ibkr_response(
            FakeResponse(authenticator_url, '{"auth_res":"false","error":""}'),
            auth_signals,
        )
        self.assertFalse(auth_signals["invalid_credentials"])

    async def test_prefill_is_not_blocked_by_a_stale_error_signal(self) -> None:
        page = FakePrefillPage()
        captured_credentials = {"username": "", "password": ""}
        prefill_state = {"done": False}

        filled = await ibkr_visible_auth._maybe_prefill_ibkr_credentials(
            page,
            state={
                "authenticated": False,
                "code_visible": False,
                "invalid_credentials": True,
                "username_visible": True,
                "password_visible": True,
            },
            credentials=("saved-user", "saved-password"),
            prefill_state=prefill_state,
            popup_lock=None,
            captured_credentials=captured_credentials,
        )

        self.assertTrue(filled)
        self.assertTrue(prefill_state["done"])
        self.assertEqual(
            page.fills,
            [
                (ibkr_visible_auth.IBKR_USERNAME_SELECTORS[0], "saved-user"),
                (ibkr_visible_auth.IBKR_PASSWORD_SELECTORS[0], "saved-password"),
            ],
        )
        self.assertEqual(captured_credentials["username"], "saved-user")
        self.assertEqual(captured_credentials["password"], "saved-password")


if __name__ == "__main__":
    unittest.main()
