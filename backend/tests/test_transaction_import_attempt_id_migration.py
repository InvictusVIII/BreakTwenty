import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class TransactionImportAttemptIdMigrationTests(unittest.TestCase):
    @staticmethod
    def _load_migration():
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0007_transaction_import_attempt_id.py"
        )
        spec = importlib.util.spec_from_file_location(
            "transaction_import_attempt_id",
            migration_path,
        )
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def test_upgrade_adds_nullable_indexed_attempt_identity(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(sa.text("""
                CREATE TABLE transaction_import_jobs (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL
                )
            """))
            connection.execute(sa.text(
                "INSERT INTO transaction_import_jobs (id, job_id) VALUES (1, 'existing-job')"
            ))

            migration = self._load_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = sa.inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns(
                "transaction_import_jobs"
            )}
            indexes = {index["name"] for index in inspector.get_indexes(
                "transaction_import_jobs"
            )}
            row = connection.execute(sa.text(
                "SELECT job_id, attempt_id FROM transaction_import_jobs WHERE id = 1"
            )).one()

        self.assertIn("attempt_id", columns)
        self.assertTrue(columns["attempt_id"]["nullable"])
        self.assertIn("ix_transaction_import_jobs_attempt_id", indexes)
        self.assertEqual(("existing-job", None), row)


if __name__ == "__main__":
    unittest.main()
