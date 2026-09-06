from __future__ import annotations

import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Category, User
from app.services.categories import reset_category_to_seed, seed_default_categories_for_user


PARENT_COLORS = {
    "income": ("#00ce31", "#01b400"),
    "investments": ("#4f87ff", "#006eff"),
    "transfers": ("#ff9927", "#ff9927"),
    "financial": ("#ff2935", "#ff0000"),
    "food_and_drink": ("#ff4a18", "#ff4a18"),
    "housing": ("#ffe700", "#ffe700"),
    "transportation": ("#11cdef", "#11cdef"),
    "shopping": ("#2e61ff", "#0082ff"),
    "subscriptions": ("#ffa801", "#ffa801"),
    "entertainment": ("#9035ff", "#b569ff"),
    "health_and_wellness": ("#72ff67", "#72ff67"),
    "personal_care": ("#a370f0", "#e248f7"),
    "travel": ("#ffff00", "#ffff00"),
    "life": ("#fb6e94", "#ff99b1"),
    "taxes": ("#ff2935", "#ff0000"),
    "other": ("#ffffff", "#969696"),
}

LEAF_COLOR_OVERRIDES = {
    ("income", "paycheck"): ("#00ce31", "#00ce31"),
    ("income", "dividend"): ("#00ff35", "#00ff35"),
    ("income", "interest"): ("#14ff00", "#14ff00"),
    ("income", "refund"): ("#00c86a", "#00c86a"),
    ("income", "reimburse"): ("#45ff6c", "#45ff6c"),
    ("income", "misc_income"): ("#00ce31", "#00ce31"),
    ("income", "gifts_received"): ("#00ce31", "#00ce31"),
    ("income", "etransfer_received"): ("#a855f7", "#d2b5ff"),
    ("investments", "buy"): ("#ffff18", "#ffff18"),
    ("investments", "sell"): ("#2bff31", "#2bff31"),
    ("investments", "expired"): ("#c4c4c4", "#888888"),
    ("transfers", "transfer"): ("#ff9400", "#f09000"),
    ("transfers", "deposit"): ("#00ff28", "#00ff28"),
    ("transfers", "withdrawal"): ("#ff2935", "#ff0000"),
    ("transfers", "cc_payment"): ("#259fde", "#21a7ff"),
    ("transfers", "loan_payment"): ("#259fde", "#21a7ff"),
    ("transfers", "loan_advance"): ("#259fde", "#21a7ff"),
    ("other", "uncategorized"): ("#8d8d8d", "#8d8d8d"),
    ("other", "other"): ("#ffffff", "#c3c3c3"),
    ("other", "cash"): ("#adffc2", "#77ffa1"),
    ("other", "business"): ("#c67050", "#d7d7d7"),
    ("other", "government"): ("#ffffff", "#e7e7e7"),
    ("other", "etransfer_sent"): ("#a855f7", "#d2b5ff"),
}


class CategorySeedThemePaletteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _seed(self) -> None:
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await seed_default_categories_for_user(session, 1)
            await session.commit()

    async def test_new_user_receives_the_complete_canonical_theme_palette(self) -> None:
        await self._seed()
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Category).where(Category.user_id == 1, Category.is_system.is_(True))
                )
            ).scalars().all()

        self.assertEqual(110, len(rows))
        parents = {row.seed_key: row for row in rows if row.parent_id is None}
        self.assertEqual(set(PARENT_COLORS), set(parents))
        for seed_key, expected in PARENT_COLORS.items():
            parent = parents[seed_key]
            self.assertEqual(expected, (parent.color_dark, parent.color_light), seed_key)

        parents_by_id = {row.id: row for row in parents.values()}
        for leaf in (row for row in rows if row.parent_id is not None):
            parent_key = parents_by_id[leaf.parent_id].seed_key
            expected = LEAF_COLOR_OVERRIDES.get(
                (parent_key, leaf.seed_key),
                PARENT_COLORS[parent_key],
            )
            self.assertEqual(
                expected,
                (leaf.color_dark, leaf.color_light),
                f"{parent_key}/{leaf.seed_key}",
            )

    async def test_reset_restores_both_theme_colors_and_leaf_classification(self) -> None:
        await self._seed()
        async with self._sessionmaker() as session:
            other_parent = (
                await session.execute(
                    select(Category).where(Category.parent_id.is_(None), Category.seed_key == "other")
                )
            ).scalar_one()
            cash = (
                await session.execute(
                    select(Category).where(
                        Category.parent_id == other_parent.id,
                        Category.seed_key == "cash",
                    )
                )
            ).scalar_one()
            cash.color_dark = "#000000"
            cash.color_light = "#000000"
            cash.classification = "expense"
            await session.flush()

            reset = await reset_category_to_seed(session, 1, cash.id)

            self.assertEqual(("#adffc2", "#77ffa1"), (reset.color_dark, reset.color_light))
            self.assertEqual("transfer", reset.classification)

