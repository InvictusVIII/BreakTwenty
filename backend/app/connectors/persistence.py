"""Persistence layer for connector sync results.

Converts normalised dataclasses from ``connectors.types`` into SQLAlchemy
model writes.  Every connector — current and future — shares this code path,
so the DB-write logic is written once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TypeVar
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    BalanceHistory,
    Category,
    Holding,
    Institution,
    Transaction,
)
from app.connectors.types import (
    NormalizedAccount,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.services.categories import CategoryResolver, canonical_amount_for_classification
from app.services.connection_auth_storage import ProviderAlreadyConnectedError
from app.services.transaction_import import record_successful_account_transaction_sync
from app.services.user_utils import get_user_timezone_info_from_db

T = TypeVar("T")


@dataclass
class TransactionUpsertCounts:
    """Optional insert-versus-dedup accounting for transaction import callers."""

    inserted: int = 0
    existing: int = 0


async def _find_existing_account(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
    acct: NormalizedAccount,
) -> Account | None:
    if acct.external_id:
        existing = (
            await db.execute(
                select(Account).where(
                    Account.institution_id == institution_id,
                    Account.user_id == user_id,
                    Account.external_id == acct.external_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    alternate_external_ids = tuple(
        external_id
        for external_id in getattr(acct, "alternate_external_ids", ())
        if external_id and external_id != acct.external_id
    )
    if alternate_external_ids:
        existing = (
            await db.execute(
                select(Account).where(
                    Account.institution_id == institution_id,
                    Account.user_id == user_id,
                    Account.external_id.in_(alternate_external_ids),
                )
            )
        ).scalars().first()
        if existing:
            return existing

    return None


def _balance_history_local_date(value: date | datetime, user_tz: ZoneInfo):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.astimezone(user_tz).date()


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _calendar_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


async def _upsert_daily_balance_history(
    db: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    balance: float,
    now: datetime,
    user_tz: ZoneInfo,
) -> None:
    rows = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account_id,
            ).order_by(BalanceHistory.date.desc(), BalanceHistory.id.desc())
        )
    ).scalars().all()

    today_local = _balance_history_local_date(now, user_tz)
    keeper_by_local_date: dict[object, BalanceHistory] = {}
    duplicate_ids: list[int] = []

    for row in rows:
        local_date = _balance_history_local_date(row.date, user_tz)
        if local_date in keeper_by_local_date:
            duplicate_ids.append(row.id)
            continue
        keeper_by_local_date[local_date] = row

    if duplicate_ids:
        await db.execute(
            delete(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account_id,
                BalanceHistory.id.in_(duplicate_ids),
            )
        )

    existing_today = keeper_by_local_date.get(today_local)
    if existing_today:
        existing_today.balance = _decimal(balance)
        existing_today.date = today_local
        return

    db.add(
        BalanceHistory(
            user_id=user_id,
            account_id=account_id,
            balance=_decimal(balance),
            date=today_local,
        )
    )


async def _upsert_balance_history_series(
    db: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    points,
    user_tz: ZoneInfo,
) -> dict[str, int]:
    """Backfill a daily balance series for an account — e.g. broker daily NAV from a Flex
    statement (the one feed that carries pre-sync daily account value). Upserts one
    ``balance_history`` row per user-local day from ``points`` (iterable of
    ``(datetime, balance)``): an existing row for that day is overwritten, a missing day is
    inserted. Days absent from ``points`` are left untouched, so this composes with the
    regular per-sync write (which finalizes today)."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    incoming: dict = {}
    for point_date, balance in points or ():
        if point_date is None or balance is None:
            continue
        incoming[_balance_history_local_date(point_date, user_tz)] = _decimal(balance)
    if not incoming:
        return counts
    existing_rows = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account_id,
            )
        )
    ).scalars().all()
    keeper_by_day: dict = {}
    for row in existing_rows:
        keeper_by_day.setdefault(_balance_history_local_date(row.date, user_tz), row)
    for local_day, balance in incoming.items():
        if balance is None:
            continue
        existing = keeper_by_day.get(local_day)
        if existing is not None:
            if abs((existing.balance or Decimal("0")) - balance) < Decimal("0.005"):
                counts["unchanged"] += 1
                continue
            existing.balance = balance
            counts["updated"] += 1
        else:
            db.add(
                BalanceHistory(
                    user_id=user_id,
                    account_id=account_id,
                    balance=balance,
                    date=local_day,
                )
            )
            counts["inserted"] += 1
    return counts


async def upsert_transactions_for_account(
    db: AsyncSession,
    *,
    user_id: int,
    account: Account,
    transactions: list,
    category_resolver: CategoryResolver | None = None,
    provider: str | None = None,
    counts: TransactionUpsertCounts | None = None,
) -> int:
    """Insert/update transactions for an account.

    Runs the full category resolution chain (user rules -> bundled rules ->
    type-mapping fallback) on every new transaction and on existing transactions
    whose `category_source` is null or `'auto'`/`'rule'`. Rows tagged
    `category_source='manual'` by a user PATCH are never overwritten.

    When ``counts`` is supplied, ``inserted`` records new ledger rows and
    ``existing`` records stable IDs that were deduplicated onto an existing row,
    even though provider-owned fields on that row are still refreshed.
    """
    from app.services.manual_accounts import reconcile_cash_mirror

    persisted = 0
    classification_cache: dict[int, str | None] = {}
    cash_category_id = (
        category_resolver.category_seed_key_to_id.get("cash")
        if category_resolver is not None
        else None
    )
    mirror_sources: list[Transaction] = []
    # Per-institution Cash cutoff: ATM/cash dated before this institution's connection date
    # stays an ordinary Withdrawal; only on/after it becomes wallet Cash. Looked up once and
    # passed into every resolve below (replaces the old global per-user cutoff).
    cash_cutoff = (
        await db.execute(
            select(Institution.cash_tracking_start).where(Institution.id == account.institution_id)
        )
    ).scalar_one_or_none()
    for tx in transactions:
        resolved_cat_id: int | None = None
        resolved_source: str | None = None
        if category_resolver is not None:
            resolved_cat_id, resolved_source = category_resolver.resolve(
                description=tx.description,
                amount=tx.amount,
                provider=provider,
                account_type=account.account_type,
                transaction_type=tx.type,
                mcc=tx.mcc,
                transaction_date=tx.date,
                cash_tracking_start=cash_cutoff,
            )

        existing = (
            await db.execute(
                select(Transaction).where(
                    Transaction.external_id == tx.external_id,
                    Transaction.user_id == user_id,
                    Transaction.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            if counts is not None:
                counts.existing += 1
            prev_category_id = existing.category_id
            existing.account_id = account.id
            existing.date = _calendar_date(tx.date)
            existing.type = tx.type
            existing.symbol = tx.symbol
            existing.description = tx.description
            existing.amount = _decimal(tx.amount)
            existing.currency = tx.currency
            existing.quantity = _decimal(tx.quantity)
            existing.price = _decimal(tx.price)
            existing.commission = _decimal(tx.commission)
            existing.mcc = tx.mcc
            existing.asset_category = tx.asset_category
            existing.realized_pnl = _decimal(tx.realized_pnl)
            existing.provider_recurring = tx.provider_recurring
            if existing.category_source != "manual" and resolved_cat_id is not None:
                existing.category_id = resolved_cat_id
                existing.category_source = resolved_source
            # A user's manual income/expense reclassification coerces the sign
            # (income +, expense −). The bank's raw amount was just re-asserted
            # above, so re-apply the flip here or this sync would revert it. Auto
            # rows keep the bank sign — only manual reclassifications flip.
            if existing.category_source == "manual" and existing.category_id is not None:
                if existing.raw_amount is None:
                    existing.raw_amount = existing.amount
                if existing.category_id not in classification_cache:
                    classification_cache[existing.category_id] = (
                        await db.execute(
                            select(Category.classification).where(
                                Category.id == existing.category_id
                            )
                        )
                    ).scalar_one_or_none()
                existing.amount = canonical_amount_for_classification(
                    existing.amount, classification_cache[existing.category_id]
                )
            if cash_category_id is not None and cash_category_id in (existing.category_id, prev_category_id):
                mirror_sources.append(existing)
            persisted += 1
            continue
        new_tx = Transaction(
            user_id=user_id,
            account_id=account.id,
            date=_calendar_date(tx.date),
            type=tx.type,
            symbol=tx.symbol,
            description=tx.description,
            amount=_decimal(tx.amount),
            currency=tx.currency,
            quantity=_decimal(tx.quantity),
            price=_decimal(tx.price),
            commission=_decimal(tx.commission),
            mcc=tx.mcc,
            asset_category=tx.asset_category,
            realized_pnl=_decimal(tx.realized_pnl),
            provider_recurring=tx.provider_recurring,
            external_id=tx.external_id,
            category_id=resolved_cat_id,
            category_source=resolved_source,
        )
        db.add(new_tx)
        if counts is not None:
            counts.inserted += 1
        if cash_category_id is not None and resolved_cat_id == cash_category_id:
            mirror_sources.append(new_tx)
        persisted += 1

    # Spawn/refresh/remove cash mirror legs for any synced row whose Cash
    # categorization changed (reconcile is idempotent and skips mirror legs).
    if mirror_sources:
        await db.flush()
        for source in mirror_sources:
            await reconcile_cash_mirror(db, user_id, source.id)
    return persisted


def _account_payload_items(mapping: dict[str, list[T]], acct: NormalizedAccount) -> list[T]:
    key = account_result_key(acct)
    if key in mapping:
        return mapping[key]
    return mapping.get(acct.name, [])


def _account_result_flagged(keys: set[str], acct: NormalizedAccount) -> bool:
    key = account_result_key(acct)
    return key in keys or acct.name in keys


def _profile_fallback_account_key(
    *,
    name: str | None,
    account_type: str | None,
    currency: str | None,
    is_liability: bool,
) -> str:
    return "|".join(
        (
            str(name or "").strip().lower(),
            str(account_type or "").strip().lower(),
            str(currency or "").strip().upper(),
            "1" if is_liability else "0",
        )
    )


def _result_account_profile_keys(acct: NormalizedAccount) -> tuple[set[str], str]:
    strong_keys = {
        str(external_id).strip()
        for external_id in (
            acct.external_id,
            *getattr(acct, "alternate_external_ids", ()),
        )
        if str(external_id or "").strip()
    }
    fallback_key = _profile_fallback_account_key(
        name=acct.name,
        account_type=acct.account_type,
        currency=acct.currency,
        is_liability=acct.is_liability,
    )
    return strong_keys, fallback_key


def _stored_account_profile_keys(acct: Account) -> tuple[set[str], str]:
    strong_keys = {str(acct.external_id).strip()} if str(acct.external_id or "").strip() else set()
    fallback_key = _profile_fallback_account_key(
        name=acct.name,
        account_type=acct.account_type,
        currency=acct.currency,
        is_liability=bool(acct.is_liability),
    )
    return strong_keys, fallback_key


async def detect_institution_profile_mismatch(
    db: AsyncSession,
    *,
    user_id: int,
    institution_id: int,
    accounts: list[NormalizedAccount],
) -> dict[str, object]:
    existing_accounts = (
        await db.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.institution_id == institution_id,
            )
        )
    ).scalars().all()
    if not existing_accounts or not accounts:
        return {
            "mismatch": False,
            "comparison_mode": "skipped",
            "existing_accounts": len(existing_accounts),
            "incoming_accounts": len(accounts),
            "overlap_count": 0,
        }

    existing_strong_keys: set[str] = set()
    incoming_strong_keys: set[str] = set()
    existing_fallback_keys: set[str] = set()
    incoming_fallback_keys: set[str] = set()

    for existing_account in existing_accounts:
        strong_keys, fallback_key = _stored_account_profile_keys(existing_account)
        existing_strong_keys.update(strong_keys)
        existing_fallback_keys.add(fallback_key)

    for account in accounts:
        strong_keys, fallback_key = _result_account_profile_keys(account)
        incoming_strong_keys.update(strong_keys)
        incoming_fallback_keys.add(fallback_key)

    use_strong_keys = bool(existing_strong_keys) and bool(incoming_strong_keys)
    overlap = (
        existing_strong_keys & incoming_strong_keys
        if use_strong_keys
        else existing_fallback_keys & incoming_fallback_keys
    )
    return {
        "mismatch": len(overlap) == 0,
        "comparison_mode": "strong" if use_strong_keys else "fallback",
        "existing_accounts": len(existing_accounts),
        "incoming_accounts": len(accounts),
        "overlap_count": len(overlap),
    }


async def persist_sync_result(
    db: AsyncSession,
    user_id: int,
    provider: str,
    provider_display_name: str,
    provider_type: str,  # "api" or "scraper"
    result: SyncResult,
    *,
    pending_add: bool = False,
    institution_id: int | None = None,
    persistence_metadata: dict[str, int] | None = None,
) -> list[Holding]:
    """Write a successful ``SyncResult`` into the database.

    Skips persistence entirely when result.status is not OK. The caller owns the
    transaction and must commit or roll back the flushed writes.
    """
    if result.status != SyncStatus.OK:
        return []

    now = datetime.now(timezone.utc)
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    category_resolver = await CategoryResolver.build(db, user_id)
    inserted_holding_rows: list[Holding] = []

    # Sync state remains connection-scoped, while each user may own only one synced
    # connection for a provider. Add retries may reuse their hidden pending row but
    # must never overwrite an already-confirmed connection.
    inst_row = None
    if institution_id is not None:
        inst_row = (
            await db.execute(
                select(Institution).where(
                    Institution.id == int(institution_id),
                    Institution.user_id == user_id,
                    Institution.provider == provider,
                )
            )
        ).scalar_one_or_none()
        if inst_row is None:
            raise ValueError("The selected institution connection does not belong to this provider.")
        if pending_add and (bool(inst_row.enabled) or not bool(inst_row.hidden)):
            raise ProviderAlreadyConnectedError(
                "This provider is already connected. Remove the existing institution before adding it again."
            )
    else:
        inst_row = (
            await db.execute(
                select(Institution)
                .where(
                    Institution.provider == provider,
                    Institution.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if inst_row is not None and pending_add and (bool(inst_row.enabled) or not bool(inst_row.hidden)):
            raise ProviderAlreadyConnectedError(
                "This provider is already connected. Remove the existing institution before adding it again."
            )

    if inst_row is None:
        inst_row = Institution(
            user_id=user_id,
            name=provider_display_name,
            type=provider_type,
            provider=provider,
            enabled=not pending_add,
            hidden=pending_add,
        )
        db.add(inst_row)
        await db.flush()
    else:
        inst_row.name = provider_display_name
        inst_row.type = provider_type

    if persistence_metadata is not None:
        persistence_metadata["institution_id"] = int(inst_row.id)

    # --- Accounts + holdings + balance history ---
    for acct in result.accounts:
        if not str(acct.external_id or "").strip():
            raise ValueError("Connector accounts require a stable provider external_id")
        acct_row = await _find_existing_account(
            db,
            user_id,
            inst_row.id,
            acct,
        )

        if not acct_row:
            acct_row = Account(
                user_id=user_id,
                institution_id=inst_row.id,
                external_id=acct.external_id,
                name=acct.name,
                account_type=acct.account_type,
                currency=acct.currency,
                is_liability=acct.is_liability,
            )
            db.add(acct_row)
            await db.flush()
        else:
            alternate_external_ids = tuple(
                external_id
                for external_id in getattr(acct, "alternate_external_ids", ())
                if external_id and external_id != acct.external_id
            )
            if acct.external_id and (
                not acct_row.external_id or acct_row.external_id in alternate_external_ids
            ):
                acct_row.external_id = acct.external_id
            # Never overwrite user-edited account name — only set on creation
            acct_row.account_type = acct.account_type
            acct_row.currency = acct.currency
            acct_row.is_liability = acct.is_liability
            # A live sync re-adopting a previously statement-imported account (same
            # external_id) makes it a real synced account again — clear the imported flag
            # so it stops measuring from its first imported point and rendering muted.
            if acct_row.is_imported:
                acct_row.is_imported = False

        if acct.holdings_authoritative:
            holdings = _account_payload_items(result.holdings_by_account, acct)
            await db.execute(
                delete(Holding).where(
                    Holding.account_id == acct_row.id,
                    Holding.user_id == user_id,
                )
            )
            for h in holdings:
                holding_row = Holding(
                    user_id=user_id,
                    account_id=acct_row.id,
                    symbol=h.symbol,
                    name=h.name,
                    quantity=_decimal(h.quantity),
                    market_value=_decimal(h.market_value),
                    average_cost=_decimal(h.average_cost),
                    last_price=_decimal(h.last_price),
                    contract_multiplier=_decimal(h.contract_multiplier),
                    change_pct=_decimal(h.change_pct),
                    daily_pnl=_decimal(h.daily_pnl),
                    currency=h.currency,
                    sector=None,
                    last_updated=now,
                )
                db.add(holding_row)
                inserted_holding_rows.append(holding_row)

        # Balance history. A daily series (e.g. IBKR Flex daily NAV) backfills history first,
        # then the regular per-sync write finalizes today's value.
        if acct.balance_history:
            await _upsert_balance_history_series(
                db,
                user_id=user_id,
                account_id=acct_row.id,
                points=acct.balance_history,
                user_tz=user_tz,
            )
        if acct.balance_authoritative and acct.balance is not None:
            await _upsert_daily_balance_history(
                db,
                user_id=user_id,
                account_id=acct_row.id,
                balance=acct.balance,
                now=now,
                user_tz=user_tz,
            )

        txns = _account_payload_items(result.transactions_by_account, acct)
        acct_row.last_synced = now

        persisted_txns = await upsert_transactions_for_account(
            db,
            user_id=user_id,
            account=acct_row,
            transactions=txns,
            category_resolver=category_resolver,
            provider=provider,
        )
        if _account_result_flagged(result.transaction_fetch_succeeded_accounts, acct):
            await record_successful_account_transaction_sync(
                db,
                user_id=user_id,
                provider=provider,
                account=acct_row,
                transaction_count=persisted_txns,
            )

    await db.flush()
    return inserted_holding_rows
