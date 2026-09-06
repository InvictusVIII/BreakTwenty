from __future__ import annotations

import unittest

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import TransactionImportAccountState


class ModelSchemaInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record) -> None:
            connection.execute("PRAGMA foreign_keys = ON")

        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_secured_asset_link_cannot_cross_user_ownership(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id, created_at) VALUES "
                "(1, '2026-08-02 00:00:00+00:00'), "
                "(2, '2026-08-02 00:00:00+00:00')"
            ))
            connection.execute(text(
                "INSERT INTO institutions "
                "(id, user_id, name, type, provider, enabled, hidden, sync_status, created_at) VALUES "
                "(11, 1, 'Owner one', 'manual', 'manual_custom', 1, 0, 'ok', '2026-08-02 00:00:00+00:00'), "
                "(22, 2, 'Owner two', 'manual', 'manual_custom', 1, 0, 'ok', '2026-08-02 00:00:00+00:00')"
            ))
            connection.execute(text(
                "INSERT INTO accounts "
                "(id, user_id, institution_id, name, currency, is_liability, hidden, created_at, is_imported) VALUES "
                "(101, 1, 11, 'Loan', 'CAD', 1, 0, '2026-08-02 00:00:00+00:00', 0), "
                "(102, 1, 11, 'Owner one asset', 'CAD', 0, 0, '2026-08-02 00:00:00+00:00', 0), "
                "(202, 2, 22, 'Other owner asset', 'CAD', 0, 0, '2026-08-02 00:00:00+00:00', 0)"
            ))

            connection.execute(text(
                "UPDATE accounts SET secured_asset_account_id=102 WHERE id=101"
            ))

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text(
                    "UPDATE accounts SET secured_asset_account_id=202 WHERE id=101"
                ))

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text("DELETE FROM accounts WHERE id=102"))

    def test_import_state_liability_flag_is_required(self) -> None:
        column = inspect(TransactionImportAccountState).columns.is_liability

        self.assertFalse(column.nullable)
        self.assertIsNotNone(column.server_default)

    def test_synced_provider_is_unique_per_user_but_manual_institutions_are_repeatable(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO users (id, created_at) VALUES "
                "(1, '2026-08-03 00:00:00+00:00'), "
                "(2, '2026-08-03 00:00:00+00:00')"
            ))
            connection.execute(text(
                "INSERT INTO institutions "
                "(user_id, name, type, provider, enabled, hidden, sync_status, created_at) VALUES "
                "(1, 'RBC', 'scraper', 'rbc', 1, 0, 'ok', '2026-08-03 00:00:00+00:00'), "
                "(2, 'RBC', 'scraper', 'rbc', 1, 0, 'ok', '2026-08-03 00:00:00+00:00'), "
                "(1, 'Manual one', 'manual', 'manual_custom', 1, 0, 'ok', '2026-08-03 00:00:00+00:00'), "
                "(1, 'Manual two', 'manual', 'manual_custom', 1, 0, 'ok', '2026-08-03 00:00:00+00:00')"
            ))

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO institutions "
                    "(user_id, name, type, provider, enabled, hidden, sync_status, created_at) VALUES "
                    "(1, 'RBC duplicate', 'scraper', 'rbc', 0, 1, 'ok', '2026-08-03 00:00:00+00:00')"
                ))


if __name__ == "__main__":
    unittest.main()
