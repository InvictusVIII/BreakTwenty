from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


class MigrationUpdateSafetyFixtureTests(unittest.TestCase):
    def run_fixture(self, mode: str) -> dict[str, object]:
        runner = Path(__file__).with_name("_migration_update_fixture_runner.py")
        result = subprocess.run(
            [sys.executable, str(runner), mode],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_installed_head_upgrades_to_next_revision(self) -> None:
        result = self.run_fixture("success")

        self.assertEqual(result["beforeRevisions"], ["0008_canonical_category_palette"])
        self.assertEqual(result["afterRevisions"], ["fixture_next"])
        self.assertFalse(result["needsMigrationAfter"])
        self.assertFalse(result["migrationFailed"])
        self.assertEqual(result["marker"], "installed-v1-data")
        self.assertTrue(result["successTablePresent"])
        self.assertTrue(result["snapshotEncrypted"])
        self.assertEqual(result["snapshotIntegrity"], "ok")

    def test_data_bearing_0001_upgrades_safely_to_0002_with_integrity(self) -> None:
        result = self.run_fixture("0001_to_0002")

        self.assertEqual(result["beforeRevisions"], ["0001"])
        self.assertEqual(result["afterRevisions"], ["0002_backend_job_integrity"])
        self.assertFalse(result["needsMigrationAfter"])
        self.assertTrue(result["migratedSafely"])
        self.assertEqual(result["marker"], "data-created-at-0001")
        self.assertEqual(result["snapshotRevision"], "0001")
        self.assertEqual(result["snapshotMarker"], "data-created-at-0001")
        self.assertTrue(result["snapshotEncrypted"])

        self.assertEqual(result["jobs"][0][0:2], ["active-first", "queued"])
        self.assertEqual(result["jobs"][0][3], "owner-first")
        self.assertEqual(result["jobs"][1][0:2], ["active-second", "failed"])
        self.assertIn("Superseded", result["jobs"][1][2])
        self.assertIsNone(result["jobs"][1][3])
        self.assertIsNone(result["jobs"][1][4])
        self.assertIsNotNone(result["jobs"][1][5])
        self.assertEqual(result["jobs"][2][0:2], ["completed", "completed"])
        self.assertTrue(result["newTablesPresent"])
        self.assertTrue(result["leaseExpiryColumnPresent"])
        self.assertTrue(result["activeUniqueIndexPresent"])
        self.assertTrue(result["uniqueActiveEnforced"])
        self.assertIsNone(result["recurringCategoryAfterDelete"])
        self.assertEqual(result["foreignKeyErrors"], 0)

    def test_data_bearing_0002_upgrades_safely_to_0003_with_model_parity(self) -> None:
        result = self.run_fixture("0002_to_0003")

        self.assertEqual(result["beforeRevisions"], ["0002_backend_job_integrity"])
        self.assertEqual(result["afterRevisions"], ["0003_runtime_service_contracts"])
        self.assertFalse(result["needsMigrationAfter"])
        self.assertTrue(result["migratedSafely"])
        self.assertTrue(result["modelParityClean"])
        self.assertEqual(result["marker"], "data-created-at-0002")
        self.assertEqual(result["snapshotRevision"], "0002_backend_job_integrity")
        self.assertEqual(result["snapshotMarker"], "data-created-at-0002")
        self.assertTrue(result["snapshotEncrypted"])
        self.assertEqual(
            result["snapshotRow"][0:4],
            [
                "fixture-provider",
                "sp500",
                "2026-08-13",
                "2026-08-13T12:00:00+00:00",
            ],
        )
        self.assertTrue(result["newTablesPresent"])
        self.assertTrue(result["runtimeModelColumnsMatch"])
        self.assertTrue(result["eventModelColumnsMatch"])
        self.assertTrue(result["runtimeIndexesPresent"])
        self.assertTrue(result["eventIndexesPresent"])
        self.assertTrue(result["duplicateServiceRejected"])
        self.assertTrue(result["duplicateOwnerRejected"])
        self.assertTrue(result["duplicateUserSlotRejected"])
        self.assertTrue(result["duplicateGlobalSlotRejected"])
        self.assertTrue(result["invalidSlotRejected"])
        self.assertEqual(result["cascadeRows"], 0)
        self.assertEqual(result["foreignKeyErrors"], 0)

    def test_failed_next_revision_restores_installed_head(self) -> None:
        result = self.run_fixture("failure")

        self.assertEqual(result["beforeRevisions"], ["0008_canonical_category_palette"])
        self.assertEqual(result["afterRevisions"], ["0008_canonical_category_palette"])
        self.assertTrue(result["needsMigrationAfter"])
        self.assertTrue(result["migrationFailed"])
        self.assertEqual(result["marker"], "installed-v1-data")
        self.assertFalse(result["partialTablePresent"])
        self.assertTrue(result["snapshotEncrypted"])
        self.assertEqual(result["snapshotIntegrity"], "ok")


if __name__ == "__main__":
    unittest.main()
