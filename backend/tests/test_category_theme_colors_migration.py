import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class CategoryThemeColorsMigrationTests(unittest.TestCase):
    @staticmethod
    def _load_migration():
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0006_category_theme_colors.py"
        )
        spec = importlib.util.spec_from_file_location("category_theme_colors", migration_path)
        assert spec is not None
        assert spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def test_upgrade_creates_independent_theme_columns_and_preserves_color(self) -> None:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE categories (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        color TEXT
                    )
                    """
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO categories (id, name, color) VALUES (1, 'Other', '#ffffff')"
                )
            )

            migration = self._load_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            columns = {column["name"] for column in sa.inspect(connection).get_columns("categories")}
            row = connection.execute(
                sa.text("SELECT color_dark, color_light FROM categories WHERE id = 1")
            ).one()
            connection.execute(
                sa.text("UPDATE categories SET color_light = '#000000' WHERE id = 1")
            )
            independent_row = connection.execute(
                sa.text("SELECT color_dark, color_light FROM categories WHERE id = 1")
            ).one()

        self.assertNotIn("color", columns)
        self.assertIn("color_dark", columns)
        self.assertIn("color_light", columns)
        self.assertEqual(("#ffffff", "#ffffff"), row)
        self.assertEqual(("#ffffff", "#000000"), independent_row)

