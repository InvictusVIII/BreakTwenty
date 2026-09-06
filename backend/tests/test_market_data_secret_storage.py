from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Setting, User, UserSecretArtifact
from app.services.market_data_settings import (
    MARKET_DATA_API_KEY_SETTING,
    get_user_market_data_api_key,
    save_user_market_data_settings,
)
from app.services.user_secret_storage import (
    MARKET_DATA_API_KEY_SECRET_KIND,
    upsert_user_secret_artifact,
)
from app.services.app_encryption import ENCRYPTED_PAYLOAD_MARKER, ENCRYPTED_PAYLOAD_VERSION
from tests.key_material_test_utils import reset_cached_key_material


class MarketDataSecretStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._database_url = f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db"
        self._engine = create_async_engine(self._database_url)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            await session.commit()
        self._encryption_key_patch = patch.dict(
            "os.environ",
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": base64.b64encode(
                    b"\x22" * 32
                ).decode("ascii"),
                "BREAKTWENTY_APP_ENCRYPTION_KEY": base64.b64encode(
                    b"\x23" * 32
                ).decode("ascii"),
            },
        )
        self._encryption_key_patch.start()
        reset_cached_key_material()

    async def asyncTearDown(self) -> None:
        reset_cached_key_material()
        self._encryption_key_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_market_data_key_loads_from_encrypted_user_secret(self) -> None:
        async with self._sessionmaker() as session:
            await upsert_user_secret_artifact(
                session,
                1,
                MARKET_DATA_API_KEY_SECRET_KIND,
                {"api_key": "market-secret"},
            )
            await session.commit()

        async with self._sessionmaker() as session:
            api_key = await get_user_market_data_api_key(session, 1)
            artifact = (
                await session.execute(
                    select(UserSecretArtifact).where(
                        UserSecretArtifact.user_id == 1,
                        UserSecretArtifact.secret_kind == MARKET_DATA_API_KEY_SECRET_KIND,
                    )
                )
            ).scalar_one()
            legacy_setting = (
                await session.execute(
                    select(Setting).where(
                        Setting.user_id == 1,
                        Setting.key == MARKET_DATA_API_KEY_SETTING,
                    )
                )
            ).scalar_one_or_none()

        self.assertEqual(api_key, "market-secret")
        self.assertIsNone(legacy_setting)
        self.assertEqual(
            json.loads(artifact.payload_json).get(ENCRYPTED_PAYLOAD_MARKER),
            ENCRYPTED_PAYLOAD_VERSION,
        )
        self.assertNotIn("market-secret", artifact.payload_json)

    async def test_saving_replaces_an_unreadable_prelaunch_secret(self) -> None:
        async with self._sessionmaker() as session:
            session.add(
                UserSecretArtifact(
                    user_id=1,
                    secret_kind=MARKET_DATA_API_KEY_SECRET_KIND,
                    payload_json=json.dumps(
                        {
                            ENCRYPTED_PAYLOAD_MARKER: 1,
                            "alg": "AES-256-GCM",
                            "nonce": "discarded-prelaunch-value",
                            "ciphertext": "discarded-prelaunch-value",
                        }
                    ),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            await save_user_market_data_settings(session, 1, "fmp", "replacement-key")

        async with self._sessionmaker() as session:
            self.assertEqual(
                await get_user_market_data_api_key(session, 1),
                "replacement-key",
            )


if __name__ == "__main__":
    unittest.main()
