from __future__ import annotations

import asyncio
import inspect
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx

from app.connectors.base import Connector
from app.connectors.connector_logging import (
    get_connector_logger,
    log_connector_event,
    safe_connector_public_message,
)
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.services.replay_diagnostics import ReplayDiagnosticsCapture, begin_replay_diagnostics
from app.services.sync_utils import (
    AUTH_REQUIRED_KEYWORDS,
    INVALID_CREDENTIAL_KEYWORDS,
    is_network_error,
)

if TYPE_CHECKING:
    from app.connectors.sync_modes import AccountSyncWindow


class ApiProviderError(Exception):
    pass


class ApiProviderAuthRequired(ApiProviderError):
    pass


class ApiProviderInvalidCredentials(ApiProviderAuthRequired):
    pass


class ApiProviderTemporaryError(ApiProviderError):
    pass


class ApiProviderConnectorBase(Connector):
    def __init__(self, provider: str, display_name: str | None = None) -> None:
        super().__init__(provider=provider)
        self.display_name = display_name or provider
        self.logger = get_connector_logger(provider)
        self._replay_diagnostics: ReplayDiagnosticsCapture | None = None

    async def sync(self, user_id: int) -> SyncResult:
        self.log_event("sync start", user_id=user_id, debug=True)
        from app.connectors.sync_context import get_connector_sync_context

        sync_context = get_connector_sync_context()
        diagnostics = begin_replay_diagnostics(
            self.provider,
            user_id,
            sync_id=sync_context.sync_id if sync_context else None,
            attempt_id=sync_context.attempt_id if sync_context else None,
        )
        self._replay_diagnostics = diagnostics
        try:
            result = await self._sync_impl(user_id)
        except Exception as exc:
            result = self.exception_result(user_id, exc)

        normalized = self.normalize_result(result, user_id=user_id)
        if diagnostics:
            path = diagnostics.finish(normalized.status.value)
            self.log_event(
                "phase2 replay diagnostics",
                debug=True,
                user_id=user_id,
                result=normalized.status.value,
                path=str(path),
                display_path=path.name,
                phase1_har_path=str(diagnostics.phase1_har_path or ""),
            )
        self._replay_diagnostics = None
        finish_is_problem = normalized.status in {
            SyncStatus.AUTH_REQUIRED,
            SyncStatus.NETWORK_ERROR,
            SyncStatus.ERROR,
        }
        self.log_event(
            "sync finish",
            level="warning" if finish_is_problem else "info",
            debug=normalized.status == SyncStatus.OK,
            user_id=user_id,
            status=normalized.status.value,
            accounts=len(normalized.accounts),
            holdings=len(normalized.holdings),
            transactions=len(normalized.transactions),
        )
        return normalized

    @abstractmethod
    async def _sync_impl(self, user_id: int) -> SyncResult:
        ...

    def log_event(
        self,
        stage: str,
        *,
        level: str = "info",
        debug: bool = False,
        **fields: Any,
    ) -> bool:
        return log_connector_event(
            self.logger,
            provider=self.provider,
            stage=stage,
            level=level,
            debug=debug,
            **fields,
        )

    def record_diagnostics_fetch(
        self,
        *,
        stage: str,
        payload: Any | None = None,
        records: Any | None = None,
        record_source: str = "",
        account_ref: Any | None = None,
        window: dict[str, Any] | None = None,
        transport: str = "api",
        url: str = "",
        method: str = "GET",
        status: int | None = 200,
        params: dict[str, Any] | None = None,
        request_json: Any | None = None,
    ) -> None:
        if not self._replay_diagnostics:
            return
        self._replay_diagnostics.record_http_fetch(
            stage=stage,
            method=method,
            url=url or f"{self.provider}://{stage}",
            status=status,
            params=params,
            request_json=request_json,
            payload=payload,
            records=records,
            record_source=record_source,
            account_ref=account_ref,
            window=window,
            transport=transport,
        )

    @property
    def transaction_import_provider(self) -> str:
        return self.provider

    def sync_scope(self) -> str:
        from app.connectors.sync_context import get_connector_sync_context

        context = get_connector_sync_context()
        return context.sync_scope if context else "full"

    def should_fetch_transactions(self) -> bool:
        return self.sync_scope() != "accounts"

    def should_persist_transaction_windows(self) -> bool:
        return self.sync_scope() == "transactions"

    async def _transaction_sync_window(
        self,
        user_id: int,
        account_external_id: str | None,
    ) -> AccountSyncWindow:
        from app.connectors.sync_modes import plan_account_sync_window
        from app.database import async_session

        async with async_session() as db:
            return await plan_account_sync_window(
                db,
                user_id,
                account_external_id or "",
                provider=self.provider,
            )

    async def plan_transaction_import(
        self,
        *,
        user_id: int,
        account_external_id: str,
        account_name: str,
        account_type: str | None = None,
        is_liability: bool = False,
        backfill_days: int | None = None,
        backfill_chunk_days: int | None = None,
        incremental_days: int | None = None,
        overlap_days: int | None = None,
        today: date | None = None,
    ):
        from app.database import async_session
        from app.services.sqlite_write_gate import sqlite_write_gate
        from app.services.transaction_import import plan_account_transaction_import

        kwargs: dict[str, Any] = {}
        if backfill_days is not None:
            kwargs["backfill_days"] = backfill_days
        if backfill_chunk_days is not None:
            kwargs["backfill_chunk_days"] = backfill_chunk_days
        if incremental_days is not None:
            kwargs["incremental_days"] = incremental_days
        if overlap_days is not None:
            kwargs["overlap_days"] = overlap_days
        if today is not None:
            kwargs["today"] = today

        async with async_session() as db, sqlite_write_gate(), db.begin():
            return await plan_account_transaction_import(
                db,
                user_id=user_id,
                provider=self.transaction_import_provider,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                **kwargs,
            )

    def transaction_import_windows(self, plan) -> tuple[tuple[date, date], ...]:
        if plan.mode == "backfill":
            return tuple(plan.pending_backfill_windows or ())
        start_date = plan.start_date or plan.end_date
        return ((start_date, plan.end_date),)

    def account_sync_window_from_transaction_plan(self, plan, window_start: date, window_end: date):
        from app.connectors.sync_modes import AccountSyncWindow

        return AccountSyncWindow(
            mode=plan.mode,
            start_date=window_start,
            end_date=window_end,
            latest_persisted_date=plan.latest_persisted_date,
            last_successful_fetch_date=plan.last_successful_fetch_date,
            overlap_days=plan.overlap_days,
        )

    async def record_transaction_import_window_started(
        self,
        *,
        user_id: int,
        account_external_id: str,
        account_name: str,
        account_type: str | None,
        is_liability: bool,
        mode: str,
        window_start_date: date,
        window_end_date: date,
    ) -> None:
        from app.connectors.sync_context import get_connector_sync_context
        from app.database import async_session
        from app.services.sqlite_write_gate import sqlite_write_gate
        from app.services.transaction_import import record_transaction_window_started

        context = get_connector_sync_context()
        async with async_session() as db, sqlite_write_gate(), db.begin():
            await record_transaction_window_started(
                db,
                user_id=user_id,
                provider=self.transaction_import_provider,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                mode=mode,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
                sync_id=context.sync_id if context else None,
            )

    async def record_transaction_import_window_failed(
        self,
        *,
        user_id: int,
        account_external_id: str,
        account_name: str,
        account_type: str | None,
        is_liability: bool,
        mode: str,
        window_start_date: date,
        window_end_date: date,
        error: str,
    ) -> None:
        from app.connectors.sync_context import get_connector_sync_context
        from app.database import async_session
        from app.services.sqlite_write_gate import sqlite_write_gate
        from app.services.transaction_import import record_transaction_window_failed

        context = get_connector_sync_context()
        async with async_session() as db, sqlite_write_gate(), db.begin():
            await record_transaction_window_failed(
                db,
                user_id=user_id,
                provider=self.transaction_import_provider,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                mode=mode,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
                error=error,
                sync_id=context.sync_id if context else None,
            )

    async def record_transaction_import_window_completed(
        self,
        *,
        user_id: int,
        account_external_id: str,
        account_name: str,
        account_type: str | None,
        is_liability: bool,
        mode: str,
        window_start_date: date,
        window_end_date: date,
        transactions: Sequence[NormalizedTransaction],
        missing_account_message: str | None = None,
        category_resolver=None,
    ) -> bool:
        from sqlalchemy import select

        from app.connectors.persistence import upsert_transactions_for_account
        from app.connectors.sync_context import get_connector_sync_context
        from app.database import async_session
        from app.models import Account, Institution
        from app.services.categories import CategoryResolver
        from app.services.sqlite_write_gate import sqlite_write_gate
        from app.services.transaction_import import (
            record_transaction_window_completed,
            record_transaction_window_failed,
        )

        context = get_connector_sync_context()
        async with async_session() as db, sqlite_write_gate(), db.begin():
            account = (
                await db.execute(
                    select(Account)
                    .join(Institution, Account.institution_id == Institution.id)
                    .where(
                        Account.user_id == user_id,
                        Institution.user_id == user_id,
                        Institution.id == (context.institution_id if context else None),
                        Account.external_id == account_external_id,
                    )
                )
            ).scalars().first()
            if not account:
                await record_transaction_window_failed(
                    db,
                    user_id=user_id,
                    provider=self.transaction_import_provider,
                    account_external_id=account_external_id,
                    account_name=account_name,
                    account_type=account_type,
                    is_liability=is_liability,
                    mode=mode,
                    window_start_date=window_start_date,
                    window_end_date=window_end_date,
                    error=missing_account_message or "Account row was not available for transaction persistence.",
                    sync_id=context.sync_id if context else None,
                )
                return False

            category_resolver = category_resolver or await CategoryResolver.build(db, user_id)
            persisted = await upsert_transactions_for_account(
                db,
                user_id=user_id,
                account=account,
                transactions=list(transactions or []),
                category_resolver=category_resolver,
                provider=self.transaction_import_provider,
            )
            await record_transaction_window_completed(
                db,
                user_id=user_id,
                provider=self.transaction_import_provider,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                mode=mode,
                window_start_date=window_start_date,
                window_end_date=window_end_date,
                transaction_count=persisted,
                sync_id=context.sync_id if context else None,
            )
            return True

    async def fetch_and_persist_transaction_import_windows(
        self,
        *,
        user_id: int,
        account_external_id: str,
        account_name: str,
        account_type: str | None,
        is_liability: bool,
        fetch_window,
        backfill_days: int | None = None,
        backfill_chunk_days: int | None = None,
        incremental_days: int | None = None,
        overlap_days: int | None = None,
        today: date | None = None,
        failure_message: str = "Transaction window failed.",
    ) -> tuple[list[NormalizedTransaction], bool, dict[str, Any]]:
        plan = await self.plan_transaction_import(
            user_id=user_id,
            account_external_id=account_external_id,
            account_name=account_name,
            account_type=account_type,
            is_liability=is_liability,
            backfill_days=backfill_days,
            backfill_chunk_days=backfill_chunk_days,
            incremental_days=incremental_days,
            overlap_days=overlap_days,
            today=today,
        )
        from app.database import async_session
        from app.services.categories import CategoryResolver

        async with async_session() as resolver_db:
            category_resolver = await CategoryResolver.build(resolver_db, user_id)
        all_transactions: list[NormalizedTransaction] = []
        all_succeeded = True
        for window_start, window_end in self.transaction_import_windows(plan):
            sync_window = self.account_sync_window_from_transaction_plan(
                plan,
                window_start,
                window_end,
            )
            await self.record_transaction_import_window_started(
                user_id=user_id,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                mode=plan.mode,
                window_start_date=window_start,
                window_end_date=window_end,
            )
            try:
                fetched = fetch_window(sync_window)
                if inspect.isawaitable(fetched):
                    fetched = await fetched
                transactions, succeeded = fetched
            except Exception as exc:
                await self.record_transaction_import_window_failed(
                    user_id=user_id,
                    account_external_id=account_external_id,
                    account_name=account_name,
                    account_type=account_type,
                    is_liability=is_liability,
                    mode=plan.mode,
                    window_start_date=window_start,
                    window_end_date=window_end,
                    error=safe_connector_public_message(exc, fallback=failure_message),
                )
                raise

            transactions = list(transactions or [])
            all_transactions.extend(transactions)
            if not succeeded:
                await self.record_transaction_import_window_failed(
                    user_id=user_id,
                    account_external_id=account_external_id,
                    account_name=account_name,
                    account_type=account_type,
                    is_liability=is_liability,
                    mode=plan.mode,
                    window_start_date=window_start,
                    window_end_date=window_end,
                    error=failure_message,
                )
                all_succeeded = False
                continue

            completed = await self.record_transaction_import_window_completed(
                user_id=user_id,
                account_external_id=account_external_id,
                account_name=account_name,
                account_type=account_type,
                is_liability=is_liability,
                mode=plan.mode,
                window_start_date=window_start,
                window_end_date=window_end,
                transactions=transactions,
                category_resolver=category_resolver,
            )
            all_succeeded = all_succeeded and completed

        return all_transactions, all_succeeded, plan.as_sync_state()

    def ok_result(
        self,
        *,
        accounts: Sequence[NormalizedAccount] | None = None,
        holdings: Sequence[NormalizedHolding] | None = None,
        transactions: Sequence[NormalizedTransaction] | None = None,
        holdings_by_account: Mapping[str, Sequence[NormalizedHolding]] | None = None,
        transactions_by_account: Mapping[str, Sequence[NormalizedTransaction]] | None = None,
        transaction_fetch_succeeded_accounts: set[str] | None = None,
        message: str | None = None,
    ) -> SyncResult:
        return SyncResult(
            status=SyncStatus.OK,
            message=message,
            accounts=list(accounts or []),
            holdings=list(holdings or []),
            transactions=list(transactions or []),
            holdings_by_account={
                key: list(value)
                for key, value in (holdings_by_account or {}).items()
            },
            transactions_by_account={
                key: list(value)
                for key, value in (transactions_by_account or {}).items()
            },
            transaction_fetch_succeeded_accounts=set(transaction_fetch_succeeded_accounts or set()),
        )

    def missing_credentials_result(
        self,
        *,
        user_id: int,
        message: str,
        stage: str = "auth missing",
        **fields: Any,
    ) -> SyncResult:
        self.log_event(stage, user_id=user_id, **fields)
        return self.auth_required_result(user_id=user_id, message=message, log=False)

    def invalid_credentials_result(
        self,
        *,
        user_id: int,
        message: str = "Invalid credentials",
        stage: str = "auth failed",
        status_code: int | None = None,
        **fields: Any,
    ) -> SyncResult:
        self.log_event(stage, level="warning", user_id=user_id, status_code=status_code, **fields)
        return self.auth_required_result(user_id=user_id, message=message, log=False)

    def auth_required_result(
        self,
        *,
        user_id: int,
        message: str | None = None,
        stage: str = "auth required",
        exception: Exception | None = None,
        log: bool = True,
        **fields: Any,
    ) -> SyncResult:
        if log:
            self.log_event(
                stage,
                user_id=user_id,
                error=type(exception).__name__ if exception else None,
                message=str(exception) if exception else None,
                **fields,
            )
        public_message = (
            safe_connector_public_message(message, fallback="Authentication required")
            if message is not None
            else None
        )
        return SyncResult(status=SyncStatus.AUTH_REQUIRED, message=public_message)

    def network_error_result(
        self,
        *,
        user_id: int,
        message: str = "Connection failed",
        stage: str = "sync network failure",
        exception: Exception | None = None,
        **fields: Any,
    ) -> SyncResult:
        self.log_event(
            stage,
            level="warning",
            user_id=user_id,
            error=type(exception).__name__ if exception else None,
            message=str(exception) if exception else message,
            **fields,
        )
        return SyncResult(
            status=SyncStatus.NETWORK_ERROR,
            message=safe_connector_public_message(message, fallback="Connection failed"),
        )

    def error_result(
        self,
        *,
        user_id: int,
        message: str,
        stage: str = "sync failed",
        exception: Exception | None = None,
        **fields: Any,
    ) -> SyncResult:
        self.log_event(
            stage,
            level="error",
            user_id=user_id,
            error=type(exception).__name__ if exception else None,
            message=str(exception) if exception else message,
            **fields,
        )
        return SyncResult(
            status=SyncStatus.ERROR,
            message=safe_connector_public_message(message),
        )

    def exception_result(
        self,
        user_id: int,
        exc: Exception,
        *,
        stage: str = "sync failed",
    ) -> SyncResult:
        if isinstance(exc, ApiProviderTemporaryError) or self.exception_is_network_error(exc):
            return self.network_error_result(user_id=user_id, exception=exc)
        if isinstance(exc, ApiProviderInvalidCredentials) or self.is_invalid_credentials_exception(exc):
            return self.invalid_credentials_result(
                user_id=user_id,
                message=safe_connector_public_message(exc, fallback="Invalid credentials"),
            )
        if isinstance(exc, ApiProviderAuthRequired) or self.is_auth_required_exception(exc):
            return self.auth_required_result(
                user_id=user_id,
                message=safe_connector_public_message(exc, fallback="Authentication required"),
                exception=exc,
            )
        return self.error_result(
            user_id=user_id,
            message=safe_connector_public_message(exc),
            stage=stage,
            exception=exc,
        )

    def is_auth_required_exception(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in AUTH_REQUIRED_KEYWORDS)

    def is_invalid_credentials_exception(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in INVALID_CREDENTIAL_KEYWORDS)

    def exception_is_network_error(self, exc: Exception) -> bool:
        return is_network_error(exc)

    def normalize_result(self, result: Any, *, user_id: int | None = None) -> SyncResult:
        if isinstance(result, SyncResult):
            return result

        if isinstance(result, Mapping):
            try:
                status = SyncStatus(result.get("status"))
            except (TypeError, ValueError):
                status = SyncStatus.ERROR
            return SyncResult(
                status=status,
                message=(
                    safe_connector_public_message(result.get("message"))
                    if result.get("message")
                    else None
                ),
                accounts=list(result.get("accounts") or []),
                holdings=list(result.get("holdings") or []),
                transactions=list(result.get("transactions") or []),
                holdings_by_account={
                    str(key): list(value or [])
                    for key, value in (result.get("holdings_by_account") or {}).items()
                },
                transactions_by_account={
                    str(key): list(value or [])
                    for key, value in (result.get("transactions_by_account") or {}).items()
                },
                transaction_fetch_succeeded_accounts=set(
                    result.get("transaction_fetch_succeeded_accounts") or set()
                ),
                challenge_method=result.get("challenge_method"),
                transaction_import_deferred=bool(result.get("transaction_import_deferred")),
            )

        self.log_event(
            "sync unexpected result",
            level="error",
            user_id=user_id,
            result_type=type(result).__name__,
        )
        return SyncResult(
            status=SyncStatus.ERROR,
            message=f"{self.display_name} returned an unexpected sync result.",
        )

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        user_id: int | None = None,
        stage: str = "api request",
        expected_statuses: tuple[int, ...] = (200,),
        auth_statuses: tuple[int, ...] = (401, 403),
        retry_attempts: int = 0,
        retry_statuses: tuple[int, ...] = (408, 429, 500, 502, 503, 504),
        retry_base_delay: float = 0.25,
        **kwargs: Any,
    ) -> httpx.Response:
        attempts = max(1, retry_attempts + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = await client.request(method, url, **kwargs)
            except Exception as exc:
                if attempt < attempts and is_network_error(exc):
                    self.log_event(
                        f"{stage} retry",
                        level="warning",
                        user_id=user_id,
                        attempt=attempt,
                        max_attempts=attempts,
                        error=type(exc).__name__,
                        message=str(exc),
                    )
                    await asyncio.sleep(retry_base_delay * attempt)
                    continue
                raise

            if response.status_code in auth_statuses:
                raise ApiProviderAuthRequired(f"{stage} returned HTTP {response.status_code}")
            if response.status_code in retry_statuses and attempt < attempts:
                self.log_event(
                    f"{stage} retry",
                    level="warning",
                    user_id=user_id,
                    attempt=attempt,
                    max_attempts=attempts,
                    status_code=response.status_code,
                )
                await asyncio.sleep(retry_base_delay * attempt)
                continue
            if response.status_code not in expected_statuses:
                raise ApiProviderTemporaryError(f"{stage} returned HTTP {response.status_code}")
            return response

        raise ApiProviderTemporaryError(f"{stage} failed after {attempts} attempts")

    async def request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        response = await self.request(client, method, url, **kwargs)
        return response.json()
