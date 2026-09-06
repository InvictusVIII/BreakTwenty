"""CSV export queries + serialization for BreakTwenty datasets.

Turns DB rows into CSV text using the canonical column contract in
``services.csv_io``. Each public function returns ``(filename, csv_text)``.

Design:
- The column layout, headers, and value formatting come from ``csv_io``. The current
  manual-account importer recognizes only its documented subset of these columns.
- Date conventions match each dataset's own UI so export == on-screen:
  - balances / networth_history use the user's LOCAL calendar day
    (``csv_io.serialize_local_date``), matching the net-worth graph.
  - transactions mirror the ``/transactions`` list, which emits the RAW calendar
    stored calendar date; the export applies the same
    ``start_date``/``end_date`` filter the route does, so a filtered export returns
    the exact rows (and dates) the list shows for the same params.
- ``include_hidden=True`` drops the hidden/enabled filters the UI read paths apply
  for a complete readable ownership export. Default (False) mirrors the on-screen view.
- ``include_internal_ids=True`` adds the non-portable DB id helper columns.
- CSV is RFC 4180 via the stdlib ``csv`` writer (proper quoting of commas/quotes/
  newlines) with a leading UTF-8 BOM for spreadsheet (Excel) compatibility; the
  import contract tolerates a leading BOM. Text cells that a spreadsheet could
  execute as formulas are prefixed with an apostrophe; typed numeric/date/boolean
  columns remain unchanged.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.brand import APP_BRAND_SLUG
from app.models import Account, BalanceHistory, Category, Holding, Institution, Setting, Transaction
from app.services import csv_io
from app.services.date_window import end_bound_exclusive, start_bound
from app.services.fx_history import build_to_primary_converter
from app.services.holdings_metrics import compute_holding_metrics, export_category
from app.services.networth import get_net_worth_payload
from app.services.user_utils import get_user_timezone_info_from_db

# Leading UTF-8 BOM so Excel detects UTF-8 (accented merchant names render
# correctly). The csv_io import contract tolerates/strips a leading BOM.
_BOM = "﻿"

_TYPED_CSV_HEADERS = frozenset(
    {
        "date",
        "month",
        "as_of",
        "amount",
        "balance",
        "quantity",
        "price",
        "commission",
        "raw_amount",
        "debit",
        "credit",
        "last_price",
        "average_cost",
        "unrealized_pnl_pct",
        "cost_basis",
        "market_value",
        "contract_multiplier",
        "change_pct",
        "daily_pnl",
        "total_assets",
        "total_liabilities",
        "net_worth",
        "income",
        "withholding",
        "total",
        "provider_recurring",
        "realized_pnl",
        "is_liability",
        "account_hidden",
        "institution_hidden",
        "opening_balance",
        "purchase_value",
        "is_imported_account",
    }
)
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _neutralize_spreadsheet_formula(header: str, value):
    if header in _TYPED_CSV_HEADERS or not isinstance(value, str):
        return value
    candidate = value.lstrip(" \t\r\n")
    if candidate.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _render_csv(headers: list[str], labels: list[str], rows: list[dict]) -> str:
    buffer = io.StringIO()
    buffer.write(_BOM)
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(labels)
    for row in rows:
        writer.writerow(
            [
                _neutralize_spreadsheet_formula(header, row.get(header, ""))
                for header in headers
            ]
        )
    return buffer.getvalue()


def _csv_ints(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


def _csv_strs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def export_filename(dataset_key: str, user_tz) -> str:
    today_local = datetime.now(timezone.utc).astimezone(user_tz).date().isoformat()
    return f"{APP_BRAND_SLUG}-{dataset_key.replace('_', '-')}-{today_local}.csv"


def _visibility_filters(include_hidden: bool, *, enabled_filter: bool = True) -> list:
    if include_hidden:
        return []
    filters = [Account.hidden.is_(False), Institution.hidden.is_(False)]
    if enabled_filter:
        filters.append(Institution.enabled.is_(True))
    return filters


async def export_transactions_csv(
    db: AsyncSession,
    user_id: int,
    *,
    account_ids: str | None = None,
    institution_ids: str | None = None,
    type: str | None = None,
    category_ids: str | None = None,
    symbol: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_hidden: bool = False,
    include_internal_ids: bool = False,
) -> tuple[str, str]:
    user_tz = await get_user_timezone_info_from_db(db, user_id)

    filters = [
        Transaction.user_id == user_id,
        Account.user_id == user_id,
        Institution.user_id == user_id,
        *_visibility_filters(include_hidden),
    ]

    acct_id_list = _csv_ints(account_ids)
    if acct_id_list:
        filters.append(Transaction.account_id.in_(acct_id_list))

    inst_id_list = _csv_ints(institution_ids)
    if inst_id_list:
        filters.append(Account.institution_id.in_(inst_id_list))

    type_list = _csv_strs(type)
    if len(type_list) > 1:
        filters.append(Transaction.type.in_(type_list))
    elif len(type_list) == 1:
        filters.append(Transaction.type == type_list[0])

    cat_id_list = _csv_ints(category_ids)
    if cat_id_list:
        filters.append(Transaction.category_id.in_(cat_id_list))

    if symbol is not None:
        filters.append(Transaction.symbol == symbol)

    # Mirror the /transactions route's date filter exactly (shared
    # services.date_window: inclusive calendar days, end covers the whole day) so
    # the export row set matches the on-screen list for the same params instead
    # of drifting at the day boundary.
    start_dt = start_bound(start_date)
    if start_dt is not None:
        filters.append(Transaction.date >= start_dt)
    end_dt = end_bound_exclusive(end_date)
    if end_dt is not None:
        filters.append(Transaction.date < end_dt)

    parent_category = aliased(Category)
    query = (
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.external_id,
            Transaction.date,
            Transaction.type,
            Transaction.symbol,
            Transaction.description,
            Transaction.amount,
            Transaction.currency,
            Transaction.quantity,
            Transaction.price,
            Transaction.commission,
            Transaction.mcc,
            Transaction.provider_recurring,
            Transaction.asset_category,
            Transaction.realized_pnl,
            Transaction.raw_amount,
            Transaction.category_source,
            Transaction.user_description,
            Transaction.user_notes,
            Account.name.label("account_name"),
            Account.external_id.label("account_external_id"),
            Account.account_type.label("account_type"),
            Account.currency.label("account_currency"),
            Institution.name.label("institution_name"),
            Institution.provider.label("institution_provider"),
            Category.name.label("category_name"),
            parent_category.name.label("category_parent_name"),
        )
        .join(Account, Transaction.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .outerjoin(
            Category,
            (Transaction.category_id == Category.id) & (Category.user_id == user_id),
        )
        .outerjoin(
            parent_category,
            (Category.parent_id == parent_category.id) & (parent_category.user_id == user_id),
        )
    )
    for clause in filters:
        query = query.where(clause)
    query = query.order_by(Transaction.date.desc(), Transaction.id.desc())

    result = await db.execute(query)
    rows = []
    for row in result.all():
        rows.append({
            "external_id": row.external_id or "",
            # Match the /transactions list date exactly (raw calendar day of the
            # stored timestamp), not the user-local day, so export == on-screen.
            "date": row.date.isoformat() if row.date else "",
            "institution_name": row.institution_name or "",
            "institution_provider": row.institution_provider or "",
            "account_name": row.account_name or "",
            "account_external_id": row.account_external_id or "",
            "account_type": row.account_type or "",
            "account_currency": csv_io.normalize_currency(row.account_currency) or "",
            "type": row.type or "",
            "description": row.user_description or row.description or "",
            "amount": csv_io.serialize_decimal(row.amount),
            "currency": csv_io.normalize_currency(row.currency) or "",
            "symbol": row.symbol or "",
            "quantity": csv_io.serialize_quantity(row.quantity),
            "price": csv_io.serialize_quantity(row.price),
            "commission": csv_io.serialize_decimal(row.commission),
            "mcc": row.mcc or "",
            "provider_recurring": csv_io.serialize_bool(row.provider_recurring),
            "asset_category": row.asset_category or "",
            "realized_pnl": csv_io.serialize_decimal(row.realized_pnl),
            "category": csv_io.join_category_path(row.category_parent_name, row.category_name),
            "category_source": row.category_source or "",
            "user_description": row.user_description or "",
            "user_notes": row.user_notes or "",
            "raw_amount": csv_io.serialize_decimal(row.raw_amount),
        })

    headers = csv_io.export_headers("transactions", include_internal_ids=include_internal_ids)
    labels = csv_io.export_header_labels("transactions", include_internal_ids=include_internal_ids)
    return export_filename("transactions", user_tz), _render_csv(headers, labels, rows)


async def export_balances_csv(
    db: AsyncSession,
    user_id: int,
    *,
    include_hidden: bool = False,
    include_internal_ids: bool = False,
    latest_only: bool = False,
) -> tuple[str, str]:
    user_tz = await get_user_timezone_info_from_db(db, user_id)

    filters = [
        BalanceHistory.user_id == user_id,
        Account.user_id == user_id,
        Institution.user_id == user_id,
        *_visibility_filters(include_hidden, enabled_filter=False),
    ]

    secured_asset = aliased(Account)
    query = (
        select(
            BalanceHistory.account_id,
            BalanceHistory.date,
            BalanceHistory.balance,
            Account.name.label("account_name"),
            Account.external_id.label("account_external_id"),
            Account.account_type.label("account_type"),
            Account.currency.label("account_currency"),
            Account.is_liability.label("is_liability"),
            Account.hidden.label("account_hidden"),
            Account.opening_balance,
            Account.opening_balance_date,
            Account.purchase_value,
            Account.purchase_date,
            Account.is_imported,
            secured_asset.name.label("secured_asset_account"),
            Institution.name.label("institution_name"),
            Institution.provider.label("institution_provider"),
            Institution.hidden.label("institution_hidden"),
        )
        .join(Account, BalanceHistory.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
        .outerjoin(
            secured_asset,
            (Account.secured_asset_account_id == secured_asset.id)
            & (secured_asset.user_id == user_id),
        )
    )
    if latest_only:
        latest = (
            select(
                BalanceHistory.account_id,
                func.max(BalanceHistory.date).label("max_date"),
            )
            .where(BalanceHistory.user_id == user_id)
            .group_by(BalanceHistory.account_id)
            .subquery()
        )
        query = query.join(
            latest,
            (BalanceHistory.account_id == latest.c.account_id)
            & (BalanceHistory.date == latest.c.max_date),
        )
    for clause in filters:
        query = query.where(clause)
    query = query.order_by(
        BalanceHistory.date.desc(), Institution.name.asc(), Account.name.asc()
    )

    result = await db.execute(query)
    rows = []
    for row in result.all():
        rows.append({
            "date": csv_io.serialize_local_date(row.date, user_tz),
            "institution_name": row.institution_name or "",
            "institution_provider": row.institution_provider or "",
            "account_name": row.account_name or "",
            "account_external_id": row.account_external_id or "",
            "account_type": row.account_type or "",
            # Signed for parity with the app: a liability exports as -balance (net owed) so
            # you-owe reads negative and a credit balance (overpaid) reads positive, matching
            # AccountBalanceCell. Assets export as-is (an overdraft stays negative). Zero → 0.00.
            "balance": csv_io.serialize_decimal(
                -row.balance if (row.is_liability and row.balance) else row.balance
            ),
            "currency": csv_io.normalize_currency(row.account_currency) or "",
            "is_liability": csv_io.serialize_bool(row.is_liability),
            "account_hidden": csv_io.serialize_bool(row.account_hidden),
            "institution_hidden": csv_io.serialize_bool(row.institution_hidden),
            "opening_balance": csv_io.serialize_decimal(row.opening_balance),
            "opening_balance_date": row.opening_balance_date.isoformat() if row.opening_balance_date else "",
            "purchase_value": csv_io.serialize_decimal(row.purchase_value),
            "purchase_date": row.purchase_date.isoformat() if row.purchase_date else "",
            "secured_asset_account": row.secured_asset_account or "",
            "is_imported_account": csv_io.serialize_bool(row.is_imported),
        })

    headers = csv_io.export_headers("balances", include_internal_ids=include_internal_ids)
    labels = csv_io.export_header_labels("balances", include_internal_ids=include_internal_ids)
    return export_filename("balances", user_tz), _render_csv(headers, labels, rows)


async def export_holdings_csv(
    db: AsyncSession,
    user_id: int,
    *,
    include_hidden: bool = False,
    include_internal_ids: bool = False,
    category: str | None = None,
    account_ids: str | None = None,
    filename_slug: str = "holdings",
) -> tuple[str, str]:
    user_tz = await get_user_timezone_info_from_db(db, user_id)

    filters = [
        Holding.user_id == user_id,
        Account.user_id == user_id,
        Institution.user_id == user_id,
        *_visibility_filters(include_hidden, enabled_filter=False),
    ]
    acct_id_list = _csv_ints(account_ids)
    if acct_id_list:
        filters.append(Holding.account_id.in_(acct_id_list))
    category_set = set(_csv_strs(category))

    query = (
        select(
            Holding.account_id,
            Holding.symbol,
            Holding.name,
            Holding.quantity,
            Holding.average_cost,
            Holding.last_price,
            Holding.market_value,
            Holding.contract_multiplier,
            Holding.change_pct,
            Holding.daily_pnl,
            Holding.currency,
            Holding.sector,
            Holding.last_updated,
            Account.name.label("account_name"),
            Account.external_id.label("account_external_id"),
            Account.account_type.label("account_type"),
            Institution.name.label("institution_name"),
            Institution.provider.label("institution_provider"),
        )
        .join(Account, Holding.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
    )
    for clause in filters:
        query = query.where(clause)
    query = query.order_by(Institution.name.asc(), Account.name.asc(), Holding.symbol.asc())

    result = await db.execute(query)
    rows = []
    for row in result.all():
        # Scope to the requested Investments sub-tab(s) (e.g. spot,cash / option / crypto).
        if category_set and export_category(row.symbol, row.name, row.account_type) not in category_set:
            continue
        metrics = compute_holding_metrics(
            symbol=row.symbol,
            name=row.name,
            quantity=row.quantity,
            market_value=row.market_value,
            average_cost=row.average_cost,
            last_price=row.last_price,
            raw_multiplier=row.contract_multiplier,
        )
        rows.append({
            "institution_name": row.institution_name or "",
            "institution_provider": row.institution_provider or "",
            "account_name": row.account_name or "",
            "account_external_id": row.account_external_id or "",
            "account_type": row.account_type or "",
            "symbol": row.symbol or "",
            "name": row.name or "",
            "last_price": csv_io.serialize_quantity(metrics.last_price),
            "average_cost": csv_io.serialize_decimal(metrics.average_cost_per_unit),
            "unrealized_pnl_pct": csv_io.serialize_decimal(metrics.unrealized_pnl_pct),
            "quantity": csv_io.serialize_quantity(metrics.quantity),
            "cost_basis": csv_io.serialize_decimal(metrics.cost_basis),
            "market_value": csv_io.serialize_decimal(metrics.market_value),
            "contract_multiplier": csv_io.serialize_quantity(row.contract_multiplier),
            "currency": csv_io.normalize_currency(row.currency) or "",
            "sector": row.sector or "",
            "change_pct": csv_io.serialize_decimal(row.change_pct),
            "daily_pnl": csv_io.serialize_decimal(row.daily_pnl),
            "as_of": csv_io.serialize_datetime_utc(row.last_updated),
        })

    headers = csv_io.export_headers("holdings", include_internal_ids=include_internal_ids)
    labels = csv_io.export_header_labels("holdings", include_internal_ids=include_internal_ids)
    return export_filename(filename_slug, user_tz), _render_csv(headers, labels, rows)


async def export_networth_history_csv(
    db: AsyncSession,
    user_id: int,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    include_hidden: bool = False,
) -> tuple[str, str]:
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    payload = await get_net_worth_payload(db, user_id, include_hidden=include_hidden)
    currency = (payload.get("current") or {}).get("currency") or "CAD"

    # Optional date window (the Dashboard passes its on-screen timeframe bounds so the
    # export matches the chart). Normal exports follow the current hidden-account scope;
    # the complete readable archive opts into hidden records explicitly.
    start_day = (start_date or "")[:10]
    end_day = (end_date or "")[:10]
    rows = []
    for point in reversed(payload.get("history", [])):  # newest day first
        day = point.get("date", "")
        if start_day and day < start_day:
            continue
        if end_day and day > end_day:
            continue
        rows.append({
            "date": point.get("date", ""),
            "total_assets": csv_io.serialize_decimal(point.get("total_assets")),
            "total_liabilities": csv_io.serialize_decimal(-(point.get("total_liabilities") or 0)),
            "net_worth": csv_io.serialize_decimal(point.get("net_worth")),
            "currency": currency,
        })

    headers = csv_io.export_headers("networth_history")
    labels = csv_io.export_header_labels("networth_history")
    return export_filename("networth_history", user_tz), _render_csv(headers, labels, rows)


async def export_all_data_archive(
    db: AsyncSession,
    user_id: int,
) -> tuple[str, bytes]:
    """Create the readable ownership export.

    This ZIP deliberately contains CSV data rather than database/key material. It is
    not a portable BreakTwenty restore format and must not be presented as one.
    """
    exports = [
        await export_transactions_csv(
            db,
            user_id,
            include_hidden=True,
            include_internal_ids=True,
        ),
        await export_balances_csv(
            db,
            user_id,
            include_hidden=True,
            include_internal_ids=True,
        ),
        await export_holdings_csv(
            db,
            user_id,
            include_hidden=True,
            include_internal_ids=True,
            filename_slug="all-holdings",
        ),
        await export_networth_history_csv(
            db,
            user_id,
            include_hidden=True,
        ),
    ]
    generated_at = datetime.now(timezone.utc)
    archive_name = f"{APP_BRAND_SLUG}-data-export-{generated_at.date().isoformat()}.zip"
    readme = f"""BreakTwenty readable data export
Generated: {generated_at.isoformat()}

This archive contains unencrypted CSV files. Store it somewhere secure.

It is not a portable BreakTwenty backup and cannot recreate every app setting,
institution connection, saved credential, or other local application state. The
breaktwenty.db file cannot be opened on its own. BreakTwenty also needs a separate
digital key, which is protected by your computer and user account. Because the database
and digital key belong together, copying the database or the whole app
data folder may not work on another computer. If you lose your computer, user account,
or digital key, BreakTwenty may no longer be able to unlock the database.

The transactions, balances, and holdings CSVs include hidden accounts and institutions
plus export-only context columns. The net-worth CSV is a derived history. Preserve all
four CSV files. For financial verification, continue to retain official statements from
your institutions.

Balance values use BreakTwenty's display sign convention: liabilities are negative.
CSV import into a manually created account is not a full restore workflow and does not
recreate account configuration or every exported field.
"""
    manifest = {
        "schema": "breaktwenty.readable-data-export",
        "version": 1,
        "generatedAt": generated_at.isoformat(),
        "encrypted": False,
        "portableRestore": False,
        "includesHiddenAccountsAndInstitutions": True,
        "files": [filename for filename, _csv_text in exports],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, csv_text in exports:
            archive.writestr(filename, csv_text.encode("utf-8"))
        archive.writestr("README.txt", readme.encode("utf-8"))
        archive.writestr(
            "manifest.json",
            f"{json.dumps(manifest, indent=2, sort_keys=True)}\n".encode("utf-8"),
        )
    return archive_name, buffer.getvalue()


_INCOME_KIND_TYPES = {
    "dividends": ["dividend", "distribution", "withholding_tax"],
    "interest": ["interest"],
}
_INCOME_ALL_TYPES = ["dividend", "distribution", "interest", "withholding_tax"]


async def export_income_by_position_csv(
    db: AsyncSession,
    user_id: int,
    *,
    kind: str | None = None,
    account_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    filename_slug: str = "income",
    sort_by_ticker: bool = False,
    label_overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Dividend/interest income aggregated by (position, currency, month) in each
    holding's NATIVE currency (no FX). Scoped to the Income view's account filter +
    timeline. Interest has no ticker, so it groups by currency + month (which can be
    two rows in one month when interest arrives in two currencies). Newest month first."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    income_types = _INCOME_KIND_TYPES.get((kind or "").strip().lower(), _INCOME_ALL_TYPES)

    filters = [
        Transaction.user_id == user_id,
        Account.user_id == user_id,
        Institution.user_id == user_id,
        Transaction.type.in_(income_types),
        *_visibility_filters(False),
    ]
    acct_id_list = _csv_ints(account_ids)
    if acct_id_list:
        filters.append(Transaction.account_id.in_(acct_id_list))
    if start_date:
        try:
            filters.append(Transaction.date >= date.fromisoformat(start_date[:10]))
        except ValueError:
            pass
    if end_date:
        try:
            filters.append(Transaction.date <= date.fromisoformat(end_date[:10]))
        except ValueError:
            pass

    query = (
        select(
            Transaction.date,
            Transaction.type,
            Transaction.symbol,
            Transaction.amount,
            Transaction.currency,
        )
        .join(Account, Transaction.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
    )
    for clause in filters:
        query = query.where(clause)
    result = (await db.execute(query)).all()

    buckets: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in result:
        if row.date is None:
            continue
        month = row.date.isoformat()[:7]
        currency = csv_io.normalize_currency(row.currency) or "CAD"
        symbol = "" if row.type == "interest" else (row.symbol or "").strip().upper()
        amount = float(row.amount or 0)
        bucket = buckets.setdefault((symbol, currency, month), {"income": 0.0, "withholding": 0.0})
        if row.type == "withholding_tax":
            bucket["withholding"] += -abs(amount)
        else:
            bucket["income"] += amount

    if sort_by_ticker:
        # Group by ticker: symbol primary, newest month first within each symbol.
        ordered = sorted(buckets.keys(), key=lambda key: key[2], reverse=True)
        ordered.sort(key=lambda key: (key[0], key[1]))
    else:
        ordered = sorted(buckets.keys(), key=lambda key: (key[0], key[1]))
        ordered.sort(key=lambda key: key[2], reverse=True)  # newest month first (stable)
    rows = []
    for symbol, currency, month in ordered:
        bucket = buckets[(symbol, currency, month)]
        rows.append({
            "symbol": symbol,
            "month": month,
            "income": csv_io.serialize_decimal(bucket["income"]),
            "withholding": csv_io.serialize_decimal(bucket["withholding"]),
            "total": csv_io.serialize_decimal(bucket["income"] + bucket["withholding"]),
            "currency": currency,
        })

    headers = csv_io.export_headers("income")
    labels = csv_io.export_header_labels("income")
    if label_overrides:
        labels = [label_overrides.get(h, label) for h, label in zip(headers, labels)]
    return export_filename(filename_slug, user_tz), _render_csv(headers, labels, rows)


async def _export_monthly_by_currency_csv(
    db: AsyncSession,
    user_id: int,
    *,
    income_types: list[str],
    filename_slug: str,
    account_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_withholding: bool = False,
) -> tuple[str, str]:
    """Income of the given transaction types aggregated per month and converted to
    the user's primary currency (each payment at its own date's historical FX), with
    the month total split into its US- and CAD-sourced portions (also in primary).
    Scoped to the Income view's account filter + timeline; newest month first."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)

    primary_setting = (
        await db.execute(
            select(Setting).where(Setting.key == "primary_currency", Setting.user_id == user_id)
        )
    ).scalar_one_or_none()
    primary_currency = (
        primary_setting.value if primary_setting and primary_setting.value else "CAD"
    ).upper()

    filters = [
        Transaction.user_id == user_id,
        Account.user_id == user_id,
        Institution.user_id == user_id,
        Transaction.type.in_(list(income_types) + (["withholding_tax"] if include_withholding else [])),
        *_visibility_filters(False),
    ]
    acct_id_list = _csv_ints(account_ids)
    if acct_id_list:
        filters.append(Transaction.account_id.in_(acct_id_list))
    if start_date:
        try:
            filters.append(Transaction.date >= date.fromisoformat(start_date[:10]))
        except ValueError:
            pass
    if end_date:
        try:
            filters.append(Transaction.date <= date.fromisoformat(end_date[:10]))
        except ValueError:
            pass

    query = (
        select(Transaction.date, Transaction.amount, Transaction.currency, Transaction.type)
        .join(Account, Transaction.account_id == Account.id)
        .join(Institution, Account.institution_id == Institution.id)
    )
    for clause in filters:
        query = query.where(clause)
    result = (await db.execute(query)).all()

    currencies = {csv_io.normalize_currency(row.currency) or "CAD" for row in result if row.date}
    currencies.add(primary_currency)
    to_primary = await build_to_primary_converter(
        db, primary_currency, currencies, datetime.now(timezone.utc).date().isoformat()
    )

    # month -> primary-currency totals; US = USD-sourced, CAD = CAD-sourced (both in
    # primary). Any other source currency still lands in `total` only.
    buckets: dict[str, dict[str, float]] = {}
    for row in result:
        if row.date is None:
            continue
        day_iso = row.date.isoformat()
        currency = csv_io.normalize_currency(row.currency) or "CAD"
        converted = to_primary(row.amount, currency, day_iso)
        bucket = buckets.setdefault(day_iso[:7], {"total": 0.0, "us": 0.0, "cad": 0.0, "withholding": 0.0})
        # Withholding tax rides alongside (not within) the dividend total — a positive
        # magnitude of tax withheld that month.
        if include_withholding and row.type == "withholding_tax":
            bucket["withholding"] += -abs(converted)
            continue
        bucket["total"] += converted
        if currency == "USD":
            bucket["us"] += converted
        elif currency == "CAD":
            bucket["cad"] += converted

    rows = []
    for month in sorted(buckets.keys(), reverse=True):  # newest month first
        bucket = buckets[month]
        record = {
            "month": month,
            "us": csv_io.serialize_decimal(bucket["us"]),
            "cad": csv_io.serialize_decimal(bucket["cad"]),
            "total": csv_io.serialize_decimal(bucket["total"]),
        }
        if include_withholding:
            record["withholding"] = csv_io.serialize_decimal(bucket["withholding"])
        rows.append(record)

    headers = ["month", "us"]
    labels = [
        "Month",
        f"US ({primary_currency})",
    ]
    if include_withholding:  # withholding sits right after the US column
        headers.append("withholding")
        labels.append(f"Withholding Tax ({primary_currency})")
    headers += ["cad", "total"]
    labels += [f"CAD ({primary_currency})", f"Total ({primary_currency})"]
    return export_filename(filename_slug, user_tz), _render_csv(headers, labels, rows)


async def export_dividends_monthly_csv(
    db: AsyncSession,
    user_id: int,
    *,
    account_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    filename_slug: str = "dividends-monthly",
) -> tuple[str, str]:
    """Companion 'sheet' for the dividends export: gross dividend income per month in
    primary currency, split into US/CAD source portions. Newest month first."""
    return await _export_monthly_by_currency_csv(
        db,
        user_id,
        income_types=["dividend", "distribution"],
        filename_slug=filename_slug,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
        include_withholding=True,
    )


async def export_dividends_combined_csv(
    db: AsyncSession,
    user_id: int,
    *,
    account_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[str, str]:
    """One file, two tables laid out SIDE BY SIDE (single Save dialog): the per-ticker
    breakdown on the left (sorted by ticker so a symbol's months group together) and the
    monthly summary to its right, as adjacent column blocks separated by a spacer column so
    the two never share a column. Same account + timeline scope as the dividends view."""
    user_tz = await get_user_timezone_info_from_db(db, user_id)
    _, monthly_csv = await export_dividends_monthly_csv(
        db, user_id, account_ids=account_ids, start_date=start_date, end_date=end_date,
    )
    _, ticker_csv = await export_income_by_position_csv(
        db,
        user_id,
        kind="dividends",
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
        sort_by_ticker=True,
        label_overrides={"income": "Dividends", "withholding": "Withholding Tax"},
    )

    def _strip_bom(text: str) -> str:
        return text[len(_BOM):] if text.startswith(_BOM) else text

    # Parse each rendered table back into rows, then emit them as adjacent column blocks
    # (ticker | spacer | monthly) so the file opens with the two tables next to each other
    # with independent column widths, instead of sharing columns down a single stack.
    ticker_rows = list(csv.reader(io.StringIO(_strip_bom(ticker_csv))))
    monthly_rows = list(csv.reader(io.StringIO(_strip_bom(monthly_csv))))
    ticker_width = max((len(row) for row in ticker_rows), default=0)
    monthly_width = max((len(row) for row in monthly_rows), default=0)

    buffer = io.StringIO()
    buffer.write(_BOM)
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        ["Dividends by Ticker"] + [""] * (ticker_width - 1)
        + [""]
        + ["Dividends by Month"] + [""] * (monthly_width - 1)
    )
    for i in range(max(len(ticker_rows), len(monthly_rows))):
        left = ticker_rows[i] if i < len(ticker_rows) else []
        right = monthly_rows[i] if i < len(monthly_rows) else []
        left = left + [""] * (ticker_width - len(left))
        right = right + [""] * (monthly_width - len(right))
        writer.writerow(left + [""] + right)
    return export_filename("dividends", user_tz), buffer.getvalue()


async def export_interest_monthly_csv(
    db: AsyncSession,
    user_id: int,
    *,
    account_ids: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    filename_slug: str = "interest",
) -> tuple[str, str]:
    """Interest income per month in primary currency, split into US/CAD source
    portions — same shape as the dividends monthly summary. Newest month first."""
    return await _export_monthly_by_currency_csv(
        db,
        user_id,
        income_types=["interest"],
        filename_slug=filename_slug,
        account_ids=account_ids,
        start_date=start_date,
        end_date=end_date,
    )
