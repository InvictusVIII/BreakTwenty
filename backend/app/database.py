import itertools
import logging
import os
import time
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.brand import APP_BRAND_NAME
from app.database_encryption import SQLCIPHER_DRIVER
from app.runtime_paths import APP_ROOT, DATABASE_PATH, sqlite_database_url

DATABASE_URL = os.getenv("DATABASE_URL", sqlite_database_url(DATABASE_PATH))
SQLITE_BUSY_TIMEOUT_MS = 30_000
SQLITE_JOURNAL_MODE = "WAL"
SQLITE_SYNCHRONOUS = "NORMAL"
_POOL_STATE_LOG_INTERVAL_SECONDS = 1.0
SQLITE_RETAINED_POOL_CONNECTIONS = 16

logger = logging.getLogger("breaktwenty.database")
_connection_ids = itertools.count(1)
_last_pool_state_log: dict[str, float] = {}

connect_args = {}
database_url = make_url(DATABASE_URL)
if database_url.get_backend_name() == "sqlite":
    if database_url.get_driver_name() != SQLCIPHER_DRIVER:
        raise RuntimeError(f"{APP_BRAND_NAME} refuses to open a plaintext SQLite database driver")
    if database_url.username or database_url.password:
        raise RuntimeError(f"{APP_BRAND_NAME} database keys must not be placed in database URLs")
    if any(
        sensitive_name in str(name).lower()
        for name in database_url.query
        for sensitive_name in ("key", "password", "secret", "token")
    ):
        raise RuntimeError(f"{APP_BRAND_NAME} database keys must not be placed in database URLs")
    connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_MS / 1000
    sqlite_path = Path(str(database_url.database or ""))
    if str(sqlite_path) and str(sqlite_path) != ":memory:":
        if not sqlite_path.is_absolute():
            sqlite_path = APP_ROOT / sqlite_path
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)

engine_options: dict[str, object] = {
    "echo": False,
    "connect_args": connect_args,
}
if database_url.get_backend_name() == "sqlite":
    # SQLAlchemy 2.0.35 otherwise selects NullPool for an async SQLite file and
    # opens a new native SQLCipher handle for every AsyncSession. Retain enough
    # healthy handles for provider work plus interactive reads, while unlimited
    # overflow preserves the existing no-checkout-cap execution contract.
    engine_options.update(
        poolclass=AsyncAdaptedQueuePool,
        pool_size=SQLITE_RETAINED_POOL_CONNECTIONS,
        max_overflow=-1,
    )

engine = create_async_engine(DATABASE_URL, **engine_options)
async_session = async_sessionmaker(engine, expire_on_commit=False)


def _connection_observation_id(connection_record) -> int:
    connection_id = connection_record.info.get("breaktwenty_connection_id")
    if connection_id is None:
        connection_id = next(_connection_ids)
        connection_record.info["breaktwenty_connection_id"] = connection_id
    return connection_id


def _pool_status() -> str:
    try:
        return str(engine.sync_engine.pool.status())
    except Exception as exc:
        return f"unavailable:{type(exc).__name__}"


def _log_pool_state(event_name: str, connection_record, *, force: bool = False) -> None:
    now = time.perf_counter()
    last_logged = _last_pool_state_log.get(event_name, 0.0)
    if not force and now - last_logged < _POOL_STATE_LOG_INTERVAL_SECONDS:
        return
    _last_pool_state_log[event_name] = now
    logger.info(
        "sqlalchemy pool state event=%s connection_id=%s state=%s",
        event_name,
        _connection_observation_id(connection_record),
        _pool_status(),
    )


def configure_sqlite_connection(dbapi_connection) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute(f"PRAGMA journal_mode = {SQLITE_JOURNAL_MODE}")
        cursor.execute(f"PRAGMA synchronous = {SQLITE_SYNCHRONOUS}")
    finally:
        cursor.close()


if database_url.get_backend_name() == "sqlite":
    @event.listens_for(engine.sync_engine, "do_connect")
    def _observe_sqlcipher_connection_open_started(
        _dialect,
        connection_record,
        _connection_arguments,
        _connection_parameters,
    ):
        connection_id = _connection_observation_id(connection_record)
        connection_record.info["breaktwenty_connection_open_started"] = time.perf_counter()
        logger.info("sqlcipher connection open started connection_id=%s", connection_id)

    @event.listens_for(engine.sync_engine.pool, "connect")
    def _observe_sqlcipher_connection_open_completed(_dbapi_connection, connection_record):
        started = connection_record.info.pop("breaktwenty_connection_open_started", None)
        duration = time.perf_counter() - started if started is not None else -1.0
        logger.info(
            "sqlcipher connection open completed connection_id=%s duration=%.6fs",
            _connection_observation_id(connection_record),
            duration,
        )
        _log_pool_state("connect", connection_record, force=True)

    @event.listens_for(engine.sync_engine.pool, "checkout")
    def _observe_sqlalchemy_pool_checkout(
        _dbapi_connection,
        connection_record,
        _connection_proxy,
    ):
        _log_pool_state("checkout", connection_record)

    @event.listens_for(engine.sync_engine.pool, "checkin")
    def _observe_sqlalchemy_pool_checkin(_dbapi_connection, connection_record):
        _log_pool_state("checkin", connection_record)

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record):
        configure_sqlite_connection(dbapi_connection)


class Base(DeclarativeBase):
    pass


async def verify_database_connection():
    async with engine.connect() as connection:
        await connection.execute(text("SELECT count(*) FROM sqlite_master"))


async def close_database_connections() -> None:
    await engine.dispose()
