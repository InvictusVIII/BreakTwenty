from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_desktop_browser_runtime():
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "scripts" / "desktop_browser_runtime.py",
        Path("/repo/scripts/desktop_browser_runtime.py"),
    ]
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location("desktop_browser_runtime_script", path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("desktop_browser_runtime.py not found")


class DesktopBrowserRuntimeScriptTests(unittest.TestCase):
    def test_windows_chromium_major_uses_pinned_metadata_without_launching_brave(self) -> None:
        runtime = _load_desktop_browser_runtime()

        with patch.object(runtime.subprocess, "run") as run:
            major = runtime._brave_chromium_major_version(
                Path("C:/BreakTwenty/browser-runtimes/brave.exe"),
                platform_key="win64",
                runtime_version=runtime.BRAVE_BROWSER_STABLE_VERSION,
            )

        self.assertEqual(major, "150")
        run.assert_not_called()

    def test_visible_auth_runtime_ensure_uses_attempt_log_from_environment(self) -> None:
        runtime = _load_desktop_browser_runtime()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            runtime_root = temp_path / "browser-runtimes"
            executable_path = (
                runtime_root
                / "brave-browser"
                / runtime.BRAVE_BROWSER_STABLE_VERSION
                / "win64"
                / "brave.exe"
            )
            executable_path.parent.mkdir(parents=True)
            executable_path.write_text("", encoding="utf-8")
            log_path = temp_path / "visible-auth" / "rbc-attempt.log"

            with (
                patch.object(runtime, "_brave_browser_platform_key", return_value="win64"),
                patch.dict(
                    runtime.os.environ,
                    {"BREAKTWENTY_VISIBLE_AUTH_LOG_PATH": str(log_path)},
                    clear=False,
                ),
                patch.object(runtime.subprocess, "run") as run,
            ):
                result = runtime.ensure_brave_browser_runtime(runtime_root=runtime_root)

            self.assertEqual(result["platform"], "win64")
            self.assertEqual(result["chromium_major"], "150")
            run.assert_not_called()
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("managed brave runtime ensure starting platform=win64", log_text)
            self.assertIn("managed brave runtime cache hit executable=", log_text)
            self.assertIn("managed brave runtime using pinned Windows Chromium major 150", log_text)


if __name__ == "__main__":
    unittest.main()
