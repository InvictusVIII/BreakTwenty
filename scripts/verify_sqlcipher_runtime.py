#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

import sqlcipher3

EXPECTED_BINDING_VERSION = "0.6.2"
EXPECTED_SQLCIPHER_VERSION = "4.12.0 community"
EXPECTED_CIPHER_COMPATIBILITY = 4


@contextmanager
def key_connection(path: Path, key: bytes):
    connection = sqlcipher3.connect(path)
    try:
        connection.execute("PRAGMA cipher_log_level = NONE")
        connection.execute(f'PRAGMA key = "x\'{key.hex()}\'"')
        connection.execute(
            f"PRAGMA cipher_compatibility = {EXPECTED_CIPHER_COMPATIBILITY}"
        )
        connection.execute("PRAGMA cipher_page_size = 4096")
        connection.execute("PRAGMA cipher_use_hmac = ON")
        connection.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
        connection.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
        connection.execute("PRAGMA cipher_memory_security = ON")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        with connection:
            yield connection
    finally:
        connection.close()


def main() -> int:
    binding_version = importlib.metadata.version("sqlcipher3")
    if binding_version != EXPECTED_BINDING_VERSION:
        raise SystemExit(
            f"Unexpected sqlcipher3 version: {binding_version}"
        )
    with tempfile.TemporaryDirectory(prefix="breaktwenty_sqlcipher_runtime_") as tmp_dir:
        database_path = Path(tmp_dir) / "runtime-proof.db"
        key = os.urandom(32)
        with key_connection(database_path, key) as connection:
            cipher_version = connection.execute(
                "PRAGMA cipher_version"
            ).fetchone()[0]
            connection.execute(
                "CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO proof (value) VALUES ('encrypted')"
            )
        if cipher_version != EXPECTED_SQLCIPHER_VERSION:
            raise SystemExit(
                f"Unexpected SQLCipher version: {cipher_version}"
            )
        with key_connection(database_path, key) as connection:
            if connection.execute(
                "SELECT value FROM proof"
            ).fetchone()[0] != "encrypted":
                raise SystemExit("SQLCipher reopen verification failed")
            if connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0].lower() != "ok":
                raise SystemExit("SQLCipher SQLite integrity check failed")
            if connection.execute("PRAGMA cipher_integrity_check").fetchall():
                raise SystemExit("SQLCipher page integrity check failed")
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute("SELECT count(*) FROM proof").fetchone()
        except sqlite3.DatabaseError:
            pass
        else:
            raise SystemExit("Ordinary SQLite unexpectedly opened an encrypted database")
        database_path.unlink()
    print(
        f"SQLCipher runtime verified: sqlcipher3 {binding_version}, "
        f"SQLCipher {EXPECTED_SQLCIPHER_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
