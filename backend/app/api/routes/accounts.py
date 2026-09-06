from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.models import Account, BalanceHistory, Institution, Transaction
from app.services.asset_groups import ASSET_GROUPS, create_group_account, is_asset_group_provider
from app.services.currency import get_fx_rates
from app.services.institution_cleanup import delete_account_and_maybe_institution
from app.services.manual_accounts import (
    CASH_ACCOUNT_TYPE,
    CASH_MIRROR_PREFIX,
    MANUAL_PROVIDER,
    get_or_create_cash_account,
    is_wallet_cash_account,
    revert_cash_mirror_sources,
    set_cash_opening_balance,
)
from app.services.manual_institutions import (
    MANUAL_INSTITUTION_PROVIDER,
    _derive_is_liability,
    delete_account_value_at,
    delete_account_value_point,
    get_account_value_history,
    set_account_opening_balance,
    set_account_value_as_of,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.networth import (
    get_account_balance_history_payload,
    get_accounts_payload,
    get_holdings_payload,
    get_net_worth_payload,
)
from app.services.transaction_import import get_transaction_import_status_payload

router = APIRouter()


@router.get("/accounts")
async def get_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_accounts_payload(db, current_user.id)


@router.get("/accounts/{account_id}/holdings")
async def get_holdings(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_holdings_payload(db, current_user.id, account_id)


@router.get("/networth")
async def get_net_worth(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_net_worth_payload(db, current_user.id)


async def _convert_account_currency(
    db: AsyncSession, user_id: int, account: Account, old_currency: str, new_currency: str
) -> None:
    """Restate a manual/asset account's value timeline (plus opening-balance anchor and
    purchase cost basis) from ``old_currency`` to ``new_currency`` at the current
    cross-rate, so changing the currency converts the worth (a 200k CAD asset becomes
    ~2.26 BTC) instead of relabeling the bare number. A missing rate leaves the values
    untouched rather than zeroing them. Caller commits."""
    rates = await get_fx_rates("CAD")  # rates[X] = units of X per 1 CAD
    r_old = 1.0 if old_currency == "CAD" else rates.get(old_currency)
    r_new = 1.0 if new_currency == "CAD" else rates.get(new_currency)
    if not r_old or not r_new or r_old <= 0 or r_new <= 0:
        return
    factor = float(r_new) / float(r_old)
    if factor == 1.0:
        return
    points = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.account_id == account.id,
                BalanceHistory.user_id == user_id,
            )
        )
    ).scalars().all()
    for point in points:
        point.balance = float(point.balance) * factor
    if account.opening_balance is not None:
        account.opening_balance = float(account.opening_balance) * factor
    if account.purchase_value is not None:
        account.purchase_value = float(account.purchase_value) * factor


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    # ``name`` is editable on any owned account (inline rename). ``currency`` /
    # ``account_type`` / ``is_liability`` are editable only on connector-less manual or
    # asset-group accounts — synced accounts are provider-authoritative. A currency or
    # liability change reinterprets the account's net-worth contribution, so it refreshes
    # the snapshot; a property-type/name change does not.
    is_manual_or_asset = (
        is_asset_group_provider(institution.provider)
        or institution.provider == MANUAL_INSTITUTION_PROVIDER
    )
    if "name" in body:
        account.name = body["name"]
    changed_value_basis = False
    if is_manual_or_asset:
        if body.get("currency"):
            new_currency = str(body["currency"]).strip().upper()
            old_currency = (account.currency or "").upper()
            if new_currency != old_currency:
                # Convert the worth into the new currency at the current cross-rate
                # (a 200k CAD house → ~2.26 BTC) rather than relabeling the bare number.
                await _convert_account_currency(db, current_user.id, account, old_currency, new_currency)
                account.currency = new_currency
                changed_value_basis = True
        if body.get("account_type"):
            account.account_type = str(body["account_type"]).strip()
            # Keep asset/liability in step with the type unless the caller pins it
            # explicitly — e.g. a manual chequing → credit_card flip becomes a liability
            # (property sub-types stay assets). A flip moves net worth, so re-snapshot.
            if "is_liability" not in body:
                derived_liability = _derive_is_liability(account.account_type, None)
                if derived_liability != bool(account.is_liability):
                    account.is_liability = derived_liability
                    changed_value_basis = True
        if "is_liability" in body:
            new_is_liability = bool(body["is_liability"])
            if new_is_liability != bool(account.is_liability):
                account.is_liability = new_is_liability
                changed_value_basis = True
    if "opening_balance" in body or "opening_balance_date" in body:
        # Manual accounts only: set the starting-balance anchor and recompute the
        # derived balance series from transactions (snapshot reflects the new balance).
        async with sqlite_write_gate():
            outcome = await set_account_opening_balance(
                db, current_user.id, account_id,
                opening_balance=body.get("opening_balance"),
                opening_balance_date=body.get("opening_balance_date"),
            )
            if outcome.get("status") != "ok":
                await db.rollback()
                return outcome
            await db.commit()
        return {"status": "ok", "name": account.name, "opening_balance": outcome["account"]["opening_balance"]}
    if changed_value_basis:
        async with sqlite_write_gate():
            await db.commit()
    else:
        await db.commit()
    return {
        "status": "ok",
        "name": account.name,
        "currency": account.currency,
        "account_type": account.account_type,
    }


def _parse_value_date(raw) -> datetime | None:
    """Parse a 'YYYY-MM-DD' (or ISO) value date into a naive-UTC datetime, or None.
    A bare date anchors to local noon (avoids a tz day-shift in the net-worth graph)."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(hour=12, minute=0, second=0, microsecond=0)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@router.post("/accounts/group")
async def create_net_worth_group_account(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a manual asset/debt account inside its connector-less net-worth group
    bucket (Real Estate, Vehicles, Valuables, Private Investments, Other Assets, or
    Debt), provisioning the bucket on first use. The initial ``value`` is recorded
    through the revaluation door as a dated balance point; an optional
    ``purchase_value`` + ``purchase_date`` seeds an earlier point so appreciation reads
    from the purchase date. Backs the Add-to-Net-Worth tiles."""
    category = str(body.get("category") or "").strip()
    if category not in ASSET_GROUPS:
        return {"status": "error", "message": "Unknown net-worth group category"}
    name = str(body.get("name") or "").strip()
    if not name:
        return {"status": "error", "message": "A name is required"}
    raw_value = body.get("value")
    value = None
    if raw_value not in (None, ""):
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Invalid value"}
    raw_purchase = body.get("purchase_value")
    purchase_value = None
    if raw_purchase not in (None, ""):
        try:
            purchase_value = float(raw_purchase)
        except (TypeError, ValueError):
            return {"status": "error", "message": "Invalid purchase value"}
    # A historical purchase point needs both a value and a date; a lone one is ignored.
    purchase_as_of = _parse_value_date(body.get("purchase_date"))
    # Current value is mandatory; when only a purchase price is given it stands in as the
    # current value (a "still worth what I paid" point at today, alongside the dated
    # purchase point), so an asset can never be created with no worth at all.
    if value is None and purchase_value is not None:
        value = purchase_value
    if value is None:
        return {"status": "error", "message": "A current value is required"}
    async with sqlite_write_gate():
        account = await create_group_account(
            db,
            current_user.id,
            category,
            name=name,
            account_type=(str(body.get("account_type") or "").strip() or None),
            currency=str(body.get("currency") or "CAD"),
            value=value,
            as_of=_parse_value_date(body.get("date")),
            purchase_value=purchase_value,
            purchase_as_of=purchase_as_of,
        )
        await db.commit()
    return {
        "status": "ok",
        "account_id": account.id,
        "institution_id": account.institution_id,
        "name": account.name,
    }


@router.post("/accounts/{account_id}/value")
async def set_net_worth_account_value(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Revaluation door: set a manual or net-worth-group account's value as of a date,
    upserting a dated balance point — this is how a connector-less account builds a
    value history (and a non-flat change column) without transactions. Restricted to
    manual/asset/debt accounts; synced accounts are provider-authoritative, and for a
    manual account that has transactions, those win on the next balance recompute."""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if not (
        is_asset_group_provider(institution.provider)
        or institution.provider == MANUAL_INSTITUTION_PROVIDER
        or account.is_imported  # statement-imported accounts get the same value-timeline tools
    ):
        return {"status": "error", "message": "Value can only be set on manual, asset, or debt accounts"}
    raw_value = body.get("value")
    if raw_value in (None, ""):
        return {"status": "error", "message": "A value is required"}
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid value"}
    as_of = _parse_value_date(body.get("date"))
    # The declared origin (a bucket's purchase point, or a manual account's opening balance)
    # is the floor of the timeline — a manual revaluation can't predate it (you didn't own/owe
    # it before acquiring/opening). Direct the user to move that anchor instead. (Day-granular
    # so intra-day anchoring differences don't false-trigger; a later transaction *import* uses
    # a different path, so this never blocks bringing in genuinely older imported history.)
    floor_date = account.purchase_date or account.opening_balance_date
    if floor_date is not None and as_of is not None and as_of.date() < floor_date:
        anchor_word = "purchase date" if account.purchase_date is not None else "opening date"
        anchor_tool = "purchase value" if account.purchase_date is not None else "opening balance"
        return {
            "status": "error",
            "message": (
                f"Value date can't be before the {anchor_word} "
                f"({floor_date.strftime('%b %d, %Y')}). Update the {anchor_tool} to record an earlier point."
            ),
        }
    async with sqlite_write_gate():
        await set_account_value_as_of(db, current_user.id, account, value, as_of=as_of)
        await db.commit()
    return {"status": "ok", "account_id": account_id}


@router.patch("/accounts/{account_id}/purchase")
async def set_account_purchase(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Set or backfill a tangible asset's purchase cost basis (value + date). Stored on
    the account and mirrored as the earliest balance point so its change reads as
    appreciation from the purchase date. Restricted to connector-less asset accounts.
    (Re-editing the purchase *date* leaves the prior date's point behind — a later
    refinement; the common path is filling it in once on an asset added without it.)"""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if not is_asset_group_provider(institution.provider):
        return {"status": "error", "message": "Purchase info can only be set on net-worth asset accounts"}
    purchase_date = _parse_value_date(body.get("date"))
    raw_value = body.get("value")
    if raw_value in (None, "") or purchase_date is None:
        return {"status": "error", "message": "A purchase value and date are both required"}
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid purchase value"}
    # Symmetric origin floor: the purchase must be the earliest point — reject a purchase date
    # later than an already-recorded (non-purchase) value, since you can't have bought it AFTER
    # a value you've logged. The old purchase point (being moved) is excluded via is_purchase.
    existing_points = await get_account_value_history(db, current_user.id, account)
    earliest_other = None
    for point in existing_points:
        if point.get("is_purchase"):
            continue
        try:
            point_day = datetime.fromisoformat(str(point["date"]).replace("Z", "")).date()
        except ValueError:
            continue
        if earliest_other is None or point_day < earliest_other:
            earliest_other = point_day
    if earliest_other is not None and purchase_date.date() > earliest_other:
        return {
            "status": "error",
            "message": (
                f"Purchase date can't be after a recorded value ({earliest_other.strftime('%b %d, %Y')}). "
                "Pick an earlier purchase date, or delete that value first."
            ),
        }
    old_purchase_value = account.purchase_value
    old_purchase_date = account.purchase_date
    async with sqlite_write_gate():
        # Re-dating the purchase moves its point: drop the prior purchase point (only if it
        # still holds the old purchase amount, so an intervening revaluation on that day is
        # preserved) before re-seeding at the new date, otherwise the old date lingers as a
        # phantom valuation on the net-worth timeline.
        if old_purchase_date is not None:
            await delete_account_value_at(
                db, current_user.id, account, old_purchase_date, only_if_value=old_purchase_value
            )
        account.purchase_value = value
        account.purchase_date = purchase_date
        await set_account_value_as_of(db, current_user.id, account, value, as_of=purchase_date)
        await db.commit()
    return {"status": "ok", "account_id": account_id}


@router.get("/accounts/{account_id}/value-history")
async def get_account_value_history_route(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Full value timeline for a connector-less manual/asset account — the dated points
    behind its worth, with the purchase point flagged. Backs the settings-modal Value
    History view. Restricted to manual/asset accounts (synced accounts are
    provider-authoritative and have no user-editable points)."""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if not (
        is_asset_group_provider(institution.provider)
        or institution.provider == MANUAL_INSTITUTION_PROVIDER
        or account.is_imported  # statement-imported accounts get the same value-timeline tools
    ):
        return {"status": "error", "message": "Value history is only available on manual or asset accounts"}
    points = await get_account_value_history(db, current_user.id, account)
    return {"status": "ok", "account_id": account_id, "currency": account.currency, "points": points}


@router.delete("/accounts/{account_id}/value-history/{point_id}")
async def delete_account_value_history_point(
    account_id: int,
    point_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete one dated value point from a manual/asset account's timeline. If it was the
    purchase point, the stored cost basis is cleared too so the two stay consistent.
    Restricted to manual/asset accounts."""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if not (
        is_asset_group_provider(institution.provider)
        or institution.provider == MANUAL_INSTITUTION_PROVIDER
        or account.is_imported  # statement-imported accounts get the same value-timeline tools
    ):
        return {"status": "error", "message": "Value history is only available on manual or asset accounts"}
    async with sqlite_write_gate():
        removed = await delete_account_value_point(db, current_user.id, account, point_id)
        if not removed:
            await db.rollback()
            return {"status": "error", "message": "Value point not found"}
        await db.commit()
    return {"status": "ok", "account_id": account_id}


@router.put("/accounts/{account_id}/linked-loans")
async def set_account_linked_loans(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Asset-side linker: set the full set of liability accounts that ``account_id`` (an
    asset) secures/finances — e.g. a house securing a mortgage + a HELOC. Stores the link on
    each liability (``secured_asset_account_id``) and clears any previously-linked loan no
    longer in the list (full replace from the asset side). Net worth is unaffected (both sides
    already count); the link drives the per-asset equity view."""
    asset = (
        await db.execute(
            select(Account).where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if asset is None:
        return {"status": "error", "message": "Account not found"}
    if asset.is_liability:
        return {"status": "error", "message": "Loans can only be linked to an asset"}
    try:
        requested = {int(x) for x in (body.get("liability_account_ids") or [])}
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid liability account ids"}
    # Keep only ids that are genuinely this user's liability accounts.
    valid_ids: set[int] = set()
    if requested:
        valid_ids = set(
            (
                await db.execute(
                    select(Account.id).where(
                        Account.id.in_(requested),
                        Account.user_id == current_user.id,
                        Account.is_liability.is_(True),
                    )
                )
            ).scalars().all()
        )
    async with sqlite_write_gate():
        # Unlink loans currently pointing at this asset that are no longer requested.
        currently_linked = (
            await db.execute(
                select(Account).where(
                    Account.secured_asset_account_id == account_id,
                    Account.user_id == current_user.id,
                )
            )
        ).scalars().all()
        for loan in currently_linked:
            if loan.id not in valid_ids:
                loan.secured_asset_account_id = None
        # Link the requested-valid loans to this asset.
        if valid_ids:
            loans = (
                await db.execute(
                    select(Account).where(Account.id.in_(valid_ids), Account.user_id == current_user.id)
                )
            ).scalars().all()
            for loan in loans:
                loan.secured_asset_account_id = account_id
        await db.commit()
    return {"status": "ok", "account_id": account_id, "linked_count": len(valid_ids)}


@router.post("/accounts/{account_id}/cash-opening")
async def set_cash_account_opening(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Set a Cash holder account's opening (starting) balance — the baseline cash held
    before any logged transaction, stored as the neutral ``cash_opening:<CCY>`` transaction
    and recomputed into the balance. Cash accounts only (the connector-less Cash singleton);
    ongoing changes come from manual cash transactions."""
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == current_user.id)
        )
    ).first()
    if row is None:
        return {"status": "error", "message": "Account not found"}
    account, institution = row
    if not (institution.provider == MANUAL_PROVIDER and account.account_type == CASH_ACCOUNT_TYPE):
        return {"status": "error", "message": "Opening balance can only be set on cash accounts"}
    raw_value = body.get("value")
    if raw_value in (None, ""):
        return {"status": "error", "message": "A value is required"}
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid value"}
    async with sqlite_write_gate():
        await set_cash_opening_balance(
            db, current_user.id, account, value, as_of=_parse_value_date(body.get("date"))
        )
        await db.commit()
    return {"status": "ok", "account_id": account_id}


@router.post("/accounts/cash")
async def add_cash_opening(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create (or reuse) the per-currency wallet Cash holder and set its opening
    (starting) balance — the Add-to-Net-Worth **Cash** tile for a user with no logged
    cash yet. Lazily provisions the Cash institution + ``Cash (<CCY>)`` account, then
    writes the neutral ``cash_opening:<CCY>`` baseline (dated today). Ongoing changes
    come from manual cash transactions."""
    currency = str(body.get("currency") or "").strip().upper()
    if not currency:
        return {"status": "error", "message": "A currency is required"}
    raw_value = body.get("value")
    if raw_value in (None, ""):
        return {"status": "error", "message": "A value is required"}
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Invalid value"}
    async with sqlite_write_gate():
        account = await get_or_create_cash_account(db, current_user.id, currency)
        await set_cash_opening_balance(
            db, current_user.id, account, value, as_of=_parse_value_date(body.get("date"))
        )
        await db.commit()
    return {"status": "ok", "account_id": account.id}


@router.put("/accounts/{account_id}/hidden")
async def toggle_account_hidden(
    account_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(Account).where(Account.id == account_id, Account.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if not account:
        return {"status": "error", "message": "Account not found"}
    account.hidden = body.get("hidden", False)
    await db.commit()
    return {"status": "ok", "hidden": account.hidden}


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Delete an account and its holdings/balances. Remove institution if last account."""
    result = await db.execute(
        select(Account).where(Account.id == account_id, Account.user_id == current_user.id)
    )
    account = result.scalar_one_or_none()
    if not account:
        return {"status": "error", "message": "Account not found"}

    # If this is a wallet Cash account, the originating bank rows of its ATM mirror
    # legs stay tagged Cash after the account (and its legs) are deleted — a resync
    # would rebuild the legs and resurrect the holder. Capture those sources now and
    # revert them to Withdrawal/Deposit after deletion so the deletion is durable.
    cash_source_transaction_ids: list[int] = []
    if is_wallet_cash_account(account):
        legs = (
            await db.execute(
                select(Transaction.external_id).where(
                    Transaction.user_id == current_user.id,
                    Transaction.account_id == account.id,
                    Transaction.external_id.like(f"{CASH_MIRROR_PREFIX}%"),
                )
            )
        ).scalars().all()
        cash_source_transaction_ids = [
            int(source_id)
            for external_id in legs
            if external_id
            for source_id in [external_id[len(CASH_MIRROR_PREFIX):]]
            if source_id.isdigit()
        ]

    institution_removed = await delete_account_and_maybe_institution(db, current_user.id, account)
    if cash_source_transaction_ids:
        await revert_cash_mirror_sources(
            db,
            current_user.id,
            cash_source_transaction_ids,
        )
    await db.commit()

    return {"status": "ok", "institution_removed": institution_removed}


@router.get("/accounts/balance-history")
async def get_account_balance_history(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_account_balance_history_payload(db, current_user.id)


@router.get("/accounts/transaction-import-status")
async def get_transaction_import_status(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    return await get_transaction_import_status_payload(db, current_user.id)
