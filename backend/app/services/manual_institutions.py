"""User-created manual institutions and their accounts.

A *manual institution* is a user-owned container (``type="manual"``,
``provider=MANUAL_INSTITUTION_PROVIDER``) that the sync loop never touches — its
provider is not in the connector catalog, and the nightly scheduler only iterates
catalog providers. Each account carries a single native currency and is the only
valid target for generic CSV import (transactions/balances).

Distinct from the Cash holder (``provider="manual"``), which is a per-user
singleton with its own manual-entry UX and is never an import target. User manual
institutions therefore use a different provider so they don't collide with the
Cash singleton lookup in ``services.manual_accounts.ensure_manual_institution``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BalanceHistory, Institution, Transaction
from app.services import csv_io
from app.services.user_utils import get_user_timezone_info_from_db

# User-created manual institutions. NOT "manual" (reserved for the singleton Cash
# holder) and NOT a connector-catalog provider, so sync never runs against them.
MANUAL_INSTITUTION_PROVIDER = "manual_custom"
MANUAL_INSTITUTION_TYPE = "manual"

# Account types treated as liabilities (debt owed). Mirrors the importer's mapping
# and the Debt-bucket sub-types (`personal_loan`/`other_debt`), so re-deriving
# is_liability after a type edit keeps a debt a liability (never flips it to an asset).
LIABILITY_ACCOUNT_TYPES = {
    "credit_card", "credit", "loc", "line_of_credit", "heloc",
    "loan", "mortgage", "student_loan", "auto_loan",
    "personal_loan", "other_debt",
}


def _derive_is_liability(account_type: str | None, explicit) -> bool:
    if isinstance(explicit, bool):
        return explicit
    return (account_type or "").strip().lower() in LIABILITY_ACCOUNT_TYPES


def _serialize_account(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "account_type": account.account_type,
        "currency": account.currency,
        "is_liability": account.is_liability,
        "opening_balance": account.opening_balance,
    }


def _parse_opening_balance(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def recompute_manual_account_balance(db: AsyncSession, user_id: int, account: Account) -> None:
    """Derive a manual account's ``balance_history`` from its ``opening_balance`` anchor
    plus transactions: ``balance = opening_balance ± cumulative`` (− for liabilities,
    where an outflow raises what's owed). With transactions this owns the account's
    balance_history (regenerated end-of-day per transaction date + an opening anchor);
    with none, the opening balance is the value itself (a single upserted point — the
    tangible-asset case). No-op only when ``opening_balance`` is unset AND there are no
    transactions (balance then comes from imported snapshots); a transaction-backed
    account with no opening derives from 0. Caller commits."""
    sign = -1 if account.is_liability else 1
    user_tz = await get_user_timezone_info_from_db(db, user_id)

    txns = (
        await db.execute(
            select(Transaction.date, Transaction.amount)
            .where(Transaction.user_id == user_id, Transaction.account_id == account.id)
            .order_by(Transaction.date)
        )
    ).all()
    # A transaction-backed account always derives a running balance from its transactions
    # (starting at 0 if its opening point was deleted, which clears opening_balance); only a
    # snapshot-only account — no opening AND no transactions — is left to imported snapshots.
    if account.opening_balance is None and not txns:
        return
    opening = account.opening_balance if account.opening_balance is not None else Decimal("0")

    def day_anchor(value):
        if isinstance(value, datetime):
            return value.astimezone(user_tz).date() if value.tzinfo else value.date()
        return value

    if not txns:
        # Single value point (asset / no-history): upsert the opening day, leave others.
        opening_dt = day_anchor(account.opening_balance_date or datetime.now(timezone.utc))
        opening_day = opening_dt.isoformat()
        existing = (
            await db.execute(
                select(BalanceHistory).where(
                    BalanceHistory.user_id == user_id, BalanceHistory.account_id == account.id
                )
            )
        ).scalars().all()
        match = next(
            (b for b in existing if b.date.isoformat() == opening_day), None
        )
        if match is not None:
            match.balance = opening
            match.date = opening_dt
        else:
            db.add(BalanceHistory(user_id=user_id, account_id=account.id, balance=opening, date=opening_dt))
        return

    # Derive mode: this fully owns the account's balance_history.
    await db.execute(
        delete(BalanceHistory).where(
            BalanceHistory.user_id == user_id, BalanceHistory.account_id == account.id
        )
    )
    # Imported transactions dated before the declared opening/borrow date mean the
    # account clearly existed then — move the opening anchor to the earliest
    # transaction AND persist it, so the "Opened"/"Borrowed" point, the revaluation
    # floor, and the balance timeline all agree instead of stranding a stale date.
    opening_dt = account.opening_balance_date
    moved_anchor = opening_dt is None or opening_dt > txns[0].date
    if moved_anchor:
        opening_dt = txns[0].date
    anchor = day_anchor(opening_dt)
    if moved_anchor:
        account.opening_balance_date = anchor
    # local day -> (datetime, end-of-day balance); seed with the opening anchor.
    day_balance: dict[str, tuple] = {anchor.isoformat(): (anchor, opening)}
    running = opening
    for row in txns:
        running += Decimal(sign) * (row.amount or Decimal("0"))
        dt = day_anchor(row.date)
        day_balance[dt.isoformat()] = (dt, running)
    for dt, bal in day_balance.values():
        db.add(BalanceHistory(user_id=user_id, account_id=account.id, balance=bal, date=dt))


async def is_manual_institution_account(db: AsyncSession, account: Account) -> bool:
    """True if ``account`` belongs to a user-created manual institution
    (``provider='manual_custom'``) — the valid target, besides the wallet Cash holder,
    for manually entered transactions and CSV import."""
    provider = (
        await db.execute(
            select(Institution.provider).where(Institution.id == account.institution_id)
        )
    ).scalar_one_or_none()
    return provider == MANUAL_INSTITUTION_PROVIDER


async def set_account_value_as_of(
    db: AsyncSession,
    user_id: int,
    account: Account,
    value: float,
    *,
    as_of: datetime | None = None,
) -> None:
    """Revaluation door: upsert a single ``balance_history`` point for a manual
    account at ``as_of`` (default: now), WITHOUT touching transactions or the
    opening-balance anchor — the latest such point is the account's current value
    (asset) or amount owed (debt). For tangible assets whose worth changes by
    re-appraisal rather than money movement, and for manual debts. Upsert is keyed
    by the owner's local calendar day (re-valuing the same day overwrites). Caller commits."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    when = as_of or datetime.now(timezone.utc)
    day_dt = csv_io.parse_csv_date_to_utc(csv_io.serialize_local_date(when, user_tz), user_tz)
    local_day = csv_io.serialize_local_date(day_dt, user_tz)
    balance_date = date.fromisoformat(local_day)
    existing = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account.id,
            )
        )
    ).scalars().all()
    match = next(
        (row for row in existing if csv_io.serialize_local_date(row.date, user_tz) == local_day),
        None,
    )
    if match is not None:
        match.balance = Decimal(str(value))
        match.date = balance_date
    else:
        db.add(BalanceHistory(
            user_id=user_id,
            account_id=account.id,
            balance=Decimal(str(value)),
            date=balance_date,
        ))


async def delete_account_value_at(
    db: AsyncSession, user_id: int, account: Account, when: datetime, *, only_if_value=None
) -> bool:
    """Delete the ``balance_history`` point(s) on ``when``'s local calendar day for a
    manual account, if any exist. When ``only_if_value`` is given, a day's point is removed
    only if its balance still equals that amount (within a cent) — so re-dating a purchase
    drops the old purchase point but leaves an intervening revaluation, or a real
    current-value point that merely shares the day, untouched. Caller commits."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    target_day = csv_io.serialize_local_date(when, user_tz)
    existing = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account.id,
            )
        )
    ).scalars().all()
    removed = False
    for row in existing:
        if csv_io.serialize_local_date(row.date, user_tz) != target_day:
            continue
        if only_if_value is not None and abs(float(row.balance) - float(only_if_value)) > 0.01:
            continue
        await db.delete(row)
        removed = True
    return removed


async def get_account_value_history(
    db: AsyncSession, user_id: int, account: Account
) -> list[dict]:
    """Return a manual account's full value timeline as ``[{id, value, date, is_purchase}]``
    sorted oldest→newest. ``is_purchase`` flags the point sitting on the account's recorded
    purchase date so the UI can label the cost-basis row."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    rows = (
        await db.execute(
            select(BalanceHistory)
            .where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account.id,
            )
            .order_by(BalanceHistory.date)
        )
    ).scalars().all()
    purchase_day = (
        csv_io.serialize_local_date(account.purchase_date, user_tz)
        if account.purchase_date is not None else None
    )
    opening_day = (
        csv_io.serialize_local_date(account.opening_balance_date, user_tz)
        if account.opening_balance_date is not None else None
    )
    return [
        {
            "id": row.id,
            "value": row.balance,
            "date": row.date.isoformat(),
            "is_purchase": (
                purchase_day is not None
                and csv_io.serialize_local_date(row.date, user_tz) == purchase_day
            ),
            "is_opening": (
                opening_day is not None
                and csv_io.serialize_local_date(row.date, user_tz) == opening_day
            ),
        }
        for row in rows
    ]


async def delete_account_value_point(
    db: AsyncSession, user_id: int, account: Account, point_id: int
) -> bool:
    """Delete one ``balance_history`` point by id from a manual account's value timeline.
    If the removed point was the account's purchase point, the stored cost basis
    (``purchase_value``/``purchase_date``) is cleared so the two stay consistent. Returns
    False when the point isn't found/owned. Caller commits."""
    row = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.id == point_id,
                BalanceHistory.account_id == account.id,
                BalanceHistory.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    row_day = csv_io.serialize_local_date(row.date, user_tz)
    # Deleting the origin point clears its stored anchor so the two stay consistent: the
    # purchase cost basis (buckets) or the opening balance (manual accounts).
    if account.purchase_date is not None and row_day == csv_io.serialize_local_date(account.purchase_date, user_tz):
        account.purchase_value = None
        account.purchase_date = None
    if account.opening_balance_date is not None and row_day == csv_io.serialize_local_date(account.opening_balance_date, user_tz):
        account.opening_balance = None
        account.opening_balance_date = None
    await db.delete(row)
    await db.flush()
    # If that was the LAST point, clear every origin anchor so the account reads truly empty —
    # no stale purchase/opening figure lingering with no value timeline behind it (covers the
    # case where the opening point had no explicit date to match above).
    remaining = (
        await db.execute(
            select(BalanceHistory.id)
            .where(BalanceHistory.user_id == user_id, BalanceHistory.account_id == account.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if remaining is None:
        account.purchase_value = None
        account.purchase_date = None
        account.opening_balance = None
        account.opening_balance_date = None
    return True


async def set_account_opening_balance(
    db: AsyncSession, user_id: int, account_id: int, *, opening_balance, opening_balance_date=None
) -> dict:
    """Set a manual account's starting balance and recompute its derived balance.
    Returns the updated account or an error dict. Caller commits."""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == user_id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if (institution.provider or "") != MANUAL_INSTITUTION_PROVIDER:
        return {"status": "error", "message": "Only manual accounts have a starting balance"}
    old_opening_value = account.opening_balance
    old_opening_date = account.opening_balance_date
    opening_date_moved = False
    if opening_balance_date:
        user_tz = await get_user_timezone_info_from_db(db, user_id)
        new_opening_dt = csv_io.parse_csv_date_to_utc(opening_balance_date, user_tz)
        # Symmetric origin floor: the opening must be the earliest point — reject an opening date
        # later than an already-recorded (non-opening) value (mirrors the bucket purchase guard).
        # Excludes the current opening point (being moved), flagged via is_opening.
        earliest_other = None
        for point in await get_account_value_history(db, user_id, account):
            if point.get("is_opening"):
                continue
            try:
                point_day = datetime.fromisoformat(str(point["date"]).replace("Z", "")).date()
            except ValueError:
                continue
            if earliest_other is None or point_day < earliest_other:
                earliest_other = point_day
        # The opening also can't sit after any transaction — a transaction-backed account
        # demonstrably existed by its earliest transaction. (The is_opening exclusion above
        # hides that earliest point once the anchor already sits on the earliest txn day, so
        # without this the floor wrongly reported the SECOND-earliest date.)
        earliest_txn = (
            await db.execute(
                select(func.min(Transaction.date)).where(
                    Transaction.user_id == user_id, Transaction.account_id == account.id
                )
            )
        ).scalar()
        if earliest_txn is not None and (earliest_other is None or earliest_txn < earliest_other):
            earliest_other = earliest_txn
        if earliest_other is not None and new_opening_dt.date() > earliest_other:
            return {
                "status": "error",
                "message": (
                    f"Opening date can't be after a recorded value ({earliest_other.strftime('%b %d, %Y')}). "
                    "Pick an earlier opening date."
                ),
            }
        opening_date_moved = (
            old_opening_date is not None
            and csv_io.serialize_local_date(old_opening_date, user_tz)
            != csv_io.serialize_local_date(new_opening_dt, user_tz)
        )
        account.opening_balance_date = new_opening_dt
    account.opening_balance = _parse_opening_balance(opening_balance)
    if account.opening_balance is None:
        await db.execute(
            delete(BalanceHistory).where(
                BalanceHistory.user_id == user_id, BalanceHistory.account_id == account.id
            )
        )
    else:
        # If the opening date moved, drop the OLD opening point (value-guarded) so it doesn't
        # linger as a phantom earlier than the new one — recompute upserts only the new day and
        # leaves other points. Mirrors the bucket purchase re-date (delete_account_value_at).
        if opening_date_moved:
            await delete_account_value_at(db, user_id, account, old_opening_date, only_if_value=old_opening_value)
        await recompute_manual_account_balance(db, user_id, account)
    return {"status": "ok", "account": _serialize_account(account)}


def _clean_manual_account_entries(
    accounts: list[dict], *, existing_names: set[str] | None = None
) -> tuple[list[dict] | None, str | None]:
    """Validate + normalize manual-account entries before any write, so a bad request
    never leaves a partial institution. ``existing_names`` (lowercased) are names
    already on the institution, used to reject duplicates when appending. Returns
    ``(cleaned, None)`` or ``(None, error_message)``."""
    if not accounts:
        return None, "Add at least one account"
    seen_names: set[str] = set(existing_names or ())
    cleaned: list[dict] = []
    for raw in accounts:
        raw = raw or {}
        acct_name = str(raw.get("name", "")).strip()
        if not acct_name:
            return None, "Each account needs a name"
        if acct_name.lower() in seen_names:
            return None, f"Duplicate account name: {acct_name}"
        seen_names.add(acct_name.lower())
        account_type = str(raw.get("account_type", "")).strip().lower() or None
        raw_opening_date = raw.get("opening_balance_date")
        cleaned.append({
            "name": acct_name,
            "account_type": account_type,
            "currency": csv_io.normalize_currency(str(raw.get("currency", ""))) or "CAD",
            "is_liability": _derive_is_liability(account_type, raw.get("is_liability")),
            "opening_balance": _parse_opening_balance(raw.get("opening_balance")),
            # Optional opening/borrowed date — anchors the opening balance at the account's
            # real start (default today). Parsed (needs the user tz) in _create_manual_accounts.
            "opening_balance_date": (str(raw_opening_date).strip() or None) if raw_opening_date not in (None, "") else None,
        })
    return cleaned, None


async def _create_manual_accounts(
    db: AsyncSession, user_id: int, institution_id: int, cleaned: list[dict]
) -> list[Account]:
    """Create validated ``cleaned`` account entries inside ``institution_id`` and seed
    each opening balance as a value point (no transactions yet; a later import
    recomputes it into a running series). Caller commits."""
    created: list[Account] = []
    user_tz = None
    for entry in cleaned:
        account = Account(
            user_id=user_id,
            institution_id=institution_id,
            external_id=None,
            name=entry["name"],
            account_type=entry["account_type"],
            currency=entry["currency"],
            is_liability=entry["is_liability"],
            opening_balance=entry["opening_balance"],
            hidden=False,
        )
        db.add(account)
        await db.flush()
        if entry.get("opening_balance_date"):
            if user_tz is None:
                user_tz = await get_user_timezone_info_from_db(db, user_id)
            account.opening_balance_date = csv_io.parse_csv_date_to_utc(entry["opening_balance_date"], user_tz)
        created.append(account)
    for account in created:
        if account.opening_balance is not None:
            await recompute_manual_account_balance(db, user_id, account)
    return created


async def create_manual_institution(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    accounts: list[dict],
    category: str | None = None,
) -> dict:
    """Create a manual institution and its accounts (each with one currency).

    Validates entirely before writing, so a bad request never leaves a partial
    institution. Returns ``{"status": "ok", "institution": {...}, "accounts":
    [...]}`` or ``{"status": "error", "message": ...}``. The caller commits.
    """
    name = (name or "").strip()
    if not name:
        return {"status": "error", "message": "Institution name is required"}
    cleaned, error = _clean_manual_account_entries(accounts)
    if error:
        return {"status": "error", "message": error}

    institution = Institution(
        user_id=user_id,
        name=name,
        type=MANUAL_INSTITUTION_TYPE,
        provider=MANUAL_INSTITUTION_PROVIDER,
        category=(str(category).strip() or None) if category else None,
        enabled=True,
        hidden=False,
        sync_status="ok",
    )
    db.add(institution)
    await db.flush()

    created = await _create_manual_accounts(db, user_id, institution.id, cleaned)

    return {
        "status": "ok",
        "institution": {
            "id": institution.id,
            "name": institution.name,
            "type": institution.type,
            "provider": institution.provider,
        },
        "accounts": [_serialize_account(a) for a in created],
    }


async def add_accounts_to_manual_institution(
    db: AsyncSession, user_id: int, institution_id: int, accounts: list[dict]
) -> dict:
    """Append accounts to an existing user manual institution (provider
    ``manual_custom``). Owner-scoped and manual-only — synced and asset-group
    institutions are rejected. Account names must be unique within the institution.
    Returns ``{"status": "ok", "accounts": [...]}`` or an error dict. Caller commits."""
    institution = (
        await db.execute(
            select(Institution).where(
                Institution.id == institution_id,
                Institution.user_id == user_id,
                Institution.provider == MANUAL_INSTITUTION_PROVIDER,
            )
        )
    ).scalar_one_or_none()
    if institution is None:
        return {"status": "error", "message": "Manual institution not found"}

    existing = (
        await db.execute(
            select(Account.name).where(
                Account.institution_id == institution_id,
                Account.user_id == user_id,
            )
        )
    ).scalars().all()
    existing_names = {str(n).strip().lower() for n in existing if n}

    cleaned, error = _clean_manual_account_entries(accounts, existing_names=existing_names)
    if error:
        return {"status": "error", "message": error}

    created = await _create_manual_accounts(db, user_id, institution.id, cleaned)
    return {"status": "ok", "accounts": [_serialize_account(a) for a in created]}
