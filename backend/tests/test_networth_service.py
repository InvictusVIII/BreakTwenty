from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Account, BalanceHistory, FxRate, Institution, Transaction, User
from app.services.fx_history import _cad_per_unit_from_map, _load_cad_rate_map
from app.services.networth import get_account_balance_history_payload


class NetWorthServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_balance_history_reconstructs_from_single_sync_anchor(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="American Express",
                type="scraper",
                provider="amex",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="amex:cobalt",
                name="Cobalt Card",
                account_type="credit_card",
                currency="CAD",
                is_liability=True,
            )
            session.add(account)
            await session.flush()
            session.add(BalanceHistory(
                user_id=1,
                account_id=account.id,
                balance=100.0,
                date=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
            ))
            session.add(Transaction(
                user_id=1,
                account_id=account.id,
                date=datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc),
                type="purchase",
                description="Statement purchase",
                amount=-25.0,
                currency="CAD",
                external_id="amex_purchase_2026_07_09",
            ))
            await session.commit()

        async with self._sessionmaker() as session:
            payload = await get_account_balance_history_payload(session, 1)

        account_history = payload[str(account.id)]
        self.assertEqual(account_history["2026-07-10"], 100.0)
        self.assertEqual(account_history["2026-07-09"], 100.0)

    async def test_fx_rate_map_normalizes_database_dates_for_lookup(self) -> None:
        async with self._sessionmaker() as session:
            session.add(FxRate(
                date=date(2026, 7, 31),
                currency="USD",
                cad_per_unit=Decimal("1.350000000000"),
            ))
            await session.commit()

            rate_map = await _load_cad_rate_map(session, {"USD"}, "2026-08-01")

        self.assertEqual(rate_map["USD"][0], ["2026-07-31"])
        self.assertEqual(
            _cad_per_unit_from_map(rate_map, "USD", date(2026, 8, 1)),
            1.35,
        )


if __name__ == "__main__":
    unittest.main()
