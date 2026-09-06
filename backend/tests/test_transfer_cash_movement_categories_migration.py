import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class TransferCashMovementCategoriesMigrationTests(unittest.TestCase):
    def test_upgrade_reclassifies_only_canonical_cash_movement_rows(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE categories (
                        id INTEGER PRIMARY KEY,
                        seed_key TEXT,
                        classification TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO categories (id, seed_key, classification)
                    VALUES
                        (1, 'deposit', 'income'),
                        (2, 'withdrawal', 'expense'),
                        (3, 'deposit', 'transfer'),
                        (4, 'withdrawal', 'transfer'),
                        (5, 'misc', 'expense')
                    """
                )
            )

            migration_path = (
                Path(__file__).resolve().parents[1]
                / "alembic"
                / "versions"
                / "0005_transfer_cash_movement_categories.py"
            )
            spec = importlib.util.spec_from_file_location(
                "transfer_cash_movement_categories", migration_path
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            rows = connection.execute(
                sa.text("SELECT id, classification FROM categories ORDER BY id")
            ).all()

        self.assertEqual(
            [
                (1, "transfer"),
                (2, "transfer"),
                (3, "transfer"),
                (4, "transfer"),
                (5, "expense"),
            ],
            rows,
        )
