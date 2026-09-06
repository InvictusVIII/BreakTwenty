"""Shared cache for provider-resolved asset/security display names.

Names are reference data, not user data, so the cache is process-shared and
keyed by `(provider, identifier)`. Identifier semantics are provider-specific:
- coinbase: currency code, uppercased (e.g. "ETH")
- questrade: stringified `symbolId` integer
- ibkr: stringified `conid` integer

Connectors check the cache before calling provider APIs, then upsert any new
lookups they did make. Stale rows (older than `SYMBOL_NAME_TTL_DAYS`) are
ignored so connectors will re-resolve and refresh.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping

from sqlalchemy import select

from app.database import async_session
from app.models import SymbolName
from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.services.time_utils import utc_now as _utcnow

SYMBOL_NAME_TTL_DAYS = 90


def _ttl_cutoff() -> datetime:
    return _utcnow() - timedelta(days=SYMBOL_NAME_TTL_DAYS)


def _normalize_identifier(identifier) -> str:
    return str(identifier or "").strip()


async def get_cached_symbol_names(
    provider: str,
    identifiers: Iterable,
) -> dict[str, str]:
    """Return cached, non-stale names keyed by normalized identifier.

    Missing or stale identifiers are absent from the returned dict.
    """
    provider_key = _normalize_provider(provider)
    if not provider_key:
        return {}
    ids = sorted({_normalize_identifier(i) for i in identifiers if _normalize_identifier(i)})
    if not ids:
        return {}
    cutoff = _ttl_cutoff()
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    SymbolName.identifier,
                    SymbolName.name,
                    SymbolName.updated_at,
                ).where(
                    SymbolName.provider == provider_key,
                    SymbolName.identifier.in_(ids),
                )
            )
        ).all()
    result: dict[str, str] = {}
    for identifier, name, updated_at in rows:
        if not identifier or not name or updated_at is None:
            continue
        ts = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        result[identifier] = name
    return result


async def upsert_symbol_names(provider: str, names: Mapping) -> None:
    """Insert or update name cache entries. Empty names/identifiers are skipped."""
    provider_key = _normalize_provider(provider)
    if not provider_key or not names:
        return
    payloads: list[tuple[str, str]] = []
    for identifier, name in names.items():
        ident = _normalize_identifier(identifier)
        nm = str(name or "").strip()
        if ident and nm:
            payloads.append((ident, nm))
    if not payloads:
        return
    now = _utcnow()
    async with async_session() as db:
        for ident, nm in payloads:
            await db.merge(
                SymbolName(
                    provider=provider_key,
                    identifier=ident,
                    name=nm,
                    updated_at=now,
                )
            )
        await db.commit()
