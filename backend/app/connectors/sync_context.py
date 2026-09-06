from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ConnectorSyncContext:
    user_id: int
    provider: str
    institution_id: int | None = None
    sync_id: str | None = None
    attempt_id: str | None = None
    add_flow: bool = False
    sync_scope: str = "full"
    transaction_job_id: str | None = None
    transaction_job_lease_token: str | None = None


_SYNC_CONTEXT: ContextVar[ConnectorSyncContext | None] = ContextVar(
    "connector_sync_context",
    default=None,
)


@contextmanager
def connector_sync_context(
    *,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
    sync_id: str | None = None,
    attempt_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    transaction_job_id: str | None = None,
    transaction_job_lease_token: str | None = None,
) -> Iterator[ConnectorSyncContext]:
    context = ConnectorSyncContext(
        user_id=user_id,
        provider=provider,
        institution_id=institution_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
        add_flow=add_flow,
        sync_scope=normalize_sync_scope(sync_scope),
        transaction_job_id=transaction_job_id,
        transaction_job_lease_token=transaction_job_lease_token,
    )
    token = _SYNC_CONTEXT.set(context)
    try:
        yield context
    finally:
        _SYNC_CONTEXT.reset(token)


def get_connector_sync_context() -> ConnectorSyncContext | None:
    return _SYNC_CONTEXT.get()


def normalize_sync_scope(sync_scope: str | None) -> str:
    normalized = str(sync_scope or "full").strip().lower()
    if normalized in {"accounts", "transactions"}:
        return normalized
    return "full"
