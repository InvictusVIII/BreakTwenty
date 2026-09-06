"""Service-level tests for Questrade PDF-statement balance import.

The PDF parser is validated separately against real statements; here we patch
``parse_statements`` with canned points and exercise the import logic: creating
``is_imported`` accounts under the Questrade institution, idempotent per-day upsert
(re-upload dedupes / corrects), backfilling an existing synced account without flagging
it imported, and error/blank accounting.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Account, BalanceHistory, Institution, User
from app.services.questrade_statement_import import import_questrade_statements
from app.services.questrade_statement_pdf import StatementPoint


def _pt(account, date, balance, **kw) -> StatementPoint:
    return StatementPoint(
        account_number=account,
        as_of_date=date,
        balance=balance,
        currency="CAD/USD",
        source=kw.get("source", "header"),
        account_type=kw.get("account_type", "tfsa"),
        warnings=kw.get("warnings", []),
        error=kw.get("error"),
        filename=kw.get("filename", f"{account}_{date}.pdf"),
    )


class QuestradeStatementImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            institution = Institution(
                user_id=1,
                name="Questrade",
                type="api",
                provider="questrade",
            )
            session.add(institution)
            await session.flush()
            self.institution_id = institution.id
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _import(self, points, items=None) -> dict:
        items = items or [("x.pdf", b"%PDF-")]
        with patch("app.services.questrade_statement_import.parse_statements", return_value=points):
            async with self._sessionmaker() as session:
                return await import_questrade_statements(
                    session, 1, self.institution_id, items
                )

    async def _counts(self):
        async with self._sessionmaker() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            institutions = (await session.execute(select(Institution))).scalars().all()
            balance_rows = (
                await session.execute(select(func.count()).select_from(BalanceHistory))
            ).scalar_one()
        return accounts, institutions, balance_rows

    async def test_creates_imported_accounts_under_questrade(self) -> None:
        result = await self._import([
            _pt("52275778", "2020-10-30", 1000.0),
            _pt("52275778", "2020-11-30", 6165.84),
            _pt("52549157", "2021-03-31", 1329.0),
        ])
        self.assertEqual(result["imported"], 3)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["accounts_created"], 2)

        accounts, institutions, balance_rows = await self._counts()
        self.assertEqual(len(institutions), 1)
        self.assertEqual(institutions[0].provider, "questrade")
        self.assertEqual(len(accounts), 2)
        self.assertTrue(all(a.is_imported for a in accounts))
        self.assertEqual({a.external_id for a in accounts}, {"account:52275778", "account:52549157"})
        self.assertTrue(all(a.account_type == "tfsa" and a.currency == "CAD" for a in accounts))
        self.assertEqual(balance_rows, 3)  # one row per distinct (account, day)

    async def test_reimport_is_idempotent(self) -> None:
        points = [_pt("52275778", "2020-10-30", 1000.0), _pt("52275778", "2020-11-30", 6165.84)]
        await self._import(points)
        result = await self._import(points)  # same statements re-uploaded
        self.assertEqual(result["accounts_created"], 0)
        self.assertEqual(result["accounts_updated"], 1)
        self.assertEqual(result["imported"], 0)  # nothing new or changed
        self.assertEqual(result["skipped"], 2)   # both points were unchanged duplicates

        accounts, _, balance_rows = await self._counts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(balance_rows, 2)  # no duplicate rows

    async def test_same_day_balance_is_overwritten(self) -> None:
        await self._import([_pt("52275778", "2020-10-30", 1000.0)])
        await self._import([_pt("52275778", "2020-10-30", 1234.56)])  # corrected, same day
        async with self._sessionmaker() as session:
            balances = (await session.execute(select(BalanceHistory.balance))).scalars().all()
        self.assertEqual(balances, [Decimal("1234.56000000")])

    async def test_backfills_existing_synced_account_without_flagging_imported(self) -> None:
        async with self._sessionmaker() as session:
            session.add(Account(
                user_id=1, institution_id=self.institution_id, external_id="account:52275778",
                name="QT-52275778", account_type="tfsa", currency="CAD",
                is_liability=False, is_imported=False,
            ))
            await session.commit()

        result = await self._import([_pt("52275778", "2020-10-30", 1000.0)])
        self.assertEqual(result["accounts_created"], 0)
        self.assertEqual(result["accounts_updated"], 1)

        accounts, institutions, balance_rows = await self._counts()
        self.assertEqual(len(institutions), 1)   # reused the existing Questrade institution
        self.assertEqual(len(accounts), 1)       # reused the existing synced account
        self.assertFalse(accounts[0].is_imported)  # stays a live synced account
        self.assertEqual(balance_rows, 1)

    async def test_errors_and_blank_statements_accounted(self) -> None:
        result = await self._import([
            _pt("52275778", "2020-10-30", 1000.0),
            _pt("52549157", "2023-04-28", 0.0, source="empty_statement",
                warnings=["Balance is blank — treated as $0.00 (closed/empty statement); please verify"]),
            _pt(None, None, None, source="error", error="Could not read PDF", filename="bad.pdf"),
        ])
        self.assertEqual(result["errors"], 1)    # the unreadable PDF
        self.assertEqual(result["imported"], 2)  # good statement + blank ($0) statement
        self.assertTrue(any("bad.pdf" in w for w in result["warnings"]))
        self.assertTrue(any("blank" in w.lower() for w in result["warnings"]))

    async def test_account_number_without_digits_is_skipped(self) -> None:
        result = await self._import([
            _pt("not-an-account", "2023-08-31", 5000.0, filename="invalid.pdf"),
        ])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["accounts_created"], 0)
        accounts, _, balance_rows = await self._counts()
        self.assertEqual(accounts, [])
        self.assertEqual(balance_rows, 0)

    async def test_imports_transactions_for_imported_accounts_with_dedup(self) -> None:
        from app.models import Transaction
        from app.services.questrade_statement_transactions import StatementTxn, TxnParse

        statement_txns = [
            StatementTxn("2023-08-01", "dividend", 50.22, "USD", "KLIP", "CASH DIV ON 71 SHS"),
            StatementTxn("2023-08-02", "buy", -55.59, "USD", "KLIP", "WE ACTED AS AGENT"),
            StatementTxn("2023-08-11", "deposit", 1000.0, "CAD", None, "CONT 5227577813"),
        ]

        async def run():
            with patch(
                "app.services.questrade_statement_import.parse_statements",
                return_value=[_pt("52275778", "2023-08-31", 5000.0, filename="stmt.pdf")],
            ), patch(
                "app.services.questrade_statement_transactions.parse_statement_transactions",
                side_effect=lambda source, filename=None: TxnParse("52275778", statement_txns),
            ):
                async with self._sessionmaker() as session:
                    return await import_questrade_statements(
                        session, 1, self.institution_id, [("stmt.pdf", b"%PDF-")]
                    )

        result = await run()
        self.assertEqual(result["transactions_imported"], 3)
        async with self._sessionmaker() as session:
            txns = (await session.execute(select(Transaction))).scalars().all()
        self.assertEqual(len(txns), 3)
        self.assertEqual({t.type for t in txns}, {"dividend", "buy", "deposit"})

        # Re-uploading the same statements is idempotent — stable external_id, no duplicates.
        await run()
        async with self._sessionmaker() as session:
            count = (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
        self.assertEqual(count, 3)

    async def test_statement_and_transaction_parsers_are_offloaded(self) -> None:
        from app.services.questrade_statement_transactions import TxnParse

        threaded_functions = []

        async def run_in_thread(function, *args, **kwargs):
            threaded_functions.append(function)
            return function(*args, **kwargs)

        point = _pt("52275778", "2023-08-31", 5000.0, filename="stmt.pdf")
        with (
            patch(
                "app.services.questrade_statement_import.parse_statements",
                return_value=[point],
            ) as parse_balances,
            patch(
                "app.services.questrade_statement_transactions.parse_statement_transactions",
                return_value=TxnParse("52275778", []),
            ) as parse_transactions,
            patch(
                "app.services.questrade_statement_import.asyncio.to_thread",
                side_effect=run_in_thread,
            ),
        ):
            async with self._sessionmaker() as session:
                await import_questrade_statements(
                    session,
                    1,
                    self.institution_id,
                    [("stmt.pdf", b"%PDF-")],
                )

        self.assertIn(parse_balances, threaded_functions)
        self.assertIn(parse_transactions, threaded_functions)

    async def test_concurrent_imports_parse_outside_and_serialize_writes(self) -> None:
        from app.services.questrade_statement_transactions import TxnParse

        writer_lock = asyncio.Lock()
        gate_owned = ContextVar("questrade_import_gate_owned", default=False)
        active_writers = gate_entries = max_active_writers = 0

        @asynccontextmanager
        async def checked_gate():
            nonlocal active_writers, gate_entries, max_active_writers
            async with writer_lock:
                token = gate_owned.set(True)
                active_writers += 1
                gate_entries += 1
                max_active_writers = max(max_active_writers, active_writers)
                try:
                    await asyncio.sleep(0)
                    yield
                finally:
                    active_writers -= 1
                    gate_owned.reset(token)

        def parse_balances(items):
            self.assertFalse(gate_owned.get())
            filename = items[0][0]
            account_number = "52275778" if filename == "first.pdf" else "52549157"
            return [_pt(account_number, "2023-08-31", 5000.0, filename=filename)]

        def parse_transactions(_source, *, filename=None):
            self.assertFalse(gate_owned.get())
            account_number = "52275778" if filename == "first.pdf" else "52549157"
            return TxnParse(account_number, [])

        async def run_import(filename):
            async with self._sessionmaker() as session:
                original_begin = session.begin

                def checked_begin():
                    self.assertTrue(gate_owned.get())
                    return original_begin()

                with patch.object(session, "begin", new=checked_begin):
                    return await import_questrade_statements(
                        session,
                        1,
                        self.institution_id,
                        [(filename, b"%PDF-")],
                    )

        with (
            patch(
                "app.services.questrade_statement_import.parse_statements",
                side_effect=parse_balances,
            ),
            patch(
                "app.services.questrade_statement_transactions.parse_statement_transactions",
                side_effect=parse_transactions,
            ),
            patch(
                "app.services.questrade_statement_import.sqlite_write_gate",
                new=checked_gate,
            ),
        ):
            results = await asyncio.gather(
                run_import("first.pdf"),
                run_import("second.pdf"),
            )

        self.assertTrue(all(result["status"] == "ok" for result in results))
        self.assertEqual(gate_entries, 2)
        self.assertEqual(max_active_writers, 1)
        async with self._sessionmaker() as session:
            account_count = (
                await session.execute(select(func.count()).select_from(Account))
            ).scalar_one()
        self.assertEqual(account_count, 2)


if __name__ == "__main__":
    unittest.main()
