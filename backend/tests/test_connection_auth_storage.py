from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.database as database
import app.connectors.orchestration as connector_orchestration
from app.connectors.types import NormalizedAccount, SyncResult, SyncStatus
from app.database import Base
from app.launch_auth import AuthPrincipal
from app.models import (
    Account,
    BalanceHistory,
    Category,
    CategoryRule,
    ConnectionAuthArtifact,
    Holding,
    Institution,
    RecurringSeries,
    Setting,
    SyncBatchJob,
    Transaction,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
    User,
    UserSecretArtifact,
    VisibleAuthAttemptArtifact,
)
from app.services import runtime_state, transaction_import_jobs, wealthsimple
from app.services.institution_cleanup import delete_institution_and_accounts
from app.services.visible_auth_attempt_storage import (
    delete_visible_auth_attempt_artifacts_async,
    load_visible_auth_attempt_artifact_async,
    save_visible_auth_attempt_artifact_async,
)
from app.services.visible_auth_persistence import promote_visible_auth_attempt_state
from app.api.routes import institutions as institutions_route
from app.api.routes import settings as settings_route
from app.api.routes import sync as sync_route
from app.services.connection_auth_storage import (
    ACTIVE_SLOT,
    API_CREDENTIAL_REPLACEMENT_TTL,
    API_CREDENTIALS_ARTIFACT_KIND,
    ProviderAlreadyConnectedError,
    QUARANTINE_SLOT,
    SCRAPER_CREDENTIALS_ARTIFACT_KIND,
    begin_api_credential_replacement,
    delete_provider_connection_artifacts,
    ensure_connection,
    get_api_credentials,
    get_scraper_credentials,
    is_scraper_username_placeholder,
    load_connection_artifact_async,
    move_connection_artifacts_to_slot,
    recover_api_credential_replacements,
    recover_expired_api_credential_replacements,
    settle_api_credential_replacement,
    upsert_api_credentials,
    upsert_connection_artifact,
    upsert_scraper_credentials,
)
from app.services.user_secret_storage import (
    get_user_secret_artifact,
    upsert_user_secret_artifact,
)
from app.services.sync_utils import (
    clear_invalid_scraper_credentials_marker,
    saved_scraper_credentials_are_known_invalid,
    set_invalid_scraper_credentials_marker,
)
from app.services.app_encryption import (
    ENCRYPTED_PAYLOAD_MARKER,
    ENCRYPTED_PAYLOAD_VERSION,
    encrypt_json_payload,
    load_json_payload,
)
from tests.key_material_test_utils import reset_cached_key_material


class RuntimeArtifactDbStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = f"{self._tmpdir.name}/test.db"
        self._runtime_root = Path(self._tmpdir.name) / "users"
        self._desktop_auth_user_root = Path(self._tmpdir.name) / "desktop-auth" / "users"
        self._database_url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(self._database_url)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self._sessionmaker() as session:
            session.add(User(id=1))
            session.add(
                Institution(
                    user_id=1,
                    name="RBC",
                    type="scraper",
                    provider="rbc",
                    enabled=True,
                )
            )
            await session.commit()

        self._db_patch = patch.object(database, "DATABASE_URL", self._database_url)
        self._sessionmaker_patch = patch.object(database, "async_session", self._sessionmaker)
        self._transaction_sessionmaker_patch = patch.object(
            transaction_import_jobs,
            "async_session",
            self._sessionmaker,
        )
        self._runtime_root_patch = patch.object(runtime_state, "USER_RUNTIME_ROOT", str(self._runtime_root))
        self._desktop_auth_root_patch = patch.object(
            runtime_state,
            "DESKTOP_AUTH_USER_ROOT",
            str(self._desktop_auth_user_root),
        )
        self._encryption_key_patch = patch.dict(
            "os.environ",
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": base64.b64encode(
                    b"\x11" * 32
                ).decode("ascii"),
                "BREAKTWENTY_APP_ENCRYPTION_KEY": base64.b64encode(
                    b"\x12" * 32
                ).decode("ascii"),
            },
        )
        self._db_patch.start()
        self._sessionmaker_patch.start()
        self._transaction_sessionmaker_patch.start()
        self._runtime_root_patch.start()
        self._desktop_auth_root_patch.start()
        self._encryption_key_patch.start()
        reset_cached_key_material()

    async def asyncTearDown(self) -> None:
        reset_cached_key_material()
        self._encryption_key_patch.stop()
        self._desktop_auth_root_patch.stop()
        self._runtime_root_patch.stop()
        self._transaction_sessionmaker_patch.stop()
        self._sessionmaker_patch.stop()
        self._db_patch.stop()
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def test_save_json_artifact_writes_canonical_runtime_to_db(self) -> None:
        artifact_path = runtime_state.get_provider_cookie_path(1, "rbc")
        payload = {"cookies": [{"name": "sid", "value": "abc"}]}

        await runtime_state.save_json_artifact_async(artifact_path, payload)

        self.assertFalse(Path(artifact_path).exists())
        self.assertEqual(
            await load_connection_artifact_async(1, "rbc", "cookies", slot=ACTIVE_SLOT),
            payload,
        )
        self.assertEqual(await runtime_state.load_json_artifact_async(artifact_path), payload)
        async with self._sessionmaker() as session:
            payload_json = (
                await session.execute(
                    select(ConnectionAuthArtifact.payload_json).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "rbc",
                        ConnectionAuthArtifact.artifact_kind == "cookies",
                    )
                )
            ).scalar_one()
        self.assertEqual(
            json.loads(payload_json).get(ENCRYPTED_PAYLOAD_MARKER),
            ENCRYPTED_PAYLOAD_VERSION,
        )
        self.assertNotIn("abc", payload_json)

    async def test_connection_artifact_slot_move_reencrypts_for_target_identity(self) -> None:
        payload = {"cookies": [{"name": "sid", "value": "slot-secret"}]}
        await runtime_state.save_json_artifact_async(
            runtime_state.get_provider_cookie_path(1, "rbc"),
            payload,
        )

        async with self._sessionmaker() as session:
            artifact = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "rbc",
                        ConnectionAuthArtifact.artifact_kind == "cookies",
                        ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                    )
                )
            ).scalar_one()
            institution_id = int(artifact.institution_id)
            active_payload_json = artifact.payload_json
            self.assertTrue(
                await move_connection_artifacts_to_slot(
                    session,
                    1,
                    "rbc",
                    ("cookies",),
                    source_slot=ACTIVE_SLOT,
                    target_slot=QUARANTINE_SLOT,
                    institution_id=institution_id,
                )
            )
            await session.commit()

        self.assertEqual(
            await load_connection_artifact_async(
                1,
                "rbc",
                "cookies",
                slot=QUARANTINE_SLOT,
                institution_id=institution_id,
            ),
            payload,
        )
        async with self._sessionmaker() as session:
            quarantine_payload_json = (
                await session.execute(
                    select(ConnectionAuthArtifact.payload_json).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == institution_id,
                        ConnectionAuthArtifact.artifact_kind == "cookies",
                        ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
                    )
                )
            ).scalar_one()
        self.assertNotEqual(active_payload_json, quarantine_payload_json)

    async def test_connection_artifact_ciphertext_rejects_another_institution(self) -> None:
        await runtime_state.save_json_artifact_async(
            runtime_state.get_provider_session_path(1, "rbc"),
            {"token": "bound-secret"},
        )
        async with self._sessionmaker() as session:
            source = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "rbc",
                        ConnectionAuthArtifact.artifact_kind == "session",
                    )
                )
            ).scalar_one()
            other = Institution(
                user_id=1,
                name="BMO",
                type="scraper",
                provider="bmo",
                enabled=True,
            )
            session.add(other)
            await session.flush()
            session.add(
                ConnectionAuthArtifact(
                    user_id=1,
                    institution_id=other.id,
                    provider="bmo",
                    artifact_kind="session",
                    slot=ACTIVE_SLOT,
                    payload_json=source.payload_json,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()
            other_id = int(other.id)

        with self.assertRaises(InvalidTag):
            await load_connection_artifact_async(
                1,
                "bmo",
                "session",
                institution_id=other_id,
            )

    async def test_staged_and_user_secret_ciphertext_rejects_relocation(self) -> None:
        self.assertTrue(
            await save_visible_auth_attempt_artifact_async(
                1,
                "rbc",
                "attempt-source",
                "session",
                {"token": "attempt-bound-secret"},
            )
        )
        async with self._sessionmaker() as session:
            staged = (
                await session.execute(
                    select(VisibleAuthAttemptArtifact).where(
                        VisibleAuthAttemptArtifact.user_id == 1,
                        VisibleAuthAttemptArtifact.provider == "rbc",
                        VisibleAuthAttemptArtifact.attempt_id == "attempt-source",
                        VisibleAuthAttemptArtifact.artifact_kind == "session",
                    )
                )
            ).scalar_one()
            session.add(
                VisibleAuthAttemptArtifact(
                    user_id=1,
                    provider="rbc",
                    attempt_id="attempt-target",
                    artifact_kind="session",
                    payload_json=staged.payload_json,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await upsert_user_secret_artifact(
                session,
                1,
                "source-secret",
                {"token": "user-bound-secret"},
            )
            source_secret = (
                await session.execute(
                    select(UserSecretArtifact).where(
                        UserSecretArtifact.user_id == 1,
                        UserSecretArtifact.secret_kind == "source-secret",
                    )
                )
            ).scalar_one()
            session.add(
                UserSecretArtifact(
                    user_id=1,
                    secret_kind="target-secret",
                    payload_json=source_secret.payload_json,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

        with self.assertRaises(InvalidTag):
            await load_visible_auth_attempt_artifact_async(
                1,
                "rbc",
                "attempt-target",
                "session",
            )
        async with self._sessionmaker() as session:
            with self.assertRaises(InvalidTag):
                await get_user_secret_artifact(session, 1, "target-secret")

    async def test_wealthsimple_otp_challenge_is_encrypted_expiring_and_single_consume(self) -> None:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        with patch.object(wealthsimple, "_utc_now", return_value=now):
            await wealthsimple.save_otp_challenge(1, "person@example.com", "otp-secret")

        async with self._sessionmaker() as session:
            stored = (
                await session.execute(
                    select(UserSecretArtifact).where(
                        UserSecretArtifact.user_id == 1,
                        UserSecretArtifact.secret_kind
                        == wealthsimple.WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
                    )
                )
            ).scalar_one()
            self.assertNotIn("person@example.com", stored.payload_json)
            self.assertNotIn("otp-secret", stored.payload_json)
            self.assertEqual(
                json.loads(stored.payload_json).get(ENCRYPTED_PAYLOAD_MARKER),
                ENCRYPTED_PAYLOAD_VERSION,
            )

        with patch.object(wealthsimple, "_utc_now", return_value=now + timedelta(minutes=1)):
            self.assertEqual(
                await wealthsimple.consume_otp_challenge(1),
                {"email": "person@example.com", "password": "otp-secret"},
            )
            self.assertEqual(await wealthsimple.consume_otp_challenge(1), {})

        with patch.object(wealthsimple, "_utc_now", return_value=now):
            await wealthsimple.save_otp_challenge(1, "person@example.com", "expired-secret")
        with patch.object(wealthsimple, "_utc_now", return_value=now + timedelta(minutes=11)):
            self.assertEqual(await wealthsimple.consume_otp_challenge(1), {})

    async def test_api_session_add_flow_ensure_creates_artifact_connection(self) -> None:
        async with self._sessionmaker() as session:
            payload = {"add_flow": True}
            institution_id = await sync_route._ensure_request_connection(
                session,
                1,
                "wealthsimple",
                payload,
            )
            self.assertEqual(payload["institution_id"], institution_id)

        artifact_path = runtime_state.get_provider_session_path(1, "wealthsimple")
        payload = {"client_id": "client", "access_token": "access"}

        await runtime_state.save_json_artifact_async(artifact_path, payload)

        self.assertEqual(
            await load_connection_artifact_async(1, "wealthsimple", "session", slot=ACTIVE_SLOT),
            payload,
        )
        async with self._sessionmaker() as session:
            institution = (
                await session.execute(
                    select(Institution).where(
                        Institution.user_id == 1,
                        Institution.provider == "wealthsimple",
                    )
                )
            ).scalar_one()

        self.assertEqual(institution.name, "Wealthsimple")
        self.assertEqual(institution.type, "api")
        self.assertFalse(institution.enabled)
        self.assertTrue(institution.hidden)

    async def test_ibkr_flex_add_flow_resolves_credential_storage_connection(self) -> None:
        async with self._sessionmaker() as session:
            settings_result = await settings_route.save_settings(
                {
                    "ibkr_flex_query_id": "123456",
                    "ibkr_flex_token": "test-token",
                    "add_flow": True,
                },
                db=session,
                current_user=SimpleNamespace(id=1),
            )
            institution_id = int(settings_result["institution_id"])
            payload = {
                "add_flow": True,
                "institution_id": institution_id,
            }

            resolved_id = await sync_route._ensure_request_connection(
                session,
                1,
                "ibkr_flex",
                payload,
            )
            institution = await session.get(Institution, institution_id)

        self.assertEqual(resolved_id, institution_id)
        self.assertEqual(payload["institution_id"], institution_id)
        self.assertEqual(institution.provider, "ibkr")
        self.assertFalse(institution.enabled)
        self.assertTrue(institution.hidden)

    async def test_existing_api_sync_resolves_owned_provider_connection_without_payload_id(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Coinbase",
                type="api",
                provider="coinbase",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            existing_id = int(institution.id)
            await session.commit()

            payload = {}
            institution_id = await sync_route._ensure_request_connection(
                session,
                1,
                "coinbase",
                payload,
            )

        self.assertEqual(existing_id, institution_id)
        self.assertEqual(existing_id, payload["institution_id"])

    async def test_existing_sync_rejects_disabled_or_wrong_provider_connection(self) -> None:
        async with self._sessionmaker() as session:
            rbc = (
                await session.execute(
                    select(Institution).where(Institution.user_id == 1, Institution.provider == "rbc")
                )
            ).scalar_one()
            rbc.enabled = False
            wrong_provider = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=True,
            )
            session.add(wrong_provider)
            await session.commit()

            for payload in ({"institution_id": rbc.id}, {"institution_id": wrong_provider.id}):
                with self.assertRaises(HTTPException) as caught:
                    await sync_route._ensure_request_connection(session, 1, "rbc", payload)
                self.assertEqual(caught.exception.status_code, 404)

    async def test_visible_auth_promotion_is_atomic_and_deletes_staging_on_success(self) -> None:
        self.assertTrue(
            await save_visible_auth_attempt_artifact_async(
                1,
                "rbc",
                "attempt-promote",
                "cookies",
                {"cookies": [{"name": "sid", "value": "abc"}]},
            )
        )
        self.assertTrue(
            await save_visible_auth_attempt_artifact_async(
                1,
                "rbc",
                "attempt-promote",
                SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                {"username": "alice", "password": "secret"},
            )
        )
        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Institution.id).where(Institution.user_id == 1, Institution.provider == "rbc")
                )
            ).scalar_one()
            async with session.begin_nested():
                promoted = await promote_visible_auth_attempt_state(
                    session,
                    user_id=1,
                    provider="rbc",
                    attempt_id="attempt-promote",
                    institution_id=institution_id,
                    artifact_kinds=("cookies",),
                )
            await session.commit()

        self.assertEqual(promoted, (True, True))
        async with self._sessionmaker() as session:
            staged = (
                await session.execute(
                    select(VisibleAuthAttemptArtifact).where(
                        VisibleAuthAttemptArtifact.user_id == 1,
                        VisibleAuthAttemptArtifact.attempt_id == "attempt-promote",
                    )
                )
            ).scalars().all()
            artifacts = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == institution_id,
                    )
                )
            ).scalars().all()
        self.assertEqual(staged, [])
        self.assertEqual({artifact.artifact_kind for artifact in artifacts}, {"cookies", SCRAPER_CREDENTIALS_ARTIFACT_KIND})

    async def test_visible_auth_promotion_failure_rolls_back_and_retains_staging(self) -> None:
        for artifact_kind, payload in (
            ("cookies", {"cookies": [{"name": "sid", "value": "abc"}]}),
            (SCRAPER_CREDENTIALS_ARTIFACT_KIND, {"username": "alice", "password": "secret"}),
        ):
            self.assertTrue(
                await save_visible_auth_attempt_artifact_async(
                    1,
                    "rbc",
                    "attempt-retry",
                    artifact_kind,
                    payload,
                )
            )
        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Institution.id).where(Institution.user_id == 1, Institution.provider == "rbc")
                )
            ).scalar_one()
            with patch(
                "app.services.visible_auth_persistence.upsert_scraper_credentials",
                new=AsyncMock(side_effect=RuntimeError("forced promotion failure")),
            ):
                with self.assertRaisesRegex(RuntimeError, "forced promotion failure"):
                    async with session.begin_nested():
                        await promote_visible_auth_attempt_state(
                            session,
                            user_id=1,
                            provider="rbc",
                            attempt_id="attempt-retry",
                            institution_id=institution_id,
                            artifact_kinds=("cookies",),
                        )
            await session.rollback()

        async with self._sessionmaker() as session:
            staged = (
                await session.execute(
                    select(VisibleAuthAttemptArtifact).where(
                        VisibleAuthAttemptArtifact.user_id == 1,
                        VisibleAuthAttemptArtifact.attempt_id == "attempt-retry",
                    )
                )
            ).scalars().all()
            canonical = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == institution_id,
                    )
                )
            ).scalars().all()
        self.assertEqual(len(staged), 2)
        self.assertEqual(canonical, [])

    async def test_visible_auth_route_post_promotion_crash_rolls_back_handoff(self) -> None:
        attempt_id = "attempt-route-rollback"
        for artifact_kind, payload in (
            ("storage_state", {"cookies": [{"name": "sid", "value": "new"}]}),
            (
                SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                {"username": "alice", "password": "new-secret"},
            ),
        ):
            self.assertTrue(
                await save_visible_auth_attempt_artifact_async(
                    1,
                    "rbc",
                    attempt_id,
                    artifact_kind,
                    payload,
                )
            )

        async with self._sessionmaker() as session:
            institution = (
                await session.execute(
                    select(Institution).where(
                        Institution.user_id == 1,
                        Institution.provider == "rbc",
                    )
                )
            ).scalar_one()
            institution.sync_status = "stale"
            institution_id = int(institution.id)
            await upsert_connection_artifact(
                session,
                1,
                "rbc",
                "storage_state",
                {"cookies": [{"name": "sid", "value": "quarantined"}]},
                slot=QUARANTINE_SLOT,
                institution_id=institution_id,
            )
            await session.commit()

        connector = SimpleNamespace(
            sync=AsyncMock(
                return_value=SyncResult(
                    status=SyncStatus.OK,
                    accounts=[
                        NormalizedAccount(
                            name="Visible auth account",
                            account_type="chequing",
                            external_id="visible-auth:rollback",
                            balance=25,
                            balance_authoritative=True,
                            holdings_authoritative=True,
                        )
                    ],
                )
            )
        )
        original_clear_quarantine = connector_orchestration._clear_provider_reauth_quarantine

        async def clear_quarantine_then_crash(
            db,
            user_id: int,
            provider: str,
            institution_id: int | None = None,
        ) -> None:
            await original_clear_quarantine(
                db,
                user_id,
                provider,
                institution_id,
            )
            raise RuntimeError("forced crash after visible-auth promotion")

        status_before_error_write: list[str | None] = []
        original_set_sync_status = sync_route.set_institution_sync_status

        async def record_then_set_error_status(
            db,
            user_id: int,
            provider: str,
            status: str,
            *,
            institution_id: int | None = None,
            commit: bool = True,
        ):
            status_before_error_write.append(
                await db.scalar(
                    select(Institution.sync_status).where(Institution.id == institution_id)
                )
            )
            return await original_set_sync_status(
                db,
                user_id,
                provider,
                status,
                institution_id=institution_id,
                commit=commit,
            )

        cleanup_attempt = AsyncMock()
        promote_attempt = AsyncMock(wraps=sync_route._promote_visible_auth_attempt_state)
        with (
            patch.object(sync_route, "acquire_sync_lock", new=AsyncMock(return_value=True)),
            patch.object(sync_route, "release_sync_lock", new=AsyncMock()),
            patch.object(sync_route, "_provider_sync_scope", return_value="full"),
            patch.object(
                sync_route, "set_institution_sync_status", new=record_then_set_error_status
            ),
            patch.object(
                sync_route,
                "_promote_visible_auth_attempt_state",
                new=promote_attempt,
            ),
            patch.object(sync_route, "_delete_visible_auth_attempt_dir_async", new=cleanup_attempt),
            patch.object(connector_orchestration, "get_connector", return_value=connector),
            patch.object(
                connector_orchestration,
                "_clear_provider_reauth_quarantine",
                new=clear_quarantine_then_crash,
            ),
            patch(
                "app.services.sync_utils._institution_family_actively_syncing",
                new=AsyncMock(return_value=False),
            ),
            patch("app.services.support_auto_archive.archive_run_fire_and_forget"),
        ):
            async with self._sessionmaker() as session:
                response = await sync_route._sync_connector_route(
                    session,
                    1,
                    provider="rbc",
                    body={
                        "sync_id": "visible-auth-route-sync",
                        "attempt_id": attempt_id,
                        "institution_id": institution_id,
                    },
                )

        self.assertEqual(response["status"], "error")
        self.assertEqual(status_before_error_write, ["stale"])
        promote_attempt.assert_awaited_once()
        cleanup_attempt.assert_not_awaited()

        async with self._sessionmaker() as session:
            institution_status = await session.scalar(
                select(Institution.sync_status).where(Institution.id == institution_id)
            )
            account = await session.scalar(
                select(Account).where(Account.external_id == "visible-auth:rollback")
            )
            staged_kinds = set(
                (
                    await session.execute(
                        select(VisibleAuthAttemptArtifact.artifact_kind).where(
                            VisibleAuthAttemptArtifact.user_id == 1,
                            VisibleAuthAttemptArtifact.provider == "rbc",
                            VisibleAuthAttemptArtifact.attempt_id == attempt_id,
                        )
                    )
                ).scalars()
            )
            auth_slots = set(
                (
                    await session.execute(
                        select(
                            ConnectionAuthArtifact.artifact_kind,
                            ConnectionAuthArtifact.slot,
                        ).where(
                            ConnectionAuthArtifact.user_id == 1,
                            ConnectionAuthArtifact.institution_id == institution_id,
                        )
                    )
                ).all()
            )

        self.assertEqual(institution_status, "error")
        self.assertIsNone(account)
        self.assertEqual(
            staged_kinds,
            {"storage_state", SCRAPER_CREDENTIALS_ARTIFACT_KIND},
        )
        self.assertEqual(auth_slots, {("storage_state", QUARANTINE_SLOT)})

    async def test_load_json_artifact_ignores_plaintext_canonical_file(self) -> None:
        artifact_path = Path(runtime_state.get_provider_storage_state_path(1, "rbc"))
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cookies": [], "origins": [{"origin": "https://www.rbcroyalbank.com"}]}
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = await runtime_state.load_json_artifact_async(str(artifact_path))

        self.assertIsNone(loaded)
        self.assertTrue(artifact_path.exists())
        self.assertIsNone(
            await load_connection_artifact_async(1, "rbc", "storage_state", slot=ACTIVE_SLOT)
        )

    def test_runtime_state_description_ignores_plaintext_filesystem_artifact(self) -> None:
        artifact_path = Path(runtime_state.get_provider_session_path(1, "rbc"))
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps({"token": "plain"}), encoding="utf-8")

        state = runtime_state.describe_provider_runtime_state(1, "rbc", artifact_kinds=("session",))

        self.assertFalse(state["artifacts"]["session"]["exists"])
        self.assertIsNone(state["artifacts"]["session"]["storage"])

    def test_json_artifact_helpers_reject_unmapped_file_paths(self) -> None:
        artifact_path = Path(self._tmpdir.name) / "legacy.json"
        artifact_path.write_text(json.dumps({"token": "plain"}), encoding="utf-8")

        with self.assertRaises(ValueError):
            runtime_state.load_json_artifact(str(artifact_path))
        with self.assertRaises(ValueError):
            runtime_state.save_json_artifact(str(artifact_path), {"token": "new"})

        self.assertEqual(json.loads(artifact_path.read_text(encoding="utf-8")), {"token": "plain"})

    def test_encrypted_payload_loader_rejects_plain_json(self) -> None:
        with self.assertRaises(ValueError):
            load_json_payload(
                json.dumps({"token": "plain"}),
                aad_context={"storage": "test", "record_id": 1},
            )

    async def test_visible_auth_attempt_artifact_writes_to_encrypted_db_staging(self) -> None:
        artifact_path = runtime_state.get_provider_visible_auth_artifact_path(
            1,
            "rbc",
            "attempt-1",
            "session",
        )
        payload = {"token": "attempt-secret"}

        await runtime_state.save_json_artifact_async(artifact_path, payload)

        self.assertFalse(Path(artifact_path).exists())
        self.assertEqual(await runtime_state.load_json_artifact_async(artifact_path), payload)

        async with self._sessionmaker() as session:
            payload_json = (
                await session.execute(
                    select(VisibleAuthAttemptArtifact.payload_json).where(
                        VisibleAuthAttemptArtifact.user_id == 1,
                        VisibleAuthAttemptArtifact.provider == "rbc",
                        VisibleAuthAttemptArtifact.attempt_id == "attempt-1",
                        VisibleAuthAttemptArtifact.artifact_kind == "session",
                    )
                )
            ).scalar_one()
        self.assertEqual(
            json.loads(payload_json).get(ENCRYPTED_PAYLOAD_MARKER),
            ENCRYPTED_PAYLOAD_VERSION,
        )
        self.assertNotIn("attempt-secret", payload_json)

        await delete_visible_auth_attempt_artifacts_async(1, "rbc", "attempt-1")
        self.assertIsNone(await runtime_state.load_json_artifact_async(artifact_path))

    async def test_runtime_artifact_endpoint_returns_decrypted_payload(self) -> None:
        artifact_path = runtime_state.get_provider_storage_state_path(1, "rbc")
        payload = {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}
        await runtime_state.save_json_artifact_async(artifact_path, payload)
        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "rbc",
                    )
                )
            ).scalar_one()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/sync/runtime-artifact/rbc/storage_state",
            "headers": [],
            "state": {
                "auth_principal": AuthPrincipal(
                    kind="runner-session",
                    user_id=1,
                    provider="rbc",
                    attempt_id="attempt-runtime",
                    institution_id=institution_id,
                    add_flow=False,
                    purpose="visible-auth",
                )
            },
        })

        with patch.object(
            sync_route,
            "get_runtime_state_metadata",
            return_value={"artifactKinds": ["storage_state"]},
        ):
            response = await sync_route.get_runtime_artifact_for_visible_auth(
                "rbc",
                "storage_state",
                request,
                slots=["active"],
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["slot"], "active")
        self.assertEqual(response["payload"], payload)

    async def test_scraper_credentials_round_trip_through_connection_store(self) -> None:
        async with self._sessionmaker() as session:
            stored = await upsert_scraper_credentials(session, 1, "rbc", "alice", "secret")
            await session.commit()
            credentials = await get_scraper_credentials(session, 1, "rbc")
            artifact = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "rbc",
                        ConnectionAuthArtifact.artifact_kind == SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                    )
                )
            ).scalar_one()

        self.assertTrue(stored)
        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.username, "alice")
        self.assertEqual(credentials.password, "secret")
        payload = json.loads(artifact.payload_json)
        self.assertEqual(payload.get(ENCRYPTED_PAYLOAD_MARKER), ENCRYPTED_PAYLOAD_VERSION)
        self.assertNotIn("alice", artifact.payload_json)
        self.assertNotIn("secret", artifact.payload_json)

    async def test_invalid_credential_memory_is_bound_to_the_connection(self) -> None:
        async with self._sessionmaker() as session:
            first_connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "rbc",
                    )
                )
            ).scalar_one()
            await set_invalid_scraper_credentials_marker(
                session,
                1,
                "rbc",
                "alice",
                "rejected-secret",
                institution_id=first_connection_id,
            )
            self.assertTrue(await saved_scraper_credentials_are_known_invalid(
                session,
                1,
                "rbc",
                "alice",
                "rejected-secret",
                institution_id=first_connection_id,
            ))
            await clear_invalid_scraper_credentials_marker(
                session,
                1,
                "rbc",
                institution_id=first_connection_id,
            )
            self.assertFalse(await saved_scraper_credentials_are_known_invalid(
                session,
                1,
                "rbc",
                "alice",
                "rejected-secret",
                institution_id=first_connection_id,
            ))

    async def test_settings_credential_endpoint_returns_maskable_saved_values(self) -> None:
        async with self._sessionmaker() as session:
            await upsert_scraper_credentials(
                session,
                1,
                "rbc",
                "settings-user",
                "settings-secret",
            )
            await session.commit()
            http_response = Response()
            payload = await settings_route.get_credential_status(
                "rbc",
                response=http_response,
                institution_id=(
                    await session.execute(
                        select(Institution.id).where(
                            Institution.user_id == 1,
                            Institution.provider == "rbc",
                        )
                    )
                ).scalar_one(),
                db=session,
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(
            payload,
            {
                "status": "ok",
                "has_saved_credentials": True,
                "username": "settings-user",
                "password": "settings-secret",
            },
        )
        self.assertEqual(http_response.headers["cache-control"], "no-store")

    async def test_settings_credential_read_does_not_cross_connection_ownership(self) -> None:
        async with self._sessionmaker() as session:
            session.add(User(id=2))
            other_connection = Institution(
                user_id=2,
                name="RBC",
                type="scraper",
                provider="rbc",
                enabled=True,
            )
            session.add(other_connection)
            await session.flush()
            await upsert_scraper_credentials(
                session,
                2,
                "rbc",
                "other-user",
                "other-secret",
                institution_id=other_connection.id,
            )
            await session.commit()
            payload = await settings_route.get_credential_status(
                "rbc",
                response=Response(),
                institution_id=other_connection.id,
                db=session,
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(payload, {"status": "not_found"})

    async def test_settings_read_returns_saved_api_credentials_for_selected_connection(self) -> None:
        async with self._sessionmaker() as session:
            await upsert_api_credentials(
                session,
                1,
                "questrade",
                {"questrade_refresh_token": "settings-api-secret"},
                ensure_institution=True,
            )
            await session.commit()
            institution_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "questrade",
                    )
                )
            ).scalar_one()
            http_response = Response()
            payload = await settings_route.get_settings(
                response=http_response,
                institution_id=institution_id,
                db=session,
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(payload["questrade_refresh_token"], "settings-api-secret")
        self.assertEqual(payload["credential_presence"], {"questrade_refresh_token": True})
        self.assertEqual(http_response.headers["cache-control"], "no-store")

    async def test_api_credentials_round_trip_through_encrypted_connection_store(self) -> None:
        async with self._sessionmaker() as session:
            stored = await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "wise-secret"},
                ensure_institution=True,
            )
            await session.commit()
            credentials = await get_api_credentials(
                session,
                1,
                "wise",
                setting_keys=("wise_api_token",),
            )
            artifact = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "wise",
                        ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                    )
                )
            ).scalar_one()

        self.assertTrue(stored)
        self.assertEqual(credentials["wise_api_token"], "wise-secret")
        payload = json.loads(artifact.payload_json)
        self.assertEqual(payload.get(ENCRYPTED_PAYLOAD_MARKER), ENCRYPTED_PAYLOAD_VERSION)
        self.assertNotIn("wise-secret", artifact.payload_json)

    async def test_api_credential_replacement_commit_and_rollback_are_owner_scoped(self) -> None:
        async with self._sessionmaker() as session:
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "old-secret"},
                ensure_institution=True,
            )
            await session.commit()
            connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "wise",
                    )
                )
            ).scalar_one()

            replacement_id = await begin_api_credential_replacement(
                session,
                1,
                "wise",
                institution_id=connection_id,
                previous_credentials={"wise_api_token": "old-secret"},
                candidate_credentials={"wise_api_token": "candidate-secret"},
            )
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "candidate-secret"},
                institution_id=connection_id,
            )
            await session.commit()

            self.assertFalse(
                await settle_api_credential_replacement(
                    session,
                    999,
                    replacement_id,
                    commit=False,
                )
            )
            self.assertTrue(
                await settle_api_credential_replacement(
                    session,
                    1,
                    replacement_id,
                    commit=False,
                )
            )
            await session.commit()
            self.assertEqual(
                await get_api_credentials(session, 1, "wise", institution_id=connection_id),
                {"wise_api_token": "old-secret"},
            )

            committed_id = await begin_api_credential_replacement(
                session,
                1,
                "wise",
                institution_id=connection_id,
                previous_credentials={"wise_api_token": "old-secret"},
                candidate_credentials={"wise_api_token": "accepted-secret"},
            )
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "accepted-secret"},
                institution_id=connection_id,
            )
            self.assertTrue(
                await settle_api_credential_replacement(
                    session,
                    1,
                    committed_id,
                    commit=True,
                )
            )
            await session.commit()
            self.assertEqual(
                await get_api_credentials(session, 1, "wise", institution_id=connection_id),
                {"wise_api_token": "accepted-secret"},
            )

    async def test_api_credential_replacement_without_prior_secret_deletes_candidate_on_rollback(self) -> None:
        async with self._sessionmaker() as session:
            session.add(
                Institution(
                    user_id=1,
                    name="Wise",
                    type="api",
                    provider="wise",
                    enabled=True,
                )
            )
            await session.flush()
            connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "wise",
                    )
                )
            ).scalar_one()
            replacement_id = await begin_api_credential_replacement(
                session,
                1,
                "wise",
                institution_id=connection_id,
                previous_credentials={},
                candidate_credentials={"wise_api_token": "candidate-secret"},
            )
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "candidate-secret"},
                institution_id=connection_id,
            )
            self.assertTrue(
                await settle_api_credential_replacement(
                    session,
                    1,
                    replacement_id,
                    commit=False,
                )
            )
            await session.commit()

            self.assertEqual(
                await get_api_credentials(session, 1, "wise", institution_id=connection_id),
                {},
            )
            active = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == connection_id,
                        ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                        ConnectionAuthArtifact.slot == ACTIVE_SLOT,
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNone(active)

    async def test_expired_api_credential_replacement_recovers_without_restart(self) -> None:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        async with self._sessionmaker() as session:
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "old-secret"},
                ensure_institution=True,
            )
            connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "wise",
                    )
                )
            ).scalar_one()
            await begin_api_credential_replacement(
                session,
                1,
                "wise",
                institution_id=connection_id,
                previous_credentials={"wise_api_token": "old-secret"},
                candidate_credentials={"wise_api_token": "interrupted-secret"},
            )
            await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "interrupted-secret"},
                institution_id=connection_id,
            )
            backup = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == connection_id,
                        ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                        ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
                    )
                )
            ).scalar_one()
            backup.updated_at = now - API_CREDENTIAL_REPLACEMENT_TTL - timedelta(seconds=1)
            await session.commit()

            self.assertEqual(
                await recover_expired_api_credential_replacements(
                    session,
                    user_id=1,
                    now=now,
                ),
                1,
            )
            await session.commit()
            self.assertEqual(
                await get_api_credentials(session, 1, "wise", institution_id=connection_id),
                {"wise_api_token": "old-secret"},
            )

    async def test_questrade_startup_recovery_preserves_a_rotated_token_set(self) -> None:
        previous = {
            "questrade_access_token": "old-access",
            "questrade_refresh_token": "old-refresh",
            "questrade_api_server": "https://old.example/",
        }
        candidate = {
            **previous,
            "questrade_refresh_token": "submitted-refresh",
        }
        rotated = {
            "questrade_access_token": "rotated-access",
            "questrade_refresh_token": "rotated-refresh",
            "questrade_api_server": "https://fresh.example/",
        }
        async with self._sessionmaker() as session:
            await upsert_api_credentials(
                session,
                1,
                "questrade",
                previous,
                ensure_institution=True,
            )
            connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "questrade",
                    )
                )
            ).scalar_one()
            await begin_api_credential_replacement(
                session,
                1,
                "questrade",
                institution_id=connection_id,
                previous_credentials=previous,
                candidate_credentials=candidate,
            )
            await upsert_api_credentials(
                session,
                1,
                "questrade",
                candidate,
                institution_id=connection_id,
                replace=True,
            )
            await upsert_api_credentials(
                session,
                1,
                "questrade",
                rotated,
                institution_id=connection_id,
                replace=True,
            )
            await session.commit()

        with patch(
            "app.services.connection_auth_storage.database.async_session",
            self._sessionmaker,
        ):
            self.assertEqual(
                await recover_api_credential_replacements(),
                1,
            )

        async with self._sessionmaker() as session:
            self.assertEqual(
                await get_api_credentials(
                    session,
                    1,
                    "questrade",
                    institution_id=connection_id,
                ),
                rotated,
            )
            remaining_backup = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == connection_id,
                        ConnectionAuthArtifact.artifact_kind == API_CREDENTIALS_ARTIFACT_KIND,
                        ConnectionAuthArtifact.slot == QUARANTINE_SLOT,
                    )
                )
            ).scalar_one_or_none()
            self.assertIsNone(remaining_backup)

            next_candidate = {
                **rotated,
                "questrade_refresh_token": "next-submitted-refresh",
            }
            replacement_id = await begin_api_credential_replacement(
                session,
                1,
                "questrade",
                institution_id=connection_id,
                previous_credentials=rotated,
                candidate_credentials=next_candidate,
            )
            await upsert_api_credentials(
                session,
                1,
                "questrade",
                next_candidate,
                institution_id=connection_id,
                replace=True,
            )
            self.assertTrue(
                await settle_api_credential_replacement(
                    session,
                    1,
                    replacement_id,
                    commit=False,
                )
            )
            await session.commit()
            self.assertEqual(
                await get_api_credentials(
                    session,
                    1,
                    "questrade",
                    institution_id=connection_id,
                ),
                rotated,
            )

    async def test_api_credentials_reject_a_connection_from_another_provider(self) -> None:
        async with self._sessionmaker() as session:
            rbc_connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "rbc",
                    )
                )
            ).scalar_one()

            with self.assertRaisesRegex(ValueError, "does not belong to this provider"):
                await get_api_credentials(
                    session,
                    1,
                    "wise",
                    institution_id=rbc_connection_id,
                )

    async def test_add_flow_rejects_an_already_connected_provider(self) -> None:
        async with self._sessionmaker() as session:
            session.add(Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=True,
            ))
            await session.flush()
            connection_id = (
                await session.execute(
                    select(Institution.id).where(
                        Institution.user_id == 1,
                        Institution.provider == "wise",
                    )
                )
            ).scalar_one()

            with self.assertRaisesRegex(ProviderAlreadyConnectedError, "already connected"):
                await ensure_connection(
                    session,
                    1,
                    "wise",
                    pending_add=True,
                )
            with self.assertRaisesRegex(ProviderAlreadyConnectedError, "already connected"):
                await ensure_connection(
                    session,
                    1,
                    "wise",
                    pending_add=True,
                    institution_id=connection_id,
                )

    async def test_available_provider_list_excludes_connected_provider(self) -> None:
        async with self._sessionmaker() as session:
            available = await institutions_route.get_available_institutions(
                db=session,
                current_user=SimpleNamespace(id=1),
            )

        available_providers = {institution["provider"] for institution in available}
        self.assertNotIn("rbc", available_providers)
        self.assertIn("wise", available_providers)

    async def test_pending_api_add_credentials_create_hidden_disabled_institution(self) -> None:
        async with self._sessionmaker() as session:
            stored = await upsert_api_credentials(
                session,
                1,
                "wise",
                {"wise_api_token": "wise-secret"},
                ensure_institution=True,
                pending_add=True,
            )
            await session.commit()
            institution = (
                await session.execute(
                    select(Institution).where(Institution.user_id == 1, Institution.provider == "wise")
                )
            ).scalar_one()

        self.assertTrue(stored)
        self.assertFalse(institution.enabled)
        self.assertTrue(institution.hidden)

    async def test_incomplete_add_cleanup_retains_confirmed_institution_with_accounts(self) -> None:
        async with self._sessionmaker() as session:
            confirmed = Institution(
                user_id=1,
                name="Wise Confirmed",
                type="api",
                provider="wise",
                enabled=True,
                hidden=False,
            )
            session.add(confirmed)
            await session.flush()
            session.add(Account(
                user_id=1,
                institution_id=confirmed.id,
                name="Confirmed Wise",
                account_type="cash",
                currency="CAD",
                is_liability=False,
            ))
            await session.commit()

            response = await institutions_route.remove_provider_add_state(
                "wise",
                db=session,
                current_user=SimpleNamespace(id=1),
            )

        async with self._sessionmaker() as session:
            remaining = (
                await session.execute(
                    select(Institution)
                    .where(Institution.user_id == 1, Institution.provider == "wise")
                    .order_by(Institution.id)
                )
            ).scalars().all()

        self.assertEqual(response["removed_institutions"], 0)
        self.assertEqual(response["skipped_institutions"], 1)
        self.assertEqual([institution.name for institution in remaining], ["Wise Confirmed"])

    async def test_incomplete_add_cleanup_clears_provider_state_when_no_institution_retained(self) -> None:
        async with self._sessionmaker() as session:
            pending = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=False,
                hidden=True,
            )
            session.add(pending)
            await session.flush()
            session.add(
                TransactionImportJob(
                    user_id=1,
                    institution_id=pending.id,
                    provider="wise",
                    job_id="wise-orphan-job",
                    status="queued",
                    reason="test",
                )
            )
            await session.commit()

            with patch("app.services.institution_cleanup.purge_provider_diagnostic_artifacts") as purge_diagnostics:
                response = await institutions_route.remove_provider_add_state(
                    "wise",
                    db=session,
                    current_user=SimpleNamespace(id=1),
                )

        async with self._sessionmaker() as session:
            remaining_institutions = (
                await session.execute(
                    select(Institution).where(Institution.user_id == 1, Institution.provider == "wise")
                )
            ).scalars().all()
            remaining_jobs = (
                await session.execute(
                    select(TransactionImportJob).where(
                        TransactionImportJob.user_id == 1,
                        TransactionImportJob.provider == "wise",
                    )
                )
            ).scalars().all()

        self.assertEqual(response["removed_institutions"], 1)
        self.assertEqual(response["skipped_institutions"], 0)
        self.assertEqual(remaining_institutions, [])
        self.assertEqual(remaining_jobs, [])
        purge_diagnostics.assert_not_called()

    async def test_sync_route_incomplete_add_cleanup_preserves_diagnostics(self) -> None:
        async with self._sessionmaker() as session:
            pending = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=False,
                hidden=True,
            )
            session.add(pending)
            await session.flush()
            pending_id = int(pending.id)
            await session.commit()

        with (
            patch.object(sync_route, "async_session", self._sessionmaker),
            patch("app.services.institution_cleanup.purge_provider_diagnostic_artifacts") as purge_diagnostics,
        ):
            async with self._sessionmaker() as session:
                await sync_route._cleanup_incomplete_institution_add(
                    session,
                    1,
                    "wise",
                    pending_id,
                )

        async with self._sessionmaker() as session:
            remaining_institutions = (
                await session.execute(
                    select(Institution).where(Institution.user_id == 1, Institution.provider == "wise")
                )
            ).scalars().all()

        self.assertEqual(remaining_institutions, [])
        purge_diagnostics.assert_not_called()

    async def test_institution_delete_purges_connection_data_and_provider_state(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=True,
            )
            cash_institution = Institution(
                user_id=1,
                name="Cash",
                type="manual",
                provider="manual",
                enabled=True,
            )
            session.add_all((institution, cash_institution))
            await session.flush()
            account = Account(
                user_id=1,
                institution_id=institution.id,
                external_id="wise-cash",
                name="Wise Cash",
                account_type="cash",
                currency="CAD",
                is_liability=False,
            )
            cash_account = Account(
                user_id=1,
                institution_id=cash_institution.id,
                external_id="cash:CAD",
                name="Cash CAD",
                account_type="cash",
                currency="CAD",
                is_liability=False,
            )
            session.add_all((account, cash_account))
            await session.flush()
            recurring = RecurringSeries(
                user_id=1,
                merchant_key="wise subscription",
                direction="outflow",
                display_name="Wise subscription",
                cadence="monthly",
                avg_amount=-10,
                occurrence_count=3,
                confidence=0.9,
            )
            session.add(recurring)
            await session.flush()
            source = Transaction(
                user_id=1,
                account_id=account.id,
                date=date(2026, 8, 3),
                type="withdrawal",
                description="Cash withdrawal",
                amount=-10,
                currency="CAD",
                external_id="wise-source",
                recurring_series_id=recurring.id,
            )
            session.add(source)
            await session.flush()
            category = Category(
                user_id=1,
                name="Wise-specific rule target",
                classification="expense",
            )
            session.add(category)
            await session.flush()
            session.add_all((
                Transaction(
                    user_id=1,
                    account_id=cash_account.id,
                    date=date(2026, 8, 3),
                    type="transfer",
                    description="Cash",
                    amount=10,
                    currency="CAD",
                    external_id=f"cash_mirror:{source.id}",
                ),
                BalanceHistory(
                    user_id=1,
                    account_id=account.id,
                    balance=100,
                    date=date(2026, 8, 3),
                ),
                Holding(
                    user_id=1,
                    account_id=account.id,
                    symbol="CAD",
                    quantity=100,
                    market_value=100,
                    currency="CAD",
                ),
                TransactionImportAccountState(
                    user_id=1,
                    institution_id=institution.id,
                    account_id=account.id,
                    provider="wise",
                    account_external_id="wise-cash",
                    account_name="Wise Cash",
                    is_liability=False,
                    backfill_status="complete",
                ),
                TransactionImportWindow(
                    user_id=1,
                    institution_id=institution.id,
                    account_id=account.id,
                    provider="wise",
                    account_external_id="wise-cash",
                    mode="backfill",
                    window_start_date=date(2026, 8, 1),
                    window_end_date=date(2026, 8, 3),
                    status="complete",
                ),
                TransactionImportJob(
                    user_id=1,
                    institution_id=institution.id,
                    provider="wise",
                    job_id="wise-delete-test",
                    status="queued",
                    reason="test",
                ),
                ConnectionAuthArtifact(
                    user_id=1,
                    institution_id=institution.id,
                    provider="wise",
                    artifact_kind=API_CREDENTIALS_ARTIFACT_KIND,
                    slot=ACTIVE_SLOT,
                    payload_json=encrypt_json_payload(
                        {"wise_api_token": "secret"},
                        aad_context={
                            "storage": "connection_auth_artifacts",
                            "user_id": 1,
                            "institution_id": institution.id,
                            "provider": "wise",
                            "artifact_kind": API_CREDENTIALS_ARTIFACT_KIND,
                            "slot": ACTIVE_SLOT,
                        },
                    ),
                    updated_at=datetime.now(timezone.utc),
                ),
                VisibleAuthAttemptArtifact(
                    user_id=1,
                    provider="wise",
                    attempt_id="delete-test",
                    artifact_kind="session",
                    payload_json=encrypt_json_payload(
                        {"session": "secret"},
                        aad_context={
                            "storage": "visible_auth_attempt_artifacts",
                            "user_id": 1,
                            "provider": "wise",
                            "attempt_id": "delete-test",
                            "artifact_kind": "session",
                        },
                    ),
                    updated_at=datetime.now(timezone.utc),
                ),
                Setting(
                    user_id=1,
                    key=f"invalid_scraper_credentials:wise:{institution.id}",
                    value="fingerprint",
                ),
                CategoryRule(
                    user_id=1,
                    category_id=category.id,
                    description_pattern="WISE TRACE",
                    provider="wise",
                ),
                SyncBatchJob(
                    user_id=1,
                    batch_id="wise-delete-batch",
                    mode="manual",
                    status="done",
                    connection_signature=json.dumps([["wise", institution.id, "wise"]]),
                    state_json=json.dumps({
                        "results": {
                            f"connection:{institution.id}": {
                                "provider": "wise",
                                "route_provider": "wise",
                                "institution_id": institution.id,
                                "status": "success",
                            },
                        },
                    }),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                ),
                SyncBatchJob(
                    user_id=1,
                    batch_id="rbc-retained-batch",
                    mode="manual",
                    status="done",
                    connection_signature=json.dumps([["rbc", 999, "rbc"]]),
                    state_json=json.dumps({
                        "results": {
                            "connection:999": {
                                "provider": "rbc",
                                "route_provider": "rbc",
                                "institution_id": 999,
                                "status": "success",
                            },
                        },
                    }),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                ),
            ))
            await session.commit()
            institution_id = int(institution.id)
            account_id = int(account.id)
            recurring_id = int(recurring.id)

        provider_runtime_dir = Path(runtime_state.get_provider_runtime_dir(1, "wise"))
        provider_desktop_dir = Path(runtime_state.get_provider_desktop_auth_dir(1, "wise"))
        provider_runtime_dir.mkdir(parents=True)
        provider_desktop_dir.mkdir(parents=True)

        with patch("app.services.institution_cleanup.purge_provider_diagnostic_artifacts") as purge_diagnostics:
            async with self._sessionmaker() as session, session.begin():
                institution = await session.get(Institution, institution_id)
                await delete_institution_and_accounts(session, 1, institution)

        async with self._sessionmaker() as session:
            self.assertIsNone(await session.get(Institution, institution_id))
            self.assertIsNone(await session.get(Account, account_id))
            self.assertIsNone(await session.get(RecurringSeries, recurring_id))
            self.assertEqual(
                (
                    await session.execute(
                        select(CategoryRule).where(
                            CategoryRule.user_id == 1,
                            CategoryRule.provider == "wise",
                        )
                    )
                ).scalars().all(),
                [],
            )
            self.assertIsNotNone(await session.get(Category, category.id))
            remaining_batches = (
                await session.execute(
                    select(SyncBatchJob).where(SyncBatchJob.user_id == 1)
                )
            ).scalars().all()
            self.assertEqual(
                [batch.batch_id for batch in remaining_batches],
                ["rbc-retained-batch"],
            )
            for model in (
                BalanceHistory,
                Holding,
                TransactionImportAccountState,
                TransactionImportWindow,
                TransactionImportJob,
                ConnectionAuthArtifact,
                VisibleAuthAttemptArtifact,
            ):
                remaining = (
                    await session.execute(select(model).where(model.user_id == 1))
                ).scalars().all()
                if model is ConnectionAuthArtifact:
                    remaining = [row for row in remaining if row.provider == "wise"]
                elif model is VisibleAuthAttemptArtifact:
                    remaining = [row for row in remaining if row.provider == "wise"]
                elif hasattr(model, "provider"):
                    remaining = [row for row in remaining if row.provider == "wise"]
                else:
                    remaining = [row for row in remaining if getattr(row, "account_id", None) == account_id]
                self.assertEqual(remaining, [], model.__name__)
            self.assertEqual(
                (
                    await session.execute(
                        select(Transaction).where(
                            Transaction.user_id == 1,
                            Transaction.external_id.like("cash_mirror:%"),
                        )
                    )
                ).scalars().all(),
                [],
            )
            self.assertEqual(
                (
                    await session.execute(
                        select(Setting).where(
                            Setting.user_id == 1,
                            Setting.key.like("invalid_scraper_credentials:wise:%"),
                        )
                    )
                ).scalars().all(),
                [],
            )

        self.assertFalse(provider_runtime_dir.exists())
        self.assertFalse(provider_desktop_dir.exists())
        purge_diagnostics.assert_called_once_with(user_id=1, provider="wise")

    async def test_moomoo_institution_delete_removes_oauth_session(self) -> None:
        async with self._sessionmaker() as session:
            institution = Institution(
                user_id=1,
                name="Moomoo",
                type="api",
                provider="moomoo",
                enabled=True,
            )
            session.add(institution)
            await session.flush()
            institution_id = int(institution.id)
            session.add(
                ConnectionAuthArtifact(
                    user_id=1,
                    institution_id=institution_id,
                    provider="moomoo",
                    artifact_kind="session",
                    slot=ACTIVE_SLOT,
                    payload_json=encrypt_json_payload(
                        {
                            "schema": "breaktwenty.moomoo-cloud-oauth.v1",
                            "mode": "cloud_oauth",
                            "client_id": "public-client-id",
                            "refresh_token": "refresh-secret",
                            "scope": "trade:read accid:1234",
                        },
                        aad_context={
                            "storage": "connection_auth_artifacts",
                            "user_id": 1,
                            "institution_id": institution_id,
                            "provider": "moomoo",
                            "artifact_kind": "session",
                            "slot": ACTIVE_SLOT,
                        },
                    ),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

        with patch("app.services.institution_cleanup.purge_provider_diagnostic_artifacts") as purge_diagnostics:
            async with self._sessionmaker() as session, session.begin():
                institution = await session.get(Institution, institution_id)
                await delete_institution_and_accounts(session, 1, institution)

        async with self._sessionmaker() as session:
            self.assertIsNone(await session.get(Institution, institution_id))
            oauth_sessions = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.institution_id == institution_id,
                        ConnectionAuthArtifact.provider == "moomoo",
                    )
                )
            ).scalars().all()

        self.assertEqual(oauth_sessions, [])
        purge_diagnostics.assert_called_once_with(user_id=1, provider="moomoo")

    async def test_add_confirm_promotes_pending_with_accounts(self) -> None:
        async with self._sessionmaker() as session:
            pending = Institution(
                user_id=1,
                name="Wise",
                type="api",
                provider="wise",
                enabled=False,
                hidden=True,
            )
            session.add(pending)
            await session.flush()
            pending_id = int(pending.id)
            account = Account(
                user_id=1,
                institution_id=pending.id,
                name="Wise Cash",
                account_type="cash",
                currency="CAD",
                is_liability=False,
            )
            session.add(account)
            await session.flush()
            session.add(BalanceHistory(user_id=1, account_id=account.id, balance=123.45))
            await session.commit()

            async def connected_request_json() -> dict:
                return {
                    "institution_id": pending.id,
                    "source_sync_id": "wise-add-sync",
                    "attempt_id": "visible-attempt-1",
                }

            with patch.object(
                institutions_route,
                "enqueue_provider_transaction_import",
                new=AsyncMock(return_value={"job_id": "wise-add-job"}),
            ) as enqueue:
                connected_response = await institutions_route.confirm_provider_add(
                    "wise",
                    request=SimpleNamespace(json=connected_request_json),
                    db=session,
                    current_user=SimpleNamespace(id=1),
                )

        async with self._sessionmaker() as session:
            institutions = (
                await session.execute(
                    select(Institution)
                    .where(Institution.user_id == 1, Institution.provider == "wise")
                    .order_by(Institution.id)
                )
            ).scalars().all()

        self.assertEqual(connected_response["confirmed_institutions"], 1)
        self.assertEqual(connected_response["removed_institutions"], 0)
        self.assertEqual([institution.name for institution in institutions], ["Wise"])
        self.assertTrue(institutions[0].enabled)
        self.assertFalse(institutions[0].hidden)
        enqueue.assert_awaited_once_with(
            1,
            "wise",
            source_sync_id="wise-add-sync",
            attempt_id="visible-attempt-1",
            reason="add_connection",
            institution_id=pending_id,
        )

    def test_ibkr_add_confirm_uses_flex_transaction_import_provider(self) -> None:
        self.assertEqual(
            "ibkr_flex",
            institutions_route._provider_transaction_import_key("ibkr"),
        )

    async def test_empty_runtime_artifact_clear_keeps_api_credentials(self) -> None:
        async with self._sessionmaker() as session:
            stored = await upsert_api_credentials(
                session,
                1,
                "coinbase",
                {
                    "coinbase_api_key": "organizations/example/apiKeys/example",
                    "coinbase_api_secret": "example-secret",
                },
                ensure_institution=True,
            )
            self.assertTrue(stored)
            await runtime_state.clear_provider_runtime_artifacts_async(session, 1, "coinbase", ())
            await session.commit()

        async with self._sessionmaker() as session:
            credentials = await get_api_credentials(
                session,
                1,
                "coinbase",
                setting_keys=("coinbase_api_key", "coinbase_api_secret"),
            )

        self.assertEqual(credentials["coinbase_api_key"], "organizations/example/apiKeys/example")
        self.assertEqual(credentials["coinbase_api_secret"], "example-secret")

    def test_scraper_username_placeholder_detection(self) -> None:
        self.assertTrue(is_scraper_username_placeholder("$USERNAME"))
        self.assertTrue(is_scraper_username_placeholder("${USERNAME}"))
        self.assertTrue(is_scraper_username_placeholder("{{ username }}"))
        self.assertFalse(is_scraper_username_placeholder("alice@example.com"))
        self.assertFalse(is_scraper_username_placeholder("alice_user"))

    async def test_placeholder_scraper_username_is_not_returned(self) -> None:
        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Institution.id).where(Institution.user_id == 1, Institution.provider == "rbc")
                )
            ).scalar_one()
            session.add(
                ConnectionAuthArtifact(
                    user_id=1,
                    institution_id=institution_id,
                    provider="rbc",
                    artifact_kind=SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                    slot=ACTIVE_SLOT,
                    payload_json=encrypt_json_payload(
                        {"username": "$USERNAME", "password": "secret"},
                        aad_context={
                            "storage": "connection_auth_artifacts",
                            "user_id": 1,
                            "institution_id": institution_id,
                            "provider": "rbc",
                            "artifact_kind": SCRAPER_CREDENTIALS_ARTIFACT_KIND,
                            "slot": ACTIVE_SLOT,
                        },
                    ),
                    updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            credentials = await get_scraper_credentials(session, 1, "rbc")

        self.assertIsNone(credentials)

    async def test_placeholder_scraper_username_does_not_overwrite_existing_credentials(self) -> None:
        async with self._sessionmaker() as session:
            self.assertTrue(await upsert_scraper_credentials(session, 1, "rbc", "alice", "secret"))
            await session.commit()

        async with self._sessionmaker() as session:
            self.assertFalse(await upsert_scraper_credentials(session, 1, "rbc", "$USERNAME", "new-secret"))
            await session.commit()

        async with self._sessionmaker() as session:
            credentials = await get_scraper_credentials(session, 1, "rbc")

        self.assertIsNotNone(credentials)
        self.assertEqual(credentials.username, "alice")
        self.assertEqual(credentials.password, "secret")

    async def test_provider_artifact_delete_can_cleanup_orphans_after_institution_deleted(self) -> None:
        async with self._sessionmaker() as session:
            institution_id = (
                await session.execute(
                    select(Institution.id).where(Institution.user_id == 1, Institution.provider == "rbc")
                )
            ).scalar_one()
            session.add(
                ConnectionAuthArtifact(
                    user_id=1,
                    institution_id=institution_id,
                    provider="rbc",
                    artifact_kind="cookies",
                    slot=ACTIVE_SLOT,
                    payload_json=json.dumps({"cookies": [{"name": "sid", "value": "abc"}]}),
                    updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
                )
            )
            await session.commit()

        async with self._sessionmaker() as session:
            await session.execute(delete(Institution).where(Institution.id == institution_id))
            await session.commit()

        async with self._sessionmaker() as session:
            await delete_provider_connection_artifacts(
                session,
                1,
                "rbc",
                ("cookies",),
                slots=(ACTIVE_SLOT,),
            )
            artifacts = (
                await session.execute(
                    select(ConnectionAuthArtifact).where(
                        ConnectionAuthArtifact.user_id == 1,
                        ConnectionAuthArtifact.provider == "rbc",
                    )
                )
            ).scalars().all()

        self.assertEqual(artifacts, [])


if __name__ == "__main__":
    unittest.main()
