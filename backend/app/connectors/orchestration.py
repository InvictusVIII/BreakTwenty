"""Shared connector dispatch + persistence orchestration."""

from __future__ import annotations

import inspect
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.connector_logging import get_connector_logger, log_connector_event
from app.connectors.persistence import detect_institution_profile_mismatch, persist_sync_result
from app.connectors.registry import get_connector, get_connector_spec, get_scraper_connector
from app.connectors.sync_context import connector_sync_context, normalize_sync_scope
from app.connectors.types import SyncResult, SyncStatus
from app.models import Institution
from app.provider_catalog import (
    get_provider_institution_type,
    get_runtime_state_metadata,
    provider_uses_background_transaction_import,
)
from app.services.runtime_state import clear_provider_reauth_quarantine_async
from app.services.network_preflight import NetworkPreflightResult, run_provider_network_preflight
from app.services.holdings_sector_enrichment import enrich_holding_rows_with_sectors
from app.services.sqlite_write_gate import retry_sqlite_busy, sqlite_write_gate
from app.services.sync_tracking import (
    ensure_provider_sync_attempt,
    finalize_provider_sync_attempt,
    start_provider_sync_attempt,
)
from app.services.sync_utils import (
    clear_invalid_scraper_credentials_marker,
    check_invalid_credentials_result,
    is_network_error,
    persist_result_sync_status,
    set_invalid_scraper_credentials_marker,
    set_ibkr_scraper_success_marker,
    set_institution_sync_status,
)


SuccessPrecommitHook = Callable[
    [AsyncSession, int | None],
    Awaitable[dict[str, object] | None],
]


def _result_summary_fields(result: SyncResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "accounts": len(result.accounts),
        "holdings": len(result.holdings),
        "transactions": len(result.transactions),
        "transaction_accounts": len(result.transaction_fetch_succeeded_accounts),
        "challenge_method": result.challenge_method,
        "message": result.message,
        "transaction_import_deferred": result.transaction_import_deferred,
    }


def _different_profile_detected_message(provider_display_name: str) -> str:
    return (
        f"This sign-in belongs to a different {provider_display_name} profile. "
        f"Your existing {provider_display_name} accounts and history were left unchanged."
    )


async def _clear_provider_reauth_quarantine(
    db: AsyncSession,
    user_id: int,
    provider: str,
    institution_id: int | None = None,
) -> None:
    if institution_id is not None:
        institution = await db.get(Institution, int(institution_id))
        if institution is not None and (
            int(institution.user_id) != int(user_id)
            or str(institution.provider or "").strip() != str(provider or "").strip()
        ):
            return
    try:
        runtime_metadata = get_runtime_state_metadata(provider)
    except KeyError:
        runtime_metadata = {}
    await clear_provider_reauth_quarantine_async(
        db,
        user_id,
        provider,
        include_desktop_auth=bool(runtime_metadata.get("desktopAuthRuntimeDir")),
        institution_id=institution_id,
    )


async def _log_network_preflight(
    *,
    provider: str,
    user_id: int,
    sync_id: str | None,
) -> NetworkPreflightResult | None:
    logger = get_connector_logger(provider)
    try:
        result = await run_provider_network_preflight(provider)
    except Exception as exc:
        log_connector_event(
            logger,
            provider=provider,
            stage="network preflight failed",
            level="warning",
            user_id=user_id,
            sync_id=sync_id,
            error=type(exc).__name__,
            message=str(exc),
        )
        return None
    log_connector_event(
        logger,
        provider=provider,
        stage="network preflight result",
        level="info" if result.status == "reachable" else "warning",
        user_id=user_id,
        sync_id=sync_id,
        **result.log_fields(),
    )
    return result


async def persist_connector_result(
    db: AsyncSession,
    user_id: int,
    provider: str,
    result: SyncResult,
    *,
    sync_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    institution_id: int | None = None,
    success_precommit_hook: SuccessPrecommitHook | None = None,
) -> dict:
    sync_scope = normalize_sync_scope(sync_scope)
    spec = get_connector_spec(provider)
    logger = get_connector_logger(provider)
    if result.status == SyncStatus.OK and any(
        not str(account.external_id or "").strip() for account in result.accounts
    ):
        log_connector_event(
            logger,
            provider=provider,
            stage="persist rejected",
            level="error",
            user_id=user_id,
            sync_id=sync_id,
            reason="missing stable account identifier",
        )
        result = SyncResult(
            status=SyncStatus.ERROR,
            message="The provider returned an account without a stable identifier.",
        )
    if sync_scope == "accounts" and (
        result.transactions
        or result.transactions_by_account
        or result.transaction_fetch_succeeded_accounts
    ):
        result = replace(
            result,
            transactions=[],
            transactions_by_account={},
            transaction_fetch_succeeded_accounts=set(),
        )
    response = result.to_route_response()
    if sync_id:
        response["sync_id"] = sync_id

    log_connector_event(
        logger,
        provider=provider,
        stage="persist start",
        user_id=user_id,
        sync_id=sync_id,
        persisted_provider=spec.persistence_provider,
        status_provider=spec.status_provider,
        provider_type=spec.provider_type,
        sync_scope=sync_scope,
        debug=True,
        **_result_summary_fields(result),
    )

    target_institution_id = int(institution_id) if institution_id is not None else None
    inserted_holding_rows = []
    persistence_metadata: dict[str, int] = {}
    async with sqlite_write_gate():
        try:
            if result.status == SyncStatus.OK and target_institution_id is not None and not add_flow:
                mismatch = await detect_institution_profile_mismatch(
                    db,
                    user_id=user_id,
                    institution_id=target_institution_id,
                    accounts=result.accounts,
                )
                if bool(mismatch.get("mismatch")):
                    await set_institution_sync_status(
                        db,
                        user_id,
                        spec.status_provider,
                        "auth_required",
                        institution_id=target_institution_id,
                        commit=False,
                    )
                    response = {
                        "status": "different_profile_detected",
                        "message": _different_profile_detected_message(spec.provider_display_name),
                        "institution_id": target_institution_id,
                        "data_preserved": True,
                    }
                    if sync_id:
                        response["sync_id"] = sync_id
                    await db.commit()
                    log_connector_event(
                        logger,
                        provider=provider,
                        stage="persist blocked",
                        user_id=user_id,
                        sync_id=sync_id,
                        target_institution_id=target_institution_id,
                        result_status="different_profile_detected",
                        comparison_mode=mismatch.get("comparison_mode"),
                        overlap_count=mismatch.get("overlap_count"),
                        existing_accounts=mismatch.get("existing_accounts"),
                        incoming_accounts=mismatch.get("incoming_accounts"),
                        debug=True,
                        **_result_summary_fields(result),
                    )
                    return response

            if sync_scope != "transactions":
                inserted_holding_rows = await persist_sync_result(
                    db,
                    user_id,
                    provider=spec.persistence_provider,
                    provider_display_name=spec.provider_display_name,
                    provider_type=get_provider_institution_type(spec.persistence_provider),
                    result=result,
                    pending_add=add_flow,
                    institution_id=target_institution_id,
                    persistence_metadata=persistence_metadata,
                )
                if persistence_metadata.get("institution_id"):
                    response["institution_id"] = persistence_metadata["institution_id"]
            if result.status != SyncStatus.ALREADY_SYNCING:
                should_persist_status = True
                response_status = str(response.get("status") or "").lower()
                if sync_scope == "transactions":
                    should_persist_status = response_status in {
                        "ok",
                        "network_error",
                        "error",
                        "auth_required",
                        "different_profile_detected",
                    }
                elif sync_scope == "accounts" and response_status == "ok":
                    try:
                        follow_up_pending = bool(provider_uses_background_transaction_import(provider))
                    except Exception:
                        follow_up_pending = False
                    if follow_up_pending:
                        # Authentication has already succeeded once accounts and balances
                        # arrive. Clear any temporary auth_required state from login/2FA
                        # before the transaction job runs; its auth failures can still
                        # replace this with auth_required when that phase resolves.
                        await set_institution_sync_status(
                            db,
                            user_id,
                            spec.status_provider,
                            "ok",
                            institution_id=(
                                persistence_metadata.get("institution_id")
                                or target_institution_id
                            ),
                            commit=False,
                        )
                        should_persist_status = False
                if should_persist_status:
                    await persist_result_sync_status(
                        db,
                        user_id,
                        spec.status_provider,
                        response,
                        error_status=spec.error_status,
                        institution_id=(
                            persistence_metadata.get("institution_id")
                            or target_institution_id
                        ),
                        commit=False,
                    )

            if result.status == SyncStatus.OK:
                resolved_institution_id = (
                    persistence_metadata.get("institution_id") or target_institution_id
                )
                if success_precommit_hook is not None:
                    hook_response = await success_precommit_hook(db, resolved_institution_id)
                    if hook_response:
                        response.update(hook_response)
                await _clear_provider_reauth_quarantine(
                    db, user_id, provider, resolved_institution_id
                )
                await _clear_provider_reauth_quarantine(
                    db,
                    user_id,
                    spec.persistence_provider,
                    resolved_institution_id,
                )
                await _clear_provider_reauth_quarantine(
                    db,
                    user_id,
                    spec.status_provider,
                    resolved_institution_id,
                )
                if provider == "ibkr" and result.accounts and resolved_institution_id is not None:
                    await set_ibkr_scraper_success_marker(db, user_id, resolved_institution_id)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    # Sector enrichment runs its market-data HTTP outside the write gate; its
    # deferred DB writes are then committed under the gate.
    if inserted_holding_rows:
        try:
            sectors_changed = await enrich_holding_rows_with_sectors(
                db,
                user_id,
                inserted_holding_rows,
            )
            if sectors_changed:
                async with sqlite_write_gate():
                    await db.commit()
        except Exception as exc:
            await db.rollback()
            log_connector_event(
                logger,
                provider=provider,
                stage="sector enrichment failed",
                level="warning",
                user_id=user_id,
                sync_id=sync_id,
                error=type(exc).__name__,
                message=str(exc),
            )

    log_connector_event(
        logger,
        provider=provider,
        stage="persist result",
        user_id=user_id,
        sync_id=sync_id,
        route_status=response.get("status"),
        sync_scope=sync_scope,
        debug=True,
        **_result_summary_fields(result),
    )
    return response


async def run_connector_sync(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    sync_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    transaction_job_id: str | None = None,
    transaction_job_lease_token: str | None = None,
    institution_id: int,
    sync_source: str | None = None,
    success_precommit_hook: SuccessPrecommitHook | None = None,
    attempt_id: str | None = None,
) -> tuple[SyncResult, dict]:
    from app.services.support_auto_archive import archive_run_fire_and_forget

    sync_scope = normalize_sync_scope(sync_scope)
    normalized_sync_source = str(sync_source or "").strip().lower() or "unspecified"
    connector = get_connector(provider)
    logger = get_connector_logger(provider)
    attempt = (
        ensure_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            institution_id=institution_id,
        )
        if sync_id
        else start_provider_sync_attempt(user_id, provider, institution_id=institution_id)
    )
    sync_id = attempt["sync_id"]
    started_at = perf_counter()
    try:
        log_connector_event(
            logger,
            provider=provider,
            stage="sync start",
            user_id=user_id,
            sync_id=sync_id,
            attempt_id=attempt_id,
            sync_scope=sync_scope,
            sync_source=normalized_sync_source,
        )
        with connector_sync_context(
            user_id=user_id,
            provider=provider,
            institution_id=institution_id,
            sync_id=sync_id,
            attempt_id=attempt_id,
            add_flow=add_flow,
            sync_scope=sync_scope,
            transaction_job_id=transaction_job_id,
            transaction_job_lease_token=transaction_job_lease_token,
        ):
            result = await connector.sync(user_id)
        if result.status == SyncStatus.NETWORK_ERROR:
            await _log_network_preflight(provider=provider, user_id=user_id, sync_id=sync_id)
        response = await retry_sqlite_busy(
            lambda: persist_connector_result(
                db,
                user_id,
                provider,
                result,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
                success_precommit_hook=success_precommit_hook,
            ),
            label=f"{provider}:persist:{sync_scope}",
            rollback=db.rollback,
        )
        log_connector_event(
            logger,
            provider=provider,
            stage="sync result",
            user_id=user_id,
            sync_id=sync_id,
            attempt_id=attempt_id,
            sync_scope=sync_scope,
            sync_source=normalized_sync_source,
            duration_ms=int((perf_counter() - started_at) * 1000),
            **_result_summary_fields(result),
        )
        response_status = str(response.get("status") or "").lower()
        archive_scheduled = False
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status=response.get("status"),
            institution_id=institution_id,
        )
        try:
            archive_run_fire_and_forget(
                user_id=user_id,
                provider=provider,
                sync_id=sync_id,
                trigger=f"sync_result_{response_status or 'unknown'}",
                error=str(response.get("message") or "") or None,
                extra_fields={
                    "sync_scope": sync_scope,
                    "result_status": result.status.value,
                    "sync_source": normalized_sync_source,
                    "institution_id": institution_id,
                },
                attempt_id=attempt_id,
            )
            archive_scheduled = True
        except Exception as exc:
            log_connector_event(
                logger,
                provider=provider,
                stage="sync archive schedule failed",
                level="warning",
                user_id=user_id,
                sync_id=sync_id,
                sync_scope=sync_scope,
                error=type(exc).__name__,
            )
        log_connector_event(
            logger,
            provider=provider,
            stage="sync finalized",
            user_id=user_id,
            sync_id=sync_id,
            sync_scope=sync_scope,
            sync_source=normalized_sync_source,
            response_status=response_status,
            archive_scheduled=archive_scheduled,
            debug=True,
        )
        return result, response
    except Exception as exc:
        network_failure = is_network_error(exc)
        if network_failure:
            await _log_network_preflight(provider=provider, user_id=user_id, sync_id=sync_id)
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="network_error" if network_failure else "error",
            institution_id=institution_id,
        )
        archive_run_fire_and_forget(
            user_id=user_id,
            provider=provider,
            sync_id=sync_id,
            trigger="sync_exception",
            error=f"{type(exc).__name__}: {exc}",
            extra_fields={
                "sync_scope": sync_scope,
                "network_failure": network_failure,
                "result_status": "network_error" if network_failure else "error",
                "sync_source": normalized_sync_source,
                "institution_id": institution_id,
            },
            traceback_text="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            attempt_id=attempt_id,
        )
        raise


async def run_scraper_connector_login(
    db: AsyncSession,
    user_id: int,
    provider: str,
    username: str,
    password: str,
    *,
    add_flow: bool = False,
    sync_id: str | None = None,
    sync_scope: str = "full",
    institution_id: int,
) -> tuple[SyncResult, dict]:
    sync_scope = normalize_sync_scope(sync_scope)
    connector = get_scraper_connector(provider)
    logger = get_connector_logger(provider)
    attempt = (
        ensure_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            institution_id=institution_id,
        )
        if sync_id
        else start_provider_sync_attempt(user_id, provider, institution_id=institution_id)
    )
    sync_id = attempt["sync_id"]
    started_at = perf_counter()
    try:
        log_connector_event(
            logger,
            provider=provider,
            stage="login start",
            user_id=user_id,
            sync_id=sync_id,
            add_flow=add_flow,
            sync_scope=sync_scope,
        )
        login_params = inspect.signature(connector.login).parameters
        with connector_sync_context(
            user_id=user_id,
            provider=provider,
            institution_id=institution_id,
            sync_id=sync_id,
            add_flow=add_flow,
            sync_scope=sync_scope,
        ):
            if "force_fresh_session" in login_params:
                result = await connector.login(
                    user_id,
                    username,
                    password,
                    force_fresh_session=add_flow,
                )
            else:
                result = await connector.login(user_id, username, password)
        response = await retry_sqlite_busy(
            lambda: persist_connector_result(
                db,
                user_id,
                provider,
                result,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
            ),
            label=f"{provider}:login-persist:{sync_scope}",
            rollback=db.rollback,
        )

        if result.status in (
            SyncStatus.OK,
            SyncStatus.AUTH_REQUIRED,
            SyncStatus.TWO_FA_REQUIRED,
            SyncStatus.WAITING,
        ):
            await clear_invalid_scraper_credentials_marker(
                db,
                user_id,
                provider,
                institution_id=institution_id,
            )
        elif check_invalid_credentials_result(response):
            await set_invalid_scraper_credentials_marker(
                db,
                user_id,
                provider,
                username,
                password,
                institution_id=institution_id,
            )
        await db.commit()

        log_connector_event(
            logger,
            provider=provider,
            stage="login result",
            user_id=user_id,
            sync_id=sync_id,
            duration_ms=int((perf_counter() - started_at) * 1000),
            **_result_summary_fields(result),
        )
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status=response.get("status"),
            institution_id=institution_id,
        )
        return result, response
    except Exception:
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        raise


async def run_scraper_connector_complete_2fa(
    db: AsyncSession,
    user_id: int,
    provider: str,
    code: str | None = None,
    *,
    sync_id: str | None = None,
    add_flow: bool = False,
    sync_scope: str = "full",
    institution_id: int,
) -> tuple[SyncResult, dict]:
    sync_scope = normalize_sync_scope(sync_scope)
    connector = get_scraper_connector(provider)
    logger = get_connector_logger(provider)
    attempt = (
        ensure_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            institution_id=institution_id,
        )
        if sync_id
        else start_provider_sync_attempt(user_id, provider, institution_id=institution_id)
    )
    sync_id = attempt["sync_id"]
    started_at = perf_counter()
    try:
        log_connector_event(
            logger,
            provider=provider,
            stage="2fa start",
            user_id=user_id,
            sync_id=sync_id,
            code_supplied=bool(code),
            sync_scope=sync_scope,
        )
        with connector_sync_context(
            user_id=user_id,
            provider=provider,
            institution_id=institution_id,
            sync_id=sync_id,
            add_flow=add_flow,
            sync_scope=sync_scope,
        ):
            result = await connector.complete_2fa(user_id, code)
        response = await retry_sqlite_busy(
            lambda: persist_connector_result(
                db,
                user_id,
                provider,
                result,
                sync_id=sync_id,
                add_flow=add_flow,
                sync_scope=sync_scope,
                institution_id=institution_id,
            ),
            label=f"{provider}:2fa-persist:{sync_scope}",
            rollback=db.rollback,
        )
        log_connector_event(
            logger,
            provider=provider,
            stage="2fa result",
            user_id=user_id,
            sync_id=sync_id,
            duration_ms=int((perf_counter() - started_at) * 1000),
            code_supplied=bool(code),
            **_result_summary_fields(result),
        )
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status=response.get("status"),
            institution_id=institution_id,
        )
        return result, response
    except Exception:
        finalize_provider_sync_attempt(
            user_id,
            provider,
            sync_id=sync_id,
            status="error",
            institution_id=institution_id,
        )
        raise
