from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.runtime_paths import LOG_DIR

BASELINE_LOG_PATH = LOG_DIR / "breaktwenty.log"
DEBUG_LOG_PATH = LOG_DIR / "breaktwenty-debug.log"
BASELINE_LOG_MAX_BYTES = 5 * 1024 * 1024
BASELINE_LOG_BACKUP_COUNT = 9
DEBUG_LOG_MAX_BYTES = 2 * 1024 * 1024
DEBUG_LOG_BACKUP_COUNT = 2


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


class _ExcludeStructuredDebugFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not bool(getattr(record, "breaktwenty_debug_event", False))


class _StructuredDebugFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not bool(getattr(record, "breaktwenty_debug_event", False)):
            return False
        provider = getattr(record, "breaktwenty_provider", None)
        if not provider:
            return False
        try:
            from app.connectors.connector_logging import connector_debug_enabled
        except Exception:
            return False
        user_id = getattr(record, "breaktwenty_user_id", None)
        env_names = getattr(record, "breaktwenty_debug_env_names", ()) or ()
        try:
            return bool(
                connector_debug_enabled(
                    provider=str(provider),
                    user_id=user_id,
                    env_names=tuple(env_names),
                )
            )
        except Exception:
            return False


def _build_rotating_file_handler(
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    debug_only: bool,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(_StructuredDebugFilter() if debug_only else _ExcludeStructuredDebugFilter())
    return handler


def _build_stream_handler(
    *,
    level: int,
    formatter: logging.Formatter,
    debug_only: bool,
) -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(_StructuredDebugFilter() if debug_only else _ExcludeStructuredDebugFilter())
    return handler


def configure_breaktwenty_logging() -> logging.Logger:
    logger = logging.getLogger("breaktwenty")
    if getattr(logger, "_breaktwenty_logging_configured", False):
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = UTCFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    logger.addHandler(
        _build_rotating_file_handler(
            BASELINE_LOG_PATH,
            level=logging.INFO,
            max_bytes=BASELINE_LOG_MAX_BYTES,
            backup_count=BASELINE_LOG_BACKUP_COUNT,
            formatter=formatter,
            debug_only=False,
        )
    )
    logger.addHandler(
        _build_stream_handler(
            level=logging.INFO,
            formatter=formatter,
            debug_only=False,
        )
    )
    logger.addHandler(
        _build_rotating_file_handler(
            DEBUG_LOG_PATH,
            level=logging.DEBUG,
            max_bytes=DEBUG_LOG_MAX_BYTES,
            backup_count=DEBUG_LOG_BACKUP_COUNT,
            formatter=formatter,
            debug_only=True,
        )
    )
    logger.addHandler(
        _build_stream_handler(
            level=logging.DEBUG,
            formatter=formatter,
            debug_only=True,
        )
    )

    from app.services.log_buffer import ProviderLogBufferHandler

    buffer_handler = ProviderLogBufferHandler()
    buffer_handler.setLevel(logging.DEBUG)
    buffer_handler.setFormatter(formatter)
    logger.addHandler(buffer_handler)

    logger._breaktwenty_logging_configured = True
    return logger
