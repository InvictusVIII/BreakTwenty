from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database_encryption import DatabaseKeyError, connect_sqlcipher
from app.key_material import get_key_material
from tests.key_material_test_utils import reset_cached_key_material


class DatabaseEncryptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._root = Path(self._tmpdir.name)
        self._database_key = os.urandom(32)
        self._app_key = os.urandom(32)
        self._environment = patch.dict(
            os.environ,
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                    self._database_key
                ).decode("ascii"),
                "BREAKTWENTY_APP_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                    self._app_key
                ).decode("ascii"),
            },
        )
        self._environment.start()
        os.environ.pop("BREAKTWENTY_KEY_BOOTSTRAP", None)
        reset_cached_key_material()

    def tearDown(self) -> None:
        reset_cached_key_material()
        self._environment.stop()
        self._tmpdir.cleanup()

    def _url(self, path: Path) -> str:
        return f"sqlite+sqlcipher_aiosqlite:///{path.as_posix()}"

    async def test_encrypted_async_database_reopens_and_rejects_plain_sqlite(self) -> None:
        database_path = self._root / "async.db"
        engine = create_async_engine(
            self._url(database_path),
            connect_args={"timeout": 30},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def configure(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout = 30000")
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.execute("PRAGMA synchronous = NORMAL")
            finally:
                cursor.close()

        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            )
            await connection.execute(
                text("INSERT INTO proof (value) VALUES (:value)"),
                {"value": "encrypted"},
            )
        await engine.dispose()

        reopened = create_async_engine(self._url(database_path))
        async with reopened.connect() as connection:
            self.assertEqual(
                (await connection.execute(text("SELECT value FROM proof"))).scalar_one(),
                "encrypted",
            )
            self.assertEqual(
                (await connection.execute(text("PRAGMA integrity_check"))).scalar_one(),
                "ok",
            )
            self.assertEqual(
                list(
                    (
                        await connection.execute(
                            text("PRAGMA cipher_integrity_check")
                        )
                    ).scalars()
                ),
                [],
            )
        await reopened.dispose()

        with self.assertRaises(sqlite3.DatabaseError):
            with sqlite3.connect(database_path) as connection:
                connection.execute("SELECT count(*) FROM proof").fetchone()

    async def test_concurrent_reads_and_serialized_writes(self) -> None:
        database_path = self._root / "concurrency.db"
        engine = create_async_engine(self._url(database_path))
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        with patch.dict(os.environ, {"DATABASE_URL": self._url(database_path)}):
            from app.services.sqlite_write_gate import sqlite_write_gate

        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            )
        async def writer(index: int):
            async with sqlite_write_gate():
                async with sessions() as session:
                    async with session.begin():
                        await session.execute(
                            text("INSERT INTO proof (value) VALUES (:value)"),
                            {"value": str(index)},
                        )

        async def reader():
            async with sessions() as session:
                return (
                    await session.execute(text("SELECT count(*) FROM proof"))
                ).scalar_one()

        await asyncio.gather(
            *(writer(index) for index in range(16)),
            *(reader() for _ in range(16)),
        )
        async with engine.connect() as connection:
            self.assertEqual(
                (await connection.execute(text("SELECT count(*) FROM proof"))).scalar_one(),
                16,
            )
        await engine.dispose()

    def test_plaintext_database_is_rejected_without_modification(self) -> None:
        database_path = self._root / "plaintext.db"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO proof (value) VALUES ('plaintext')"
            )
        original = database_path.read_bytes()

        with self.assertRaises(DatabaseKeyError):
            connect_sqlcipher(database_path)
        self.assertEqual(database_path.read_bytes(), original)
        with sqlite3.connect(database_path) as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM proof").fetchone()[0],
                "plaintext",
            )

    def test_wrong_key_fails_without_key_material_in_error(self) -> None:
        database_path = self._root / "wrong-key.db"
        with connect_sqlcipher(database_path) as connection:
            connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY)")
        key_text = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        with patch.dict(
            os.environ,
            {"BREAKTWENTY_DATABASE_ENCRYPTION_KEY": key_text},
        ):
            reset_cached_key_material()
            with self.assertRaisesRegex(DatabaseKeyError, "protected key") as raised:
                connect_sqlcipher(database_path)
        self.assertNotIn(key_text, str(raised.exception))
        self.assertNotIn(self._database_key.hex(), str(raised.exception))

    def test_full_alembic_chain_runs_on_encrypted_database(self) -> None:
        database_path = self._root / "alembic.db"
        backend_root = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            "DATABASE_URL": self._url(database_path),
            "BREAKTWENTY_DB_PATH": str(database_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(self._root / "alembic-pycache"),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(backend_root / "alembic.ini"),
                "upgrade",
                "head",
            ],
            cwd=backend_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with connect_sqlcipher(database_path) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0]
        alembic_config = Config(str(backend_root / "alembic.ini"))
        alembic_config.set_main_option(
            "script_location",
            str(backend_root / "alembic"),
        )
        self.assertEqual(
            revision,
            ScriptDirectory.from_config(alembic_config).get_current_head(),
        )
        drift_check = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(backend_root / "alembic.ini"),
                "check",
            ],
            cwd=backend_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(drift_check.returncode, 0, drift_check.stderr)
        combined_output = (
            f"{result.stdout}\n{result.stderr}\n"
            f"{drift_check.stdout}\n{drift_check.stderr}"
        )
        self.assertNotIn(self._database_key.hex(), combined_output)
        self.assertNotIn(
            base64.urlsafe_b64encode(self._database_key).decode("ascii"),
            combined_output,
        )
        self.assertNotIn(self._app_key.hex(), combined_output)
        self.assertNotIn(
            base64.urlsafe_b64encode(self._app_key).decode("ascii"),
            combined_output,
        )


class KeyBootstrapTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_cached_key_material()

    def test_environment_keys_must_both_be_exact_base64_256_bit_values(self) -> None:
        valid_key = base64.b64encode(os.urandom(32)).decode("ascii")
        with patch.dict(
            os.environ,
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": valid_key,
                "BREAKTWENTY_APP_ENCRYPTION_KEY": "not-base64",
            },
        ):
            reset_cached_key_material()
            with self.assertRaisesRegex(RuntimeError, "app encryption key is invalid"):
                get_key_material()

    def test_stdin_bootstrap_loads_exact_keys_and_launch_roles(self) -> None:
        database_key = os.urandom(32)
        app_key = os.urandom(32)
        desktop_token = os.urandom(32)
        renderer_token = os.urandom(32)
        payload = json.dumps(
            {
                "version": 2,
                "databaseKey": base64.urlsafe_b64encode(database_key).decode("ascii"),
                "appEncryptionKey": base64.urlsafe_b64encode(app_key).decode("ascii"),
                "desktopLaunchToken": base64.b64encode(desktop_token).decode("ascii"),
                "rendererLaunchToken": base64.b64encode(renderer_token).decode("ascii"),
            }
        )
        backend_root = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            "BREAKTWENTY_KEY_BOOTSTRAP": "stdin-v2",
            "PYTHONPATH": str(backend_root),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.key_material import get_key_material; "
                "k=get_key_material(); print(len(k.database_key), len(k.app_encryption_key), "
                "len(k.desktop_launch_token), len(k.renderer_launch_token), k.source)",
            ],
            cwd=backend_root,
            env=environment,
            input=f"{payload}\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "32 32 44 44 electron-stdin")
        self.assertNotIn(database_key.hex(), result.stdout + result.stderr)
        self.assertNotIn(
            base64.urlsafe_b64encode(database_key).decode("ascii"),
            result.stdout + result.stderr,
        )
        self.assertNotIn(app_key.hex(), result.stdout + result.stderr)
        self.assertNotIn(
            base64.urlsafe_b64encode(app_key).decode("ascii"),
            result.stdout + result.stderr,
        )

    def test_stdin_bootstrap_rejects_identical_launch_roles(self) -> None:
        launch_token = base64.b64encode(os.urandom(32)).decode("ascii")
        payload = json.dumps({
            "version": 2,
            "databaseKey": base64.b64encode(os.urandom(32)).decode("ascii"),
            "appEncryptionKey": base64.b64encode(os.urandom(32)).decode("ascii"),
            "desktopLaunchToken": launch_token,
            "rendererLaunchToken": launch_token,
        })
        stdin = type("Stdin", (), {"buffer": io.BytesIO(f"{payload}\n".encode("ascii"))})()
        with (
            patch.dict(os.environ, {"BREAKTWENTY_KEY_BOOTSTRAP": "stdin-v2"}),
            patch.object(sys, "stdin", stdin),
        ):
            reset_cached_key_material()
            with self.assertRaisesRegex(RuntimeError, "roles are invalid"):
                get_key_material()


if __name__ == "__main__":
    unittest.main()
