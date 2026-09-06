from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.orchestration import persist_connector_result
from app.connectors.persistence import _find_existing_account, persist_sync_result
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.database import Base
from app.models import (
    Account,
    BalanceHistory,
    ConnectionAuthArtifact,
    Holding,
    Institution,
    User,
    VisibleAuthAttemptArtifact,
)
from app.services import sync_utils
from app.services.sync_tracking import finalize_provider_sync_attempt, start_provider_sync_attempt


class ConnectorPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/connector-persistence.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._sync_session_patch = patch.object(sync_utils, "async_session", self._sessionmaker)
        self._sync_session_patch.start()
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await session.commit()

    async def asyncTearDown(self) -> None:
        self._sync_session_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _seed_snapshot(self) -> tuple[int, int]:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=True,
                sync_status="stale",
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="balance:cad",
                name="CAD Balance",
                account_type="chequing",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            session.add_all(
                [
                    Holding(
                        user_id=1,
                        account_id=account.id,
                        symbol="OLD",
                        name="Prior holding",
                        quantity=1,
                        market_value=125,
                        currency="CAD",
                    ),
                    BalanceHistory(
                        user_id=1,
                        account_id=account.id,
                        balance=125,
                        date=datetime.now(timezone.utc).date(),
                    ),
                ]
            )
            await session.commit()
            return int(institution.id), int(account.id)

    async def test_persist_sync_result_leaves_commit_to_caller(self) -> None:
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:caller-owned",
            balance=10,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                result=SyncResult(status=SyncStatus.OK, accounts=[account]),
            )
            await session.rollback()

        async with self._sessionmaker() as session:
            account_count = (
                await session.execute(
                    select(func.count(Account.id)).where(Account.user_id == 1)
                )
            ).scalar_one()
            institution_count = (
                await session.execute(
                    select(func.count(Institution.id)).where(Institution.user_id == 1)
                )
            ).scalar_one()

        self.assertEqual(account_count, 0)
        self.assertEqual(institution_count, 0)

    async def test_successful_split_add_clears_temporary_auth_required_status(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Wealthsimple",
                type="api",
                provider="wealthsimple",
                enabled=False,
                hidden=True,
                sync_status="auth_required",
            )
            session.add(institution)
            await session.flush()
            institution_id = int(institution.id)
            await session.commit()

        async with self._sessionmaker() as session:
            response = await persist_connector_result(
                session,
                1,
                "wealthsimple",
                SyncResult(
                    status=SyncStatus.OK,
                    accounts=[
                        NormalizedAccount(
                            name="Crypto",
                            account_type="crypto",
                            external_id="wealthsimple:crypto",
                            balance=0,
                            currency="CAD",
                            balance_authoritative=True,
                            holdings_authoritative=True,
                        )
                    ],
                ),
                add_flow=True,
                sync_scope="accounts",
                institution_id=institution_id,
            )

        async with self._sessionmaker() as session:
            sync_status = await session.scalar(
                select(Institution.sync_status).where(Institution.id == institution_id)
            )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(sync_status, "ok")

        async with self._sessionmaker() as session:
            with patch(
                "app.services.sync_utils._institution_family_actively_syncing",
                new=AsyncMock(return_value=False),
            ):
                transaction_response = await persist_connector_result(
                    session,
                    1,
                    "wealthsimple",
                    SyncResult(
                        status=SyncStatus.AUTH_REQUIRED,
                        message="The Wealthsimple session expired.",
                    ),
                    sync_scope="transactions",
                    institution_id=institution_id,
                )

        async with self._sessionmaker() as session:
            sync_status = await session.scalar(
                select(Institution.sync_status).where(Institution.id == institution_id)
            )

        self.assertEqual(transaction_response["status"], "auth_required")
        self.assertEqual(sync_status, "auth_required")

    async def test_persist_logs_use_explicit_sync_id_when_active_attempt_is_stale(self) -> None:
        start_provider_sync_attempt(1, "wise", sync_id="wise-stale-sync")
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:cad",
            balance=10,
            currency="CAD",
            balance_authoritative=True,
            holdings_authoritative=True,
        )

        try:
            async with self._sessionmaker() as session:
                with self.assertLogs("breaktwenty.connectors.wise", level="INFO") as captured:
                    response = await persist_connector_result(
                        session,
                        1,
                        "wise",
                        SyncResult(status=SyncStatus.OK, accounts=[account]),
                        sync_id="wise-fresh-sync",
                        sync_scope="accounts",
                    )
        finally:
            finalize_provider_sync_attempt(1, "wise", sync_id="wise-stale-sync", status="ok")

        rendered = "\n".join(captured.output)
        self.assertEqual(response["sync_id"], "wise-fresh-sync")
        self.assertIn("persist start sync_id=wise-fresh-sync", rendered)
        self.assertIn("persist result sync_id=wise-fresh-sync", rendered)
        self.assertNotIn("persist start sync_id=wise-stale-sync", rendered)
        self.assertNotIn("persist result sync_id=wise-stale-sync", rendered)

    async def test_transaction_phase_failure_replaces_account_phase_ok_status(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Questrade",
                type="api",
                provider="questrade",
                enabled=True,
                sync_status="ok",
            )
            session.add(institution)
            await session.flush()
            institution_id = int(institution.id)
            await session.commit()

        for sync_status, expected_status in (
            (SyncStatus.NETWORK_ERROR, "network_error"),
            (SyncStatus.ERROR, "error"),
        ):
            async with self._sessionmaker() as session:
                await session.execute(
                    Institution.__table__.update()
                    .where(Institution.id == institution_id)
                    .values(sync_status="ok")
                )
                await session.commit()

            async with self._sessionmaker() as session:
                with patch(
                    "app.services.sync_utils._institution_family_actively_syncing",
                    new=AsyncMock(return_value=False),
                ):
                    transaction_response = await persist_connector_result(
                        session,
                        1,
                        "questrade",
                        SyncResult(
                            status=sync_status,
                            message="Questrade transaction history did not finish.",
                        ),
                        sync_scope="transactions",
                        institution_id=institution_id,
                    )

            async with self._sessionmaker() as session:
                persisted_status = await session.scalar(
                    select(Institution.sync_status).where(Institution.id == institution_id)
                )

            self.assertEqual(transaction_response["status"], expected_status)
            self.assertEqual(persisted_status, expected_status)

    async def test_hybrid_scraper_sync_preserves_catalog_institution_type(self) -> None:
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

        async with self._sessionmaker() as session:
            response = await persist_connector_result(
                session,
                1,
                "ibkr",
                SyncResult(
                    status=SyncStatus.OK,
                    accounts=[
                        NormalizedAccount(
                            name="Individual",
                            account_type="taxable",
                            external_id="ibkr:individual",
                            balance=250,
                            currency="CAD",
                            balance_authoritative=True,
                            holdings_authoritative=True,
                        )
                    ],
                ),
                institution_id=institution_id,
            )

        async with self._sessionmaker() as session:
            institution_type = await session.scalar(
                select(Institution.type).where(Institution.id == institution_id)
            )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(institution_type, "api")

    async def test_nonauthoritative_snapshot_preserves_prior_balance_and_holdings(self) -> None:
        institution_id, account_id = await self._seed_snapshot()
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:cad",
            balance=0,
            balance_authoritative=False,
            holdings_authoritative=False,
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                institution_id=institution_id,
                result=SyncResult(
                    status=SyncStatus.OK,
                    accounts=[account],
                    holdings_by_account={account_result_key(account): []},
                ),
            )
            await session.commit()

        async with self._sessionmaker() as session:
            holding_symbols = (
                await session.execute(
                    select(Holding.symbol).where(Holding.account_id == account_id)
                )
            ).scalars().all()
            balances = (
                await session.execute(
                    select(BalanceHistory.balance).where(
                        BalanceHistory.account_id == account_id
                    )
                )
            ).scalars().all()

        self.assertEqual(holding_symbols, ["OLD"])
        self.assertEqual([float(balance) for balance in balances], [125.0])

    async def test_authoritative_zero_snapshot_clears_holdings_and_records_zero(self) -> None:
        institution_id, account_id = await self._seed_snapshot()
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:cad",
            balance=0,
            balance_authoritative=True,
            holdings_authoritative=True,
        )

        async with self._sessionmaker() as session:
            await persist_sync_result(
                session,
                1,
                provider="wise",
                provider_display_name="Wise",
                provider_type="api",
                institution_id=institution_id,
                result=SyncResult(
                    status=SyncStatus.OK,
                    accounts=[account],
                    holdings_by_account={account_result_key(account): []},
                ),
            )
            await session.commit()

        async with self._sessionmaker() as session:
            holding_count = (
                await session.execute(
                    select(func.count(Holding.id)).where(Holding.account_id == account_id)
                )
            ).scalar_one()
            balance = (
                await session.execute(
                    select(BalanceHistory.balance).where(
                        BalanceHistory.account_id == account_id,
                        BalanceHistory.date == datetime.now(timezone.utc).date(),
                    )
                )
            ).scalar_one()

        self.assertEqual(holding_count, 0)
        self.assertEqual(float(balance), 0.0)

    async def test_failure_after_finance_and_status_writes_rolls_back_everything(self) -> None:
        institution_id, account_id = await self._seed_snapshot()
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:cad",
            balance=999,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            holdings_by_account={
                account_result_key(account): [
                    NormalizedHolding(
                        symbol="NEW",
                        name="Replacement holding",
                        quantity=2,
                        market_value=999,
                    )
                ]
            },
        )

        async with self._sessionmaker() as session:
            with (
                patch(
                    "app.services.sync_utils._institution_family_actively_syncing",
                    new=AsyncMock(return_value=False),
                ),
                patch(
                    "app.connectors.orchestration._clear_provider_reauth_quarantine",
                    new=AsyncMock(side_effect=RuntimeError("quarantine cleanup failed")),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "quarantine cleanup failed"):
                    await persist_connector_result(
                        session,
                        1,
                        "wise",
                        result,
                        sync_scope="full",
                        institution_id=institution_id,
                    )

        async with self._sessionmaker() as session:
            sync_status = await session.scalar(
                select(Institution.sync_status).where(Institution.id == institution_id)
            )
            holding_symbols = (
                await session.execute(
                    select(Holding.symbol).where(Holding.account_id == account_id)
                )
            ).scalars().all()
            balance = await session.scalar(
                select(BalanceHistory.balance).where(
                    BalanceHistory.account_id == account_id,
                    BalanceHistory.date == datetime.now(timezone.utc).date(),
                )
            )

        self.assertEqual(sync_status, "stale")
        self.assertEqual(holding_symbols, ["OLD"])
        self.assertEqual(float(balance), 125.0)

    async def test_profile_mismatch_preserves_finance_auth_and_staged_artifacts(self) -> None:
        institution_id, account_id = await self._seed_snapshot()
        now = datetime.now(timezone.utc)
        async with self._sessionmaker() as session:
            session.add_all(
                [
                    ConnectionAuthArtifact(
                        user_id=1,
                        institution_id=institution_id,
                        provider="wise",
                        artifact_kind="credentials",
                        slot="active",
                        payload_json="active-auth-unchanged",
                        updated_at=now,
                    ),
                    ConnectionAuthArtifact(
                        user_id=1,
                        institution_id=institution_id,
                        provider="wise",
                        artifact_kind="credentials",
                        slot="quarantine",
                        payload_json="quarantine-auth-unchanged",
                        updated_at=now,
                    ),
                    VisibleAuthAttemptArtifact(
                        user_id=1,
                        provider="wise",
                        attempt_id="different-profile-attempt",
                        artifact_kind="credentials",
                        payload_json="staged-auth-unchanged",
                        updated_at=now,
                    ),
                ]
            )
            await session.commit()

        incoming = NormalizedAccount(
            name="Different Profile",
            account_type="chequing",
            external_id="balance:different-profile",
            balance=999,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        promotion_hook = AsyncMock()
        with patch(
            "app.connectors.orchestration._clear_provider_reauth_quarantine",
            new=AsyncMock(),
        ) as clear_quarantine:
            async with self._sessionmaker() as session:
                response = await persist_connector_result(
                    session,
                    1,
                    "wise",
                    SyncResult(
                        status=SyncStatus.OK,
                        accounts=[incoming],
                        holdings_by_account={
                            account_result_key(incoming): [
                                NormalizedHolding(
                                    symbol="NEW",
                                    name="Different profile holding",
                                    quantity=1,
                                    market_value=999,
                                )
                            ]
                        },
                    ),
                    sync_scope="full",
                    institution_id=institution_id,
                    success_precommit_hook=promotion_hook,
                )

        self.assertEqual(response["status"], "different_profile_detected")
        self.assertTrue(response["data_preserved"])
        promotion_hook.assert_not_awaited()
        clear_quarantine.assert_not_awaited()

        async with self._sessionmaker() as session:
            accounts = (
                await session.execute(
                    select(Account.external_id).where(
                        Account.institution_id == institution_id
                    )
                )
            ).scalars().all()
            holding_symbols = (
                await session.execute(
                    select(Holding.symbol).where(Holding.account_id == account_id)
                )
            ).scalars().all()
            balances = (
                await session.execute(
                    select(BalanceHistory.balance).where(
                        BalanceHistory.account_id == account_id
                    )
                )
            ).scalars().all()
            auth_payloads = (
                await session.execute(
                    select(
                        ConnectionAuthArtifact.slot,
                        ConnectionAuthArtifact.payload_json,
                    ).where(ConnectionAuthArtifact.institution_id == institution_id)
                )
            ).all()
            staged_payload = await session.scalar(
                select(VisibleAuthAttemptArtifact.payload_json).where(
                    VisibleAuthAttemptArtifact.attempt_id
                    == "different-profile-attempt"
                )
            )
            sync_status = await session.scalar(
                select(Institution.sync_status).where(
                    Institution.id == institution_id
                )
            )

        self.assertEqual(accounts, ["balance:cad"])
        self.assertEqual(holding_symbols, ["OLD"])
        self.assertEqual([float(balance) for balance in balances], [125.0])
        self.assertEqual(
            set(auth_payloads),
            {
                ("active", "active-auth-unchanged"),
                ("quarantine", "quarantine-auth-unchanged"),
            },
        )
        self.assertEqual(staged_payload, "staged-auth-unchanged")
        self.assertEqual(sync_status, "auth_required")

    async def test_optional_sector_enrichment_failure_keeps_committed_sync_success(self) -> None:
        account = NormalizedAccount(
            name="CAD Balance",
            account_type="chequing",
            external_id="balance:sector-failure",
            balance=50,
            balance_authoritative=True,
            holdings_authoritative=True,
        )
        result = SyncResult(
            status=SyncStatus.OK,
            accounts=[account],
            holdings_by_account={
                account_result_key(account): [
                    NormalizedHolding(
                        symbol="TEST",
                        name="Test holding",
                        quantity=1,
                        market_value=50,
                    )
                ]
            },
        )

        async with self._sessionmaker() as session:
            with (
                patch(
                    "app.services.sync_utils._institution_family_actively_syncing",
                    new=AsyncMock(return_value=False),
                ),
                patch(
                    "app.connectors.orchestration.enrich_holding_rows_with_sectors",
                    new=AsyncMock(side_effect=RuntimeError("optional enrichment failed")),
                ),
            ):
                response = await persist_connector_result(
                    session,
                    1,
                    "wise",
                    result,
                    sync_scope="full",
                )

        self.assertEqual(response["status"], "ok")
        async with self._sessionmaker() as session:
            institution_status = await session.scalar(
                select(Institution.sync_status).where(
                    Institution.user_id == 1,
                    Institution.provider == "wise",
                )
            )
            holding = await session.scalar(
                select(Holding).where(
                    Holding.user_id == 1,
                    Holding.symbol == "TEST",
                )
            )

        self.assertEqual(institution_status, "ok")
        self.assertIsNotNone(holding)

    async def test_same_type_and_currency_do_not_adopt_a_different_named_account(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Broker",
                type="api",
                provider="wise",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            session.add(
                Account(
                    user_id=1,
                    institution_id=institution.id,
                    external_id=None,
                    name="TFSA A",
                    account_type="tfsa",
                    currency="CAD",
                )
            )
            await session.commit()
            institution_id = int(institution.id)

        incoming = NormalizedAccount(
            name="TFSA B",
            account_type="tfsa",
            external_id="account:tfsa-b",
            currency="CAD",
        )
        async with self._sessionmaker() as session:
            existing = await _find_existing_account(
                session,
                1,
                institution_id,
                incoming,
            )

        self.assertIsNone(existing)

    async def test_exact_name_does_not_adopt_an_account_without_external_id(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Broker",
                type="api",
                provider="wise",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            session.add(
                Account(
                    user_id=1,
                    institution_id=institution.id,
                    external_id=None,
                    name="Same name",
                    account_type="tfsa",
                    currency="CAD",
                )
            )
            await session.commit()
            institution_id = int(institution.id)

        async with self._sessionmaker() as session:
            existing = await _find_existing_account(
                session,
                1,
                institution_id,
                NormalizedAccount(
                    name="Same name",
                    account_type="tfsa",
                    external_id="account:stable",
                    currency="CAD",
                ),
            )

        self.assertIsNone(existing)


if __name__ == "__main__":
    unittest.main()
