from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, BalanceHistory, Holding, Institution, Setting, SymbolSector, Transaction
from app.services.balance_reconstruction import reconstruct_daily_balances
from app.services.fx_history import build_to_primary_converter
from app.services.user_utils import get_user_timezone_info_from_db


async def get_accounts_payload(
    db: AsyncSession,
    user_id: int,
    *,
    institution_id: int | None = None,
    include_hidden: bool = False,
):
    account_filters = [Account.user_id == user_id]
    institution_filters = [Institution.user_id == user_id]
    if institution_id is not None:
        account_filters.append(Account.institution_id == institution_id)
    if not include_hidden:
        account_filters.append(Account.hidden.is_(False))
        institution_filters.append(Institution.hidden.is_(False))

    latest_balance = (
        select(
            BalanceHistory.account_id,
            func.max(BalanceHistory.date).label("max_date"),
        )
        .join(Account, BalanceHistory.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .where(
            BalanceHistory.user_id == user_id,
            *account_filters,
            *institution_filters,
        )
        .group_by(BalanceHistory.account_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Account,
            Institution.name.label("institution_name"),
            Institution.provider.label("institution_provider"),
            Institution.created_at.label("institution_created_at"),
            BalanceHistory.balance,
        )
        .join(Institution, Account.institution_id == Institution.id)
        .where(
            *account_filters,
            *institution_filters,
        )
        .outerjoin(latest_balance, latest_balance.c.account_id == Account.id)
        .outerjoin(
            BalanceHistory,
            (BalanceHistory.account_id == Account.id)
            & (BalanceHistory.date == latest_balance.c.max_date),
        )
    )
    rows = result.all()
    # Which accounts carry transactions — a manual account with imported transactions is
    # "transaction-backed" (its balance/history are derived, not user-maintained), so the
    # UI hides value-history editing and currency relabeling for it.
    tx_account_ids = set(
        (
            await db.execute(
                select(Transaction.account_id)
                .join(Account, Transaction.account_id == Account.id)
                .join(Institution, Account.institution_id == Institution.id)
                .where(
                    Transaction.user_id == user_id,
                    *account_filters,
                    *institution_filters,
                )
                .distinct()
            )
        ).scalars().all()
    )
    # Cash holders' opening (starting) balances — the `cash_opening:<CCY>` seed transaction —
    # so the Cash settings editor can prefill/show the current baseline.
    cash_opening_by_account: dict[int, dict] = {}
    for acc_id, amt, dt in (
        await db.execute(
            select(Transaction.account_id, Transaction.amount, Transaction.date)
            .join(Account, Transaction.account_id == Account.id)
            .join(Institution, Account.institution_id == Institution.id)
            .where(
                Transaction.user_id == user_id,
                *account_filters,
                *institution_filters,
                Transaction.external_id.like("cash_opening:%"),
            )
        )
    ).all():
        cash_opening_by_account[acc_id] = {
            "amount": amt,
            "date": (dt.isoformat() + "Z") if dt is not None else None,
        }
    from app.services.asset_groups import is_asset_group_provider
    from app.services.manual_accounts import MANUAL_PROVIDER
    from app.services.manual_institutions import MANUAL_INSTITUTION_PROVIDER

    seen = set()
    accounts = []
    for account, institution_name, institution_provider, institution_created_at, balance in rows:
        if account.id in seen:
            continue
        seen.add(account.id)
        accounts.append({
            "id": account.id,
            "institution": institution_name,
            "institution_id": account.institution_id,
            "provider": institution_provider,
            "name": account.name,
            "account_type": account.account_type,
            "currency": account.currency,
            "is_liability": account.is_liability,
            "hidden": account.hidden or False,
            "balance": balance or 0,
            "last_synced": (account.last_synced.isoformat() + "Z") if account.last_synced else None,
            "added_at": (account.created_at.isoformat() + "Z") if account.created_at else None,
            # All-Time change floor. Synced accounts measure from when the institution was
            # connected, so reconstructed pre-connection history isn't counted as a gain/loss.
            # Manual / asset-group accounts — and is_imported accounts (e.g. a closed Questrade
            # account rebuilt from PDF statements) — return None: their history is user-supplied
            # (a backdated purchase, opening balance, imported transactions, or imported statement
            # balances), which is intentional and measured in full, never clamped.
            "connected_at": (
                None
                if (
                    account.is_imported
                    or is_asset_group_provider(institution_provider)
                    or institution_provider in (MANUAL_PROVIDER, MANUAL_INSTITUTION_PROVIDER)
                )
                else (institution_created_at.isoformat() + "Z") if institution_created_at else None
            ),
            "is_imported": account.is_imported,
            "purchase": (
                {"value": account.purchase_value, "date": account.purchase_date.isoformat()}
                if account.purchase_date is not None else None
            ),
            "has_transactions": account.id in tx_account_ids,
            "cash_opening": cash_opening_by_account.get(account.id),
            "opening_balance": account.opening_balance,
            "opening_balance_date": account.opening_balance_date.isoformat() if account.opening_balance_date else None,
            "secured_asset_account_id": account.secured_asset_account_id,
        })
    return accounts


async def get_holdings_payload(db: AsyncSession, user_id: int, account_id: int):
    result = await db.execute(
        select(Holding).where(Holding.account_id == account_id, Holding.user_id == user_id)
    )
    holdings = result.scalars().all()
    normalized_symbols = {
        str(holding.symbol or "").strip().upper().split(" @", 1)[0].strip()
        for holding in holdings
        if str(holding.symbol or "").strip()
    }
    symbol_sector_map = {}
    if normalized_symbols:
        symbol_sector_rows = await db.execute(
            select(SymbolSector).where(SymbolSector.symbol.in_(normalized_symbols))
        )
        symbol_sector_map = {
            row.symbol: row for row in symbol_sector_rows.scalars().all()
        }
    return [
        {
            "id": holding.id,
            "symbol": holding.symbol,
            "name": holding.name,
            "quantity": holding.quantity,
            "market_value": holding.market_value,
            "average_cost": holding.average_cost,
            "last_price": holding.last_price,
            "contract_multiplier": holding.contract_multiplier,
            "change_pct": holding.change_pct,
            "daily_pnl": holding.daily_pnl,
            "currency": holding.currency,
            "sector": holding.sector,
            "instrument_kind": (
                symbol_sector_map.get(
                    str(holding.symbol or "").strip().upper().split(" @", 1)[0].strip()
                ).instrument_kind
                if symbol_sector_map.get(
                    str(holding.symbol or "").strip().upper().split(" @", 1)[0].strip()
                )
                else None
            ),
        }
        for holding in holdings
    ]


async def get_net_worth_payload(
    db: AsyncSession,
    user_id: int,
    *,
    include_hidden: bool = False,
):
    # Net worth is converted into the user's primary currency: current balances
    # at today's rate, each history point at its own date.
    primary_setting = (
        await db.execute(
            select(Setting).where(Setting.key == "primary_currency", Setting.user_id == user_id)
        )
    ).scalar_one_or_none()
    primary_currency = (
        primary_setting.value if primary_setting and primary_setting.value else "CAD"
    ).upper()
    today_str = datetime.now(timezone.utc).date().isoformat()
    account_currencies = {
        str(c).upper()
        for c in (
            await db.execute(select(func.distinct(Account.currency)).where(Account.user_id == user_id))
        ).scalars().all()
        if c
    }
    to_primary = await build_to_primary_converter(db, primary_currency, account_currencies, today_str)

    latest_balance = (
        select(
            BalanceHistory.account_id,
            func.max(BalanceHistory.date).label("max_date"),
        )
        .where(BalanceHistory.user_id == user_id)
        .group_by(BalanceHistory.account_id)
        .subquery()
    )
    visibility_filters = [] if include_hidden else [
        Account.hidden.is_(False),
        Institution.hidden.is_(False),
    ]
    result = await db.execute(
        select(
            Account.is_liability,
            Account.currency,
            BalanceHistory.balance,
        )
        .join(Institution)
        .join(latest_balance, latest_balance.c.account_id == Account.id)
        .join(
            BalanceHistory,
            (BalanceHistory.account_id == Account.id)
            & (BalanceHistory.date == latest_balance.c.max_date),
        )
        .where(
            Account.user_id == user_id,
            Institution.user_id == user_id,
            BalanceHistory.user_id == user_id,
            *visibility_filters,
        )
    )
    total_assets = 0.0
    total_liabilities = 0.0
    for row in result.all():
        converted = to_primary(row.balance, row.currency, today_str)
        if row.is_liability:
            total_liabilities += converted
        else:
            total_assets += converted
    # Signed: a credit balance (overpaid liability, negative owed) adds to net worth.
    live_net_worth = total_assets - total_liabilities

    # History reuses the SAME reconstructed per-account series as the dashboard graph
    # (transaction-driven accounts densified to daily; brokers' daily NAV backfilled where
    # available), so the CSV net-worth export can't diverge from the on-screen graph. Each
    # account's native daily balance is carried forward and converted at that day's rate.
    per_account_history = await get_account_balance_history_payload(
        db,
        user_id,
        include_hidden=include_hidden,
    )
    meta_rows = (
        await db.execute(
            select(Account.id, Account.is_liability, Account.currency)
            .join(Institution, Account.institution_id == Institution.id)
            .where(
                Account.user_id == user_id,
                Institution.user_id == user_id,
                *visibility_filters,
            )
        )
    ).all()
    account_balances = {
        str(acc_id): {"is_liability": is_liability, "currency": currency}
        for acc_id, is_liability, currency in meta_rows
    }

    date_entries = defaultdict(dict)
    for account_id, points in per_account_history.items():
        if account_id not in account_balances:
            continue
        for local_date, balance in points.items():
            date_entries[local_date][account_id] = balance

    all_dates = sorted(date_entries.keys())
    history = []
    running = {}
    for day in all_dates:
        for account_id, balance in date_entries[day].items():
            running[account_id] = balance
        assets = 0.0
        liabilities = 0.0
        for account_id, balance in running.items():
            meta = account_balances[account_id]
            converted = to_primary(balance, meta["currency"], day)
            if meta["is_liability"]:
                liabilities += converted
            else:
                assets += converted
        history.append({
            "date": day,
            "total_assets": assets,
            "total_liabilities": liabilities,
            "net_worth": assets - liabilities,
        })
    return {
        "current": {
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "net_worth": live_net_worth,
            "currency": primary_currency,
            "date": None,
        },
        "history": history,
    }


async def get_account_balance_history_payload(
    db: AsyncSession,
    user_id: int,
    *,
    include_hidden: bool = False,
):
    """Per-account daily balance history (native currency) for the net-worth graph and
    change figures. Transaction-driven accounts (no holdings, with transactions) are
    densified to a daily series via ``reconstruct_daily_balances`` — every gap day between
    two real synced points is recovered exactly by walking the transaction flow, anchored
    to each real point. Holdings/brokerage accounts (value moves with the market) and
    revaluation-only accounts (asset groups with no transactions) keep their sparse points
    and step on sync. Each account's earliest point is preserved, so change-figure
    baselines and the graph's start date do not move."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    now_local = datetime.now(user_tz)

    cutoff = now_local - timedelta(days=365 * 5 + 30)
    cutoff_utc = cutoff.astimezone(timezone.utc).replace(tzinfo=None)

    visibility_filters = [] if include_hidden else [
        Account.hidden.is_(False),
        Institution.hidden.is_(False),
    ]
    result = await db.execute(
        select(
            BalanceHistory.account_id,
            BalanceHistory.balance,
            BalanceHistory.date,
        )
        .join(Account, BalanceHistory.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .where(
            BalanceHistory.user_id == user_id,
            Account.user_id == user_id,
            Institution.user_id == user_id,
            *visibility_filters,
            BalanceHistory.date >= cutoff_utc.date(),
        )
        .order_by(BalanceHistory.date)
    )
    rows = result.all()

    def _to_local_date(dt):
        try:
            utc_dt = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
            return str(utc_dt.astimezone(user_tz).date())
        except Exception:
            return str(dt)[:10]

    account_history = defaultdict(dict)
    for row in rows:
        account_history[row.account_id][_to_local_date(row.date)] = row.balance

    # Also include each account's EARLIEST point even when it predates the 5-year window, so
    # the All-Time change uses the true first value (e.g. a decades-old purchase / first
    # revaluation) as the baseline. Without this, an asset whose only in-window point is its
    # current value reads as 0 change even though its purchase/earliest value differs.
    earliest_dates = (
        select(
            BalanceHistory.account_id,
            func.min(BalanceHistory.date).label("min_date"),
        )
        .where(BalanceHistory.user_id == user_id)
        .group_by(BalanceHistory.account_id)
        .subquery()
    )
    earliest_rows = (
        await db.execute(
            select(BalanceHistory.account_id, BalanceHistory.balance, BalanceHistory.date)
            .join(
                earliest_dates,
                (BalanceHistory.account_id == earliest_dates.c.account_id)
                & (BalanceHistory.date == earliest_dates.c.min_date),
            )
            .join(Account, BalanceHistory.account_id == Account.id)
            .join(Institution, Account.institution_id == Institution.id)
            .where(
                BalanceHistory.user_id == user_id,
                Account.user_id == user_id,
                Institution.user_id == user_id,
                *visibility_filters,
            )
        )
    ).all()
    for row in earliest_rows:
        account_history[row.account_id].setdefault(_to_local_date(row.date), row.balance)

    # Densify transaction-driven accounts to a daily series. An account whose balance moves
    # ONLY via transactions (banks / cash / credit / loans, and manual institutions with
    # imported transactions) can have each gap day between two real synced points recovered
    # exactly by walking its transaction flow. Excluded: holdings/brokerage accounts (value
    # moves with the market, not transactions → stay step-on-sync) and revaluation-only
    # accounts (asset groups with no transactions → their value jumps aren't transaction
    # backed). Anchored to every real point, so each synced balance stays exact and the
    # earliest point is unchanged (graph start date + change baselines do not move).
    holdings_account_ids = set(
        (
            await db.execute(
                select(Holding.account_id).where(Holding.user_id == user_id).distinct()
            )
        ).scalars().all()
    )
    # Statement-imported (defunct brokerage) accounts also stay step-on-sync: their value is
    # the imported month-end NAV (market-driven), not transaction-derived, even though they
    # carry imported dividend/trade transactions. Reconstructing them from those transactions
    # would replace the real (e.g. ~$95k) NAV with a tiny transaction-flow figure.
    imported_account_ids = set(
        (
            await db.execute(
                select(Account.id).where(Account.user_id == user_id, Account.is_imported.is_(True))
            )
        ).scalars().all()
    )
    candidate_ids = [
        aid for aid in account_history
        if aid not in holdings_account_ids and aid not in imported_account_ids
    ]
    if candidate_ids:
        tx_rows = (
            await db.execute(
                select(Transaction.account_id, Transaction.date, Transaction.amount).where(
                    Transaction.user_id == user_id,
                    Transaction.account_id.in_(candidate_ids),
                )
            )
        ).all()
        tx_by_account = defaultdict(list)
        for account_id, tx_date, amount in tx_rows:
            tx_by_account[account_id].append((_to_local_date(tx_date), amount))
        recon_ids = [
            aid
            for aid in candidate_ids
            if aid in tx_by_account and len(account_history[aid]) >= 1
        ]
        if recon_ids:
            liability_by_account = dict(
                (
                    await db.execute(
                        select(Account.id, Account.is_liability).where(Account.id.in_(recon_ids))
                    )
                ).all()
            )
            for aid in recon_ids:
                dense = reconstruct_daily_balances(
                    list(account_history[aid].items()),
                    tx_by_account[aid],
                    bool(liability_by_account.get(aid)),
                )
                if dense:
                    account_history[aid] = dense

    return {str(account_id): history for account_id, history in account_history.items()}
