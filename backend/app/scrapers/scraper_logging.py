from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.services.support_logging import support_logging_enabled
from app.services.sync_tracking import get_provider_sync_id

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
SHARED_SCRAPER_DEBUG_ENV = "BREAKTWENTY_SCRAPER_DEBUG"
JWT_LOG_REDACTION_PATTERN = r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
EMAIL_LOG_REDACTION_PATTERN = r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
NUMBER_LOG_REDACTION_PATTERN = r"\b\d{5,}\b"
_SCRAPER_LOG_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "scraper_log_context",
    default=None,
)

LogRedaction = tuple[str, str, int]


def _collapse_log_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize_scraper_log_text(
    text: Any,
    *,
    limit: int = 240,
    redactions: tuple[LogRedaction, ...] = (),
    collapse_first: bool = False,
) -> str:
    sanitized = str(text or "")
    if collapse_first:
        sanitized = _collapse_log_whitespace(sanitized)
    for pattern, replacement, flags in redactions:
        sanitized = re.sub(pattern, replacement, sanitized, flags=flags)
    sanitized = re.sub(NUMBER_LOG_REDACTION_PATTERN, "<number>", sanitized)
    if not collapse_first:
        sanitized = _collapse_log_whitespace(sanitized)
    if len(sanitized) <= limit:
        return sanitized
    return f"{sanitized[:limit].rstrip()}..."


def sanitize_scraper_log_url(url: str | None, *, safe_query_keys: set[str] | frozenset[str] = frozenset()) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if not safe_query_keys or not parsed.query:
        return safe_url
    query_parts = []
    for pair in parsed.query.split("&"):
        key = pair.split("=", 1)[0]
        value = pair.split("=", 1)[1] if key in safe_query_keys and "=" in pair else "<redacted>"
        query_parts.append(f"{key}={value}")
    return f"{safe_url}?{'&'.join(query_parts)}"


def _coerce_user_id(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


@contextmanager
def scraper_log_context(
    *,
    provider: str | None = None,
    user_id: int | None = None,
    sync_id: str | None = None,
):
    current_context = dict(_SCRAPER_LOG_CONTEXT.get() or {})
    next_context = dict(current_context)

    normalized_provider = _normalize_provider(provider)
    if normalized_provider:
        next_context["provider"] = normalized_provider

    normalized_user_id = _coerce_user_id(user_id)
    if normalized_user_id is not None:
        next_context["user_id"] = normalized_user_id

    normalized_sync_id = str(sync_id or "").strip()
    if normalized_sync_id:
        next_context["sync_id"] = normalized_sync_id

    token = _SCRAPER_LOG_CONTEXT.set(next_context)
    try:
        yield
    finally:
        _SCRAPER_LOG_CONTEXT.reset(token)


def scraper_debug_enabled(
    *,
    logger: logging.Logger | None = None,
    env_names: tuple[str, ...] = (),
) -> bool:
    seen: set[str] = set()
    for env_name in (*env_names, SHARED_SCRAPER_DEBUG_ENV):
        name = str(env_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        raw_value = str(os.getenv(name) or "").strip().lower()
        if raw_value in TRUTHY_ENV_VALUES:
            return True
    return False


def log_scraper_event(
    logger: logging.Logger,
    *,
    provider: str,
    stage: str,
    level: str = "info",
    debug: bool = False,
    debug_enabled: bool = False,
    **fields: Any,
) -> bool:
    fields = dict(fields)
    context = _SCRAPER_LOG_CONTEXT.get() or {}
    context_provider = _normalize_provider(context.get("provider"))
    provider_key = _normalize_provider(provider)
    context_applies = bool(context) and (not context_provider or context_provider == provider_key)
    if context_applies:
        if fields.get("user_id") in (None, ""):
            context_user_id = _coerce_user_id(context.get("user_id"))
            if context_user_id is not None:
                fields["user_id"] = context_user_id
        if fields.get("sync_id") in (None, ""):
            context_sync_id = str(context.get("sync_id") or "").strip()
            if context_sync_id:
                fields["sync_id"] = context_sync_id

    raw_user_id = fields.get("user_id")
    user_id = _coerce_user_id(raw_user_id)

    support_debug_enabled = support_logging_enabled(provider, user_id=user_id)
    if debug and not (debug_enabled or support_debug_enabled):
        return False

    resolved_sync_id = str(fields.get("sync_id") or "").strip()
    if not resolved_sync_id and user_id is not None:
        resolved_sync_id = str(get_provider_sync_id(user_id, provider) or "").strip()

    logger_method = getattr(logger, level, logger.info)
    rendered_fields = []
    if resolved_sync_id:
        rendered_fields.append(f"sync_id={resolved_sync_id}")
    for key, value in fields.items():
        if key == "sync_id":
            continue
        if value is None or value == "":
            continue
        rendered_fields.append(f"{key}={value}")
    extra = {
        "breaktwenty_provider": provider,
        "breaktwenty_user_id": user_id,
        "breaktwenty_sync_id": resolved_sync_id or None,
        "breaktwenty_attempt_id": str(fields.get("attempt_id") or "").strip() or None,
        "breaktwenty_debug_event": bool(debug),
    }
    if rendered_fields:
        logger_method("[%s] %s %s", provider, stage, " ".join(rendered_fields), extra=extra)
        return True
    logger_method("[%s] %s", provider, stage, extra=extra)
    return True
