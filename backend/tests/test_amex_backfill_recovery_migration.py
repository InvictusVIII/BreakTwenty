import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class AmexBackfillRecoveryMigrationTests(unittest.TestCase):
    def test_reopens_only_zero_row_completed_amex_backfills(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE transaction_import_account_states (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        institution_id INTEGER NOT NULL,
                        account_external_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        backfill_status TEXT NOT NULL,
                        backfill_completed_at DATETIME,
                        last_error TEXT,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE transaction_import_windows (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        institution_id INTEGER NOT NULL,
                        account_external_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        transaction_count INTEGER NOT NULL,
                        completed_at DATETIME,
                        last_error TEXT,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO transaction_import_account_states
                        (id, user_id, institution_id, account_external_id, provider,
                         backfill_status, backfill_completed_at, last_error, updated_at)
                    VALUES
                        (1, 1, 10, 'amex-zero', 'amex', 'complete', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP),
                        (2, 1, 11, 'amex-good', 'amex', 'complete', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP),
                        (3, 1, 12, 'other-zero', 'rbc', 'complete', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO transaction_import_windows
                        (id, user_id, institution_id, account_external_id, provider, mode,
                         status, transaction_count, completed_at, last_error, updated_at)
                    VALUES
                        (1, 1, 10, 'amex-zero', 'amex', 'backfill', 'complete', 0, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP),
                        (2, 1, 11, 'amex-good', 'amex', 'backfill', 'complete', 25, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP),
                        (3, 1, 12, 'other-zero', 'rbc', 'backfill', 'complete', 0, CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP)
                    """
                )
            )

            migration_path = (
                Path(__file__).resolve().parents[1]
                / "alembic"
                / "versions"
                / "0004_amex_backfill_recovery.py"
            )
            spec = importlib.util.spec_from_file_location("amex_backfill_recovery", migration_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            states = connection.execute(
                sa.text(
                    "SELECT id, backfill_status, backfill_completed_at, last_error "
                    "FROM transaction_import_account_states ORDER BY id"
                )
            ).mappings().all()
            windows = connection.execute(
                sa.text(
                    "SELECT id, status, completed_at, last_error "
                    "FROM transaction_import_windows ORDER BY id"
                )
            ).mappings().all()

        self.assertEqual("error", states[0]["backfill_status"])
        self.assertIsNone(states[0]["backfill_completed_at"])
        self.assertEqual(migration.RETRY_ERROR, states[0]["last_error"])
        self.assertEqual("error", windows[0]["status"])
        self.assertIsNone(windows[0]["completed_at"])
        self.assertEqual(migration.RETRY_ERROR, windows[0]["last_error"])
        self.assertEqual("complete", states[1]["backfill_status"])
        self.assertEqual("complete", windows[1]["status"])
        self.assertEqual("complete", states[2]["backfill_status"])
        self.assertEqual("complete", windows[2]["status"])
