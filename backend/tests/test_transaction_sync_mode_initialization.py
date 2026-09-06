from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.orchestration import persist_connector_result
from app.connectors.persistence import persist_sync_result
from app.connectors.sync_context import connector_sync_context
from app.connectors.sync_modes import plan_account_sync_window
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.database import Base
from app.models import (
    Account,
    Institution,
    Setting,
    Transaction,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
    User,
)
from app.services.sync_activity import get_sync_activity_payload
from app.services.transaction_import import (
    get_transaction_import_status_payload,
    plan_account_transaction_import,
    record_transaction_window_completed,
    record_transaction_window_failed,
    record_transaction_window_started,
)
from app.services.transaction_import_jobs import _latest_completed_daily_job
from app.scrapers import bmo as bmo_scraper
from app.scrapers import td as td_scraper
from app.scrapers.td import _td_bound_backfill_sync_window, _td_pending_backfill_windows
from app.services.user_utils import get_user_local_today_from_db


class TransactionSyncModeInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._connection_contexts = ExitStack()
        self._institution_ids: dict[str, int] = {}
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = f"{self._tmpdir.name}/test.db"
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._db_path}")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self._connection_contexts.enter_context(
            patch("app.services.sync_utils.async_session", self._sessionmaker)
        )

        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self._connection_contexts.close()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    def _activate_connection(self, provider: str, institution_id: int) -> None:
        self._institution_ids[provider] = int(institution_id)
        self._connection_contexts.enter_context(
            connector_sync_context(
                user_id=1,
                provider=provider,
                institution_id=int(institution_id),
            )
        )

    async def _create_account(
        self,
        *,
        provider: str,
        external_id: str,
        successful_fetch_at: datetime | None = None,
    ) -> int:
        async with self._sessionmaker() as session:
            institution_id = self._institution_ids.get(provider)
            if institution_id is None:
                institution = Institution(
                    user_id=1,
                    name=provider.title(),
                    type="api",
                    provider=provider,
                    enabled=True,
                )
                session.add(institution)
                await session.flush()
                institution_id = int(institution.id)
                self._institution_ids[provider] = institution_id

            account = Account(
                user_id=1,
                institution_id=institution_id,
                external_id=external_id,
                name=external_id,
                account_type="chequing",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            if successful_fetch_at is not None:
                session.add(TransactionImportAccountState(
                    user_id=1,
                    institution_id=institution_id,
                    account_id=account_id,
                    provider=provider,
                    account_external_id=external_id,
                    account_name=external_id,
                    account_type="chequing",
                    is_liability=False,
                    backfill_status="complete",
                    backfill_completed_at=successful_fetch_at,
                    last_successful_fetch_at=successful_fetch_at,
                ))
            await session.commit()
            self._activate_connection(provider, institution_id)
            return account_id

    async def test_account_without_transactions_or_import_state_stays_backfill(self) -> None:
        await self._create_account(provider="wise", external_id="balance:empty-backfill")

        async with self._sessionmaker() as session:
            window = await plan_account_sync_window(
                session,
                1,
                "balance:empty-backfill",
                provider="wise",
            )
        self.assertEqual(window.mode, "backfill")

    async def test_persistence_releases_sqlite_write_lock_before_holding_enrichment(self) -> None:
        account = NormalizedAccount(
            name="Invest",
            account_type="tfsa",
            external_id="account:lock-probe",
            balance=100.0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            holdings_by_account={
                account_result_key(account): [
                    NormalizedHolding(
                        symbol="XEQT",
                        name="iShares Core Equity ETF",
                        quantity=1,
                        market_value=100.0,
                    )
                ]
            },
        )

        async def probe_enrichment(_db, _user_id, _holdings):
            conn = sqlite3.connect(self._db_path, timeout=0.1)
            try:
                conn.execute(
                    "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                    (1, "lock_probe", "ok"),
                )
                conn.commit()
            finally:
                conn.close()
            return False

        async with self._sessionmaker() as session:
            with patch(
                "app.connectors.orchestration.enrich_holding_rows_with_sectors",
                side_effect=probe_enrichment,
            ):
                await persist_connector_result(
                    session,
                    1,
                    "wealthsimple",
                    result,
                    sync_scope="full",
                )

        async with self._sessionmaker() as session:
            value = (
                await session.execute(
                    select(Setting.value).where(
                        Setting.user_id == 1,
                        Setting.key == "lock_probe",
                    )
                )
            ).scalar_one()

        self.assertEqual(value, "ok")

    async def test_network_error_persists_as_latest_attempt_status(self) -> None:
        async with self._sessionmaker() as session:
            session.add(
                Institution(
                    user_id=1,
                    name="Questrade",
                    type="api",
                    provider="questrade",
                    enabled=True,
                    sync_status="ok",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            response = await persist_connector_result(
                session,
                1,
                "questrade",
                SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Connection failed - Questrade auth returned 500",
                ),
                sync_scope="accounts",
            )

        async with self._sessionmaker() as session:
            sync_status = (
                await session.execute(
                    select(Institution.sync_status).where(
                        Institution.user_id == 1,
                        Institution.provider == "questrade",
                    )
                )
            ).scalar_one()

        self.assertEqual("network_error", response["status"])
        self.assertEqual("network_error", sync_status)

    async def test_ibkr_flex_persistence_clears_quarantine_through_visible_ibkr_connection(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Interactive Brokers",
                type="api",
                provider="ibkr",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            institution_id = int(institution.id)
            await session.commit()

        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[
                NormalizedAccount(
                    name="IBKR Account",
                    account_type="investment",
                    external_id="ibkr:account",
                    balance=100.0,
                    currency="CAD",
                    balance_authoritative=True,
                    holdings_authoritative=True,
                )
            ],
        )

        async with self._sessionmaker() as session:
            response = await persist_connector_result(
                session,
                1,
                "ibkr_flex",
                result,
                sync_scope="accounts",
                institution_id=institution_id,
            )

        self.assertEqual("ok", response["status"])

    async def test_sync_window_uses_latest_transaction_with_overlap(self) -> None:
        account_id = await self._create_account(
            provider="wise",
            external_id="balance:window-transaction",
            successful_fetch_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )

        async with self._sessionmaker() as session:
            session.add(
                Transaction(
                    user_id=1,
                    account_id=account_id,
                    date=datetime(2026, 4, 10, tzinfo=timezone.utc),
                    type="deposit",
                    amount=100.0,
                    currency="CAD",
                    external_id="wise_window_txn",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            window = await plan_account_sync_window(
                session,
                1,
                "balance:window-transaction",
                provider="wise",
                overlap_days=14,
                today=date(2026, 5, 3),
            )

        self.assertEqual(window.mode, "incremental")
        self.assertEqual(window.latest_persisted_date, date(2026, 4, 10))
        self.assertEqual(window.last_successful_fetch_date, date(2026, 1, 10))
        self.assertEqual(window.start_date, date(2026, 3, 27))
        self.assertEqual(window.end_date, date(2026, 5, 3))

    async def test_sync_window_uses_successful_fetch_checkpoint_for_empty_account(self) -> None:
        await self._create_account(
            provider="wise",
            external_id="balance:window-empty",
            successful_fetch_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
        )

        async with self._sessionmaker() as session:
            window = await plan_account_sync_window(
                session,
                1,
                "balance:window-empty",
                provider="wise",
                overlap_days=7,
                today=date(2026, 5, 3),
            )

        self.assertEqual(window.mode, "incremental")
        self.assertIsNone(window.latest_persisted_date)
        self.assertEqual(window.last_successful_fetch_date, date(2026, 2, 15))
        self.assertEqual(window.start_date, date(2026, 2, 8))
        self.assertEqual(window.end_date, date(2026, 5, 3))

    async def test_durable_incremental_plan_replays_stale_gap_from_checkpoint_overlap(self) -> None:
        await self._create_account(provider="tangerine", external_id="tangerine:stale")

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="tangerine",
                account_external_id="tangerine:stale",
                account_name="Stale Tangerine",
                account_type="chequing",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2025, 6, 7),
                window_end_date=date(2026, 6, 7),
                target_start_date=date(2025, 6, 7),
                target_end_date=date(2026, 6, 7),
                transaction_count=0,
            )
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.user_id == 1,
                        TransactionImportAccountState.provider == "tangerine",
                        TransactionImportAccountState.account_external_id == "tangerine:stale",
                    )
                )
            ).scalar_one()
            state.backfill_completed_at = datetime(2026, 6, 7, tzinfo=timezone.utc)
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="tangerine",
                account_external_id="tangerine:stale",
                account_name="Stale Tangerine",
                account_type="chequing",
                is_liability=False,
                backfill_days=365,
                backfill_chunk_days=365,
                overlap_days=14,
                today=date(2026, 7, 10),
            )

        self.assertEqual(plan.mode, "incremental")
        self.assertEqual(plan.last_successful_fetch_date, date(2026, 6, 7))
        self.assertEqual(plan.start_date, date(2026, 5, 24))
        self.assertEqual(plan.end_date, date(2026, 7, 10))

    async def test_status_groups_split_incremental_windows_by_sync_run(self) -> None:
        await self._create_account(provider="scotiabank", external_id="scotiabank:split")

        async with self._sessionmaker() as session:
            for start, end in (
                (date(2026, 5, 23), date(2026, 5, 31)),
                (date(2026, 6, 1), date(2026, 6, 30)),
                (date(2026, 7, 1), date(2026, 7, 10)),
            ):
                await record_transaction_window_completed(
                    session,
                    user_id=1,
                    provider="scotiabank",
                    account_external_id="scotiabank:split",
                    account_name="Split Scotia",
                    account_type="credit_card",
                    is_liability=True,
                    mode="incremental",
                    window_start_date=start,
                    window_end_date=end,
                    transaction_count=0,
                    sync_id="scotiabank-split-run",
                )
            await session.commit()

        async with self._sessionmaker() as session:
            payload = await get_transaction_import_status_payload(session, 1)

        institution = next(
            item for item in payload["institutions"] if item["provider"] == "scotiabank"
        )
        account = institution["accounts"][0]
        self.assertEqual(account["latest_incremental_start_date"], "2026-05-23")
        self.assertEqual(account["latest_incremental_end_date"], "2026-07-10")

    async def test_sync_window_clamps_future_checkpoint_to_today(self) -> None:
        await self._create_account(
            provider="wise",
            external_id="balance:future-checkpoint",
            successful_fetch_at=datetime(2026, 5, 16, 1, 42, tzinfo=timezone.utc),
        )

        async with self._sessionmaker() as session:
            window = await plan_account_sync_window(
                session,
                1,
                "balance:future-checkpoint",
                provider="wise",
                overlap_days=14,
                today=date(2026, 5, 15),
            )

        self.assertEqual(window.mode, "incremental")
        self.assertEqual(window.start_date, date(2026, 5, 1))
        self.assertEqual(window.end_date, date(2026, 5, 15))

    async def test_user_local_today_uses_configured_timezone(self) -> None:
        async with self._sessionmaker() as session:
            session.add(Setting(user_id=1, key="user_timezone", value="America/Toronto"))
            await session.commit()

        async with self._sessionmaker() as session:
            today = await get_user_local_today_from_db(
                session,
                1,
                now=datetime(2026, 5, 16, 1, 42, tzinfo=timezone.utc),
            )

        self.assertEqual(today, date(2026, 5, 15))

    async def test_successful_empty_initialization_switches_to_incremental(self) -> None:
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:empty-success",
            currency="CAD",
            balance=0.0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            transaction_fetch_succeeded_accounts={account_result_key(account)},
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                result=result,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Account.institution_id).where(
                        Account.external_id == "balance:empty-success"
                    )
                )
            ).scalar_one()
            window = await plan_account_sync_window(
                session,
                1,
                "balance:empty-success",
                provider="wise",
                institution_id=institution_id,
            )
            account_row = (
                await session.execute(
                    select(Account).where(Account.external_id == "balance:empty-success")
                )
            ).scalar_one()
            transaction_count = (
                await session.execute(
                    select(func.count(Transaction.id)).where(Transaction.account_id == account_row.id)
                )
            ).scalar_one()

        self.assertEqual(window.mode, "incremental")
        self.assertEqual(window.last_successful_fetch_date, window.end_date)
        self.assertEqual(transaction_count, 0)

    async def test_successful_fetch_refreshes_transaction_checkpoint(self) -> None:
        account_id = await self._create_account(
            provider="wise",
            external_id="balance:checkpoint-refresh",
            successful_fetch_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:checkpoint-refresh",
            currency="CAD",
            balance=0.0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            transaction_fetch_succeeded_accounts={account_result_key(account)},
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                result=result,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            state_row = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_id == account_id
                    )
                )
            ).scalar_one()

        self.assertGreater(
            state_row.last_successful_fetch_at.replace(tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    async def test_persisted_transactions_without_success_state_stay_backfill(self) -> None:
        account_id = await self._create_account(
            provider="wise",
            external_id="balance:has-transactions",
        )

        async with self._sessionmaker() as session:
            session.add(
                Transaction(
                    user_id=1,
                    account_id=account_id,
                    date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    type="deposit",
                    amount=100.0,
                    currency="CAD",
                    external_id="wise_txn_1",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            window = await plan_account_sync_window(
                session,
                1,
                "balance:has-transactions",
                provider="wise",
            )
        self.assertEqual(window.mode, "backfill")

    async def test_import_state_is_not_written_without_successful_fetch_completion(self) -> None:
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:partial-failure",
            currency="CAD",
            balance=0.0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        account_key = account_result_key(account)
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            transactions_by_account={
                account_key: [
                    NormalizedTransaction(
                        external_id="wise_partial_txn",
                        date=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        type="deposit",
                        amount=25.0,
                        currency="CAD",
                    )
                ]
            },
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                result=result,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            account_row = (
                await session.execute(
                    select(Account).where(Account.external_id == "balance:partial-failure")
                )
            ).scalar_one()
            transaction_count = (
                await session.execute(
                    select(func.count(Transaction.id)).where(Transaction.account_id == account_row.id)
                )
            ).scalar_one()
            state_count = (
                await session.execute(
                    select(func.count(TransactionImportAccountState.id)).where(
                        TransactionImportAccountState.account_id == account_row.id
                    )
                )
            ).scalar_one()

        self.assertEqual(state_count, 0)
        self.assertEqual(transaction_count, 1)

    async def test_existing_transaction_external_id_refreshes_normalized_fields(self) -> None:
        account_id = await self._create_account(
            provider="wise",
            external_id="balance:refresh-existing-tx",
        )

        async with self._sessionmaker() as session:
            session.add(
                Transaction(
                    user_id=1,
                    account_id=account_id,
                    date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    type="deposit",
                    amount=0.0,
                    currency="CAD",
                    description="OLD DESCRIPTION",
                    external_id="wise_refresh_txn",
                )
            )
            await session.commit()

        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:refresh-existing-tx",
            currency="CAD",
            balance=0.0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        account_key = account_result_key(account)
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            transactions_by_account={
                account_key: [
                    NormalizedTransaction(
                        external_id="wise_refresh_txn",
                        date=datetime(2026, 5, 6, tzinfo=timezone.utc),
                        type="deposit",
                        amount=1.0,
                        currency="CAD",
                        description="NEW DESCRIPTION",
                    )
                ]
            },
            transaction_fetch_succeeded_accounts={account_key},
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                result=result,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            transactions = (
                await session.execute(
                    select(Transaction).where(Transaction.external_id == "wise_refresh_txn")
                )
            ).scalars().all()

        self.assertEqual(1, len(transactions))
        self.assertEqual(1.0, transactions[0].amount)
        self.assertEqual("NEW DESCRIPTION", transactions[0].description)
        self.assertEqual(date(2026, 5, 6), transactions[0].date)

    async def test_transaction_import_plan_resumes_only_uncovered_child_windows(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:card")

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2023, 5, 17),
                window_end_date=date(2023, 8, 15),
                transaction_count=12,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                backfill_days=1825,
                backfill_chunk_days=365,
                today=date(2026, 5, 15),
            )

        pending = set(plan.pending_backfill_windows)
        self.assertEqual("backfill", plan.mode)
        self.assertNotIn((date(2023, 5, 17), date(2024, 5, 15)), pending)
        self.assertNotIn((date(2023, 5, 17), date(2023, 8, 15)), pending)
        self.assertIn((date(2023, 8, 16), date(2024, 5, 15)), pending)
        self.assertIn((date(2022, 5, 17), date(2023, 5, 16)), pending)

    async def test_transaction_import_plan_retries_split_leaf_windows(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:split-card")

        async with self._sessionmaker() as session:
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
                error="RBC returned 500",
            )
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 4),
                error="RBC returned 500",
            )
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 5, 5),
                window_end_date=date(2026, 5, 7),
                transaction_count=3,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                backfill_days=10,
                backfill_chunk_days=20,
                today=date(2026, 5, 10),
            )
            status = await get_transaction_import_status_payload(session, 1)

        pending = set(plan.pending_backfill_windows)
        self.assertNotIn((date(2026, 4, 30), date(2026, 5, 10)), pending)
        self.assertIn((date(2026, 4, 30), date(2026, 5, 4)), pending)
        self.assertIn((date(2026, 5, 8), date(2026, 5, 10)), pending)
        self.assertNotIn((date(2026, 5, 5), date(2026, 5, 7)), pending)

        account_status = status["institutions"][0]["accounts"][0]
        self.assertEqual("needs_retry", account_status["status"])
        self.assertEqual(3, account_status["total_windows"])
        self.assertEqual(1, account_status["completed_windows"])
        self.assertEqual(2, account_status["pending_windows"])
        self.assertEqual(1, account_status["failed_windows"])

    async def test_transaction_import_plan_honors_provider_start_floor_after_stale_leaf_failure(self) -> None:
        await self._create_account(provider="td", external_id="td:savings")

        async with self._sessionmaker() as session:
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:savings",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2016, 5, 18),
                window_end_date=date(2016, 5, 18),
                error="TD transaction window failed",
            )
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:savings",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                backfill_days=3650,
                backfill_chunk_days=365,
                backfill_start_date=date(2026, 4, 16),
                today=date(2026, 5, 16),
            )
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_external_id == "td:savings"
                    )
                )
            ).scalar_one()

        self.assertEqual(date(2026, 4, 16), plan.target_start_date)
        self.assertEqual(date(2026, 5, 16), plan.target_end_date)
        self.assertEqual(((date(2026, 4, 16), date(2026, 5, 16)),), plan.pending_backfill_windows)
        self.assertEqual(date(2026, 4, 16), state.target_start_date)
        self.assertEqual(date(2026, 5, 16), state.target_end_date)

    async def test_incremental_completion_ignores_stale_backfill_failure_outside_target(self) -> None:
        await self._create_account(provider="td", external_id="td:savings")

        async with self._sessionmaker() as session:
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:savings",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2016, 5, 18),
                window_end_date=date(2016, 5, 18),
                error="TD transaction window failed",
            )
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:savings",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                backfill_days=3650,
                backfill_chunk_days=365,
                backfill_start_date=date(2026, 4, 16),
                today=date(2026, 5, 16),
            )
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:savings",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                mode="incremental",
                window_start_date=date(2026, 5, 15),
                window_end_date=date(2026, 5, 16),
                transaction_count=3,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_external_id == "td:savings"
                    )
                )
            ).scalar_one()

        self.assertEqual("queued", state.backfill_status)
        self.assertEqual(date(2026, 4, 16), state.target_start_date)
        self.assertEqual(date(2026, 5, 16), state.target_end_date)

    async def test_transaction_window_failure_can_persist_full_backfill_target(self) -> None:
        await self._create_account(provider="td", external_id="td:target")

        async with self._sessionmaker() as session:
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="td",
                account_external_id="td:target",
                account_name="TD Savings",
                account_type="savings",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2016, 5, 18),
                window_end_date=date(2016, 5, 18),
                target_start_date=date(2016, 5, 18),
                target_end_date=date(2026, 5, 16),
                error="TD transaction window failed",
            )
            await session.commit()

        async with self._sessionmaker() as session:
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_external_id == "td:target"
                    )
                )
            ).scalar_one()

        self.assertEqual(date(2016, 5, 18), state.target_start_date)
        self.assertEqual(date(2026, 5, 16), state.target_end_date)

    def test_td_stale_backfill_sync_window_is_bounded_to_open_date(self) -> None:
        account = {
            "sync_window": {
                "mode": "backfill",
                "backfill_start_date": "2016-05-18",
                "backfill_end_date": "2026-05-16",
                "pending_backfill_windows": [
                    {"start": "2016-05-18", "end": "2016-05-18"},
                    {"start": "2025-05-17", "end": "2026-05-16"},
                ],
            }
        }

        windows, planned_windows = _td_pending_backfill_windows(
            account,
            date(2026, 4, 16),
            date(2026, 5, 16),
        )
        _td_bound_backfill_sync_window(
            account,
            date(2026, 4, 16),
            date(2026, 5, 16),
        )

        self.assertEqual([(date(2026, 4, 16), date(2026, 5, 16))], windows)
        self.assertTrue(planned_windows)
        self.assertEqual("2026-04-16", account["sync_window"]["backfill_start_date"])
        self.assertEqual("2026-05-16", account["sync_window"]["backfill_end_date"])

    async def test_td_saved_artifact_resolves_mode_after_account_details(self) -> None:
        resolved_accounts: list[dict[str, object]] = []
        fetched_modes: list[str] = []

        async def fake_load_app_versions(storage_state, *, user_agent):
            del storage_state, user_agent
            return dict(td_scraper.TD_DEFAULT_APP_VERSIONS)

        async def fake_direct_request(storage_state, method, url, *, headers=None, body=None):
            del storage_state, method, url, headers, body
            return {"status": 200, "payload": {}}

        def fake_summary_accounts(payload):
            del payload
            return [
                {
                    "accountKey": "summary-key",
                    "external_id": "summary-id",
                    "product_group_type": "BANKING",
                }
            ]

        async def fake_account_details(storage_state, account_key, *, app_versions, user_agent):
            del storage_state, account_key, app_versions, user_agent
            return {
                "bankNum": "004",
                "branchNum": "1234",
                "accountNum": "5678901",
                "accountName": "TD Every Day Savings Account",
                "product_group_type": "BANKING",
                "accountDetailType": "ACTIVITY",
                "openDt": "2026-04-16",
            }

        async def fake_fetch_transactions(storage_state, account, **kwargs):
            del storage_state, kwargs
            fetched_modes.append(account["sync_window"]["mode"])
            return [], True

        async def resolver(accounts):
            resolved_accounts.append(dict(accounts[0]))
            return {
                accounts[0]["external_id"]: {
                    "mode": "incremental",
                    "start_date": "2026-05-02",
                    "end_date": "2026-05-16",
                }
            }

        with (
            patch.object(td_scraper, "_load_td_app_versions_direct", fake_load_app_versions),
            patch.object(td_scraper, "_td_direct_request", fake_direct_request),
            patch.object(td_scraper, "_td_summary_accounts", fake_summary_accounts),
            patch.object(td_scraper, "_fetch_td_account_details_direct", fake_account_details),
            patch.object(td_scraper, "save_session_artifact", AsyncMock()),
            patch.object(
                td_scraper,
                "_fetch_td_transactions_for_account_direct",
                fake_fetch_transactions,
            ),
        ):
            payload = await td_scraper._sync_td_with_saved_artifacts(
                {"cookies": []},
                {"user_agent": "test-agent"},
                user_id=1,
                mode_resolver=resolver,
                sync_scope="transactions",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual("account:004:1234:5678901", resolved_accounts[0]["external_id"])
        self.assertEqual("2026-04-16", resolved_accounts[0]["openDt"])
        self.assertEqual(["incremental"], fetched_modes)

    async def test_active_scraper_import_job_before_window_marks_account_fetching(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:queued")
        await self._create_account(provider="rbc", external_id="rbc:failed")

        async with self._sessionmaker() as session:
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:queued",
                account_name="Queued Card",
                account_type="credit_card",
                is_liability=True,
                backfill_days=10,
                backfill_chunk_days=20,
                today=date(2026, 5, 10),
            )
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:failed",
                account_name="Failed Card",
                account_type="credit_card",
                is_liability=True,
                backfill_days=10,
                backfill_chunk_days=20,
                today=date(2026, 5, 10),
            )
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:failed",
                account_name="Failed Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
                error="RBC returned 500",
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["rbc"],
                    provider="rbc",
                    job_id="rbc-test-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        accounts = {
            account["account_external_id"]: (institution, account)
            for institution in status["institutions"]
            for account in institution["accounts"]
        }
        queued_institution, queued_account = accounts["rbc:queued"]
        failed_institution, failed_account = accounts["rbc:failed"]

        self.assertEqual("needs_retry", queued_institution["status"])
        self.assertEqual("running", queued_institution["current_fetch_status"])
        self.assertEqual("backfill", queued_institution["current_fetch_mode"])
        self.assertEqual("needs_retry", failed_institution["status"])
        self.assertEqual("running", failed_institution["current_fetch_status"])
        self.assertEqual("backfill_queued", queued_account["status"])
        self.assertEqual("Queued", queued_account["status_label"])
        self.assertEqual("running", queued_account["current_fetch_status"])
        self.assertEqual("Fetching transactions", queued_account["current_fetch_label"])
        self.assertEqual("needs_retry", failed_account["status"])
        self.assertEqual("Retry needed", failed_account["status_label"])
        self.assertEqual("idle", failed_account["current_fetch_status"])

    async def test_bmo_backfill_fetches_all_pending_windows_despite_oldest_txn_date(self) -> None:
        account = {
            "accountIndex": 0,
            "external_id": "account:bmo:test",
            "currency": "CAD",
            "sync_window": {
                "mode": "backfill",
                "end_date": "2026-05-16",
                "pending_backfill_windows": [
                    {"start": "2016-05-18", "end": "2016-05-18"},
                    {"start": "2016-05-19", "end": "2017-05-18"},
                    {"start": "2025-05-17", "end": "2026-05-16"},
                ],
            },
        }
        fetched_windows: list[tuple[date, date]] = []
        recorded_events: list[tuple[str, str, str]] = []

        async def fake_fetch_window(_client, **kwargs):
            fetched_windows.append((kwargs["start_date"], kwargs["end_date"]))
            return {"oldestTxnDate": "2026-05-05", "currency": "CAD"}, []

        async def recorder(event):
            recorded_events.append((event["event"], event["start_date"], event["end_date"]))

        with patch.object(
            bmo_scraper,
            "_bmo_fetch_bank_account_resolved_window_direct",
            fake_fetch_window,
        ):
            _details, transactions, succeeded = await bmo_scraper._bmo_fetch_bank_account_history_direct(
                None,
                account,
                mfa_device_token="token",
                user_agent="agent",
                user_id=1,
                transaction_window_recorder=recorder,
            )

        self.assertTrue(succeeded)
        self.assertEqual([], transactions)
        self.assertEqual(
            [
                (date(2025, 5, 17), date(2026, 5, 16)),
                (date(2016, 5, 19), date(2017, 5, 18)),
                (date(2016, 5, 18), date(2016, 5, 18)),
            ],
            fetched_windows,
        )
        self.assertEqual(
            [
                ("started", "2025-05-17", "2026-05-16"),
                ("completed", "2025-05-17", "2026-05-16"),
                ("started", "2016-05-19", "2017-05-18"),
                ("completed", "2016-05-19", "2017-05-18"),
                ("started", "2016-05-18", "2016-05-18"),
                ("completed", "2016-05-18", "2016-05-18"),
            ],
            recorded_events,
        )

    async def test_never_started_import_status_stays_not_started(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:never-started")

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("not_started", institution["status"])
        self.assertEqual("not_started", institution["history_status"])
        self.assertEqual("idle", institution["current_fetch_status"])
        self.assertEqual("not_started", account["status"])
        self.assertEqual("not_started", account["history_status"])
        self.assertEqual("idle", account["current_fetch_status"])

    async def test_active_alias_import_job_without_state_marks_visible_account_queued(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:queued")

        async with self._sessionmaker() as session:
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["ibkr"],
                    provider="ibkr_flex",
                    job_id="ibkr-flex-test-job",
                    status="queued",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("backfill_queued", institution["status"])
        self.assertEqual("queued", institution["history_status"])
        self.assertEqual("queued", institution["current_fetch_status"])
        self.assertEqual("backfill", institution["current_fetch_mode"])
        self.assertEqual("ibkr-flex-test-job", institution["transaction_import_job_id"])
        self.assertEqual("backfill_queued", account["status"])
        self.assertEqual("queued", account["history_status"])
        self.assertEqual("queued", account["current_fetch_status"])
        self.assertEqual("backfill", account["current_fetch_mode"])

    async def test_terminal_auth_required_job_without_state_marks_visible_account_retry_needed(self) -> None:
        await self._create_account(provider="bmo", external_id="bmo:savings")
        recent_job_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        async with self._sessionmaker() as session:
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["bmo"],
                    provider="bmo",
                    job_id="bmo-auth-required-job",
                    status="auth_required",
                    reason="provider_add",
                    last_error="Provider authentication is required.",
                    last_progress_at=recent_job_time,
                    last_progress_label="Transaction import needs authentication.",
                    finished_at=recent_job_time,
                    updated_at=recent_job_time,
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("needs_retry", institution["status"])
        self.assertEqual("retry_needed", institution["history_status"])
        self.assertEqual("idle", institution["current_fetch_status"])
        self.assertEqual("auth_required", institution["transaction_import_job_status"])
        self.assertEqual("bmo-auth-required-job", institution["transaction_import_job_id"])
        self.assertEqual(
            "Transaction import needs authentication.",
            institution["transaction_import_job_last_progress_label"],
        )
        self.assertEqual("needs_retry", account["status"])
        self.assertEqual("retry_needed", account["history_status"])
        self.assertEqual("idle", account["current_fetch_status"])
        self.assertEqual("Provider authentication is required.", account["last_error"])

    async def test_newer_complete_job_hides_stale_recent_failed_job(self) -> None:
        await self._create_account(provider="moomoo", external_id="moomoo:complete-after-failure")
        now = datetime.now(timezone.utc)

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="moomoo",
                account_external_id="moomoo:complete-after-failure",
                account_name="Moomoo Complete",
                account_type="margin",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2016, 8, 24),
                window_end_date=date(2026, 8, 22),
                transaction_count=1,
            )
            session.add_all(
                [
                    TransactionImportJob(
                        user_id=1,
                        institution_id=self._institution_ids["moomoo"],
                        provider="moomoo",
                        job_id="moomoo-old-failed-job",
                        status="failed",
                        reason="manual_sync",
                        last_error="Previous transaction import failed.",
                        last_progress_at=now - timedelta(minutes=10),
                        last_progress_label="Transaction import failed.",
                        finished_at=now - timedelta(minutes=10),
                        updated_at=now - timedelta(minutes=10),
                    ),
                    TransactionImportJob(
                        user_id=1,
                        institution_id=self._institution_ids["moomoo"],
                        provider="moomoo",
                        job_id="moomoo-new-complete-job",
                        status="complete",
                        reason="manual_sync",
                        last_progress_at=now,
                        last_progress_label="Transaction import complete.",
                        finished_at=now,
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("incremental", institution["status"])
        self.assertEqual("complete", institution["history_status"])
        self.assertEqual("idle", institution["current_fetch_status"])
        self.assertIsNone(institution["transaction_import_job_status"])
        self.assertIsNone(institution["transaction_import_job_id"])
        self.assertEqual("incremental", account["status"])
        self.assertEqual("complete", account["history_status"])
        self.assertIsNone(account["last_error"])

    async def test_running_ibkr_flex_job_marks_visible_accounts_fetching(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:running")

        async with self._sessionmaker() as session:
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["ibkr"],
                    provider="ibkr_flex",
                    job_id="ibkr-flex-running-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("backfill_queued", institution["status"])
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual("backfill", institution["current_fetch_mode"])
        self.assertEqual("backfill_queued", account["status"])
        self.assertEqual("running", account["current_fetch_status"])
        self.assertEqual("backfill", account["current_fetch_mode"])

    async def test_split_parent_running_window_without_active_job_is_not_current_fetch(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:split-card")

        async with self._sessionmaker() as session:
            await record_transaction_window_started(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2021, 5, 17),
                window_end_date=date(2022, 5, 16),
            )
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2021, 5, 17),
                window_end_date=date(2021, 11, 14),
                transaction_count=38,
            )
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:split-card",
                account_name="Credit Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2021, 11, 15),
                window_end_date=date(2022, 5, 16),
                transaction_count=18,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)
            activity = await get_sync_activity_payload(session, 1)
            parent_window = (
                await session.execute(
                    select(TransactionImportWindow).where(
                        TransactionImportWindow.user_id == 1,
                        TransactionImportWindow.provider == "rbc",
                        TransactionImportWindow.account_external_id == "rbc:split-card",
                        TransactionImportWindow.window_start_date == date(2021, 5, 17),
                        TransactionImportWindow.window_end_date == date(2022, 5, 16),
                    )
                )
            ).scalar_one()

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("running", parent_window.status)
        self.assertEqual("incremental", account["status"])
        self.assertEqual("complete", account["history_status"])
        self.assertEqual("idle", account["current_fetch_status"])
        self.assertIsNone(account["current_window_start_date"])
        self.assertIsNone(account["current_window_end_date"])
        self.assertEqual([], activity["active"])

    async def test_ibkr_flex_daily_dedupe_uses_user_local_completion_day(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:daily-dedupe")
        async with self._sessionmaker() as session:
            session.add(Setting(user_id=1, key="user_timezone", value="America/Toronto"))
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["ibkr"],
                    provider="ibkr_flex",
                    job_id="ibkr-flex-complete-today",
                    status="complete",
                    reason="autosync",
                    finished_at=datetime(2026, 5, 16, 13, 0, tzinfo=timezone.utc),
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            completed = await _latest_completed_daily_job(
                session,
                user_id=1,
                provider="ibkr_flex",
                institution_id=self._institution_ids["ibkr"],
                now=datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc),
            )
            non_daily_provider = await _latest_completed_daily_job(
                session,
                user_id=1,
                provider="wise",
                institution_id=self._institution_ids["ibkr"],
                now=datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc),
            )

        self.assertIsNotNone(completed)
        self.assertEqual("ibkr-flex-complete-today", completed.job_id)
        self.assertIsNone(non_daily_provider)

    async def test_ibkr_flex_daily_dedupe_does_not_cross_user_local_day(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:daily-boundary")
        async with self._sessionmaker() as session:
            session.add(Setting(user_id=1, key="user_timezone", value="America/Toronto"))
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["ibkr"],
                    provider="ibkr_flex",
                    job_id="ibkr-flex-complete-yesterday-local",
                    status="complete",
                    reason="autosync",
                    finished_at=datetime(2026, 5, 16, 3, 0, tzinfo=timezone.utc),
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            completed = await _latest_completed_daily_job(
                session,
                user_id=1,
                provider="ibkr_flex",
                institution_id=self._institution_ids["ibkr"],
                now=datetime(2026, 5, 16, 23, 0, tzinfo=timezone.utc),
            )

        self.assertIsNone(completed)

    async def test_running_import_job_before_window_marks_queued_account_fetching(self) -> None:
        await self._create_account(provider="questrade", external_id="account:qt-running")

        async with self._sessionmaker() as session:
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="questrade",
                account_external_id="account:qt-running",
                account_name="Questrade",
                account_type="investment",
                is_liability=False,
                backfill_days=10,
                backfill_chunk_days=20,
                today=date(2026, 5, 10),
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["questrade"],
                    provider="questrade",
                    job_id="questrade-running-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("backfill_queued", institution["status"])
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual("Fetching transactions", institution["current_fetch_label"])
        self.assertEqual("backfill_queued", account["status"])
        self.assertEqual("queued", account["history_status"])
        self.assertEqual("running", account["current_fetch_status"])
        self.assertEqual("Fetching transactions", account["current_fetch_label"])
        self.assertEqual("backfill", account["current_fetch_mode"])

    async def test_completed_incremental_coverage_clears_stale_failed_incremental_window(self) -> None:
        account_id = await self._create_account(
            provider="questrade",
            external_id="account:qt-covered-failure",
            successful_fetch_at=datetime(2026, 8, 24, 15, 13, tzinfo=timezone.utc),
        )
        institution_id = self._institution_ids["questrade"]

        async with self._sessionmaker() as session:
            session.add_all(
                [
                    TransactionImportWindow(
                        user_id=1,
                        institution_id=institution_id,
                        account_id=account_id,
                        provider="questrade",
                        account_external_id="account:qt-covered-failure",
                        mode="incremental",
                        window_start_date=date(2026, 8, 9),
                        window_end_date=date(2026, 8, 24),
                        status="error",
                        last_error="Questrade transaction history window did not finish.",
                        updated_at=datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc),
                    ),
                    TransactionImportWindow(
                        user_id=1,
                        institution_id=institution_id,
                        account_id=account_id,
                        provider="questrade",
                        account_external_id="account:qt-covered-failure",
                        mode="incremental",
                        window_start_date=date(2026, 8, 9),
                        window_end_date=date(2026, 8, 23),
                        status="complete",
                        transaction_count=1,
                        completed_at=datetime(2026, 8, 23, 23, 5, tzinfo=timezone.utc),
                        updated_at=datetime(2026, 8, 23, 23, 5, tzinfo=timezone.utc),
                    ),
                    TransactionImportWindow(
                        user_id=1,
                        institution_id=institution_id,
                        account_id=account_id,
                        provider="questrade",
                        account_external_id="account:qt-covered-failure",
                        mode="incremental",
                        window_start_date=date(2026, 8, 16),
                        window_end_date=date(2026, 8, 24),
                        status="complete",
                        transaction_count=1,
                        completed_at=datetime(2026, 8, 24, 15, 13, tzinfo=timezone.utc),
                        updated_at=datetime(2026, 8, 24, 15, 13, tzinfo=timezone.utc),
                    ),
                ]
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("complete", institution["history_status"])
        self.assertEqual("idle", institution["current_fetch_status"])
        self.assertEqual("complete", account["history_status"])
        self.assertEqual("idle", account["current_fetch_status"])

    async def test_running_api_import_job_keeps_waiting_account_queued(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Questrade",
                type="api",
                provider="questrade",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            self._activate_connection("questrade", institution.id)
            for external_id in ("account:qt-running", "account:qt-waiting"):
                session.add(
                    Account(
                        user_id=1,
                        institution_id=institution.id,
                        external_id=external_id,
                        name=external_id,
                        account_type="investment",
                        currency="CAD",
                    )
                )
            await session.commit()

        async with self._sessionmaker() as session:
            for external_id in ("account:qt-running", "account:qt-waiting"):
                await plan_account_transaction_import(
                    session,
                    user_id=1,
                    provider="questrade",
                    account_external_id=external_id,
                    account_name=external_id,
                    account_type="investment",
                    is_liability=False,
                    backfill_days=10,
                    backfill_chunk_days=20,
                    today=date(2026, 5, 10),
                )
            await record_transaction_window_started(
                session,
                user_id=1,
                provider="questrade",
                account_external_id="account:qt-running",
                account_name="account:qt-running",
                account_type="investment",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["questrade"],
                    provider="questrade",
                    job_id="questrade-running-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual(
            ["running", "queued"],
            [account["current_fetch_status"] for account in institution["accounts"]],
        )
        self.assertEqual(
            ["Fetching transactions", "Queued"],
            [account["current_fetch_label"] for account in institution["accounts"]],
        )

    async def test_running_job_marks_single_visible_queued_account_fetching_when_hidden_window_runs(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Tangerine",
                type="scraper",
                provider="tangerine",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            self._activate_connection("tangerine", institution.id)
            session.add_all(
                [
                    Account(
                        user_id=1,
                        institution_id=institution.id,
                        external_id="tangerine:visible",
                        name="Visible",
                        account_type="chequing",
                        currency="CAD",
                    ),
                    Account(
                        user_id=1,
                        institution_id=institution.id,
                        external_id="tangerine:hidden",
                        name="Hidden",
                        account_type="chequing",
                        currency="CAD",
                        hidden=True,
                    ),
                ]
            )
            await session.commit()

        async with self._sessionmaker() as session:
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="tangerine",
                account_external_id="tangerine:visible",
                account_name="Visible",
                account_type="chequing",
                is_liability=False,
                backfill_days=10,
                backfill_chunk_days=20,
                today=date(2026, 5, 10),
            )
            await record_transaction_window_started(
                session,
                user_id=1,
                provider="tangerine",
                account_external_id="tangerine:hidden",
                account_name="Hidden",
                account_type="chequing",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["tangerine"],
                    provider="tangerine",
                    job_id="tangerine-running-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual("running", account["current_fetch_status"])
        self.assertEqual("Fetching transactions", account["current_fetch_label"])

    async def test_running_scraper_import_job_keeps_waiting_accounts_queued(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="RBC",
                type="scraper",
                provider="rbc",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            self._activate_connection("rbc", institution.id)
            for external_id in ("rbc:card", "rbc:loc"):
                session.add(
                    Account(
                        user_id=1,
                        institution_id=institution.id,
                        external_id=external_id,
                        name=external_id,
                        account_type="credit_card",
                        currency="CAD",
                    )
                )
            await session.commit()

        async with self._sessionmaker() as session:
            for external_id in ("rbc:card", "rbc:loc"):
                await plan_account_transaction_import(
                    session,
                    user_id=1,
                    provider="rbc",
                    account_external_id=external_id,
                    account_name=external_id,
                    account_type="credit_card",
                    is_liability=True,
                    backfill_days=10,
                    backfill_chunk_days=20,
                    today=date(2026, 5, 10),
                )
            await record_transaction_window_started(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:card",
                account_name="rbc:card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["rbc"],
                    provider="rbc",
                    job_id="rbc-multi-account-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual("backfill", institution["current_fetch_mode"])
        self.assertEqual("Fetching transactions", institution["current_fetch_label"])
        self.assertEqual(
            ["running", "queued"],
            [account["current_fetch_status"] for account in institution["accounts"]],
        )
        self.assertEqual(
            ["backfill", "backfill"],
            [account["current_fetch_mode"] for account in institution["accounts"]],
        )

    async def test_active_job_does_not_mark_complete_account_as_fetching(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:complete")

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:complete",
                account_name="Complete Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
                transaction_count=3,
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["rbc"],
                    provider="rbc",
                    job_id="rbc-incremental-test-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("incremental", institution["status"])
        self.assertEqual("complete", institution["history_status"])
        self.assertEqual("idle", institution["current_fetch_status"])
        self.assertIsNone(institution["current_fetch_mode"])
        self.assertEqual("incremental", account["status"])
        self.assertEqual("complete", account["history_status"])
        self.assertEqual("idle", account["current_fetch_status"])
        self.assertIsNone(account["current_fetch_mode"])

    async def test_running_incremental_window_marks_complete_account_as_fetching(self) -> None:
        await self._create_account(provider="rbc", external_id="rbc:complete")

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:complete",
                account_name="Complete Card",
                account_type="credit_card",
                is_liability=True,
                mode="backfill",
                window_start_date=date(2026, 4, 30),
                window_end_date=date(2026, 5, 10),
                transaction_count=3,
            )
            await record_transaction_window_started(
                session,
                user_id=1,
                provider="rbc",
                account_external_id="rbc:complete",
                account_name="Complete Card",
                account_type="credit_card",
                is_liability=True,
                mode="incremental",
                window_start_date=date(2026, 5, 9),
                window_end_date=date(2026, 5, 16),
            )
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=self._institution_ids["rbc"],
                    provider="rbc",
                    job_id="rbc-incremental-test-job",
                    status="running",
                    reason="test",
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            status = await get_transaction_import_status_payload(session, 1)

        institution = status["institutions"][0]
        account = institution["accounts"][0]
        self.assertEqual("incremental", institution["status"])
        self.assertEqual("complete", institution["history_status"])
        self.assertEqual("running", institution["current_fetch_status"])
        self.assertEqual("incremental", institution["current_fetch_mode"])
        self.assertEqual("incremental", account["status"])
        self.assertEqual("complete", account["history_status"])
        self.assertEqual("running", account["current_fetch_status"])
        self.assertEqual("incremental", account["current_fetch_mode"])

    async def test_completed_history_target_is_preserved_for_later_regular_imports(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:U1234567")

        async with self._sessionmaker() as session:
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:U1234567",
                account_name="IBKR Margin",
                account_type="investment",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2026, 1, 12),
                window_end_date=date(2026, 5, 15),
                transaction_count=7,
            )
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:U1234567",
                account_name="IBKR Margin",
                account_type="investment",
                is_liability=False,
                backfill_days=124,
                backfill_chunk_days=365,
                today=date(2026, 5, 16),
            )
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_external_id == "account:U1234567"
                    )
                )
            ).scalar_one()

        self.assertEqual("incremental", plan.mode)
        self.assertEqual(date(2026, 1, 12), plan.target_start_date)
        self.assertEqual(date(2026, 5, 15), plan.target_end_date)
        self.assertEqual((), plan.pending_backfill_windows)
        self.assertEqual(date(2026, 1, 12), state.target_start_date)
        self.assertEqual(date(2026, 5, 15), state.target_end_date)

    async def test_partial_history_target_is_preserved_for_later_regular_retries(self) -> None:
        await self._create_account(provider="ibkr", external_id="account:partial")

        async with self._sessionmaker() as session:
            await plan_account_transaction_import(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:partial",
                account_name="IBKR Partial",
                account_type="investment",
                is_liability=False,
                backfill_days=123,
                backfill_chunk_days=365,
                today=date(2026, 5, 15),
            )
            await record_transaction_window_completed(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:partial",
                account_name="IBKR Partial",
                account_type="investment",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2026, 1, 12),
                window_end_date=date(2026, 3, 15),
                transaction_count=4,
            )
            await record_transaction_window_failed(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:partial",
                account_name="IBKR Partial",
                account_type="investment",
                is_liability=False,
                mode="backfill",
                window_start_date=date(2026, 3, 16),
                window_end_date=date(2026, 5, 15),
                error="temporary failure",
            )
            await session.commit()

        async with self._sessionmaker() as session:
            plan = await plan_account_transaction_import(
                session,
                user_id=1,
                provider="ibkr",
                account_external_id="account:partial",
                account_name="IBKR Partial",
                account_type="investment",
                is_liability=False,
                backfill_days=124,
                backfill_chunk_days=365,
                today=date(2026, 5, 16),
            )
            state = (
                await session.execute(
                    select(TransactionImportAccountState).where(
                        TransactionImportAccountState.account_external_id == "account:partial"
                    )
                )
            ).scalar_one()

        self.assertEqual("backfill", plan.mode)
        self.assertIn((date(2026, 3, 16), date(2026, 5, 15)), plan.pending_backfill_windows)
        self.assertNotIn((date(2026, 5, 16), date(2026, 5, 16)), plan.pending_backfill_windows)
        self.assertEqual(date(2026, 1, 12), state.target_start_date)
        self.assertEqual(date(2026, 5, 15), state.target_end_date)


if __name__ == "__main__":
    unittest.main()
