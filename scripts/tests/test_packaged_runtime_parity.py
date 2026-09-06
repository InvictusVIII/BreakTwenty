from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagedRuntimeParityTests(unittest.TestCase):
    def test_provider_contract_modules_are_bounded_and_present(self) -> None:
        verifier = load_script("verify_packaged_provider_runtime.py")

        self.assertGreaterEqual(len(verifier.PROVIDER_TEST_MODULES), 10)
        self.assertEqual(
            len(verifier.PROVIDER_TEST_MODULES),
            len(set(verifier.PROVIDER_TEST_MODULES)),
        )
        for module_name in verifier.PROVIDER_TEST_MODULES:
            with self.subTest(module=module_name):
                self.assertTrue(
                    (ROOT / "backend" / "tests" / f"{module_name}.py").is_file()
                )

    def test_packaged_builder_verifies_before_publishing_same_runtime(self) -> None:
        builder = (ROOT / "scripts" / "build_desktop_packaged_runtime.js").read_text(
            encoding="utf-8"
        )

        verify_index = builder.index("verifyPackagedProviderRuntime();")
        fingerprint_index = builder.index("writePythonRuntimeFingerprint();")
        publish_index = builder.index("publishOutput();")
        self.assertLess(verify_index, fingerprint_index)
        self.assertLess(fingerprint_index, publish_index)
        self.assertIn(
            "manifest.pythonRuntimeFingerprint = 'python-runtime-fingerprint.json'",
            builder,
        )
        self.assertIn("'backend', 'runtime-policy.json'", builder)

    def test_all_portable_desktop_release_scripts_share_the_builder(self) -> None:
        import json

        package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        for script_name in (
            "dist:linux:prebuilt-frontend",
            "dist:win:prebuilt-frontend",
            "dist:mac:prebuilt-frontend",
        ):
            with self.subTest(script=script_name):
                script = package["scripts"][script_name]
                self.assertIn("prepare:portable-python", script)
                self.assertIn("build_desktop_packaged_runtime.js --python-source", script)


if __name__ == "__main__":
    unittest.main()
