from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database_encryption import connect_sqlcipher
from app.migration_safety import (
    create_encrypted_snapshot,
    restore_encrypted_snapshot,
)


class MigrationSafetyTests(unittest.TestCase):
    def test_encrypted_snapshot_restores_pre_migration_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "breaktwenty.db"
            snapshot_path = Path(temp_dir) / "recovery" / "pre-migration.db"
            connection = connect_sqlcipher(database_path)
            connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
            connection.execute("INSERT INTO alembic_version VALUES ('0001')")
            connection.execute("CREATE TABLE fixture_records (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO fixture_records VALUES (1, 'private-fixture-value')")
            connection.commit()
            connection.close()

            snapshot = create_encrypted_snapshot(database_path, snapshot_path)

            self.assertEqual(snapshot["integrity"], "ok")
            self.assertEqual(snapshot["foreignKeyErrors"], 0)
            self.assertNotEqual(snapshot_path.read_bytes()[:16], b"SQLite format 3\x00")
            self.assertNotIn(b"private-fixture-value", snapshot_path.read_bytes())
            with self.assertRaisesRegex(RuntimeError, "checksum does not match"):
                restore_encrypted_snapshot(
                    database_path,
                    snapshot_path,
                    expected_sha256="0" * 64,
                )

            connection = connect_sqlcipher(database_path)
            connection.execute("UPDATE alembic_version SET version_num='partial-0002'")
            connection.execute("UPDATE fixture_records SET value='partially-migrated'")
            connection.commit()
            connection.close()

            restored = restore_encrypted_snapshot(
                database_path,
                snapshot_path,
                expected_sha256=snapshot["sha256"],
            )

            self.assertEqual(restored["currentRevisions"], ["0001"])
            connection = connect_sqlcipher(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT value FROM fixture_records WHERE id=1").fetchone()[0],
                    "private-fixture-value",
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
