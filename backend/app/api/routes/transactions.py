from datetime import date, datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import (
    CurrentUser,
    _parse_csv_ints,
    _parse_csv_strings,
    _parse_iso_date,
    get_current_user,
    get_db,
)
from app.models import Account, Category, CategoryRule, Institution, Transaction
from app.services.categories import (
    canonical_amount_for_classification,
    compute_default_category_for_transaction,
    normalize_rule_pattern,
    re_evaluate_all_transactions_for_user,
    reset_transaction_category_to_auto,
)
from app.services.date_window import end_bound_exclusive, start_bound
from app.services.fx_history import build_to_primary_converter
from app.services.manual_institutions import (
    MANUAL_INSTITUTION_PROVIDER,
    is_manual_institution_account,
    recompute_manual_account_balance,
)
from app.services.manual_accounts import (
    CASH_MIRROR_PREFIX,
    get_or_create_cash_account,
    is_wallet_cash_account,
    manual_kind_for_external_id,
    reconcile_cash_mirror,
    recompute_cash_account_balance,
)

router = APIRouter()


def _transaction_search_filter(raw: str | None):
    text = (raw or "").strip().lower()
    if not text:
        return None

    pattern = f"%{text}%"
    conditions = [
        func.lower(func.coalesce(Transaction.symbol, "")).like(pattern),
        func.lower(func.coalesce(Transaction.description, "")).like(pattern),
        func.lower(func.coalesce(Transaction.user_description, "")).like(pattern),
        func.lower(func.coalesce(Account.name, "")).like(pattern),
        func.lower(func.coalesce(Institution.name, "")).like(pattern),
        func.lower(func.coalesce(Transaction.type, "")).like(pattern),
        func.lower(func.coalesce(Category.name, "")).like(pattern),
        func.strftime("%Y-%m-%d", Transaction.date).like(pattern),
    ]

    month_names = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    matching_months = [
        f"{index:02d}"
        for index, name in enumerate(month_names, start=1)
        if name.startswith(text) or name[:3] == text
    ]
    if matching_months:
        conditions.append(func.strftime("%m", Transaction.date).in_(matching_months))

    return or_(*conditions)


async def _refresh_recurring_series(db: AsyncSession, user_id: int) -> None:
    """Re-run recurrence detection after a category edit so the Recurring
    Expenses panel reflects it — the panel renders a per-series snapshot
    (category pill + whether the series still qualifies) that only refreshes on
    detection, so without this a recategorized row keeps its stale pill. Runs
    after the user's change is committed and is best-effort: a detection hiccup
    must never turn a successful edit into an error."""
    from app.services.recurrence import run_detection_for_user

    try:
        await run_detection_for_user(db, user_id)
    except Exception:
        pass


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_description: str | None = Field(None, max_length=500)
    clear_user_description: bool = False
    user_notes: str | None = Field(None, max_length=5_000)
    clear_user_notes: bool = False
    category_id: int | None = None
    clear_category: bool = False
    reset_category: bool = False
    apply_description_to_similar: bool = False
    apply_category_to_similar: bool = False


class ManualTransactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omit account_id for a cash transaction — the per-currency holder is created
    # on demand from `currency` (so cash is always addable even if no cash
    # account currently exists). Provide account_id to target a specific account.
    account_id: int | None = None
    category_id: int
    amount: float = Field(..., allow_inf_nan=False)  # magnitude; sign is derived from classification
    date: str = Field(..., min_length=10, max_length=10)
    description: str | None = Field(None, max_length=500)
    currency: str | None = Field(None, min_length=3, max_length=8)


MANUAL_EXTERNAL_ID_PREFIX = "manual:"
# A user-created cash transaction is, by definition, money in or money out — so
# its category must be income or expense; transfer/investment direction is
# meaningful and not user-creatable here.
_MANUAL_ALLOWED_CLASSIFICATIONS = frozenset({"income", "expense"})
# Transfer paydown categories allowed on a manual LIABILITY account (loan / credit card):
# a repayment or draw recorded on the loan itself. Gated by the account in the POST handler.
_LIABILITY_PAYMENT_SEED_KEYS = frozenset({"cc_payment", "loan_payment", "loan_advance"})


def _parse_manual_date(raw: str) -> date:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="A date is required")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date")


async def _load_user_account(db: AsyncSession, user_id: int, account_id: int) -> Account:
    account = (
        await db.execute(
            select(Account).where(Account.user_id == user_id, Account.id == account_id)
        )
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=400, detail="Invalid account_id")
    return account


async def _load_user_leaf_category(
    db: AsyncSession, user_id: int, category_id: int | None, *, allow_liability_payment: bool = False
) -> Category:
    if category_id is None:
        raise HTTPException(status_code=400, detail="A category is required")
    category = (
        await db.execute(
            select(Category).where(Category.user_id == user_id, Category.id == category_id)
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=400, detail="Invalid category_id")
    if category.parent_id is None:
        raise HTTPException(status_code=400, detail="Pick a subcategory, not a group")
    # Money in / money out only — except on a liability account, where the loan/credit-card
    # paydown transfer categories are a legitimate one-sided entry on the loan itself.
    if category.classification not in _MANUAL_ALLOWED_CLASSIFICATIONS and not (
        allow_liability_payment and category.seed_key in _LIABILITY_PAYMENT_SEED_KEYS
    ):
        raise HTTPException(
            status_code=400,
            detail="Manual transactions support income or expense categories only",
        )
    return category


def _serialize_manual_transaction(tx: Transaction, category: Category | None) -> dict:
    return {
        "id": tx.id,
        "account_id": tx.account_id,
        "category_id": tx.category_id,
        "category_source": tx.category_source,
        "amount": tx.amount,
        "currency": tx.currency,
        "date": tx.date.isoformat() if tx.date else None,
        "description": tx.description,
        "classification": category.classification if category else None,
    }


@router.post("/transactions/manual")
async def create_manual_transaction(
    payload: ManualTransactionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Create a user-entered transaction (cash expense/income). Amount magnitude is
    signed to the category classification (income +, expense −); the row is tagged
    ``category_source='manual'`` so re-evaluation never overrides it."""
    if payload.account_id is None:
        if not payload.currency:
            raise HTTPException(status_code=400, detail="A currency is required for a cash transaction")
        account = await get_or_create_cash_account(db, current_user.id, payload.currency)
    else:
        account = await _load_user_account(db, current_user.id, payload.account_id)
        # Manual transactions target the wallet Cash holder or a user manual institution.
        # Synced accounts are provider-authoritative (a manual row would drift/duplicate on
        # the next sync); asset-group buckets are revaluation-only — both are rejected.
        if is_wallet_cash_account(account):
            if payload.currency:
                account = await get_or_create_cash_account(db, current_user.id, payload.currency)
        elif not await is_manual_institution_account(db, account):
            raise HTTPException(
                status_code=400,
                detail="Manual transactions can only be added to Cash or a manual institution.",
            )
    category = await _load_user_leaf_category(
        db, current_user.id, payload.category_id,
        allow_liability_payment=bool(account.is_liability),
    )
    tx = Transaction(
        user_id=current_user.id,
        account_id=account.id,
        date=_parse_manual_date(payload.date),
        type="manual",
        description=(payload.description or "").strip() or ("Cash" if is_wallet_cash_account(account) else "Transaction"),
        amount=canonical_amount_for_classification(payload.amount, category.classification),
        currency=account.currency,
        external_id=f"{MANUAL_EXTERNAL_ID_PREFIX}{uuid4().hex}",
        category_id=category.id,
        category_source="manual",
    )
    db.add(tx)
    await db.flush()
    await _recompute_after_manual_change(db, current_user.id, {account.id})
    await db.commit()
    return _serialize_manual_transaction(tx, category)


@router.delete("/transactions/manual/{transaction_id}")
async def delete_manual_transaction(
    transaction_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    tx = (
        await db.execute(
            select(Transaction).where(
                Transaction.id == transaction_id,
                Transaction.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Manual-institution transactions are fully user-owned (no sync re-creates them),
    # so they're directly deletable like cash entries.
    inst_provider = (
        await db.execute(
            select(Institution.provider)
            .join(Account, Account.institution_id == Institution.id)
            .where(Account.id == tx.account_id, Account.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    is_manual_institution = inst_provider == MANUAL_INSTITUTION_PROVIDER

    kind = manual_kind_for_external_id(tx.external_id)
    if kind == "cash_mirror":
        # A mirror leg is removed by un-tagging its source from Cash; allow a
        # direct delete only when the source is gone (orphaned).
        try:
            source_transaction_id = int((tx.external_id or "")[len(CASH_MIRROR_PREFIX):])
        except ValueError:
            source_transaction_id = 0
        source_exists = (
            await db.execute(
                select(Transaction.id).where(
                    Transaction.user_id == current_user.id,
                    Transaction.id == source_transaction_id,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if source_exists is not None:
            raise HTTPException(
                status_code=400,
                detail="This cash entry mirrors a bank transaction tagged Cash. Un-tag that transaction to remove it.",
            )
    elif kind not in ("manual", "cash_opening") and not is_manual_institution:
        raise HTTPException(status_code=404, detail="Transaction is not removable")

    account_id = tx.account_id
    await db.delete(tx)
    await db.flush()
    if account_id is not None:
        await _recompute_after_manual_change(db, current_user.id, {account_id})
    await db.commit()
    return {"deleted": True}


async def _recompute_after_manual_change(
    db: AsyncSession, user_id: int, account_ids: set[int]
) -> None:
    """Recompute balance history for affected user-managed accounts after a manual
    transaction change: the wallet Cash holder (running transaction sum) and manual-
    institution accounts (opening ± cumulative). Synced accounts own their balances via
    sync, so they are skipped."""
    if not account_ids:
        return
    rows = (
        await db.execute(
            select(Account, Institution.provider)
            .join(Institution, Institution.id == Account.institution_id)
            .where(Account.user_id == user_id, Account.id.in_(account_ids))
        )
    ).all()
    for account, provider in rows:
        if is_wallet_cash_account(account):
            await recompute_cash_account_balance(db, user_id, account.id)
        elif provider == MANUAL_INSTITUTION_PROVIDER:
            await recompute_manual_account_balance(db, user_id, account)


@router.get("/transactions")
async def get_transactions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    account_ids: str | None = Query(None),
    institution_ids: str | None = Query(None),
    type: str | None = Query(None, max_length=1_024),
    category_ids: str | None = Query(None),
    symbol: str | None = Query(None, max_length=64),
    asset_category: str | None = Query(
        None,
        max_length=32,
        description="Filter to one asset class, e.g. 'FUT' for futures",
    ),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    search: str | None = Query(
        None,
        max_length=200,
        description="Case-insensitive search across displayed transaction fields",
    ),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    convert_to: str | None = Query(
        None,
        max_length=8,
        description="Convert each amount into this currency (returned as amount_primary)",
    ),
):
    """Return transactions with optional filters, ordered by date DESC.

    Multi-value filters: `institution_ids=1,4,2`, `type=dividend,buy,sell`,
    `category_ids=12,18`. Response shape: `{"transactions": [...], "total": N}`
    where each transaction nests `category: {id, parent_id, name, icon, color_dark, color_light, classification}`
    or `null` if uncategorized.
    """
    filters = []

    filters.extend([
        Transaction.user_id == current_user.id,
        Account.user_id == current_user.id,
        Institution.user_id == current_user.id,
        Account.hidden.is_(False),
        Institution.hidden.is_(False),
        Institution.enabled.is_(True),
    ])

    acct_id_list = _parse_csv_ints(account_ids)
    if acct_id_list:
        filters.append(Transaction.account_id.in_(acct_id_list))

    inst_id_list = _parse_csv_ints(institution_ids)
    if inst_id_list:
        filters.append(Account.institution_id.in_(inst_id_list))

    type_list = _parse_csv_strings(type, field_name="type")
    if len(type_list) > 1:
        filters.append(Transaction.type.in_(type_list))
    elif len(type_list) == 1:
        filters.append(Transaction.type == type_list[0])

    cat_id_list = _parse_csv_ints(category_ids)
    if cat_id_list:
        filters.append(Transaction.category_id.in_(cat_id_list))

    if symbol is not None:
        filters.append(Transaction.symbol == symbol)
    if asset_category is not None:
        filters.append(Transaction.asset_category == asset_category)
    # `start_date` / `end_date` are inclusive calendar days. `end_date` covers the
    # whole day (services.date_window) so a transaction stamped after midnight on
    # the period's last day stays in the list/tray/export instead of being
    # dropped by a midnight `<=` bound.
    _parse_iso_date(start_date, field_name="start_date")
    _parse_iso_date(end_date, field_name="end_date")
    start_dt = start_bound(start_date)
    if start_dt is not None:
        filters.append(Transaction.date >= start_dt)
    end_dt = end_bound_exclusive(end_date)
    if end_dt is not None:
        filters.append(Transaction.date < end_dt)
    search_filter = _transaction_search_filter(search)
    if search_filter is not None:
        filters.append(search_filter)

    base_join = (
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.external_id,
            Account.name.label("account_name"),
            Account.account_type.label("account_type"),
            Institution.name.label("institution_name"),
            Institution.provider.label("institution_provider"),
            Transaction.date,
            Transaction.type,
            Transaction.symbol,
            Transaction.asset_category,
            Transaction.description,
            Transaction.user_description,
            Transaction.user_notes,
            Transaction.amount,
            Transaction.currency,
            Transaction.quantity,
            Transaction.price,
            Transaction.commission,
            Transaction.realized_pnl,
            Transaction.category_id,
            Transaction.category_source,
            Category.parent_id.label("category_parent_id"),
            Category.name.label("category_name"),
            Category.icon.label("category_icon"),
            Category.icon_set.label("category_icon_set"),
            Category.color_dark.label("category_color_dark"),
            Category.color_light.label("category_color_light"),
            Category.classification.label("category_classification"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
    )

    count_query = (
        select(func.count(Transaction.id))
        .join(Account, Transaction.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
    )

    for f in filters:
        base_join = base_join.where(f)
        count_query = count_query.where(f)

    total = (await db.execute(count_query)).scalar_one() or 0

    rows_query = base_join.order_by(Transaction.date.desc(), Transaction.id.desc()).offset(offset).limit(limit)
    rows = (await db.execute(rows_query)).all()

    to_primary = None
    if convert_to and rows:
        row_currencies = {str(r.currency).upper() for r in rows if r.currency}
        to_primary = await build_to_primary_converter(
            db, convert_to, row_currencies, datetime.now(timezone.utc).date().isoformat()
        )

    return {
        "transactions": [
            {
                "id": row.id,
                "account_id": row.account_id,
                "account_name": row.account_name,
                "account_type": row.account_type,
                "institution_name": row.institution_name,
                "institution_provider": row.institution_provider,
                "date": row.date.isoformat() if row.date else None,
                "type": row.type,
                "symbol": row.symbol,
                "asset_category": row.asset_category,
                "description": row.description,
                "user_description": row.user_description,
                "display_description": row.user_description or row.description,
                "user_notes": row.user_notes,
                "manual_kind": manual_kind_for_external_id(row.external_id),
                "amount": row.amount,
                "currency": row.currency,
                "amount_primary": (
                    to_primary(row.amount, row.currency, row.date.isoformat())
                    if to_primary and row.date else None
                ),
                "quantity": row.quantity,
                "price": row.price,
                "commission": row.commission,
                "realized_pnl": row.realized_pnl,
                "realized_pnl_primary": (
                    to_primary(row.realized_pnl, row.currency, row.date.isoformat())
                    if to_primary and row.date and row.realized_pnl is not None else None
                ),
                "category_source": row.category_source,
                "category": (
                    {
                        "id": row.category_id,
                        "parent_id": row.category_parent_id,
                        "name": row.category_name,
                        "icon": row.category_icon,
                        "icon_set": row.category_icon_set,
                        "color_dark": row.category_color_dark,
                        "color_light": row.category_color_light,
                        "classification": row.category_classification,
                    }
                    if row.category_id
                    else None
                ),
            }
            for row in rows
        ],
        "total": int(total),
    }


@router.get("/transactions/{transaction_id}/similar-count")
async def get_similar_count(
    transaction_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Count of transactions sharing the same raw description (case-insensitive)
    for this user — used by the detail drawer's "apply to similar" affordance."""
    tx = (
        await db.execute(
            select(Transaction.description).where(
                Transaction.id == transaction_id,
                Transaction.user_id == current_user.id,
            )
        )
    ).first()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    raw = (tx.description or "").strip()
    if not raw:
        return {"count": 0, "raw_description": raw}
    count = (
        await db.execute(
            select(func.count(Transaction.id)).where(
                Transaction.user_id == current_user.id,
                func.lower(Transaction.description) == raw.lower(),
            )
        )
    ).scalar_one() or 0
    return {"count": int(count), "raw_description": raw}


@router.patch("/transactions/{transaction_id}")
async def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Update user-editable fields on a transaction (display description, notes,
    category). With `apply_description_to_similar` or `apply_category_to_similar`,
    the same change is also written to every other transaction sharing the same
    raw description (case-insensitive). `reset_category` forgets the transaction's
    descriptor rule and restores its automatic category in the same transaction as
    any text or notes edit. Any explicit category assignment also creates (or
    re-points) a persistent per-user CategoryRule from this transaction's descriptor
    so the same merchant auto-categorizes on future syncs, and existing non-manual
    rows with that descriptor are re-evaluated immediately."""
    tx = (
        await db.execute(
            select(Transaction).where(
                Transaction.id == transaction_id,
                Transaction.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if payload.reset_category and (
        payload.category_id is not None
        or payload.clear_category
        or payload.apply_category_to_similar
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "reset_category cannot be combined with category_id, "
                "clear_category, or apply_category_to_similar"
            ),
        )

    # System-managed cash legs (ATM mirror, opening balance) are neutral and their
    # amount is sign-locked to Cash — re-categorizing would corrupt the cash balance.
    if (
        (payload.reset_category or payload.clear_category or payload.category_id is not None)
        and manual_kind_for_external_id(tx.external_id) in ("cash_mirror", "cash_opening")
    ):
        raise HTTPException(
            status_code=400,
            detail="This is a system-managed cash entry and can't be re-categorized.",
        )

    description_changed = False
    category_changed = False
    new_user_description: str | None = tx.user_description
    new_user_notes: str | None = tx.user_notes
    new_category_id: int | None = tx.category_id
    new_category_classification: str | None = None

    if payload.clear_user_description:
        new_user_description = None
        description_changed = True
    elif payload.user_description is not None:
        cleaned = payload.user_description.strip()
        new_user_description = cleaned or None
        description_changed = True

    if payload.clear_user_notes:
        new_user_notes = None
    elif payload.user_notes is not None:
        cleaned = payload.user_notes.strip()
        new_user_notes = cleaned or None

    if payload.clear_category:
        new_category_id = None
        category_changed = True
    elif payload.category_id is not None and payload.category_id != tx.category_id:
        category = (
            await db.execute(
                select(Category).where(
                    Category.user_id == current_user.id,
                    Category.id == payload.category_id,
                )
            )
        ).scalar_one_or_none()
        if category is None:
            raise HTTPException(status_code=400, detail="Invalid category_id")
        if category.parent_id is None:
            raise HTTPException(status_code=400, detail="Cannot assign a group category; pick a subcategory")
        new_category_id = payload.category_id
        new_category_classification = category.classification
        category_changed = True

    tx.user_description = new_user_description
    tx.user_notes = new_user_notes
    if category_changed:
        tx.category_id = new_category_id
        tx.category_source = "manual" if new_category_id is not None else None
        if new_category_id is not None:
            # Capture the pre-flip (true provider) sign once, so a later revert can
            # restore it — sign-routed types like interest can't recover it otherwise.
            if tx.raw_amount is None:
                tx.raw_amount = tx.amount
            tx.amount = canonical_amount_for_classification(tx.amount, new_category_classification, tx.type)

    bulk_updated = 0
    similar_ids: list[int] = []
    raw_description = (tx.description or "").strip()
    if raw_description and (
        (payload.apply_description_to_similar and description_changed)
        or (payload.apply_category_to_similar and category_changed)
    ):
        similar_ids = [
            row[0]
            for row in (
                await db.execute(
                    select(Transaction.id).where(
                        Transaction.user_id == current_user.id,
                        Transaction.id != transaction_id,
                        func.lower(Transaction.description) == raw_description.lower(),
                    )
                )
            ).all()
        ]
        if similar_ids:
            values: dict = {}
            if payload.apply_description_to_similar and description_changed:
                values["user_description"] = new_user_description
            if payload.apply_category_to_similar and category_changed:
                values["category_id"] = new_category_id
                values["category_source"] = "manual" if new_category_id is not None else None
                if new_category_id is not None:
                    # Mirror the single-row capture so bulk-tagged rows stay revertible.
                    values["raw_amount"] = func.coalesce(Transaction.raw_amount, Transaction.amount)
                if new_category_classification == "income":
                    values["amount"] = func.abs(Transaction.amount)
                elif new_category_classification == "expense":
                    values["amount"] = -func.abs(Transaction.amount)
            if values:
                await db.execute(
                    sa_update(Transaction)
                    .where(Transaction.id.in_(similar_ids))
                    .values(**values)
                )
                bulk_updated = len(similar_ids)

    # Recategorizing a transaction always "remembers": create (or re-point) a
    # per-user rule from this row's specific descriptor so the same merchant
    # auto-files the same way on future syncs, and existing non-manual rows with
    # that descriptor are re-evaluated now. No opt-in — a user who recategorizes
    # a transaction once expects it to stick without building rules by hand.
    rule_created = False
    rule_id: int | None = None
    future_rows_updated = 0
    # Normalize the descriptor to a stable merchant substring so the rule
    # generalizes to future charges instead of matching only this one row
    # (None when the descriptor is too generic to rule on safely — then we just
    # keep the manual override on the edited row(s) and skip rule creation).
    pattern = (
        normalize_rule_pattern(raw_description)
        if category_changed and new_category_id is not None
        else None
    )
    if pattern is not None:
        existing_rule = (
            await db.execute(
                select(CategoryRule).where(
                    CategoryRule.user_id == current_user.id,
                    CategoryRule.is_regex.is_(False),
                    func.lower(CategoryRule.description_pattern) == pattern.lower(),
                )
            )
        ).scalar_one_or_none()
        if existing_rule is not None:
            rule = existing_rule
            if existing_rule.category_id != new_category_id or not existing_rule.enabled:
                existing_rule.category_id = new_category_id
                existing_rule.enabled = True
        else:
            rule = CategoryRule(
                user_id=current_user.id,
                category_id=new_category_id,
                priority=100,
                enabled=True,
                description_pattern=pattern,
                is_regex=False,
            )
            db.add(rule)
            rule_created = True
        await db.flush()
        rule_id = rule.id
        # Apply the rule to existing non-manual rows now (also fixes stray
        # auto-categorized rows that synced in before the rule existed).
        counters = await re_evaluate_all_transactions_for_user(db, current_user.id)
        future_rows_updated = counters.get("updated", 0)

    # The edited row(s) are tagged manual (so re_evaluate skips them); reconcile
    # their Cash mirror legs explicitly. re_evaluate handled the auto/rule rows.
    if category_changed:
        for cash_tx_id in {transaction_id, *similar_ids}:
            await reconcile_cash_mirror(db, current_user.id, cash_tx_id)

    if payload.reset_category:
        tx = await reset_transaction_category_to_auto(db, current_user.id, transaction_id)

    await db.commit()
    await _refresh_recurring_series(db, current_user.id)
    return {
        "id": tx.id,
        "user_description": tx.user_description,
        "user_notes": tx.user_notes,
        "category_id": tx.category_id,
        "category_source": tx.category_source,
        "amount": tx.amount,
        "bulk_updated_count": bulk_updated,
        "rule_created": rule_created,
        "rule_id": rule_id,
        "future_rows_updated": future_rows_updated,
    }


@router.get("/transactions/{transaction_id}/default-category")
async def get_transaction_default_category(
    transaction_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Preview the category this transaction would revert to (no mutation) — backs
    the tray's staged Default control so it can show the default before saving."""
    category_id, category_source = await compute_default_category_for_transaction(
        db, current_user.id, transaction_id
    )
    return {"category_id": category_id, "category_source": category_source}
