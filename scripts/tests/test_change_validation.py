from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.change_validation import (
    ValidationMappingError,
    fingerprint_paths,
    infer_validation_plan,
    receipt_base,
    verify_receipt,
)
from scripts import run_release_health
from scripts import run_backend_task_validation


class ChangeValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str = "content") -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_frontend_ui_change_requires_semantics_behavior_build_style_and_electron(self) -> None:
        self.write("frontend/src/pages/Dashboard.css")
        plan = infer_validation_plan(self.root, ["frontend/src/pages/Dashboard.css"])
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {
                "frontend_contracts",
                "frontend_js_lint",
                "frontend_css_lint",
                "frontend_tests",
                "frontend_build",
                "electron_e2e",
            },
        )
        self.assertIn("frontend_style", plan.boundaries)

    def test_desktop_entrypoint_requires_complete_check_and_real_electron(self) -> None:
        self.write("desktop/src/bootstrap.js")
        plan = infer_validation_plan(self.root, ["desktop/src/bootstrap.js"])
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {"desktop_check", "electron_e2e", "packaged_native_smoke"},
        )

    def test_ordinary_desktop_module_does_not_force_packaging(self) -> None:
        self.write("desktop/src/appDiagnostics.js")
        plan = infer_validation_plan(self.root, ["desktop/src/appDiagnostics.js"])
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {"desktop_check", "electron_e2e"},
        )
        self.assertNotIn("packaged_native_renderer", plan.boundaries)

    def test_backend_migration_requires_backend_semantics_tests_and_import(self) -> None:
        self.write("backend/alembic/versions/0010_example.py")
        plan = infer_validation_plan(self.root, ["backend/alembic/versions/0010_example.py"])
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {"backend_lint", "backend_tests", "backend_import", "packaged_native_smoke"},
        )
        self.assertIn("database_schema", plan.boundaries)

    def test_shared_contract_requires_every_current_consumer(self) -> None:
        self.write("config/provider_catalog.json", "{}")
        plan = infer_validation_plan(self.root, ["config/provider_catalog.json"])
        self.assertTrue({"backend", "frontend", "desktop"}.isdisjoint(plan.boundaries))
        self.assertEqual(plan.boundaries, ("shared_contract",))
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {
                "frontend_contracts",
                "frontend_js_lint",
                "frontend_tests",
                "frontend_build",
                "desktop_check",
                "electron_e2e",
                "backend_lint",
                "backend_tests",
                "backend_import",
            },
        )

    def test_unmapped_source_file_fails_closed(self) -> None:
        self.write("unknown/tool.py")
        with self.assertRaisesRegex(ValidationMappingError, "no validation mapping"):
            infer_validation_plan(self.root, ["unknown/tool.py"])

    def test_release_health_change_exercises_receipts_desktop_and_real_electron(self) -> None:
        self.write("scripts/run_release_health.py")
        plan = infer_validation_plan(self.root, ["scripts/run_release_health.py"])
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {
                "validation_gate_tests",
                "release_tooling_tests",
                "desktop_check",
                "electron_e2e",
                "packaged_native_smoke",
            },
        )

    def test_release_config_change_requires_release_tooling_contracts(self) -> None:
        self.write("release-config/public-desktop-release.yml")
        plan = infer_validation_plan(self.root, ["release-config/public-desktop-release.yml"])
        self.assertEqual(plan.boundaries, ("release_tooling",))
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {"release_tooling_tests"},
        )

    def test_packaged_desktop_tooling_requires_desktop_electron_and_native_smoke(self) -> None:
        paths = [
            "scripts/run_packaged_native_smoke.test.js",
            "scripts/run_packaged_safe_storage_smoke.js",
        ]
        for path in paths:
            self.write(path)
        plan = infer_validation_plan(self.root, paths)
        self.assertEqual(
            {check.check_id for check in plan.checks},
            {"desktop_check", "electron_e2e", "packaged_native_smoke"},
        )
        self.assertIn("packaged_native_renderer", plan.boundaries)

    def test_documentation_change_is_mapped_without_runtime_claims(self) -> None:
        self.write("docs/example.md")
        plan = infer_validation_plan(self.root, ["docs/example.md"])
        self.assertEqual(plan.boundaries, ("documentation",))
        self.assertEqual(plan.checks, ())
        self.assertEqual(plan.skipped_boundaries[0]["boundary"], "runtime_execution")

    def test_receipt_verification_is_bound_to_exact_file_bytes(self) -> None:
        self.write("desktop/src/main.js", "first")
        fingerprint = fingerprint_paths(self.root, ["desktop/src/main.js"])
        receipt = receipt_base(
            self.root,
            mode="task",
            paths=["desktop/src/main.js"],
            boundaries=["desktop"],
            skipped_boundaries=[],
            fingerprint=fingerprint,
        )
        receipt["status"] = "passed"
        receipt["checks"] = [{"id": "example", "status": "passed"}]
        verify_receipt(self.root, receipt)
        self.write("desktop/src/main.js", "second")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            verify_receipt(self.root, receipt)

    def test_receipt_is_machine_readable_and_contains_required_evidence_fields(self) -> None:
        self.write("README.md")
        receipt = receipt_base(
            self.root,
            mode="task",
            paths=["README.md"],
            boundaries=["documentation"],
            skipped_boundaries=[{"boundary": "runtime", "reason": "docs"}],
        )
        payload = json.loads(json.dumps(receipt))
        self.assertEqual(payload["changed_paths"], ["README.md"])
        self.assertEqual(payload["boundaries"], ["documentation"])
        self.assertIn("fingerprint", payload["repository"])
        self.assertEqual(payload["skipped_boundaries"][0]["reason"], "docs")

    def test_complete_release_gate_writes_and_self_verifies_shared_receipt(self) -> None:
        source_root = self.root / "source"
        source_root.mkdir()
        (source_root / "tracked.txt").write_text("stable", encoding="utf-8")
        receipt_path = self.root / "receipt.json"
        args = SimpleNamespace(changed_path=["tracked.txt"])
        with patch.object(run_release_health, "run_release_health", return_value=0):
            result = run_release_health.run_with_receipt(source_root, args, receipt_path)
        self.assertEqual(result, 0)
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["mode"], "complete-release")
        self.assertEqual(payload["changed_paths"], ["tracked.txt"])
        self.assertIn("electron_runtime", payload["boundaries"])

    def test_backend_runtime_validation_replaces_every_live_state_root(self) -> None:
        root = "/tmp/breaktwenty-task-validation-0123456789abcdef0123456789abcdef"
        environment = run_backend_task_validation.isolated_environment(root)
        isolated_path_keys = {
            "BREAKTWENTY_BROWSER_RUNTIME_DIR",
            "BREAKTWENTY_DATA_DIR",
            "BREAKTWENTY_DB_PATH",
            "BREAKTWENTY_DESKTOP_AUTH_DIR",
            "BREAKTWENTY_DIAGNOSTIC_DIR",
            "BREAKTWENTY_LAUNCH_TOKEN_FILE",
            "BREAKTWENTY_LOG_DIR",
            "DATABASE_URL",
            "HOME",
            "PYTHONPYCACHEPREFIX",
            "TMPDIR",
            "XDG_CACHE_HOME",
        }
        self.assertTrue(all(root in environment[key] for key in isolated_path_keys))
        self.assertEqual(environment["PROVIDER_CATALOG_PATH"], "/provider-config/provider_catalog.json")
        self.assertNotIn("/app/data", environment.values())

    def test_backend_container_command_passes_isolated_environment_explicitly(self) -> None:
        command = run_backend_task_validation.docker_command(
            "/usr/bin/docker",
            ["python3", "-c", "import app.main"],
            environment={"BREAKTWENTY_DATA_DIR": "/tmp/test/data"},
        )
        self.assertEqual(command[:2], ["/usr/bin/docker", "exec"])
        self.assertIn("env", command)
        self.assertIn("-i", command)
        self.assertIn("BREAKTWENTY_DATA_DIR=/tmp/test/data", command)
        self.assertEqual(command[-3:], ["python3", "-c", "import app.main"])


if __name__ == "__main__":
    unittest.main()
