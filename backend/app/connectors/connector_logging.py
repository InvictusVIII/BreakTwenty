from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.services.support_logging import support_logging_enabled
from app.services.sync_tracking import get_provider_sync_id

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
SHARED_CONNECTOR_DEBUG_ENV = "BREAKTWENTY_CONNECTOR_DEBUG"
CONNECTOR_LOG_REDACTED = "<redacted>"
CONNECTOR_LOG_VALUE_LIMIT = 1000
CONNECTOR_PUBLIC_MESSAGE_LIMIT = 500
# Keep redaction work bounded when exception or provider text is uncontrolled.
CONNECTOR_LOG_INPUT_LIMIT = 16_384
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_PRIVATE_KEY_MARKER_PATTERN = re.compile(
    r"-----(BEGIN|END) [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_AUTH_SCHEME_PATTERN = re.compile(r"\b(Bearer|Basic)\s+[^\s,;]+", re.IGNORECASE)
_JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_NAMED_SECRET_PATTERN = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie|password|passwd|passcode|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|query[_-]?id|secret)\b"
    r"((?:\"|')?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_PARTS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "key",
        "passcode",
        "passwd",
        "password",
        "secret",
        "token",
    }
)
_SENSITIVE_FIELD_NAMES = frozenset({"query_id", "set_cookie", "proxy_authorization"})


def _redact_url(url: str) -> str:
    trailing = ""
    candidate = url
    while candidate and candidate[-1] in ".,;)]}":
        trailing = candidate[-1] + trailing
        candidate = candidate[:-1]
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return CONNECTOR_LOG_REDACTED + trailing
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    query = ""
    if parsed.query:
        query = "&".join(
            f"{part.split('=', 1)[0]}={CONNECTOR_LOG_REDACTED}"
            for part in parsed.query.split("&")
            if part
        )
    fragment = CONNECTOR_LOG_REDACTED if parsed.fragment else ""
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment)) + trailing


def _redact_private_key_blocks(text: str) -> str:
    """Redact complete or unterminated PEM private-key blocks in one marker pass."""
    parts: list[str] = []
    cursor = 0
    block_start: int | None = None
    for marker in _PRIVATE_KEY_MARKER_PATTERN.finditer(text):
        marker_kind = marker.group(1).upper()
        if block_start is None:
            if marker_kind == "BEGIN":
                block_start = marker.start()
            continue
        if marker_kind != "END":
            continue
        parts.extend((text[cursor:block_start], CONNECTOR_LOG_REDACTED))
        cursor = marker.end()
        block_start = None
    if block_start is not None:
        parts.extend((text[cursor:block_start], CONNECTOR_LOG_REDACTED))
        cursor = len(text)
    if not parts:
        return text
    parts.append(text[cursor:])
    return "".join(parts)


def sanitize_connector_log_text(value: Any, *, limit: int = CONNECTOR_LOG_VALUE_LIMIT) -> str:
    raw_text = str(value or "")
    input_truncated = len(raw_text) > CONNECTOR_LOG_INPUT_LIMIT
    text = raw_text[:CONNECTOR_LOG_INPUT_LIMIT]
    text = _redact_private_key_blocks(text)
    text = _URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), text)
    text = _AUTH_SCHEME_PATTERN.sub(lambda match: f"{match.group(1)} {CONNECTOR_LOG_REDACTED}", text)
    text = _NAMED_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{CONNECTOR_LOG_REDACTED}",
        text,
    )
    text = _JWT_PATTERN.sub(CONNECTOR_LOG_REDACTED, text)
    text = _EMAIL_PATTERN.sub(CONNECTOR_LOG_REDACTED, text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit and not input_truncated:
        return text
    return f"{text[:limit].rstrip()}..."


def safe_connector_public_message(
    value: Any,
    *,
    fallback: str = "Sync failed",
    limit: int = CONNECTOR_PUBLIC_MESSAGE_LIMIT,
) -> str:
    """Return bounded, redacted exception text safe for a client response."""
    sanitized = sanitize_connector_log_text(value, limit=limit)
    if sanitized:
        return sanitized
    return sanitize_connector_log_text(fallback, limit=limit) or "Sync failed"


def _field_is_sensitive(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    return bool(set(normalized.split("_")) & _SENSITIVE_FIELD_PARTS)


def _sanitize_connector_log_value(key: Any, value: Any) -> Any:
    if _field_is_sensitive(key):
        return CONNECTOR_LOG_REDACTED
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {
            sanitize_connector_log_text(nested_key, limit=120): _sanitize_connector_log_value(nested_key, nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_connector_log_value("", item) for item in value]
    return sanitize_connector_log_text(value)


def get_connector_logger(provider: str) -> logging.Logger:
    return logging.getLogger(f"breaktwenty.connectors.{provider}")


def connector_debug_enabled(
    *,
    provider: str,
    user_id: int | None = None,
    logger: logging.Logger | None = None,
    env_names: tuple[str, ...] = (),
) -> bool:
    if support_logging_enabled(provider, user_id=user_id):
        return True

    seen: set[str] = set()
    for env_name in (*env_names, SHARED_CONNECTOR_DEBUG_ENV):
        name = str(env_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        raw_value = str(os.getenv(name) or "").strip().lower()
        if raw_value in TRUTHY_ENV_VALUES:
            return True
    return False


def log_connector_event(
    logger: logging.Logger,
    *,
    provider: str,
    stage: str,
    level: str = "info",
    debug: bool = False,
    env_names: tuple[str, ...] = (),
    **fields: Any,
) -> bool:
    raw_user_id = fields.get("user_id")
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None

    resolved_sync_id = sanitize_connector_log_text(fields.get("sync_id"), limit=160)
    if not resolved_sync_id and user_id is not None:
        resolved_sync_id = sanitize_connector_log_text(get_provider_sync_id(user_id, provider), limit=160)

    logger_method = getattr(logger, level, logger.info)
    rendered_fields = []
    if resolved_sync_id:
        rendered_fields.append(f"sync_id={resolved_sync_id}")
    for key, value in fields.items():
        if key == "sync_id":
            continue
        if value is None or value == "":
            continue
        safe_key = sanitize_connector_log_text(key, limit=120)
        safe_value = _sanitize_connector_log_value(key, value)
        rendered_fields.append(f"{safe_key}={safe_value}")
    extra = {
        "breaktwenty_provider": provider,
        "breaktwenty_user_id": user_id,
        "breaktwenty_sync_id": resolved_sync_id or None,
        "breaktwenty_attempt_id": sanitize_connector_log_text(fields.get("attempt_id"), limit=160) or None,
        "breaktwenty_debug_event": bool(debug),
        "breaktwenty_debug_env_names": tuple(env_names) if env_names else (),
    }
    if rendered_fields:
        logger_method(
            "[%s] %s %s",
            sanitize_connector_log_text(provider, limit=80),
            sanitize_connector_log_text(stage, limit=160),
            " ".join(rendered_fields),
            extra=extra,
        )
        return True
    logger_method(
        "[%s] %s",
        sanitize_connector_log_text(provider, limit=80),
        sanitize_connector_log_text(stage, limit=160),
        extra=extra,
    )
    return True
