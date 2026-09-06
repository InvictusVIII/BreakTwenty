from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    BalanceHistory,
    CategoryRule,
    ConnectionAuthArtifact,
    Holding,
    Institution,
    PendingSyncStatus,
    ProviderSyncLease,
    RecurringSeries,
    Setting,
    Transaction,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
    VisibleAuthAttemptArtifact,
)
from app.provider_catalog import (
    get_provider_metadata,
    get_related_state_providers,
    get_runtime_state_metadata,
    get_transient_setting_keys,
)
from app.services.connection_auth_storage import (
    delete_api_credential_keys,
    delete_api_credentials,
    delete_scraper_credentials,
)
from app.services.runtime_state import (
    clear_provider_reauth_quarantine_async,
    clear_provider_runtime_artifacts_async,
    clear_runtime_state_namespaces,
    quarantine_provider_desktop_auth_dir,
    quarantine_provider_runtime_dir_async,
    remove_provider_desktop_auth_dir,
    remove_provider_runtime_dir,
    remove_provider_runtime_quarantine_dir,
)
from app.services.sync_utils import (
    clear_pending_status_writes,
    get_ibkr_scraper_success_setting_key,
    get_invalid_scraper_credentials_setting_key,
    get_invalid_scraper_credentials_setting_prefix,
)
from app.services.support_auto_archive import purge_provider_diagnostic_artifacts
from app.services.sync_batch import purge_provider_sync_batch_history
from app.services.user_secret_storage import delete_user_secret_artifact
from app.services.wealthsimple import WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND

async def invalidate_provider_auth_state(
    db: AsyncSession,
    user_id: int,
    provider: str,
    *,
    clear_transient_settings: bool = True,
    preserve_pending_session: bool = False,
    quarantine_runtime: bool = False,
    institution_id: int | None = None,
):
    runtime_metadata = get_runtime_state_metadata(provider)
    if clear_transient_settings:
        transient_keys = get_transient_setting_keys(provider)
        await delete_api_credential_keys(
            db,
            user_id,
            provider,
            transient_keys,
            institution_id=institution_id,
        )
        if institution_id is not None:
            invalid_credential_filter = (
                Setting.key == get_invalid_scraper_credentials_setting_key(provider, institution_id)
            )
        else:
            invalid_credential_filter = Setting.key.like(
                f"{get_invalid_scraper_credentials_setting_prefix(provider)}%"
            )
        await db.execute(delete(Setting).where(
            invalid_credential_filter,
            Setting.user_id == user_id,
        ))
        if provider == "ibkr":
            if institution_id is not None:
                scraper_success_filter = (
                    Setting.key == get_ibkr_scraper_success_setting_key(institution_id)
                )
            else:
                scraper_success_filter = Setting.key.like("ibkr_scraper_last_success_at:%")
            await db.execute(delete(Setting).where(
                scraper_success_filter,
                Setting.user_id == user_id,
            ))

    if not preserve_pending_session:
        uses_desktop_auth_runtime_dir = bool(runtime_metadata.get("desktopAuthRuntimeDir"))
        if quarantine_runtime:
            await quarantine_provider_runtime_dir_async(
                db,
                user_id,
                provider,
                institution_id=institution_id,
            )
            if uses_desktop_auth_runtime_dir and institution_id is None:
                quarantine_provider_desktop_auth_dir(user_id, provider)
        else:
            await clear_provider_runtime_artifacts_async(
                db,
                user_id,
                provider,
                runtime_metadata["artifactKinds"],
                institution_id=institution_id,
            )
        if provider == "wealthsimple":
            await delete_user_secret_artifact(
                db,
                user_id,
                WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
            )

    if not preserve_pending_session:
        pending_cleanup_failed = False
        try:
            from app.connectors.registry import get_scraper_connector

            connector = get_scraper_connector(provider)
            await connector.cleanup_pending(user_id)
        except (KeyError, TypeError):
            pass
        except Exception:
            pending_cleanup_failed = True
        if not pending_cleanup_failed and institution_id is None:
            clear_runtime_state_namespaces(user_id, runtime_metadata["pendingRuntimeNamespaces"])
        if not quarantine_runtime and institution_id is None:
            remove_provider_runtime_dir(user_id, provider)
            if uses_desktop_auth_runtime_dir:
                remove_provider_desktop_auth_dir(user_id, provider)
        if institution_id is None:
            for runtime_provider in runtime_metadata.get("extraRuntimeProviders", ()):
                remove_provider_runtime_dir(user_id, runtime_provider)
                remove_provider_runtime_quarantine_dir(user_id, runtime_provider)


def _provider_in_catalog(provider: str | None) -> bool:
    """Connector-catalog membership. Provider-state cleanup only applies to catalog
    providers; manual institutions (the Cash holder, user-created manual
    institutions) carry none and their providers aren't in the catalog."""
    try:
        get_provider_metadata(provider or "")
        return True
    except KeyError:
        return False


async def delete_provider_state(
    db: AsyncSession,
    user_id: int,
    provider: str,
):
    # Manual institutions carry no connector state (and aren't in the catalog) —
    # skip cleanly instead of raising on metadata lookups for an unknown provider.
    if not _provider_in_catalog(provider):
        return
    for state_provider in get_related_state_providers(provider):
        await db.execute(
            delete(CategoryRule).where(
                CategoryRule.user_id == user_id,
                CategoryRule.provider == state_provider,
            )
        )
        import_filters = [
            TransactionImportAccountState.user_id == user_id,
            TransactionImportAccountState.provider == state_provider,
        ]
        window_filters = [
            TransactionImportWindow.user_id == user_id,
            TransactionImportWindow.provider == state_provider,
        ]
        job_filters = [
            TransactionImportJob.user_id == user_id,
            TransactionImportJob.provider == state_provider,
        ]
        await db.execute(delete(TransactionImportWindow).where(*window_filters))
        await db.execute(delete(TransactionImportAccountState).where(*import_filters))
        await db.execute(delete(TransactionImportJob).where(*job_filters))
        await invalidate_provider_auth_state(
            db,
            user_id,
            state_provider,
        )
        runtime_metadata = get_runtime_state_metadata(state_provider)
        clear_runtime_state_namespaces(user_id, runtime_metadata["pendingRuntimeNamespaces"])
        remove_provider_desktop_auth_dir(user_id, state_provider)
        await clear_provider_reauth_quarantine_async(
            db,
            user_id,
            state_provider,
            include_desktop_auth=True,
        )
        await delete_scraper_credentials(
            db,
            user_id,
            state_provider,
        )
        await delete_api_credentials(
            db,
            user_id,
            state_provider,
        )
        await db.execute(
            delete(VisibleAuthAttemptArtifact).where(
                VisibleAuthAttemptArtifact.user_id == user_id,
                VisibleAuthAttemptArtifact.provider == state_provider,
            )
        )


async def delete_institution_and_accounts(
    db: AsyncSession,
    user_id: int,
    institution: Institution,
    *,
    purge_diagnostics: bool = True,
):
    await purge_provider_sync_batch_history(
        db,
        user_id=user_id,
        provider=institution.provider,
        institution_id=institution.id,
    )
    accounts_result = await db.execute(
        select(Account).where(Account.institution_id == institution.id, Account.user_id == user_id)
    )
    accounts = accounts_result.scalars().all()
    account_ids = [account.id for account in accounts]
    recurring_series_ids: set[int] = set()
    if account_ids:
        recurring_series_ids = {
            int(series_id)
            for series_id in (
                await db.execute(
                    select(Transaction.recurring_series_id).where(
                        Transaction.user_id == user_id,
                        Transaction.account_id.in_(account_ids),
                        Transaction.recurring_series_id.is_not(None),
                    )
                )
            ).scalars()
            if series_id is not None
        }
        # The owner-safe self-FK intentionally restricts raw deletes. Clear links through
        # the application before removing an institution so liabilities in other
        # institutions remain intact and cannot retain dangling asset references.
        await db.execute(
            update(Account)
            .where(
                Account.user_id == user_id,
                Account.secured_asset_account_id.in_(account_ids),
            )
            .values(secured_asset_account_id=None)
        )
    for acc in accounts:
        await db.execute(delete(Transaction).where(Transaction.account_id == acc.id, Transaction.user_id == user_id))
        await db.execute(
            delete(TransactionImportWindow).where(
                TransactionImportWindow.account_id == acc.id,
                TransactionImportWindow.user_id == user_id,
            )
        )
        await db.execute(
            delete(TransactionImportAccountState).where(
                TransactionImportAccountState.account_id == acc.id,
                TransactionImportAccountState.user_id == user_id,
            )
        )
        await db.execute(delete(Holding).where(Holding.account_id == acc.id, Holding.user_id == user_id))
        await db.execute(
            delete(BalanceHistory).where(BalanceHistory.account_id == acc.id, BalanceHistory.user_id == user_id)
        )
    await db.execute(
        delete(Account).where(Account.institution_id == institution.id, Account.user_id == user_id)
    )
    await db.execute(
        delete(ConnectionAuthArtifact).where(
            ConnectionAuthArtifact.institution_id == institution.id,
            ConnectionAuthArtifact.user_id == user_id,
        )
    )
    await db.execute(
        delete(ProviderSyncLease).where(
            ProviderSyncLease.user_id == user_id,
            ProviderSyncLease.institution_key == str(int(institution.id)),
        )
    )
    await db.execute(
        delete(PendingSyncStatus).where(
            PendingSyncStatus.user_id == user_id,
            PendingSyncStatus.institution_key == str(int(institution.id)),
        )
    )
    await delete_provider_state(
        db,
        user_id,
        institution.provider,
    )
    await db.delete(institution)
    await db.flush()

    if recurring_series_ids:
        remaining_series_ids = set(
            (
                await db.execute(
                    select(Transaction.recurring_series_id).where(
                        Transaction.user_id == user_id,
                        Transaction.recurring_series_id.in_(recurring_series_ids),
                    )
                )
            ).scalars()
        )
        orphaned_series_ids = recurring_series_ids - remaining_series_ids
        if orphaned_series_ids:
            await db.execute(
                delete(RecurringSeries).where(
                    RecurringSeries.user_id == user_id,
                    RecurringSeries.id.in_(orphaned_series_ids),
                )
            )

    from app.services.manual_accounts import cleanup_orphaned_cash_mirrors
    await cleanup_orphaned_cash_mirrors(db, user_id)

    if purge_diagnostics:
        purge_provider_diagnostic_artifacts(user_id=user_id, provider=institution.provider)
    try:
        from app.provider_catalog import get_status_provider_for_result
        status_provider = get_status_provider_for_result(institution.provider)
    except Exception:
        status_provider = institution.provider
    await clear_pending_status_writes(user_id, status_provider, db=db)


async def delete_account_and_maybe_institution(db: AsyncSession, user_id: int, account: Account) -> bool:
    institution_id = account.institution_id
    recurring_series_ids = set(
        (
            await db.execute(
                select(Transaction.recurring_series_id).where(
                    Transaction.account_id == account.id,
                    Transaction.user_id == user_id,
                    Transaction.recurring_series_id.is_not(None),
                )
            )
        ).scalars()
    )

    await db.execute(delete(Transaction).where(Transaction.account_id == account.id, Transaction.user_id == user_id))
    await db.execute(
        delete(TransactionImportWindow).where(
            TransactionImportWindow.account_id == account.id,
            TransactionImportWindow.user_id == user_id,
        )
    )
    await db.execute(
        delete(TransactionImportAccountState).where(
            TransactionImportAccountState.account_id == account.id,
            TransactionImportAccountState.user_id == user_id,
        )
    )
    await db.execute(delete(Holding).where(Holding.account_id == account.id, Holding.user_id == user_id))
    await db.execute(
        delete(BalanceHistory).where(BalanceHistory.account_id == account.id, BalanceHistory.user_id == user_id)
    )
    # If this asset secured any loans, null those links so they don't dangle at a deleted id.
    await db.execute(
        update(Account)
        .where(Account.secured_asset_account_id == account.id, Account.user_id == user_id)
        .values(secured_asset_account_id=None)
    )
    await db.delete(account)
    await db.flush()

    if recurring_series_ids:
        remaining_series_ids = set(
            (
                await db.execute(
                    select(Transaction.recurring_series_id).where(
                        Transaction.user_id == user_id,
                        Transaction.recurring_series_id.in_(recurring_series_ids),
                    )
                )
            ).scalars()
        )
        orphaned_series_ids = recurring_series_ids - remaining_series_ids
        if orphaned_series_ids:
            await db.execute(
                delete(RecurringSeries).where(
                    RecurringSeries.user_id == user_id,
                    RecurringSeries.id.in_(orphaned_series_ids),
                )
            )

    # Deleting this account removed its transactions, which may have been the
    # source of cash mirror legs on the Cash holder — sweep any now-orphaned ones.
    from app.services.manual_accounts import cleanup_orphaned_cash_mirrors
    await cleanup_orphaned_cash_mirrors(db, user_id)

    remaining = await db.execute(
        select(Account).where(Account.institution_id == institution_id, Account.user_id == user_id)
    )
    remaining_list = remaining.scalars().all()
    institution_removed = len(remaining_list) == 0

    if institution_removed:
        inst = await db.execute(
            select(Institution).where(Institution.id == institution_id, Institution.user_id == user_id)
        )
        institution = inst.scalar_one_or_none()
        if institution:
            await delete_institution_and_accounts(db, user_id, institution)

    return institution_removed
