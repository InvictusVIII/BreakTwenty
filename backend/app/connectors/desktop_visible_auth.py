"""Shared connector base for desktop-visible-auth saved-artifact providers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import select

from app.brand import APP_BRAND_NAME
from app.connectors.connector_logging import (
    get_connector_logger,
    log_connector_event,
    safe_connector_public_message,
)
from app.connectors.persistence import upsert_transactions_for_account
from app.services.categories import CategoryResolver
from app.connectors.scraper import ModuleBackedScraperConnector
from app.connectors.sync_context import get_connector_sync_context
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.database import async_session
from app.models import Account, Institution
from app.scrapers.scraper_logging import scraper_log_context
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.sync_utils import (
    is_network_error,
)
from app.services.transaction_import import (
    record_transaction_window_completed,
    record_transaction_window_failed,
    record_transaction_window_started,
)


class DesktopVisibleAuthSavedSessionConnector(ModuleBackedScraperConnector):
    """Common adapter for providers whose manual auth happens in BreakTwenty Desktop.

    The provider-specific scraper module owns the bank API replay and parsing.
    This class owns the common connector contract: silent saved-artifact sync,
    standardized retired login/2FA behavior, and route-facing result shaping.
    """

    unexpected_payload_message = "Unexpected scraper response"

    def __init__(self, provider: str, module_path: str, display_name: str) -> None:
        super().__init__(provider=provider, module_path=module_path)
        self.display_name = display_name
        self._logger = get_connector_logger(provider)

    def _manual_auth_message(self) -> str:
        return (
            f"{self.display_name} secure login now runs through the {APP_BRAND_NAME} Desktop browser popup. "
            f"Start sync again from {APP_BRAND_NAME} Desktop."
        )

    def _make_mode_resolver(self, user_id: int):
        del user_id
        return None

    async def _saved_artifact_sync_kwargs(self, user_id: int) -> dict[str, Any]:
        resolver = self._make_mode_resolver(user_id)
        return {"mode_resolver": resolver} if resolver else {}

    async def _try_saved_artifact_sync(self, user_id: int):
        module = self._load_module()
        kwargs = await self._saved_artifact_sync_kwargs(user_id)
        with scraper_log_context(provider=self.provider, user_id=user_id):
            return await module.try_headless_sync(user_id, **kwargs)

    def _preprocess_saved_artifact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def _sync_exception_result(self, user_id: int, exc: Exception) -> SyncResult | None:
        del user_id, exc
        return None

    def _log_sync_exception(
        self,
        user_id: int,
        exc: Exception,
        *,
        stage: str = "sync exception",
        level: str = "warning",
        **details: Any,
    ) -> None:
        log_connector_event(
            self._logger,
            provider=self.provider,
            stage=stage,
            level=level,
            user_id=user_id,
            message=str(exc),
            **details,
        )

    async def sync(self, user_id: int) -> SyncResult:
        try:
            payload = await self._try_saved_artifact_sync(user_id)
            return self._saved_artifact_payload_to_result(user_id, payload)
        except Exception as exc:
            provider_result = self._sync_exception_result(user_id, exc)
            if provider_result is not None:
                return provider_result
            if is_network_error(exc):
                return SyncResult(status=SyncStatus.NETWORK_ERROR, message="Connection failed")
            self._log_sync_exception(user_id, exc)
            return SyncResult(
                status=SyncStatus.ERROR,
                message=safe_connector_public_message(exc),
            )

    async def login(
        self,
        user_id: int,
        username: str,
        password: str,
        *,
        force_fresh_session: bool = False,
    ) -> SyncResult:
        del user_id, username, password, force_fresh_session
        return SyncResult(
            status=SyncStatus.AUTH_REQUIRED,
            message=self._manual_auth_message(),
        )

    async def complete_2fa(self, user_id: int, code: str | None = None) -> SyncResult:
        del user_id, code
        return SyncResult(
            status=SyncStatus.AUTH_REQUIRED,
            message=self._manual_auth_message(),
        )

    def _saved_artifact_payload_to_result(self, user_id: int, payload: Any) -> SyncResult:
        if isinstance(payload, dict):
            prepared = self._preprocess_saved_artifact_payload(dict(payload))
            self._record_module_payload_diagnostics(user_id, prepared, source="scraper.saved_artifact")
            return self._convert_module_result(prepared)

        log_connector_event(
            self._logger,
            provider=self.provider,
            stage="sync unexpected scraper payload",
            level="warning",
            user_id=user_id,
            payload_type=type(payload).__name__,
        )
        return SyncResult(status=SyncStatus.ERROR, message=self.unexpected_payload_message)

    def _convert_module_result(
        self,
        result: Any,
    ) -> SyncResult:
        if not isinstance(result, dict):
            return SyncResult(status=SyncStatus.ERROR, message=self.unexpected_payload_message)

        payload = result
        status = payload.get("status")
        raw_message = payload.get("message")
        message = safe_connector_public_message(raw_message) if raw_message else None

        if status == "ok":
            return self._build_from_payload(payload)
        if status == "auth_required":
            return SyncResult(status=SyncStatus.AUTH_REQUIRED, message=message)
        if status == "network_error":
            return SyncResult(status=SyncStatus.NETWORK_ERROR, message=message or "Connection failed")
        if status == "error":
            return SyncResult(status=SyncStatus.ERROR, message=message or "Sync failed")
        return SyncResult(status=SyncStatus.ERROR, message=message or "Sync failed")

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
        if type(self)._build_success_result is DesktopVisibleAuthSavedSessionConnector._build_success_result:
            raise NotImplementedError("Desktop visible-auth connectors must implement payload normalization")
        return self._build_success_result(payload.get("accounts") or [])

    def _build_success_result(self, accounts_data: list[dict[str, Any]]) -> SyncResult:
        if type(self)._build_from_payload is DesktopVisibleAuthSavedSessionConnector._build_from_payload:
            raise NotImplementedError("Desktop visible-auth connectors must implement payload normalization")
        return self._build_from_payload({"accounts": accounts_data, "transactions": {}})


class DesktopVisibleAuthTransactionImportConnector(DesktopVisibleAuthSavedSessionConnector):
    transaction_window_account_name = "Account"
    transaction_window_failed_message = "Transaction window failed"
    missing_account_window_message = "Account row was not available for transaction persistence."

    async def _saved_artifact_sync_kwargs(self, user_id: int) -> dict[str, Any]:
        kwargs = await super()._saved_artifact_sync_kwargs(user_id)
        context = get_connector_sync_context()
        kwargs["sync_scope"] = context.sync_scope if context else "full"
        kwargs["transaction_window_recorder"] = self._make_transaction_window_recorder(user_id)
        return kwargs

    async def _find_account_row(
        self,
        db,
        *,
        user_id: int,
        account_external_id: str,
    ) -> Account | None:
        context = get_connector_sync_context()
        return (
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

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict[str, Any]],
    ) -> list[NormalizedTransaction]:
        raise NotImplementedError("Transaction-import connectors must normalize window transactions")

    def _normalize_transaction_rows(
        self,
        raw_transactions: list[dict[str, Any]],
        normalizer: Callable[[dict[str, Any]], NormalizedTransaction | None],
    ) -> list[NormalizedTransaction]:
        normalized: list[NormalizedTransaction] = []
        seen: set[str] = set()
        for raw in raw_transactions or []:
            if not isinstance(raw, dict):
                continue
            transaction = normalizer(raw)
            if not transaction or transaction.external_id in seen:
                continue
            seen.add(transaction.external_id)
            normalized.append(transaction)
        return normalized

    def _build_result_from_accounts_and_transactions(
        self,
        *,
        accounts: Sequence[NormalizedAccount],
        account_by_external_id: dict[str, NormalizedAccount],
        transactions_data: dict[str, list[dict[str, Any]]],
        transaction_fetch_succeeded_external_ids: set[str],
        transaction_normalizer: Callable[[dict[str, Any], str, NormalizedAccount], NormalizedTransaction | None],
    ) -> SyncResult:
        transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
        transaction_fetch_succeeded_accounts: set[str] = set()
        all_transactions: list[NormalizedTransaction] = []

        for external_id in transaction_fetch_succeeded_external_ids:
            account = account_by_external_id.get(external_id)
            if account:
                transaction_fetch_succeeded_accounts.add(account_result_key(account))

        for external_id, raw_txns in transactions_data.items():
            account = account_by_external_id.get(external_id)
            if not account:
                continue

            account_key = account_result_key(account)
            normalized_list: list[NormalizedTransaction] = []
            seen: set[str] = set()

            for raw in raw_txns or []:
                if not isinstance(raw, dict):
                    continue
                tx = transaction_normalizer(raw, external_id, account)
                if not tx or tx.external_id in seen:
                    continue
                seen.add(tx.external_id)
                normalized_list.append(tx)
                all_transactions.append(tx)

            if normalized_list:
                transactions_by_account[account_key] = normalized_list

        return SyncResult(
            status=SyncStatus.OK,
            accounts=list(accounts),
            transactions=all_transactions,
            transactions_by_account=transactions_by_account,
            transaction_fetch_succeeded_accounts=transaction_fetch_succeeded_accounts,
        )

    def _make_transaction_window_recorder(self, user_id: int):
        def parse_window_date(value: Any) -> date | None:
            try:
                return date.fromisoformat(str(value or "")[:10])
            except ValueError:
                return None

        async def recorder(event: dict[str, Any]) -> None:
            account_payload = event.get("account") if isinstance(event.get("account"), dict) else {}
            external_id = str(account_payload.get("external_id") or "").strip()
            if not external_id:
                return
            try:
                window_start = date.fromisoformat(str(event.get("start_date") or "")[:10])
                window_end = date.fromisoformat(str(event.get("end_date") or "")[:10])
            except ValueError:
                return

            account_name = account_payload.get("name") or self.transaction_window_account_name
            account_type = account_payload.get("account_type")
            is_liability = bool(account_payload.get("is_liability"))
            mode = str(event.get("mode") or "backfill")
            sync_context = get_connector_sync_context()
            sync_id = str(event.get("sync_id") or (sync_context.sync_id if sync_context else "") or "") or None
            event_type = str(event.get("event") or "").strip().lower()
            sync_window = account_payload.get("sync_window") if isinstance(account_payload.get("sync_window"), dict) else {}
            target_start = parse_window_date(sync_window.get("backfill_start_date"))
            target_end = parse_window_date(sync_window.get("backfill_end_date"))

            if event_type not in ("started", "failed", "completed"):
                return

            # Pure CPU normalization runs outside the write gate so it does
            # not extend the time the global SQLite write lock is held.
            normalized = (
                self._normalize_window_transactions(
                    account_payload=account_payload,
                    raw_transactions=event.get("transactions") or [],
                )
                if event_type == "completed"
                else None
            )

            async with async_session() as db, sqlite_write_gate(), db.begin():
                if event_type == "started":
                    await record_transaction_window_started(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=account_name,
                        account_type=account_type,
                        is_liability=is_liability,
                        mode=mode,
                        window_start_date=window_start,
                        window_end_date=window_end,
                        target_start_date=target_start,
                        target_end_date=target_end,
                        sync_id=sync_id,
                    )
                    return

                if event_type == "failed":
                    await record_transaction_window_failed(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=account_name,
                        account_type=account_type,
                        is_liability=is_liability,
                        mode=mode,
                        window_start_date=window_start,
                        window_end_date=window_end,
                        target_start_date=target_start,
                        target_end_date=target_end,
                        error=str(event.get("error") or self.transaction_window_failed_message),
                        sync_id=sync_id,
                    )
                    return

                account = await self._find_account_row(
                    db,
                    user_id=user_id,
                    account_external_id=external_id,
                )
                if not account:
                    await record_transaction_window_failed(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=account_name,
                        account_type=account_type,
                        is_liability=is_liability,
                        mode=mode,
                        window_start_date=window_start,
                        window_end_date=window_end,
                        target_start_date=target_start,
                        target_end_date=target_end,
                        error=self.missing_account_window_message,
                        sync_id=sync_id,
                    )
                    return

                category_resolver = await CategoryResolver.build(db, user_id)
                persisted = await upsert_transactions_for_account(
                    db,
                    user_id=user_id,
                    account=account,
                    transactions=normalized or [],
                    category_resolver=category_resolver,
                    provider=self.provider,
                )
                await record_transaction_window_completed(
                    db,
                    user_id=user_id,
                    provider=self.provider,
                    account_external_id=external_id,
                    account_name=account_name,
                    account_type=account_type,
                    is_liability=is_liability,
                    mode=mode,
                    window_start_date=window_start,
                    window_end_date=window_end,
                    target_start_date=target_start,
                    target_end_date=target_end,
                    transaction_count=persisted,
                    sync_id=sync_id,
                )

        return recorder
