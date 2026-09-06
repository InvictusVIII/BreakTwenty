"""Cash Flow aggregation endpoint.

Powers the Cash Flow page's observation-first spending review. Returns
income/expense totals, per-leaf breakdowns, investment activity, a 12-month
trend, and recurring-this-month detection for a given period + scope + currency.

Design choices (from the budget-app research summary):
  * Classification = "investment" or "transfer" rows are EXCLUDED from
    income/expense rollups. They surface as a separate ``investment_activity``
    section so users can see "I bought $5k of stocks + got $50 dividends"
    without it polluting "I spent $X on food".
  * Aggregation is at the leaf level (parents are containers). The frontend
    can roll leaves up by ``parent_id`` when the user toggles "By Group".
  * Recurring detection is intentionally simple: any transaction whose
    category sits under the Subscriptions group counts as "recurring this
    month". A smarter pattern-matcher (across all categories) can replace
    this in a later pass.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, _parse_csv_ints, _parse_iso_date, get_current_user, get_db
from app.models import Account, Category, Holding, Institution, RecurringSeries, Setting, Transaction
from app.services.holdings_metrics import _is_future
from app.services.fx_history import build_to_primary_converter


router = APIRouter()


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    """Shift ``d`` forward by ``n`` months (negative for past), day-clamped."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


@router.get("/cash-flow")
async def get_cash_flow(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    start_date: str | None = Query(None, description="ISO date inclusive, e.g. 2026-05-01"),
    end_date: str | None = Query(None, description="ISO date inclusive, e.g. 2026-05-31"),
    previous_start_date: str | None = Query(None, description="ISO date inclusive — caller-supplied previous-period start for the vs-previous comparison; matches stepping the picker back one unit so calendar periods of unequal length line up"),
    previous_end_date: str | None = Query(None, description="ISO date inclusive — caller-supplied previous-period end"),
    institution_ids: str | None = Query(None, description="Comma-separated institution IDs"),
    account_ids: str | None = Query(None, description="Comma-separated account IDs"),
):
    """Aggregate cash-flow data for the period + scope.

    Response shape::

        {
          "period": {"start": "2026-05-01", "end": "2026-05-31", "currency": "CAD"},
          "totals": {"income": 5556.95, "expense": 3662.15, "net": 1894.80},
          "expense_breakdown": [
            {"category_id": 47, "name": "Misc", "icon": "...", "icon_set": "...",
             "color_dark": "#...", "color_light": "#...",
             "parent_id": 46, "parent_name": "Shopping",
             "seed_key": "misc", "amount": 320.50, "percent_of_total": 8.75,
             "transaction_count": 4}
          ],
          "income_breakdown": [...],   # same shape as expense_breakdown
          "needs_review": {
            "category_id": 92, "transaction_count": 3
          },
          "investment_activity": {
            "buys": ..., "sells": ..., "dividends": ..., "interest": ...,
            "withholding_tax": ..., "transfers_in": ..., "transfers_out": ...,
            "deposits": ..., "withdrawals": ...,
            "breakdown": [{...per-leaf row...}]
          },
          "trend_12_months": [
            {"month": "2025-06", "income": ..., "expense": ..., "net": ...},
            ...
          ],
          "recurring": [
            {"id": 12, "name": "Netflix", "direction": "outflow",
             "category": {...}, "cadence": "monthly", "interval_days": 30.0,
             "amount": -18.99, "amount_min": ..., "amount_max": ..., "currency": "CAD",
             "last_seen_date": ..., "next_expected_date": ..., "occurrence_count": 7,
             "confidence": 0.82, "is_confirmed": false,
             "period_amount": -18.99, "period_count": 1}
          ]
        }
    """
    start = _parse_iso_date(start_date, field_name="start_date")
    end = _parse_iso_date(end_date, field_name="end_date")
    # Only default to "current calendar month" when the caller supplies
    # neither bound (e.g. a raw curl without query params). When the user
    # picks "All" the frontend sends only `end_date`, and when they pick a
    # preset like "1D" / "YTD" both bounds are sent — in those cases
    # respect missing as "unbounded" rather than silently overwriting.
    today = datetime.now(timezone.utc).date()
    if start is None and end is None:
        start = _month_start(today)
        end = _add_months(start, 1) - timedelta(days=1)

    inst_id_list = _parse_csv_ints(institution_ids)
    acct_id_list = _parse_csv_ints(account_ids)

    # Common scope filters reused by every aggregation below.
    base_filters = [
        Transaction.user_id == current_user.id,
        Account.user_id == current_user.id,
        Institution.user_id == current_user.id,
        Account.hidden.is_(False),
        Institution.hidden.is_(False),
        Institution.enabled.is_(True),
    ]
    if inst_id_list:
        base_filters.append(Account.institution_id.in_(inst_id_list))
    if acct_id_list:
        base_filters.append(Transaction.account_id.in_(acct_id_list))

    # Everything is converted into the user's primary currency (chosen in
    # Settings) at each transaction's own date via the historical FX rates.
    primary_setting = (
        await db.execute(
            select(Setting).where(
                Setting.key == "primary_currency",
                Setting.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    primary_currency = (
        primary_setting.value if primary_setting and primary_setting.value else "CAD"
    ).upper()
    scope_currencies = {
        str(c).upper()
        for c in (
            await db.execute(
                select(func.distinct(Transaction.currency))
                .select_from(Transaction)
                .join(Account, Account.id == Transaction.account_id)
                .join(Institution, Institution.id == Account.institution_id)
                .where(and_(*base_filters))
            )
        ).scalars().all()
        if c
    }
    to_primary = await build_to_primary_converter(
        db, primary_currency, scope_currencies, today.isoformat()
    )

    # Period filter for the headline aggregations. Either bound can be None
    # (unbounded) when the caller picks "All" or specifies only one side.
    period_filters = list(base_filters)
    if start is not None:
        period_filters.append(Transaction.date >= start)
    if end is not None:
        period_filters.append(Transaction.date < end + timedelta(days=1))

    # Futures trades move no cash at their notional (you post margin; the realized P&L is the
    # real flow), so they must NOT count as buys/sells in cash flow — otherwise a shorted
    # contract shows as a huge phantom "sale". Exclude them by the connector's asset_category
    # ="FUT" tag (set at sync — the reliable, portable signal across every broker) plus a SAFE
    # fallback for rows not yet tagged: any transaction whose symbol matches one of the user's
    # current futures *holdings* (identified by contract multiplier in `_is_future`, so a stock
    # sell can never be mistaken for one). After a re-sync every futures trade carries the tag,
    # so the fallback only matters before that / for brokers that expose futures holdings but
    # don't tag their trades.
    futures_holding_symbols = {
        h.symbol
        for h in (
            await db.execute(
                select(Holding.symbol, Holding.name, Holding.contract_multiplier)
                .where(Holding.user_id == current_user.id)
            )
        ).all()
        if h.symbol and _is_future(h.symbol, h.name, h.contract_multiplier)
    }
    futures_predicate = func.coalesce(Transaction.asset_category, "") == "FUT"
    if futures_holding_symbols:
        futures_predicate = or_(futures_predicate, Transaction.symbol.in_(futures_holding_symbols))
    futures_period_filters = period_filters + [futures_predicate]  # base + period, futures ONLY (for the realized-P&L line)
    period_filters.append(not_(futures_predicate))

    # 1. Per-leaf aggregation within the period, joined to category + parent
    # category for group context. Excludes uncategorized rows from leaf
    # breakdowns (they're rolled into a synthetic "Uncategorized" entry
    # downstream if we want — left out of v1 for clarity).
    Parent = Category.__table__.alias("parent_cat")
    leaf_rows = (
        await db.execute(
            select(
                Category.id.label("category_id"),
                Category.name.label("name"),
                Category.icon.label("icon"),
                Category.icon_set.label("icon_set"),
                Category.color_dark.label("color_dark"),
                Category.color_light.label("color_light"),
                Category.classification.label("classification"),
                Category.seed_key.label("seed_key"),
                Category.parent_id.label("parent_id"),
                Parent.c.name.label("parent_name"),
                Parent.c.seed_key.label("parent_seed_key"),
                Parent.c.color_dark.label("parent_color_dark"),
                Parent.c.color_light.label("parent_color_light"),
                Parent.c.icon.label("parent_icon"),
                Parent.c.icon_set.label("parent_icon_set"),
                func.sum(Transaction.amount).label("amount"),
                func.count(Transaction.id).label("tx_count"),
                Transaction.currency.label("currency"),
                func.strftime("%Y-%m-%d", Transaction.date).label("day"),
            )
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .join(Category, Category.id == Transaction.category_id)
            .outerjoin(Parent, Parent.c.id == Category.parent_id)
            .where(and_(*period_filters))
            .group_by(
                Category.id,
                Category.name,
                Category.icon,
                Category.icon_set,
                Category.color_dark,
                Category.color_light,
                Category.classification,
                Category.seed_key,
                Category.parent_id,
                Parent.c.name,
                Parent.c.seed_key,
                Parent.c.color_dark,
                Parent.c.color_light,
                Parent.c.icon,
                Parent.c.icon_set,
                Transaction.currency,
                func.strftime("%Y-%m-%d", Transaction.date),
            )
        )
    ).all()

    # Uncategorized is deliberately excluded from income/spending totals, but
    # the frontend needs a conditional marker for its inline review queue.
    review_row = (
        await db.execute(
            select(
                Category.id.label("category_id"),
                func.count(Transaction.id).label("tx_count"),
            )
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(and_(*period_filters, Category.seed_key == "uncategorized"))
            .group_by(Category.id)
        )
    ).first()
    needs_review = {
        "category_id": review_row.category_id if review_row is not None else None,
        "transaction_count": int(review_row.tx_count or 0) if review_row is not None else 0,
    }

    income_breakdown: list[dict[str, Any]] = []
    expense_breakdown: list[dict[str, Any]] = []
    investment_breakdown: list[dict[str, Any]] = []

    income_total = 0.0
    expense_total = 0.0

    # Rows are grouped by (category, currency, day); convert each slice into the
    # primary currency at its own date, then accumulate per category.
    by_category: dict[Any, dict[str, Any]] = {}
    for row in leaf_rows:
        converted = to_primary(float(row.amount or 0), row.currency, row.day)
        entry = by_category.get(row.category_id)
        if entry is None:
            entry = {
                "category_id": row.category_id,
                "name": row.name,
                "icon": row.icon,
                "icon_set": row.icon_set,
                "color_dark": row.color_dark,
                "color_light": row.color_light,
                "classification": row.classification,
                "seed_key": row.seed_key,
                "parent_id": row.parent_id,
                "parent_name": row.parent_name,
                "parent_color_dark": row.parent_color_dark,
                "parent_color_light": row.parent_color_light,
                "parent_icon": row.parent_icon,
                "parent_icon_set": row.parent_icon_set,
                "amount": 0.0,
                "transaction_count": 0,
            }
            by_category[row.category_id] = entry
        entry["amount"] += converted
        entry["transaction_count"] += int(row.tx_count or 0)

    for entry in by_category.values():
        classification = entry["classification"]
        if entry.get("seed_key") == "uncategorized":
            # Pending review — neutral, excluded from income/expense/activity
            # totals until the user assigns a real category.
            continue
        if classification in ("investment", "transfer"):
            investment_breakdown.append(entry)
        elif classification == "income":
            income_total += entry["amount"]
            income_breakdown.append(entry)
        else:  # 'expense' — amounts come through negative; flip for display.
            entry["amount"] = -entry["amount"]
            expense_total += entry["amount"]
            expense_breakdown.append(entry)

    # Compute % of total per row, sort descending.
    for row in expense_breakdown:
        row["percent_of_total"] = (row["amount"] / expense_total * 100.0) if expense_total else 0.0
    for row in income_breakdown:
        row["percent_of_total"] = (row["amount"] / income_total * 100.0) if income_total else 0.0
    expense_breakdown.sort(key=lambda r: r["amount"], reverse=True)
    income_breakdown.sort(key=lambda r: r["amount"], reverse=True)
    investment_breakdown.sort(key=lambda r: abs(r["amount"]), reverse=True)

    # 2. Investment activity rollup — totals per seed_key for the structural
    # transaction types the connector sets (Buy/Sell/Dividend/Interest/etc.).
    activity_keys = {
        "buy": 0.0, "sell": 0.0, "dividend": 0.0, "interest": 0.0,
        "withheld": 0.0, "expired": 0.0,
        "transfer": 0.0, "deposit": 0.0, "withdrawal": 0.0, "cc_payment": 0.0,
        "loan_payment": 0.0, "realized_pnl": 0.0,
    }
    for row in investment_breakdown:
        sk = row.get("seed_key")
        if sk in activity_keys:
            activity_keys[sk] += row["amount"]

    # CC / loan-payment tiles must reflect the actual paydown, not the net of
    # both legs: the funding outflow (from chequing) AND the inflow that lands on
    # the card/loan both carry the cc_payment/loan_payment category, so the net
    # cancels and understates the real payment. The true payment is the positive
    # leg — the credit that reduces the liability balance — so re-sum these two
    # seed_keys positive-only (FX-converted per its own transaction date).
    paydown_rows = (
        await db.execute(
            select(
                Category.seed_key.label("seed_key"),
                Transaction.currency.label("currency"),
                func.strftime("%Y-%m-%d", Transaction.date).label("day"),
                func.sum(Transaction.amount).label("amount"),
            )
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(
                and_(
                    *period_filters,
                    or_(
                        # Paydown leg of a card/loan payment (positive on the liability).
                        and_(
                            Category.seed_key.in_(("cc_payment", "loan_payment")),
                            Transaction.amount > 0,
                        ),
                        # Draw leg of a loan advance (negative on the liability — debt taken on).
                        and_(Category.seed_key == "loan_advance", Transaction.amount < 0),
                    ),
                )
            )
            .group_by(
                Category.seed_key,
                Transaction.currency,
                func.strftime("%Y-%m-%d", Transaction.date),
            )
        )
    ).all()
    activity_keys["cc_payment"] = 0.0
    activity_keys["loan_payment"] = 0.0
    activity_keys["loan_advance"] = 0.0
    for r in paydown_rows:
        activity_keys[r.seed_key] += to_primary(float(r.amount or 0), r.currency, r.day)

    # Futures realized P&L — the real cash impact of a CLOSED futures position (broker
    # fifoPnlRealized) in place of the meaningless notional that's kept out of buys/sells.
    # 0 while a position is open. FX-converted per trade date.
    realized_rows = (
        await db.execute(
            select(
                Transaction.realized_pnl.label("pnl"),
                Transaction.currency.label("currency"),
                func.strftime("%Y-%m-%d", Transaction.date).label("day"),
            )
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .where(and_(*futures_period_filters, Transaction.realized_pnl.isnot(None)))
        )
    ).all()
    activity_keys["realized_pnl"] = sum(
        to_primary(float(r.pnl or 0), r.currency, r.day) for r in realized_rows
    )

    investment_activity = {
        "buys": activity_keys["buy"],
        "sells": activity_keys["sell"],
        "dividends": activity_keys["dividend"],
        "interest": activity_keys["interest"],
        "withholding_tax": activity_keys["withheld"],
        "expired": activity_keys["expired"],
        "transfers": activity_keys["transfer"],
        "deposits": activity_keys["deposit"],
        "withdrawals": activity_keys["withdrawal"],
        "cc_payments": activity_keys["cc_payment"],
        "loan_payments": activity_keys["loan_payment"],
        # Advance draw leg is negative (debt taken on) — keep that sign so the tile reads
        # red/negative, consistent with the underlying transaction and the net-worth hit.
        "loan_advances": activity_keys["loan_advance"],
        # Futures realized P&L (gain +, loss −) — what futures actually contribute to cash,
        # vs their excluded notional buy/sell.
        "realized_pnl": activity_keys["realized_pnl"],
        "breakdown": investment_breakdown,
    }

    # 3. 12-month trend ending at the period's end-month (or today when the
    # period is unbounded on the right).
    trend_anchor = end if end is not None else today
    trend_start = _add_months(_month_start(trend_anchor), -11)
    trend_filters = base_filters + [
        Transaction.date >= trend_start,
        Transaction.date < _add_months(_month_start(trend_anchor), 1),
        or_(Category.seed_key != "uncategorized", Category.seed_key.is_(None)),
    ]
    trend_rows = (
        await db.execute(
            select(
                func.strftime("%Y-%m", Transaction.date).label("month"),
                Category.classification.label("classification"),
                func.sum(Transaction.amount).label("amount"),
                Transaction.currency.label("currency"),
                func.strftime("%Y-%m-%d", Transaction.date).label("day"),
            )
            .select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .join(Category, Category.id == Transaction.category_id)
            .where(and_(*trend_filters))
            .group_by("month", Category.classification, Transaction.currency, func.strftime("%Y-%m-%d", Transaction.date))
        )
    ).all()

    month_buckets: dict[str, dict[str, float]] = {}
    for row in trend_rows:
        m = row.month
        if m not in month_buckets:
            month_buckets[m] = {"income": 0.0, "expense": 0.0}
        cls = row.classification
        amt = to_primary(float(row.amount or 0), row.currency, row.day)
        if cls == "income":
            month_buckets[m]["income"] += amt
        elif cls == "expense":
            month_buckets[m]["expense"] += -amt  # flip sign
        # investment/transfer excluded from trend
    trend_12_months = []
    cursor = trend_start
    for _ in range(12):
        key = cursor.strftime("%Y-%m")
        bucket = month_buckets.get(key, {"income": 0.0, "expense": 0.0})
        trend_12_months.append(
            {
                "month": key,
                "income": bucket["income"],
                "expense": bucket["expense"],
                "net": bucket["income"] - bucket["expense"],
            }
        )
        cursor = _add_months(cursor, 1)

    # 4. Previous-period totals — same-length window immediately preceding
    # the current period. Powers the "vs. previous period" delta on the
    # headline. Skipped when the current period is unbounded on the left
    # (e.g. "All Time") since there's no anchor for a prior window.
    previous_totals = None
    if start is not None and end is not None:
        prev_start_param = _parse_iso_date(previous_start_date, field_name="previous_start_date")
        prev_end_param = _parse_iso_date(previous_end_date, field_name="previous_end_date")
        if prev_start_param is not None and prev_end_param is not None:
            # Caller supplied the exact prior period (it matches stepping the
            # picker back one unit), so the comparison lines up with calendar
            # months/quarters/years of unequal length instead of a fixed
            # day-span that drops a day on a 30- vs 31-day boundary.
            prev_start, prev_end = prev_start_param, prev_end_param
        else:
            period_length = end - start
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - period_length
        prev_filters = list(base_filters)
        prev_filters.append(Transaction.date >= prev_start)
        prev_filters.append(Transaction.date < prev_end + timedelta(days=1))
        prev_filters.append(or_(Category.seed_key != "uncategorized", Category.seed_key.is_(None)))
        prev_rows = (
            await db.execute(
                select(
                    Category.classification.label("classification"),
                    func.sum(Transaction.amount).label("amount"),
                    Transaction.currency.label("currency"),
                    func.strftime("%Y-%m-%d", Transaction.date).label("day"),
                )
                .select_from(Transaction)
                .join(Account, Account.id == Transaction.account_id)
                .join(Institution, Institution.id == Account.institution_id)
                .join(Category, Category.id == Transaction.category_id)
                .where(and_(*prev_filters))
                .group_by(Category.classification, Transaction.currency, func.strftime("%Y-%m-%d", Transaction.date))
            )
        ).all()
        prev_income = 0.0
        prev_expense = 0.0
        for r in prev_rows:
            amt = to_primary(float(r.amount or 0), r.currency, r.day)
            if r.classification == "income":
                prev_income += amt
            elif r.classification == "expense":
                prev_expense += -amt
            # investment/transfer excluded — same exclusion as the headline.
        previous_totals = {
            "start": prev_start.isoformat(),
            "end": prev_end.isoformat(),
            "income": prev_income,
            "expense": prev_expense,
            "net": prev_income - prev_expense,
        }

    # 5. Recurring — read the series maintained by sync/category write paths and
    # annotate how much each one charged in the selected period.
    series_rows = (
        await db.execute(
            select(RecurringSeries, Category)
            .outerjoin(Category, Category.id == RecurringSeries.category_id)
            .where(
                RecurringSeries.user_id == current_user.id,
                RecurringSeries.status == "active",
            )
            .order_by(RecurringSeries.last_seen_date.desc())
        )
    ).all()
    # Respect the global visible scope (hidden/enabled) so the Recurring panel
    # matches the rest of the page: keep only series with at least one linked
    # transaction in a visible account.
    all_series_ids = [s.id for s, _ in series_rows]
    visible_series_ids: set[int] = set()
    if all_series_ids:
        vis_rows = (
            await db.execute(
                select(func.distinct(Transaction.recurring_series_id))
                .select_from(Transaction)
                .join(Account, Account.id == Transaction.account_id)
                .join(Institution, Institution.id == Account.institution_id)
                .where(
                    Transaction.user_id == current_user.id,
                    Transaction.recurring_series_id.in_(all_series_ids),
                    Account.hidden.is_(False),
                    Institution.hidden.is_(False),
                    Institution.enabled.is_(True),
                )
            )
        ).all()
        visible_series_ids = {r[0] for r in vis_rows if r[0] is not None}
    series_rows = [(s, c) for s, c in series_rows if s.id in visible_series_ids]
    series_ids = [s.id for s, _ in series_rows]
    # The Recurring Expenses panel is a stable, timeline-independent view of the
    # user's *current* recurring expenses: each active series shown with its most
    # recent actual charge. It deliberately does NOT change with the page's
    # selected month/timeline, so we pull the latest charge per series rather than
    # a window slice (no "due"/projected rows).
    latest_by_series: dict[int, dict[str, Any]] = {}
    if series_ids:
        latest_rows = (
            await db.execute(
                select(
                    Transaction.recurring_series_id,
                    Transaction.amount,
                    Transaction.currency,
                    Account.name.label("account_name"),
                    Institution.name.label("institution_name"),
                )
                .select_from(Transaction)
                .join(Account, Account.id == Transaction.account_id)
                .join(Institution, Institution.id == Account.institution_id)
                .where(Transaction.recurring_series_id.in_(series_ids))
                .order_by(
                    Transaction.recurring_series_id,
                    Transaction.date.desc(),
                    Transaction.id.desc(),
                )
            )
        ).all()
        for row in latest_rows:
            sid = row.recurring_series_id
            if sid not in latest_by_series:  # first row per series = most recent charge
                latest_by_series[sid] = {
                    "amount": float(row.amount or 0.0),
                    "currency": row.currency,
                    "account_name": row.account_name,
                    "institution_name": row.institution_name,
                }

    recurring: list[dict[str, Any]] = []
    for s, c in series_rows:
        last_seen_iso = s.last_seen_date.isoformat() if s.last_seen_date else today.isoformat()
        latest = latest_by_series.get(s.id)
        last_amount = latest["amount"] if latest else float(s.avg_amount or 0.0)
        recurring.append(
            {
                "id": s.id,
                "name": s.display_name,
                "direction": s.direction,
                "category": (
                    {
                        "id": c.id,
                        "name": c.name,
                        "icon": c.icon,
                        "icon_set": c.icon_set,
                        "color_dark": c.color_dark,
                        "color_light": c.color_light,
                    }
                    if c is not None
                    else None
                ),
                "cadence": s.cadence,
                "interval_days": s.interval_days,
                # The panel shows `last_amount` (most recent actual charge);
                # `amount`/min/max remain available as the series average + range.
                "amount": s.avg_amount,
                "amount_min": s.amount_min,
                "amount_max": s.amount_max,
                "currency": s.currency,
                "amount_primary": to_primary(s.avg_amount, s.currency, last_seen_iso),
                "last_amount": last_amount,
                "last_amount_primary": to_primary(last_amount, s.currency, last_seen_iso),
                # Source of the most recent charge, so the panel can show where it
                # comes from (institution logo + account name).
                "account_name": (latest or {}).get("account_name"),
                "institution_name": (latest or {}).get("institution_name"),
                "last_seen_date": s.last_seen_date.isoformat() if s.last_seen_date else None,
                "next_expected_date": s.next_expected_date.isoformat() if s.next_expected_date else None,
                "occurrence_count": s.occurrence_count,
                "confidence": s.confidence,
                "is_confirmed": s.is_confirmed,
            }
        )

    return {
        "period": {
            "start": start.isoformat() if start is not None else None,
            "end": end.isoformat() if end is not None else None,
            "currency": primary_currency,
        },
        "totals": {
            "income": income_total,
            "expense": expense_total,
            "net": income_total - expense_total,
        },
        "expense_breakdown": expense_breakdown,
        "income_breakdown": income_breakdown,
        "needs_review": needs_review,
        "investment_activity": investment_activity,
        "trend_12_months": trend_12_months,
        "recurring": recurring,
        "previous_totals": previous_totals,
    }
