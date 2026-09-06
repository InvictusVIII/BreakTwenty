from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.settings import save_market_data_settings
from app.database import Base
from app.models import Account, Holding, Institution, SymbolSector, User
from app.services.holdings_sector_enrichment import (
    _load_symbol_sector_cache,
    enrich_existing_user_holdings_with_sectors,
)


class HoldingsSectorEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._tmpdir.name}/holdings-sector-enrichment.db"
        )
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            institution = Institution(
                user_id=1,
                name="Broker",
                type="api",
                provider="broker",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="account-1",
                name="Investments",
                account_type="investment",
                currency="CAD",
            )
            session.add(account)
            await session.flush()
            session.add(
                Holding(
                    user_id=1,
                    account_id=account.id,
                    symbol="RETRY",
                    name="Retry Company Inc",
                    quantity=1,
                    market_value=100,
                    currency="CAD",
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_cache_reuses_positive_and_local_terminal_rows_only(self) -> None:
        async with self._sessionmaker() as session:
            session.add_all(
                [
                    SymbolSector(symbol="RETRY", sector=None, instrument_kind=None, source="fmp"),
                    SymbolSector(symbol="LOCAL", sector=None, instrument_kind=None, source="local"),
                    SymbolSector(symbol="FUND", sector=None, instrument_kind="etf", source="fmp"),
                    SymbolSector(symbol="AAPL", sector="Technology", instrument_kind=None, source="fmp"),
                ]
            )
            await session.commit()

            cache = await _load_symbol_sector_cache(
                session,
                ["RETRY", "LOCAL", "FUND", "AAPL"],
            )

        self.assertEqual(set(cache), {"LOCAL", "FUND", "AAPL"})

    async def test_existing_provider_miss_is_retried_and_replaced(self) -> None:
        async with self._sessionmaker() as session:
            session.add(SymbolSector(symbol="RETRY", sector=None, instrument_kind=None, source="fmp"))
            await session.commit()

            with (
                patch(
                    "app.services.holdings_sector_enrichment.get_effective_market_data_config",
                    new=AsyncMock(
                        return_value={
                            "provider": "fmp",
                            "api_key": "test-key",
                        }
                    ),
                ),
                patch(
                    "app.services.holdings_sector_enrichment._classify_symbol_from_candidates",
                    new=AsyncMock(return_value=("Industrials", None, False)),
                ) as classify,
                patch(
                    "app.services.holdings_sector_enrichment.asyncio.sleep",
                    new=AsyncMock(),
                ),
            ):
                changed = await enrich_existing_user_holdings_with_sectors(session, 1)
                await session.commit()

            holding_sector = (
                await session.execute(select(Holding.sector).where(Holding.symbol == "RETRY"))
            ).scalar_one()
            cached = await session.get(SymbolSector, "RETRY")

        self.assertTrue(changed)
        self.assertEqual(classify.await_count, 1)
        self.assertEqual(holding_sector, "Industrials")
        self.assertEqual(cached.sector, "Industrials")

    async def test_empty_provider_results_remain_retryable(self) -> None:
        async with self._sessionmaker() as session:
            session.add(SymbolSector(symbol="RETRY", sector=None, instrument_kind=None, source="fmp"))
            await session.commit()

            with (
                patch(
                    "app.services.holdings_sector_enrichment.get_effective_market_data_config",
                    new=AsyncMock(
                        return_value={
                            "provider": "fmp",
                            "api_key": "test-key",
                        }
                    ),
                ),
                patch(
                    "app.services.holdings_sector_enrichment._classify_symbol_from_candidates",
                    new=AsyncMock(return_value=(None, None, False)),
                ) as classify,
                patch(
                    "app.services.holdings_sector_enrichment.asyncio.sleep",
                    new=AsyncMock(),
                ),
            ):
                first_changed = await enrich_existing_user_holdings_with_sectors(session, 1)
                second_changed = await enrich_existing_user_holdings_with_sectors(session, 1)

            cached = await session.get(SymbolSector, "RETRY")

        self.assertFalse(first_changed)
        self.assertFalse(second_changed)
        self.assertEqual(classify.await_count, 2)
        self.assertIsNone(cached.sector)
        self.assertIsNone(cached.instrument_kind)

    async def test_market_data_save_refreshes_existing_holdings(self) -> None:
        db = AsyncMock()
        metadata = {
            "mode": "user_key",
            "provider": "fmp",
            "provider_label": "FMP",
            "available_providers": [],
            "has_key": True,
            "masked_key": "••••test",
            "source": "user",
            "config_revision": "revision",
        }
        with (
            patch(
                "app.api.routes.settings.save_user_market_data_settings",
                new=AsyncMock(),
            ) as save_settings,
            patch(
                "app.api.routes.settings.enrich_existing_user_holdings_with_sectors",
                new=AsyncMock(return_value=True),
            ) as enrich,
            patch(
                "app.api.routes.settings.get_market_data_key_metadata",
                new=AsyncMock(return_value=metadata),
            ),
        ):
            response = await save_market_data_settings(
                {"provider": "fmp", "api_key": "test-key"},
                db,
                SimpleNamespace(id=1),
            )

        save_settings.assert_awaited_once_with(db, 1, "fmp", "test-key")
        enrich.assert_awaited_once_with(db, 1)
        db.commit.assert_awaited_once()
        self.assertEqual(response, {"status": "ok", **metadata})


if __name__ == "__main__":
    unittest.main()
