import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class TransactionImportRecoveryMessageMigrationTests(unittest.TestCase):
    @staticmethod
    def _load_migration():
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0009_tximport_recovery_message.py"
        )
        spec = importlib.util.spec_from_file_location(
            "transaction_import_recovery_message",
            migration_path,
        )
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def test_upgrade_moves_only_restart_marker_out_of_last_error(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("""
                CREATE TABLE transaction_import_jobs (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    last_error TEXT
                )
            """))
            connection.execute(
                sa.text("""
                    INSERT INTO transaction_import_jobs (id, job_id, last_error)
                    VALUES
                        (1, 'restarted-job', :restart_message),
                        (2, 'failed-job', 'provider timeout'),
                        (3, 'clean-job', NULL)
                """),
                {"restart_message": "Transaction import resumed after backend restart."},
            )

            migration = self._load_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            columns = {
                column["name"]: column
                for column in sa.inspect(connection).get_columns("transaction_import_jobs")
            }
            rows = connection.execute(sa.text("""
                SELECT job_id, last_error, recovery_message
                FROM transaction_import_jobs
                ORDER BY id
            """)).all()

        self.assertIn("recovery_message", columns)
        self.assertTrue(columns["recovery_message"]["nullable"])
        self.assertEqual(
            (
                "restarted-job",
                None,
                "Transaction import resumed after backend restart.",
            ),
            rows[0],
        )
        self.assertEqual(("failed-job", "provider timeout", None), rows[1])
        self.assertEqual(("clean-job", None, None), rows[2])

    def test_downgrade_preserves_recovery_when_no_real_error_exists(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("""
                CREATE TABLE transaction_import_jobs (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    last_error TEXT,
                    recovery_message TEXT
                )
            """))
            connection.execute(sa.text("""
                INSERT INTO transaction_import_jobs
                    (id, job_id, last_error, recovery_message)
                VALUES
                    (1, 'recovered-job', NULL, 'restart recovery'),
                    (2, 'failed-after-recovery', 'provider timeout', 'restart recovery')
            """))

            migration = self._load_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.downgrade()

            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("transaction_import_jobs")
            }
            rows = connection.execute(sa.text("""
                SELECT job_id, last_error
                FROM transaction_import_jobs
                ORDER BY id
            """)).all()

        self.assertNotIn("recovery_message", columns)
        self.assertEqual(("recovered-job", "restart recovery"), rows[0])
        self.assertEqual(("failed-after-recovery", "provider timeout"), rows[1])


if __name__ == "__main__":
    unittest.main()
