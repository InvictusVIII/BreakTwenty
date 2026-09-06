"""Scraper-specific connector extension + shared adapter helpers."""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from typing import Any

from app.connectors.base import Connector
from app.connectors.connector_logging import (
    get_connector_logger,
    log_connector_event,
    safe_connector_public_message,
)
from app.connectors.types import SyncResult, SyncStatus
from app.database import async_session
from app.services.connection_auth_storage import (
    StoredScraperCredentials,
    get_scraper_credentials,
)
from app.services.replay_diagnostics import begin_replay_diagnostics, record_provider_payload_sections
from app.services.sync_utils import (
    is_network_error,
)


class ScraperConnector(Connector, ABC):
    """Protocol extension for connectors with interactive auth steps."""

    @abstractmethod
    async def login(self, user_id: int, username: str, password: str) -> SyncResult:
        ...

    @abstractmethod
    async def complete_2fa(self, user_id: int, code: str | None = None) -> SyncResult:
        ...

    async def cleanup_pending(self, user_id: int) -> None:
        """Close any provider-specific pending auth/browser state for *user_id*."""


class ModuleBackedScraperConnector(ScraperConnector):
    """Shared adapter for the existing scraper modules.

    The scraper modules continue to own the browser automation and user-scoped
    runtime state. Connector wrappers translate their raw account payloads and
    auth result dicts into normalized connector results.
    """

    module_path: str
    generic_phase2_diagnostics_enabled = True

    def __init__(self, provider: str, module_path: str) -> None:
        super().__init__(provider=provider)
        self.module_path = module_path
        self._logger = get_connector_logger(provider)

    def _load_module(self):
        return importlib.import_module(self.module_path)

    async def _get_saved_credentials(self, user_id: int) -> StoredScraperCredentials | None:
        async with async_session() as db:
            return await get_scraper_credentials(db, user_id, self.provider)

    async def sync(self, user_id: int) -> SyncResult:
        module = self._load_module()
        try:
            data = await module.try_headless_sync(user_id)
            self._record_module_payload_diagnostics(user_id, data, source="scraper.saved_session")
            return self._convert_module_result(data)
        except Exception as e:
            if is_network_error(e):
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Connection failed",
                )
            return SyncResult(status=SyncStatus.ERROR, message=safe_connector_public_message(e))

    async def cleanup_pending(self, user_id: int) -> None:
        module = self._load_module()
        cleanup_pending = getattr(module, "cleanup_pending", None)
        if cleanup_pending:
            await cleanup_pending(user_id)

    def _convert_module_result(
        self,
        result: Any,
    ) -> SyncResult:
        if not isinstance(result, dict):
            return SyncResult(status=SyncStatus.ERROR, message="Unexpected scraper response")

        payload = result
        status = payload.get("status")
        raw_message = payload.get("message")
        message = safe_connector_public_message(raw_message) if raw_message else None

        if status == "ok":
            accounts = payload.get("accounts") or []
            if not accounts:
                return SyncResult(
                    status=SyncStatus.ERROR,
                    message=message or "No accounts returned",
                )
            return self._build_success_result(accounts)

        if status == "auth_required":
            return SyncResult(status=SyncStatus.AUTH_REQUIRED, message=message)

        if status == "network_error":
            return SyncResult(status=SyncStatus.NETWORK_ERROR, message=message or "Connection failed")

        if status == "error":
            return SyncResult(status=SyncStatus.ERROR, message=message or "Sync failed")

        return SyncResult(status=SyncStatus.ERROR, message=message or "Sync failed")

    def _record_module_payload_diagnostics(self, user_id: int, payload: Any, *, source: str) -> None:
        if not self.generic_phase2_diagnostics_enabled or not isinstance(payload, dict):
            return
        status = str(payload.get("status") or "").strip().lower()
        if status != "ok":
            return
        diagnostics = begin_replay_diagnostics(self.provider, user_id)
        if not diagnostics:
            return
        record_provider_payload_sections(
            diagnostics,
            payload,
            source=source,
            transport="scraper",
        )
        result = "succeeded"
        path = diagnostics.finish(result)
        log_connector_event(
            self._logger,
            provider=self.provider,
            stage="phase2 replay diagnostics",
            user_id=user_id,
            debug=True,
            sync_id=diagnostics.sync_id,
            attempt_id=diagnostics.attempt_id,
            result=result,
            path=str(path),
            display_path=path.name,
            phase1_har_path=str(diagnostics.phase1_har_path or ""),
        )

    @abstractmethod
    def _build_success_result(self, accounts_data: list[dict[str, Any]]) -> SyncResult:
        ...
