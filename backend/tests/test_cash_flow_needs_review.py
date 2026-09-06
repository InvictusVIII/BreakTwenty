from __future__ import annotations

import tempfile
import unittest
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.cash_flow import get_cash_flow
from app.database import Base
from app.models import Account, Category, Institution, Transaction, User
from app.services.categories import seed_default_categories_for_user


class CashFlowNeedsReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_uncategorized_transactions_are_reviewable_but_excluded_from_totals(self) -> None:
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await seed_default_categories_for_user(session, 1)
            categories = dict(
                (
                    await session.execute(
                        select(Category.seed_key, Category.id).where(Category.user_id == 1)
                    )
                ).all()
            )
            institution = Institution(
                user_id=1,
                name="Test Bank",
                type="api",
                provider="test",
                enabled=True,
                hidden=False,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="test-chequing",
                name="Chequing",
                account_type="chequing",
                currency="CAD",
                hidden=False,
            )
            session.add(account)
            await session.flush()
            session.add_all(
                [
                    Transaction(
                        user_id=1,
                        account_id=account.id,
                        date=date(2026, 9, 1),
                        type="deposit",
                        description="Unknown credit",
                        amount=200,
                        currency="CAD",
                        external_id="unknown-credit",
                        category_id=categories["uncategorized"],
                        category_source="auto",
                    ),
                    Transaction(
                        user_id=1,
                        account_id=account.id,
                        date=date(2026, 9, 1),
                        type="withdrawal",
                        description="Unknown debit",
                        amount=-40,
                        currency="CAD",
                        external_id="unknown-debit",
                        category_id=categories["uncategorized"],
                        category_source="auto",
                    ),
                    Transaction(
                        user_id=1,
                        account_id=account.id,
                        date=date(2026, 9, 1),
                        type="withdrawal",
                        description="Known grocery",
                        amount=-10,
                        currency="CAD",
                        external_id="known-grocery",
                        category_id=categories["groceries"],
                        category_source="rule",
                    ),
                ]
            )
            await session.commit()

            payload = await get_cash_flow(
                db=session,
                current_user=SimpleNamespace(id=1),
                start_date="2026-09-01",
                end_date="2026-09-30",
                previous_start_date=None,
                previous_end_date=None,
                institution_ids=None,
                account_ids=None,
            )

        self.assertEqual(0, payload["totals"]["income"])
        self.assertEqual(10, payload["totals"]["expense"])
        self.assertEqual(
            {
                "category_id": categories["uncategorized"],
                "transaction_count": 2,
            },
            payload["needs_review"],
        )
