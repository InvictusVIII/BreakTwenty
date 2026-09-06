from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    Account,
    BalanceHistory,
    Category,
    Holding,
    Institution,
    Setting,
    Transaction,
    User,
)
from app.services import csv_export


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))


class CsvExportCompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/csv-export.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with self._sessionmaker() as db:
            db.add(User(id=1))
            db.add_all(
                (
                    Setting(user_id=1, key="user_timezone", value="UTC"),
                    Setting(user_id=1, key="primary_currency", value="CAD"),
                )
            )
            visible_institution = Institution(
                user_id=1,
                name="Visible Broker",
                type="api",
                provider="ibkr",
                enabled=True,
            )
            hidden_institution = Institution(
                user_id=1,
                name="Private Manual Records",
                type="manual",
                provider="manual_custom",
                enabled=True,
                hidden=True,
            )
            db.add_all((visible_institution, hidden_institution))
            await db.flush()

            brokerage = Account(
                user_id=1,
                institution_id=visible_institution.id,
                external_id="brokerage:margin",
                name="Margin",
                account_type="margin",
                currency="CAD",
            )
            home = Account(
                user_id=1,
                institution_id=hidden_institution.id,
                external_id="manual:home",
                name="Primary Home",
                account_type="real_estate",
                currency="CAD",
                hidden=True,
                purchase_value=Decimal("350000.00"),
                purchase_date=date(2020, 6, 15),
            )
            mortgage = Account(
                user_id=1,
                institution_id=hidden_institution.id,
                external_id="manual:mortgage",
                name="Mortgage",
                account_type="mortgage",
                currency="CAD",
                is_liability=True,
                opening_balance=Decimal("225000.00"),
                opening_balance_date=date(2020, 6, 15),
                is_imported=True,
            )
            db.add_all((brokerage, home, mortgage))
            await db.flush()
            mortgage.secured_asset_account_id = home.id

            parent = Category(
                user_id=1,
                name="Investments",
                classification="investment",
                is_system=False,
            )
            db.add(parent)
            await db.flush()
            futures = Category(
                user_id=1,
                parent_id=parent.id,
                name="Futures",
                classification="investment",
                is_system=False,
            )
            db.add(futures)
            await db.flush()

            db.add_all(
                (
                    BalanceHistory(
                        user_id=1,
                        account_id=brokerage.id,
                        date=date(2026, 8, 20),
                        balance=Decimal("10000.00"),
                    ),
                    BalanceHistory(
                        user_id=1,
                        account_id=home.id,
                        date=date(2020, 6, 15),
                        balance=Decimal("350000.00"),
                    ),
                    BalanceHistory(
                        user_id=1,
                        account_id=mortgage.id,
                        date=date(2026, 8, 20),
                        balance=Decimal("120000.00"),
                    ),
                    Holding(
                        user_id=1,
                        account_id=brokerage.id,
                        symbol="AAPL",
                        name="Apple Inc.",
                        quantity=Decimal("10"),
                        average_cost=Decimal("150.00"),
                        last_price=Decimal("200.00"),
                        market_value=Decimal("2000.00"),
                        currency="CAD",
                    ),
                    Holding(
                        user_id=1,
                        account_id=home.id,
                        symbol="HOME",
                        name="Primary Home",
                        quantity=Decimal("1"),
                        average_cost=Decimal("350000.00"),
                        last_price=Decimal("500000.00"),
                        market_value=Decimal("500000.00"),
                        currency="CAD",
                    ),
                    Transaction(
                        user_id=1,
                        account_id=brokerage.id,
                        date=date(2026, 8, 19),
                        type="sell",
                        symbol="ES",
                        description="E-mini close",
                        amount=Decimal("25.00"),
                        currency="CAD",
                        quantity=Decimal("1"),
                        price=Decimal("5000.00"),
                        asset_category="FUT",
                        realized_pnl=Decimal("125.50"),
                        external_id="ibkr:futures-close",
                        category_id=futures.id,
                        category_source="manual",
                        user_notes="Keep this tax-lot note",
                    ),
                    Transaction(
                        user_id=1,
                        account_id=home.id,
                        date=date(2026, 8, 18),
                        type="other",
                        description="Hidden manual history",
                        amount=Decimal("100.00"),
                        currency="CAD",
                        external_id="manual:hidden-history",
                    ),
                )
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_complete_exports_preserve_financial_and_manual_account_context(self) -> None:
        async with self._sessionmaker() as db:
            _, visible_transactions = await csv_export.export_transactions_csv(
                db,
                1,
                include_hidden=False,
                include_internal_ids=True,
            )
            _, all_transactions = await csv_export.export_transactions_csv(
                db,
                1,
                include_hidden=True,
                include_internal_ids=True,
            )
            _, balances = await csv_export.export_balances_csv(
                db,
                1,
                include_hidden=True,
                include_internal_ids=True,
            )
            _, holdings = await csv_export.export_holdings_csv(
                db,
                1,
                include_hidden=True,
                include_internal_ids=True,
            )

        visible_rows = _rows(visible_transactions)
        all_transaction_rows = _rows(all_transactions)
        self.assertEqual(len(visible_rows), 1)
        self.assertEqual(len(all_transaction_rows), 2)
        futures_row = next(row for row in all_transaction_rows if row["Symbol"] == "ES")
        self.assertEqual(futures_row["Asset Category"], "FUT")
        self.assertEqual(futures_row["Realized P&L"], "125.50")
        self.assertEqual(futures_row["User Notes"], "Keep this tax-lot note")
        self.assertEqual(futures_row["Category"], "Investments > Futures")

        balance_rows = _rows(balances)
        self.assertEqual(len(balance_rows), 3)
        home_row = next(row for row in balance_rows if row["Account Name"] == "Primary Home")
        mortgage_row = next(row for row in balance_rows if row["Account Name"] == "Mortgage")
        self.assertEqual(home_row["Purchase Value"], "350000.00")
        self.assertEqual(home_row["Purchase Date"], "2020-06-15")
        self.assertEqual(mortgage_row["Balance"], "-120000.00")
        self.assertEqual(mortgage_row["Opening Balance"], "225000.00")
        self.assertEqual(mortgage_row["Secured Asset Account"], "Primary Home")
        self.assertEqual(mortgage_row["Is Imported Account"], "true")

        holding_rows = _rows(holdings)
        self.assertEqual({row["Symbol"] for row in holding_rows}, {"AAPL", "HOME"})

    async def test_export_all_archive_contains_four_unencrypted_datasets_and_guidance(self) -> None:
        async with self._sessionmaker() as db:
            archive_name, archive_bytes = await csv_export.export_all_data_archive(db, 1)

        self.assertRegex(archive_name, r"^breaktwenty-data-export-\d{4}-\d{2}-\d{2}\.zip$")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            readme = archive.read("README.txt").decode("utf-8")

            self.assertEqual(len(manifest["files"]), 4)
            self.assertTrue(set(manifest["files"]).issubset(names))
            self.assertIn("README.txt", names)
            self.assertFalse(manifest["encrypted"])
            self.assertFalse(manifest["portableRestore"])
            self.assertTrue(manifest["includesHiddenAccountsAndInstitutions"])
            self.assertIn("not a portable BreakTwenty backup", readme)
            self.assertIn("unencrypted CSV files", readme)

            archived_transactions = next(
                name for name in manifest["files"] if "-transactions-" in name
            )
            self.assertEqual(len(_rows(archive.read(archived_transactions).decode("utf-8"))), 2)

    async def test_networth_full_export_includes_hidden_accounts(self) -> None:
        async with self._sessionmaker() as db:
            _, visible = await csv_export.export_networth_history_csv(
                db,
                1,
                include_hidden=False,
            )
            _, complete = await csv_export.export_networth_history_csv(
                db,
                1,
                include_hidden=True,
            )

        visible_rows = _rows(visible)
        complete_rows = _rows(complete)
        visible_latest = next(row for row in visible_rows if row["Date"] == "2026-08-20")
        complete_latest = next(row for row in complete_rows if row["Date"] == "2026-08-20")
        self.assertEqual(visible_latest["Net Worth"], "10000.00")
        self.assertEqual(complete_latest["Net Worth"], "240000.00")


if __name__ == "__main__":
    unittest.main()
