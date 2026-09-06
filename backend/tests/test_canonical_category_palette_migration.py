import importlib.util
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


class CanonicalCategoryPaletteMigrationTests(unittest.TestCase):
    @staticmethod
    def _load_migration():
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "0008_canonical_category_palette.py"
        )
        spec = importlib.util.spec_from_file_location("canonical_category_palette", migration_path)
        assert spec is not None
        assert spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        return migration

    def test_upgrade_applies_complete_palette_only_to_seeded_categories(self) -> None:
        migration = self._load_migration()
        engine = sa.create_engine("sqlite://")

        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE categories (
                        id INTEGER PRIMARY KEY,
                        parent_id INTEGER,
                        seed_key TEXT,
                        is_system BOOLEAN NOT NULL,
                        color_dark TEXT,
                        color_light TEXT
                    )
                    """
                )
            )

            expected = {}
            next_id = 1
            for group_seed_key, colors in migration._GROUP_COLORS.items():
                parent_id = next_id
                next_id += 1
                connection.execute(
                    sa.text(
                        "INSERT INTO categories "
                        "(id, parent_id, seed_key, is_system, color_dark, color_light) "
                        "VALUES (:id, NULL, :seed_key, 1, '#111111', '#111111')"
                    ),
                    {"id": parent_id, "seed_key": group_seed_key},
                )
                expected[parent_id] = colors

                default_leaf_id = next_id
                next_id += 1
                connection.execute(
                    sa.text(
                        "INSERT INTO categories "
                        "(id, parent_id, seed_key, is_system, color_dark, color_light) "
                        "VALUES (:id, :parent_id, :seed_key, 1, '#111111', '#111111')"
                    ),
                    {
                        "id": default_leaf_id,
                        "parent_id": parent_id,
                        "seed_key": f"{group_seed_key}_default_leaf",
                    },
                )
                expected[default_leaf_id] = colors

                for (override_group, leaf_seed_key), override_colors in (
                    migration._LEAF_COLOR_OVERRIDES.items()
                ):
                    if override_group != group_seed_key:
                        continue
                    leaf_id = next_id
                    next_id += 1
                    connection.execute(
                        sa.text(
                            "INSERT INTO categories "
                            "(id, parent_id, seed_key, is_system, color_dark, color_light) "
                            "VALUES (:id, :parent_id, :seed_key, 1, '#111111', '#111111')"
                        ),
                        {"id": leaf_id, "parent_id": parent_id, "seed_key": leaf_seed_key},
                    )
                    expected[leaf_id] = override_colors

            custom_id = next_id
            connection.execute(
                sa.text(
                    "INSERT INTO categories "
                    "(id, parent_id, seed_key, is_system, color_dark, color_light) "
                    "VALUES (:id, NULL, 'shopping', 0, '#123456', '#654321')"
                ),
                {"id": custom_id},
            )

            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            rows = connection.execute(
                sa.text("SELECT id, color_dark, color_light FROM categories ORDER BY id")
            ).all()

        actual = {row.id: (row.color_dark, row.color_light) for row in rows}
        self.assertEqual(expected, {category_id: actual[category_id] for category_id in expected})
        self.assertEqual(("#123456", "#654321"), actual[custom_id])

