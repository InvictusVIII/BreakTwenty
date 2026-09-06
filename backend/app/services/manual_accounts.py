"""Manual (connector-less) cash holder provisioning and balance maintenance.

BreakTwenty tracks user cash via a virtual "Manual" institution (``provider='manual'``)
that the sync loop never touches. Cash is multi-currency, but an ``Account`` is
single-currency and net worth FX-sums one balance per account, so the holder is a
set of sibling ``account_type='cash'`` accounts — one per currency — surfaced to
the user only through the Accounts-page Cash panel, never as a managed account.

Balances are derived: a cash account's balance is the running sum of its
transactions, written into ``balance_history`` on every mutation so the existing
net-worth path (latest balance per account) picks it up unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BalanceHistory, Category, Institution, Transaction

MANUAL_PROVIDER = "manual"
MANUAL_INSTITUTION_NAME = "Cash"
MANUAL_INSTITUTION_TYPE = "manual"
CASH_ACCOUNT_TYPE = "cash"
# Deterministic external_id prefix for the auto-spawned counter-leg of a bank
# withdrawal categorized as Cash, e.g. `cash_mirror:rbc_abc123`.
CASH_MIRROR_PREFIX = "cash_mirror:"
# User-created manual transaction, and the onboarding opening-balance row.
MANUAL_TX_PREFIX = "manual:"
CASH_OPENING_PREFIX = "cash_opening:"
CASH_SEED_KEY = "cash"


def manual_kind_for_external_id(external_id: str | None) -> str | None:
    """Classify a transaction by its external_id prefix for UI/policy decisions:
    ``manual`` (user-created, editable+deletable), ``cash_opening`` (onboarding
    balance: category-locked, deletable), ``cash_mirror`` (ATM counter-leg:
    category-locked, removed by un-tagging its source), or None (bank/auto)."""
    ext = external_id or ""
    if ext.startswith(MANUAL_TX_PREFIX):
        return "manual"
    if ext.startswith(CASH_OPENING_PREFIX):
        return "cash_opening"
    if ext.startswith(CASH_MIRROR_PREFIX):
        return "cash_mirror"
    return None


def cash_account_external_id(currency: str) -> str:
    return f"cash:{(currency or 'CAD').upper()}"


def is_wallet_cash_account(account: Account) -> bool:
    """True if ``account`` is the connector-less wallet Cash holder (the
    ``provider='manual'`` singleton), identified by its reserved ``cash:<CCY>``
    external_id. Manual-institution cash sleeves (``account_type='cash'`` with a
    different/empty external_id) and synced cash accounts are excluded, so manual
    cash transactions can only ever land on the wallet."""
    return account.account_type == CASH_ACCOUNT_TYPE and (account.external_id or "").startswith("cash:")


async def ensure_manual_institution(db: AsyncSession, user_id: int) -> Institution:
    """Idempotently return the user's virtual Manual institution.

    Kept ``enabled=True``/``hidden=False`` so its transactions flow into cash-flow
    and its accounts count toward net worth; ``provider='manual'`` keeps the sync
    loop from ever treating it as a connector.
    """
    institution = (
        await db.execute(
            select(Institution).where(
                Institution.user_id == user_id,
                Institution.provider == MANUAL_PROVIDER,
            )
        )
    ).scalar_one_or_none()
    if institution is None:
        institution = Institution(
            user_id=user_id,
            name=MANUAL_INSTITUTION_NAME,
            type=MANUAL_INSTITUTION_TYPE,
            provider=MANUAL_PROVIDER,
            enabled=True,
            hidden=False,
        )
        db.add(institution)
        await db.flush()
    elif institution.name != MANUAL_INSTITUTION_NAME:
        # Idempotently migrate a previously-seeded display name (e.g. "Manual").
        institution.name = MANUAL_INSTITUTION_NAME
    return institution


async def get_or_create_cash_account(
    db: AsyncSession, user_id: int, currency: str
) -> Account:
    """Idempotently return the user's ``Cash (<currency>)`` holder account."""
    currency = (currency or "CAD").upper()
    institution = await ensure_manual_institution(db, user_id)
    external_id = cash_account_external_id(currency)
    account = (
        await db.execute(
            select(Account).where(
                Account.user_id == user_id,
                Account.institution_id == institution.id,
                Account.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        account = Account(
            user_id=user_id,
            institution_id=institution.id,
            external_id=external_id,
            name=f"Cash ({currency})",
            account_type=CASH_ACCOUNT_TYPE,
            currency=currency,
            is_liability=False,
        )
        db.add(account)
        await db.flush()
    return account


async def recompute_cash_account_balance(
    db: AsyncSession, user_id: int, account_id: int
) -> float:
    """Recompute a cash account's balance from its transactions and upsert today's
    ``balance_history`` row so net worth reflects it. If no transactions remain,
    the empty cash holder is pruned (so a currency disappears from the Cash panel
    once its last entry is removed; it's re-created on demand). Returns the new
    balance."""
    count_total = (
        await db.execute(
            select(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0.0),
            ).where(
                Transaction.user_id == user_id,
                Transaction.account_id == account_id,
            )
        )
    ).first()
    tx_count = count_total[0] if count_total else 0
    total = (count_total[1] if count_total else 0.0) or 0.0

    if tx_count == 0:
        account = await db.get(Account, account_id)
        if account is not None and account.user_id == user_id and account.account_type == CASH_ACCOUNT_TYPE:
            await db.execute(
                delete(BalanceHistory).where(
                    BalanceHistory.account_id == account_id,
                    BalanceHistory.user_id == user_id,
                )
            )
            await db.delete(account)
            await db.flush()
        return 0.0

    now = datetime.now(timezone.utc)
    latest = (
        await db.execute(
            select(BalanceHistory)
            .where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account_id,
            )
            .order_by(BalanceHistory.date.desc(), BalanceHistory.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if latest is not None and latest.date == now.date():
        latest.balance = total
        latest.date = now.date()
    else:
        db.add(
            BalanceHistory(
                user_id=user_id,
                account_id=account_id,
                balance=total,
                date=now.date(),
            )
        )
    return float(total)


async def prune_empty_wallet_cash_accounts(db: AsyncSession, user_id: int) -> None:
    """Delete wallet Cash holder accounts that carry no transactions, and drop the
    virtual Cash institution itself once it holds no accounts. The Cash holder is
    provisioned lazily on first cash activity, so a vestigial empty holder left by
    earlier eager provisioning is pruned on the next launch (re-created on demand) —
    keeping it out of the account list and institution scope until there's real cash.
    Idempotent; accounts that have transactions are left untouched."""
    institution = (
        await db.execute(
            select(Institution).where(
                Institution.user_id == user_id,
                Institution.provider == MANUAL_PROVIDER,
            )
        )
    ).scalar_one_or_none()
    if institution is None:
        return
    account_ids = (
        await db.execute(
            select(Account.id).where(
                Account.user_id == user_id,
                Account.institution_id == institution.id,
                Account.account_type == CASH_ACCOUNT_TYPE,
            )
        )
    ).scalars().all()
    for account_id in account_ids:
        tx_count = (
            await db.execute(
                select(func.count(Transaction.id)).where(
                    Transaction.user_id == user_id,
                    Transaction.account_id == account_id,
                )
            )
        ).scalar() or 0
        if tx_count == 0:
            await recompute_cash_account_balance(db, user_id, account_id)
    remaining = (
        await db.execute(
            select(func.count(Account.id)).where(
                Account.user_id == user_id,
                Account.institution_id == institution.id,
            )
        )
    ).scalar() or 0
    if remaining == 0:
        await db.delete(institution)
        await db.flush()


async def set_cash_opening_balance(
    db: AsyncSession,
    user_id: int,
    account: Account,
    amount: float,
    as_of: datetime | None = None,
) -> float:
    """Set (or replace) a cash account's opening/starting balance — the cash held before
    any logged transaction — as the neutral ``cash_opening:<CCY>`` transaction, then
    recompute the balance. Mirrors the onboarding seed path so the two stay consistent.
    Returns the new balance. Caller commits."""
    from app.services.categories import build_category_seed_key_to_id_map

    currency = (account.currency or "CAD").upper()
    external_id = f"{CASH_OPENING_PREFIX}{currency}"
    cash_category_id = (await build_category_seed_key_to_id_map(db, user_id)).get(CASH_SEED_KEY)
    when = as_of or datetime.now(timezone.utc)
    amount_val = abs(Decimal(str(amount)))
    existing = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.account_id == account.id,
                Transaction.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            Transaction(
                user_id=user_id,
                account_id=account.id,
                date=when.date(),
                type="transfer",
                description="Opening cash balance",
                amount=amount_val,
                currency=currency,
                external_id=external_id,
                category_id=cash_category_id,
                category_source="manual",  # never re-evaluated/overwritten
            )
        )
    else:
        existing.account_id = account.id
        existing.amount = amount_val
        existing.date = when.date()
        existing.currency = currency
        existing.category_id = cash_category_id
    await db.flush()
    return await recompute_cash_account_balance(db, user_id, account.id)


async def reconcile_cash_mirror(
    db: AsyncSession, user_id: int, source_tx_id: int
) -> set[int]:
    """Keep a bank cash move's counter-leg in sync with its categorization.

    When a non-cash-account transaction is categorized **Cash**, a deterministic
    mirror leg (`external_id='cash_mirror:<source external_id>'`) is upserted on the
    matching per-currency cash holder with the **negated** source amount on the
    same date: a withdrawal (−X from the bank) becomes +X cash you now hold; a
    cash deposit (+X to the bank) becomes −X cash leaving your wallet (which may
    drive the cash balance negative when the cash came from an untracked source —
    that's intentional and surfaced in the UI). If the transaction is no longer
    Cash, any existing mirror is removed. Idempotent and reversible. Returns the
    cash account ids whose balance was recomputed.
    """
    row = (
        await db.execute(
            select(Transaction, Account.account_type, Category.seed_key)
            .join(Account, Account.id == Transaction.account_id)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(Transaction.id == source_tx_id, Transaction.user_id == user_id)
        )
    ).first()
    if row is None:
        return set()
    source, account_type, seed_key = row
    # Never mirror a mirror leg (it lives on the cash account itself).
    if (source.external_id or "").startswith(CASH_MIRROR_PREFIX):
        return set()

    mirror_external_id = f"{CASH_MIRROR_PREFIX}{source.id}"
    existing = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.external_id == mirror_external_id,
            )
        )
    ).scalar_one_or_none()

    should_mirror = seed_key == CASH_SEED_KEY and account_type != CASH_ACCOUNT_TYPE
    touched: set[int] = set()
    if should_mirror:
        cash_account = await get_or_create_cash_account(db, user_id, source.currency)
        # Negated: withdrawal (−X) → +X cash; cash deposit (+X) → −X cash.
        mirror_amount = -(source.amount or Decimal("0"))
        if existing is None:
            db.add(
                Transaction(
                    user_id=user_id,
                    account_id=cash_account.id,
                    date=source.date,
                    type="transfer",
                    description="Cash",
                    amount=mirror_amount,
                    currency=source.currency,
                    external_id=mirror_external_id,
                    category_id=source.category_id,
                    category_source="auto",
                )
            )
        else:
            touched.add(existing.account_id)
            existing.account_id = cash_account.id
            existing.date = source.date
            existing.amount = mirror_amount
            existing.currency = source.currency
            existing.category_id = source.category_id
        touched.add(cash_account.id)
        await db.flush()
    elif existing is not None:
        touched.add(existing.account_id)
        await db.delete(existing)
        await db.flush()

    for account_id in touched:
        await recompute_cash_account_balance(db, user_id, account_id)
    return touched


async def revert_cash_mirror_sources(
    db: AsyncSession, user_id: int, source_transaction_ids: list[int]
) -> int:
    """Make Cash-holder deletion durable. When a wallet Cash account is deleted its
    ``cash_mirror:<src>`` legs go with it, but the originating bank rows stay tagged
    **Cash** — so a resync would rebuild the mirror (and the holder). Revert those
    source rows to their plain Withdrawal/Deposit category and pin them
    ``category_source='manual'`` so re-evaluation and future syncs leave them be.
    Returns the number of rows reverted. Idempotent."""
    if not source_transaction_ids:
        return 0
    from app.services.categories import (
        build_category_seed_key_to_id_map,
        build_type_to_category_id_map,
        canonical_amount_for_classification,
    )

    seed_map = await build_category_seed_key_to_id_map(db, user_id)
    type_map = await build_type_to_category_id_map(db, user_id)
    cash_id = seed_map.get(CASH_SEED_KEY)
    withdrawal_id = seed_map.get("withdrawal")
    deposit_id = seed_map.get("deposit")
    target_ids = {cid for cid in (withdrawal_id, deposit_id, *type_map.values()) if cid is not None}
    classifications: dict[int, str | None] = {}
    if target_ids:
        for cid, cls in (
            await db.execute(
                select(Category.id, Category.classification).where(Category.id.in_(target_ids))
            )
        ).all():
            classifications[cid] = cls

    sources = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.id.in_(source_transaction_ids),
            )
        )
    ).scalars().all()
    reverted = 0
    for source in sources:
        # Only touch rows still tagged Cash (the user may have since recategorized one).
        if cash_id is not None and source.category_id != cash_id:
            continue
        target = type_map.get(source.type)
        if target is None:
            target = withdrawal_id if (source.amount or 0) < 0 else deposit_id
        if target is None:
            continue
        source.category_id = target
        source.category_source = "manual"
        source.amount = canonical_amount_for_classification(
            source.amount, classifications.get(target), source.type
        )
        reverted += 1
    await db.flush()
    return reverted


async def cleanup_orphaned_cash_mirrors(db: AsyncSession, user_id: int) -> set[int]:
    """Delete cash mirror legs whose source transaction no longer exists (e.g. the
    bank account holding the source was deleted) and recompute the affected cash
    balances. Returns the cash account ids touched. Idempotent."""
    mirrors = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id,
                Transaction.external_id.like(f"{CASH_MIRROR_PREFIX}%"),
            )
        )
    ).scalars().all()
    if not mirrors:
        return set()

    source_ids = {
        int(source_id)
        for mirror in mirrors
        for source_id in [mirror.external_id[len(CASH_MIRROR_PREFIX):]]
        if source_id.isdigit()
    }
    existing_sources = set(
        (
            await db.execute(
                select(Transaction.id).where(
                    Transaction.user_id == user_id,
                    Transaction.id.in_(source_ids),
                )
            )
        ).scalars().all()
    )

    touched: set[int] = set()
    for mirror in mirrors:
        source_id = mirror.external_id[len(CASH_MIRROR_PREFIX):]
        if not source_id.isdigit() or int(source_id) not in existing_sources:
            if mirror.account_id is not None:
                touched.add(mirror.account_id)
            await db.delete(mirror)
    if touched:
        await db.flush()
        for account_id in touched:
            await recompute_cash_account_balance(db, user_id, account_id)
    return touched
