from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.transactions import (
    TransactionUpdate,
    router as transactions_router,
    update_transaction,
)
from app.database import Base
from app.models import Account, Category, CategoryRule, Institution, RecurringSeries, Transaction, User
from app.services.categories import (
    delete_category,
    normalize_rule_pattern,
    reset_transaction_category_to_auto,
    seed_default_categories_for_user,
)


class TransactionCategoryResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await seed_default_categories_for_user(session, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _create_committed_manual_fee(self) -> dict[str, int]:
        description = "CANCEL[I**********81:SNAPSHOTVALUENONPRO] FOR JUN 2026"
        pattern = normalize_rule_pattern(description)
        self.assertIsNotNone(pattern)

        async with self._sessionmaker() as session:
            fee_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "fee")
                )
            ).scalar_one()
            refund_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "refund")
                )
            ).scalar_one()
            institution = Institution(
                user_id=1,
                name="Interactive Brokers",
                type="api",
                provider="ibkr",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="ibkr:atomic-reset",
                name="Margin",
                account_type="margin",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            tx = Transaction(
                user_id=1,
                account_id=account.id,
                date=datetime(2026, 7, 2, tzinfo=timezone.utc),
                type="fee",
                description=description,
                amount=13.80,
                raw_amount=13.80,
                currency="CAD",
                external_id="ibkr_atomic_reset_fee",
                category_id=refund_id,
                category_source="manual",
                user_description="Original label",
                user_notes="Original note",
            )
            rule = CategoryRule(
                user_id=1,
                category_id=refund_id,
                priority=100,
                enabled=True,
                description_pattern=pattern,
                is_regex=False,
            )
            session.add_all([tx, rule])
            await session.commit()
            return {
                "transaction_id": tx.id,
                "rule_id": rule.id,
                "fee_id": fee_id,
                "refund_id": refund_id,
            }

    async def test_delete_custom_category_nulls_owned_recurring_series_reference(self) -> None:
        async with self._sessionmaker() as session:
            parent_id = (
                await session.execute(
                    select(Category.id).where(
                        Category.user_id == 1,
                        Category.parent_id.is_(None),
                    ).limit(1)
                )
            ).scalar_one()
            category = Category(
                user_id=1,
                name="Custom Membership",
                parent_id=parent_id,
                classification="expense",
                is_system=False,
            )
            session.add(category)
            await session.flush()
            series = RecurringSeries(
                user_id=1,
                merchant_key="custom membership",
                direction="outflow",
                display_name="Custom Membership",
                category_id=category.id,
                cadence="monthly",
                avg_amount=-20,
                occurrence_count=3,
                confidence=0.9,
            )
            session.add(series)
            await session.commit()
            category_id = category.id
            series_id = series.id

        async with self._sessionmaker() as session:
            await delete_category(session, 1, category_id)
            await session.commit()

        async with self._sessionmaker() as session:
            retained_series = await session.get(RecurringSeries, series_id)
            deleted_category = await session.get(Category, category_id)

        self.assertIsNotNone(retained_series)
        self.assertIsNone(retained_series.category_id)
        self.assertIsNone(deleted_category)

    async def test_reset_default_preserves_provider_positive_fee_sign(self) -> None:
        async with self._sessionmaker() as session:
            fee_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "fee")
                )
            ).scalar_one()
            refund_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "refund")
                )
            ).scalar_one()
            institution = Institution(
                user_id=1,
                name="Interactive Brokers",
                type="api",
                provider="ibkr",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="ibkr:margin",
                name="Margin",
                account_type="margin",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            tx = Transaction(
                user_id=1,
                account_id=account.id,
                date=datetime(2026, 7, 2, tzinfo=timezone.utc),
                type="fee",
                description="CANCEL[I**********81:SNAPSHOTVALUENONPRO] FOR JUN 2026",
                amount=13.80,
                raw_amount=13.80,
                currency="CAD",
                external_id="ibkr_flex_positive_fee_refund",
                category_id=refund_id,
                category_source="manual",
            )
            session.add(tx)
            await session.flush()
            tx_id = tx.id

            reverted = await reset_transaction_category_to_auto(session, 1, tx_id)

            self.assertEqual(reverted.category_id, fee_id)
            self.assertEqual(reverted.category_source, "auto")
            self.assertEqual(reverted.amount, Decimal("13.80000000"))
            self.assertIsNone(reverted.raw_amount)

    async def test_reset_default_preserves_current_sign_when_raw_amount_missing(self) -> None:
        async with self._sessionmaker() as session:
            fee_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "fee")
                )
            ).scalar_one()
            refund_id = (
                await session.execute(
                    select(Category.id).where(Category.user_id == 1, Category.seed_key == "refund")
                )
            ).scalar_one()
            institution = Institution(
                user_id=1,
                name="Interactive Brokers",
                type="api",
                provider="ibkr",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="ibkr:margin:missing-raw",
                name="Margin",
                account_type="margin",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            tx = Transaction(
                user_id=1,
                account_id=account.id,
                date=datetime(2026, 7, 2, tzinfo=timezone.utc),
                type="fee",
                description="CANCEL[I**********81:SNAPSHOTVALUENONPRO] FOR JUN 2026",
                amount=13.80,
                raw_amount=None,
                currency="CAD",
                external_id="ibkr_flex_positive_fee_refund_missing_raw",
                category_id=refund_id,
                category_source="manual",
            )
            session.add(tx)
            await session.flush()
            tx_id = tx.id

            reverted = await reset_transaction_category_to_auto(session, 1, tx_id)

            self.assertEqual(reverted.category_id, fee_id)
            self.assertEqual(reverted.category_source, "auto")
            self.assertEqual(reverted.amount, Decimal("13.80000000"))
            self.assertIsNone(reverted.raw_amount)

    async def test_patch_atomically_saves_text_notes_and_resets_category(self) -> None:
        fixture = await self._create_committed_manual_fee()
        refresh = AsyncMock()

        async with self._sessionmaker() as session:
            with patch(
                "app.api.routes.transactions._refresh_recurring_series",
                new=refresh,
            ):
                response = await update_transaction(
                    fixture["transaction_id"],
                    TransactionUpdate(
                        user_description="  Updated label  ",
                        user_notes="  Updated note  ",
                        reset_category=True,
                    ),
                    db=session,
                    current_user=SimpleNamespace(id=1),
                )

            self.assertEqual(response["user_description"], "Updated label")
            self.assertEqual(response["user_notes"], "Updated note")
            self.assertEqual(response["category_id"], fixture["fee_id"])
            self.assertEqual(response["category_source"], "auto")
            self.assertEqual(response["amount"], Decimal("13.80000000"))
            refresh.assert_awaited_once_with(session, 1)

        async with self._sessionmaker() as session:
            stored = await session.get(Transaction, fixture["transaction_id"])
            self.assertEqual(stored.user_description, "Updated label")
            self.assertEqual(stored.user_notes, "Updated note")
            self.assertEqual(stored.category_id, fixture["fee_id"])
            self.assertEqual(stored.category_source, "auto")
            self.assertEqual(stored.amount, Decimal("13.80000000"))
            self.assertIsNone(stored.raw_amount)
            self.assertIsNone(await session.get(CategoryRule, fixture["rule_id"]))

    async def test_patch_rolls_back_text_and_reset_when_reset_fails(self) -> None:
        fixture = await self._create_committed_manual_fee()

        async def reset_then_fail(db, user_id, transaction_id):
            await reset_transaction_category_to_auto(db, user_id, transaction_id)
            raise RuntimeError("forced reset failure")

        with patch(
            "app.api.routes.transactions.reset_transaction_category_to_auto",
            new=reset_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "forced reset failure"):
                async with self._sessionmaker() as session:
                    await update_transaction(
                        fixture["transaction_id"],
                        TransactionUpdate(
                            user_description="Should roll back",
                            user_notes="Should also roll back",
                            reset_category=True,
                        ),
                        db=session,
                        current_user=SimpleNamespace(id=1),
                    )

        async with self._sessionmaker() as session:
            stored = await session.get(Transaction, fixture["transaction_id"])
            self.assertEqual(stored.user_description, "Original label")
            self.assertEqual(stored.user_notes, "Original note")
            self.assertEqual(stored.category_id, fixture["refund_id"])
            self.assertEqual(stored.category_source, "manual")
            self.assertEqual(stored.raw_amount, Decimal("13.80000000"))
            self.assertIsNotNone(await session.get(CategoryRule, fixture["rule_id"]))

    async def test_patch_rejects_reset_category_conflicts_without_mutation(self) -> None:
        fixture = await self._create_committed_manual_fee()
        conflicts = (
            ("category_id", {"category_id": fixture["fee_id"]}),
            ("clear_category", {"clear_category": True}),
            ("apply_category_to_similar", {"apply_category_to_similar": True}),
        )

        for name, conflict in conflicts:
            with self.subTest(conflict=name):
                async with self._sessionmaker() as session:
                    with self.assertRaises(HTTPException) as raised:
                        await update_transaction(
                            fixture["transaction_id"],
                            TransactionUpdate(
                                user_description=f"Rejected {name}",
                                reset_category=True,
                                **conflict,
                            ),
                            db=session,
                            current_user=SimpleNamespace(id=1),
                        )
                    self.assertEqual(raised.exception.status_code, 400)
                    self.assertIn("reset_category cannot be combined", raised.exception.detail)

        async with self._sessionmaker() as session:
            stored = await session.get(Transaction, fixture["transaction_id"])
            self.assertEqual(stored.user_description, "Original label")
            self.assertEqual(stored.user_notes, "Original note")
            self.assertEqual(stored.category_id, fixture["refund_id"])
            self.assertEqual(stored.category_source, "manual")
            self.assertIsNotNone(await session.get(CategoryRule, fixture["rule_id"]))

    def test_router_keeps_preview_get_and_removes_mutating_reset_post(self) -> None:
        route_methods = {
            (route.path, method)
            for route in transactions_router.routes
            for method in (route.methods or set())
        }

        self.assertIn(("/transactions/{transaction_id}/default-category", "GET"), route_methods)
        self.assertNotIn(("/transactions/{transaction_id}/reset-category", "POST"), route_methods)


if __name__ == "__main__":
    unittest.main()
