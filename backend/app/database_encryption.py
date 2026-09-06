from __future__ import annotations

from pathlib import Path
from threading import Lock

import aiosqlite
import sqlcipher3
from sqlalchemy.dialects import registry
from sqlalchemy.dialects.sqlite.aiosqlite import (
    AsyncAdapt_aiosqlite_dbapi,
    SQLiteDialect_aiosqlite,
)

from app.brand import APP_BRAND_NAME
from app.key_material import get_key_material

SQLCIPHER_BINDING_VERSION = "0.6.2"
SQLCIPHER_VERSION = "4.12.0 community"
SQLCIPHER_COMPATIBILITY = 4
SQLCIPHER_PAGE_SIZE = 4096
SQLCIPHER_USE_HMAC = True
SQLCIPHER_HMAC_ALGORITHM = "HMAC_SHA512"
SQLCIPHER_KDF_ALGORITHM = "PBKDF2_HMAC_SHA512"
SQLCIPHER_MEMORY_SECURITY = True
SQLCIPHER_DRIVER = "sqlcipher_aiosqlite"

_diagnostics_lock = Lock()
_runtime_cipher_version: str | None = None


class DatabaseKeyError(RuntimeError):
    pass


def _apply_key(connection: sqlcipher3.Connection, key: bytes) -> None:
    connection.execute("PRAGMA cipher_log_level = NONE")
    connection.execute(f'PRAGMA key = "x\'{key.hex()}\'"')
    connection.execute(f"PRAGMA cipher_compatibility = {SQLCIPHER_COMPATIBILITY}")
    connection.execute(f"PRAGMA cipher_page_size = {SQLCIPHER_PAGE_SIZE}")
    connection.execute(
        f"PRAGMA cipher_use_hmac = {'ON' if SQLCIPHER_USE_HMAC else 'OFF'}"
    )
    connection.execute(
        f"PRAGMA cipher_hmac_algorithm = {SQLCIPHER_HMAC_ALGORITHM}"
    )
    connection.execute(
        f"PRAGMA cipher_kdf_algorithm = {SQLCIPHER_KDF_ALGORITHM}"
    )
    connection.execute(
        f"PRAGMA cipher_memory_security = {'ON' if SQLCIPHER_MEMORY_SECURITY else 'OFF'}"
    )


def connect_sqlcipher(
    database: str | bytes | Path,
    **kwargs,
) -> sqlcipher3.Connection:
    location = database.decode("utf-8") if isinstance(database, bytes) else str(database)
    connection = sqlcipher3.connect(location, **kwargs)
    try:
        _apply_key(connection, get_key_material().database_key)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        cipher_version = connection.execute("PRAGMA cipher_version").fetchone()
        if cipher_version:
            with _diagnostics_lock:
                global _runtime_cipher_version
                _runtime_cipher_version = str(cipher_version[0])
    except Exception as exc:
        connection.close()
        raise DatabaseKeyError(
            f"{APP_BRAND_NAME} could not open the encrypted database with the protected key"
        ) from exc
    return connection


class _AsyncSqlcipherModule:
    DatabaseError = sqlcipher3.DatabaseError
    Error = sqlcipher3.Error
    IntegrityError = sqlcipher3.IntegrityError
    NotSupportedError = sqlcipher3.NotSupportedError
    OperationalError = sqlcipher3.OperationalError
    ProgrammingError = sqlcipher3.ProgrammingError
    sqlite_version = sqlcipher3.sqlite_version
    sqlite_version_info = sqlcipher3.sqlite_version_info

    @staticmethod
    def connect(
        database: str | bytes | Path,
        *,
        iter_chunk_size: int = 64,
        **kwargs,
    ) -> aiosqlite.Connection:
        def connector():
            return connect_sqlcipher(database, **kwargs)

        return aiosqlite.Connection(connector, iter_chunk_size)


class SQLiteDialectSqlcipherAiosqlite(SQLiteDialect_aiosqlite):
    driver = SQLCIPHER_DRIVER
    supports_statement_cache = True

    @classmethod
    def import_dbapi(cls):
        return AsyncAdapt_aiosqlite_dbapi(_AsyncSqlcipherModule, sqlcipher3)


def sqlcipher_diagnostics() -> dict[str, object]:
    with _diagnostics_lock:
        runtime_version = _runtime_cipher_version
    return {
        "enabled": True,
        "binding": "sqlcipher3",
        "bindingVersion": SQLCIPHER_BINDING_VERSION,
        "sqlcipherVersion": runtime_version or SQLCIPHER_VERSION,
        "cipherCompatibility": SQLCIPHER_COMPATIBILITY,
        "cipherPageSize": SQLCIPHER_PAGE_SIZE,
        "hmac": SQLCIPHER_USE_HMAC,
        "hmacAlgorithm": SQLCIPHER_HMAC_ALGORITHM,
        "kdfAlgorithm": SQLCIPHER_KDF_ALGORITHM,
        "memorySecurity": SQLCIPHER_MEMORY_SECURITY,
    }

registry.register(
    f"sqlite.{SQLCIPHER_DRIVER}",
    "app.database_encryption",
    "SQLiteDialectSqlcipherAiosqlite",
)
