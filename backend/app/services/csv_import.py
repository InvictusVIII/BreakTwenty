"""CSV import into a chosen manual account.

Generic CSV import for user-created MANUAL accounts only (see
``services.manual_institutions``). The target account is selected up front in the
Add-Institution wizard, so import files carry NO institution/account columns — just
``Date``+``Amount`` (transactions) or ``Date``+``Balance`` (balances). Synced
accounts and the Cash holder are never import targets.

- transactions: require Date+Amount; optional Description, Category (``Parent >
  Child``), Type, Symbol, Currency. Identity is the ``External ID`` column when
  present (round-trip from a BreakTwenty export) else a deterministic minted id with a
  stable per-file ``occurrence`` so identical same-day rows don't merge and
  re-importing the SAME file is idempotent (existing id -> update in place; else
  insert). No content-overlap suppression — a manual account has no competing sync
  feed to double-count against.
- balances: require Date+Balance; upsert one row per account per local day.
- currency safeguard: the account has ONE native currency; any row whose explicit
  Currency differs is a conflict that aborts the import (nothing written). A file
  with no Currency column is taken to be the account's currency.
- ``commit=False`` runs the whole pass in a transaction and rolls back, so the
  report is an accurate dry-run preview.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.models import Account, BalanceHistory, Category, Institution, Transaction
from app.services import csv_io
from app.services.categories import CategoryResolver, canonical_amount_for_classification
from app.services.manual_institutions import MANUAL_INSTITUTION_PROVIDER, recompute_manual_account_balance
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.user_utils import get_user_timezone_info_from_db

logger = logging.getLogger("breaktwenty.csv_import")

_REPORT_LIST_CAP = 50

# Account CSV import recognizes ONLY the columns documented in the import
# instructions; every other column (currency, external id, type, symbol, debit/
# credit, quantity/price/commission, …) is dropped during row normalization so
# the import behaves exactly as the instructions describe. Dedup is therefore
# always the minted date+amount+description fingerprint — no hidden External ID
# round-trip — so editing one of those fields makes a row import as a new record.
_IMPORT_ALLOWED_KEYS = {
    "transactions": {"date", "amount", "description", "category"},
    "balances": {"date", "balance"},
}


def detect_dataset(headers: list[str]) -> str | None:
    """Best-effort dataset detection, tolerant of friendly labels and snake_case."""
    norm = {csv_io.normalize_header_key(h) for h in headers}

    def present(dataset_key: str) -> set:
        mapping = csv_io.canonical_header_map(dataset_key)
        return {mapping[key] for key in norm if key in mapping}

    bal = present("balances")
    if "balance" in bal and "date" in bal:
        return "balances"
    txn = present("transactions")
    if "amount" in txn and "date" in txn:
        return "transactions"
    return None


def parse_csv(csv_text: str) -> tuple[list[str], list[dict]]:
    """Parse CSV text (BOM-tolerant) into (headers, list-of-row-dicts)."""
    if csv_text.startswith("﻿"):
        csv_text = csv_text[1:]
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v if v is not None else "") for k, v in raw.items()})
    return headers, rows


def _get(row: dict, key: str) -> str:
    return str(row.get(key, "") or "").strip()


def _resolve_amount(row: dict, amount_text: str) -> float | None:
    """Signed amount; supports a two-column debit/credit shape and ``(123)`` negatives."""
    debit = csv_io.parse_decimal(_get(row, "debit"))
    credit = csv_io.parse_decimal(_get(row, "credit"))
    if debit is not None or credit is not None:
        return (credit or 0.0) - (debit or 0.0)
    text = (amount_text or "").strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    value = csv_io.parse_decimal(text)
    if value is None:
        return None
    return -value if negative else value


_DATE_SPLIT_RE = re.compile(r"^\s*(\d{1,4})\s*[/.\-]\s*(\d{1,4})\s*[/.\-]\s*(\d{1,4})\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}
_MONTHNAME_MDY_RE = re.compile(r"^\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\s*$")  # Apr 23, 2024
_MONTHNAME_DMY_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\s*$")  # 23 Apr 2024


def _norm_year(value: int) -> int:
    return value + (2000 if value < 70 else 1900) if value < 100 else value


def _month_name_to_iso(text: str):
    for rgx, order in ((_MONTHNAME_MDY_RE, "mdy"), (_MONTHNAME_DMY_RE, "dmy")):
        m = rgx.match(text)
        if not m:
            continue
        mon, day, year = (m.group(1), m.group(2), m.group(3)) if order == "mdy" else (m.group(2), m.group(1), m.group(3))
        month = _MONTHS.get(mon[:3].lower())
        if not month:
            return None
        try:
            return date(_norm_year(int(year)), month, int(day)).isoformat()
        except ValueError:
            return None
    return None


def build_date_to_iso(date_texts: list[str]):
    """Infer one date format for the whole file and return ``(to_iso, error)``.

    ``to_iso(text)`` normalizes a cell to ``YYYY-MM-DD`` (or returns None). Supports
    ISO (year-first), month names, and numeric M/D/Y vs D/M/Y — disambiguating the
    numeric order at the file level (a value with day > 12 anywhere fixes it). The
    normalized ISO string is then anchored to local noon by
    ``csv_io.parse_csv_date_to_utc``. ``error`` is set only when dates are present but
    the order is genuinely ambiguous or unrecognized (so the import surfaces one clear
    message instead of failing every row silently)."""
    samples = [t.strip() for t in date_texts if t and t.strip()]
    if not samples:
        return (lambda _t: None), None

    if all(_month_name_to_iso(t) for t in samples):
        return _month_name_to_iso, None

    triples = []
    for text in samples:
        m = _DATE_SPLIT_RE.match(text)
        if not m:
            return (lambda _t: None), f"date '{text}' isn't a recognized format — use YYYY-MM-DD"
        triples.append(tuple(int(g) for g in m.groups()))

    # Year-first (ISO-like): the first component is the 4-digit year.
    if all(len(str(a)) == 4 for a, _b, _c in triples):
        def year_first(text):
            m = _DATE_SPLIT_RE.match(text or "")
            if not m:
                return None
            y, mo, d = (int(g) for g in m.groups())
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                return None
        return year_first, None

    # Year-last: the third component is the year; disambiguate M/D vs D/M.
    first_gt12 = any(a > 12 for a, _b, _c in triples)
    second_gt12 = any(b > 12 for _a, b, _c in triples)
    if first_gt12 and second_gt12:
        return (lambda _t: None), "dates mix day/month order — reformat as YYYY-MM-DD"
    if not first_gt12 and not second_gt12:
        return (lambda _t: None), "dates are ambiguous (can't tell MM/DD from DD/MM) — reformat as YYYY-MM-DD"
    day_first = first_gt12

    def year_last(text):
        m = _DATE_SPLIT_RE.match(text or "")
        if not m:
            return None
        a, b, c = (int(g) for g in m.groups())
        day, month = (a, b) if day_first else (b, a)
        try:
            return date(_norm_year(c), month, day).isoformat()
        except ValueError:
            return None
    return year_last, None


async def _resolve_category_leaf(db, user_id: int, path: str, cache: dict):
    if not path:
        return None
    if path in cache:
        return cache[path]
    parent_name, leaf_name = csv_io.split_category_path(path)
    if not leaf_name:
        cache[path] = None
        return None
    parent_cat = aliased(Category)
    query = select(Category.id, Category.classification).where(
        Category.user_id == user_id,
        func.lower(Category.name) == leaf_name.lower(),
    )
    if parent_name:
        query = query.join(parent_cat, Category.parent_id == parent_cat.id).where(
            func.lower(parent_cat.name) == parent_name.lower()
        )
    result = (await db.execute(query.limit(1))).first()
    cache[path] = result
    return result


async def _load_target_account(db, user_id: int, account_id):
    """Resolve a valid manual import target. Returns ((account, institution), None)
    or (None, error_message)."""
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return None, "A target account is required"
    row = (
        await db.execute(
            select(Account, Institution)
            .join(Institution, Account.institution_id == Institution.id)
            .where(Account.id == account_id, Account.user_id == user_id)
        )
    ).first()
    if row is None:
        return None, "Account not found"
    account, institution = row
    if (institution.provider or "") != MANUAL_INSTITUTION_PROVIDER:
        return None, "Import is only supported into manual accounts"
    return (account, institution), None


async def import_csv(
    db,
    user_id: int,
    *,
    account_id,
    dataset: str,
    csv_text: str,
    commit: bool,
) -> dict:
    if dataset not in ("balances", "transactions"):
        return {"status": "error", "message": f"Unsupported import dataset: {dataset}"}

    target, error = await _load_target_account(db, user_id, account_id)
    if error:
        return {"status": "error", "message": error}
    account, institution = target
    # Capture identity now; the ORM row's attributes expire after commit and a
    # post-commit access would trigger an illegal sync lazy-load.
    account_pk = account.id
    account_name = account.name
    account_currency = (account.currency or "CAD")

    user_tz = await get_user_timezone_info_from_db(db, user_id)
    _, rows = await asyncio.to_thread(parse_csv, csv_text)
    # Normalize incoming headers to canonical keys so friendly export labels,
    # snake_case keys, and minor foreign-CSV variants all resolve.
    header_map = csv_io.canonical_header_map(dataset)
    allowed = _IMPORT_ALLOWED_KEYS.get(dataset, set())
    rows = [
        {
            canonical: value
            for key, value in row.items()
            if (canonical := header_map.get(csv_io.normalize_header_key(key))) in allowed
        }
        for row in rows
    ]
    # Infer the date format once for the whole file (ISO, M/D/Y, D/M/Y, month names).
    date_to_iso, date_error = build_date_to_iso([_get(row, "date") for row in rows])

    counters = {"inserted": 0, "updated": 0, "skipped": 0}
    warnings: list[str] = []
    errors: list[str] = []
    conflicts: list[str] = []
    currencies: set[str] = set()

    def warn(message: str) -> None:
        if len(warnings) < _REPORT_LIST_CAP:
            warnings.append(message)

    def err(message: str) -> None:
        if len(errors) < _REPORT_LIST_CAP:
            errors.append(message)

    def conflict(message: str) -> None:
        if len(conflicts) < _REPORT_LIST_CAP:
            conflicts.append(message)

    if date_error:
        err(date_error)

    # ``commit=False`` (preview) always rolls back. A currency conflict aborts even a
    # commit, so a mismatched file never writes a partial import.
    try:
        async with sqlite_write_gate():
            if dataset == "balances":
                await _import_balances(
                    db, user_id, account, rows, user_tz, date_to_iso,
                    counters, warn, err, conflict, currencies,
                )
            else:
                await _import_transactions(
                    db, user_id, account, institution, rows, user_tz, date_to_iso,
                    counters, warn, err, conflict, currencies,
                )
            do_commit = commit and not conflicts
            if do_commit:
                await db.commit()
            else:
                await db.rollback()
    except Exception:
        await db.rollback()
        raise

    if commit and not conflicts and currencies:
        try:
            async with sqlite_write_gate():
                await _backfill_fx(db, user_id, currencies, dataset)
                await db.commit()
        except Exception as exc:  # best effort; FX gaps degrade gracefully
            logger.warning("csv import FX backfill failed: %s", exc)
            await db.rollback()

    # If the account has a starting balance, derive its running balance from the
    # (now-imported) transactions so net worth reflects them. No-op otherwise.
    if commit and not conflicts and dataset == "transactions":
        try:
            async with sqlite_write_gate():
                acct = (
                    await db.execute(
                        select(Account).where(Account.id == account_pk, Account.user_id == user_id)
                    )
                ).scalar_one_or_none()
                if acct is not None and acct.opening_balance is not None:
                    await recompute_manual_account_balance(db, user_id, acct)
                    await db.commit()
        except Exception as exc:  # best effort; balance can be recomputed from settings
            logger.warning("balance recompute after import failed: %s", exc)
            await db.rollback()

    # Balance imports can carry snapshots earlier than the declared opening date — move
    # the opening anchor to the earliest point so the "Opened"/"Borrowed" badge + the
    # revaluation floor agree (the transaction path does this inside recompute; the
    # balance path writes balance_history directly and never runs it).
    if commit and not conflicts and dataset == "balances":
        try:
            async with sqlite_write_gate():
                acct = (
                    await db.execute(
                        select(Account).where(Account.id == account_pk, Account.user_id == user_id)
                    )
                ).scalar_one_or_none()
                if acct is not None:
                    earliest = (
                        await db.execute(
                            select(func.min(BalanceHistory.date)).where(
                                BalanceHistory.user_id == user_id,
                                BalanceHistory.account_id == account_pk,
                            )
                        )
                    ).scalar()
                    if earliest is not None and (
                        acct.opening_balance_date is None or acct.opening_balance_date > earliest
                    ):
                        acct.opening_balance_date = earliest
                        await db.commit()
        except Exception as exc:  # best effort; the badge can be re-derived later
            logger.warning("opening-date sync after balance import failed: %s", exc)
            await db.rollback()

    return {
        "status": "ok",
        "dataset": dataset,
        "account_id": account_pk,
        "account_name": account_name,
        "account_currency": account_currency,
        "committed": bool(commit and not conflicts),
        "total_rows": len(rows),
        **counters,
        "currency_conflicts": conflicts,
        "warnings": warnings,
        "errors": errors,
    }


async def _import_balances(
    db, user_id, account, rows, user_tz, date_to_iso, counters, warn, err, conflict, currencies
):
    acct_currency = (account.currency or "CAD").upper()
    existing_rows = (
        await db.execute(
            select(BalanceHistory).where(
                BalanceHistory.user_id == user_id,
                BalanceHistory.account_id == account.id,
            )
        )
    ).scalars().all()
    day_map = {csv_io.serialize_local_date(r.date, user_tz): r for r in existing_rows}
    today_local = datetime.now(timezone.utc).astimezone(user_tz).date()

    for index, row in enumerate(rows, start=2):  # row 1 is the header
        date_text = _get(row, "date")
        balance_text = _get(row, "balance")
        if not (date_text and balance_text):
            err(f"row {index}: missing Date or Balance")
            counters["skipped"] += 1
            continue
        balance = csv_io.parse_decimal(balance_text)
        iso = date_to_iso(date_text)
        date_dt = csv_io.parse_csv_date_to_utc(iso, user_tz) if iso else None
        if balance is None or date_dt is None:
            err(f"row {index}: unparseable Date or Balance")
            counters["skipped"] += 1
            continue
        if date_dt.astimezone(user_tz).date() > today_local:
            err(f"row {index}: date is in the future — skipped")
            counters["skipped"] += 1
            continue
        row_ccy = csv_io.normalize_currency(_get(row, "currency"))
        if row_ccy and row_ccy != acct_currency:
            conflict(f"row {index}: {row_ccy} differs from account currency {acct_currency}")
            counters["skipped"] += 1
            continue

        currencies.add(acct_currency)
        local_date = date_dt.astimezone(user_tz).date()
        local_day = local_date.isoformat()
        existing = day_map.get(local_day)
        if existing is not None:
            existing.balance = balance
            existing.date = local_date
            counters["updated"] += 1
        else:
            new_row = BalanceHistory(
                user_id=user_id, account_id=account.id, balance=balance, date=local_date
            )
            db.add(new_row)
            day_map[local_day] = new_row
            counters["inserted"] += 1


async def _import_transactions(
    db, user_id, account, institution, rows, user_tz, date_to_iso, counters, warn, err, conflict, currencies
):
    resolver = await CategoryResolver.build(db, user_id)
    category_cache: dict = {}
    acct_currency = (account.currency or "CAD").upper()
    institution_name = institution.name
    # Stable per-file occurrence so identical same-day rows mint distinct ids (and a
    # re-import of the same file lands the same ids -> updates, not duplicates).
    occurrence_counter: dict[tuple, int] = defaultdict(int)
    today_local = datetime.now(timezone.utc).astimezone(user_tz).date()

    for index, row in enumerate(rows, start=2):
        external_id = _get(row, "external_id")
        if csv_io.is_reserved_external_id(external_id):
            warn(f"row {index}: reserved id '{external_id}' is not importable into a manual account")
            counters["skipped"] += 1
            continue

        date_text = _get(row, "date")
        amount_text = _get(row, "amount")
        if not (date_text and amount_text):
            err(f"row {index}: missing Date or Amount")
            counters["skipped"] += 1
            continue
        amount = _resolve_amount(row, amount_text)
        iso = date_to_iso(date_text)
        date_dt = csv_io.parse_csv_date_to_utc(iso, user_tz) if iso else None
        if amount is None or date_dt is None:
            err(f"row {index}: unparseable Date or Amount")
            counters["skipped"] += 1
            continue
        if date_dt.astimezone(user_tz).date() > today_local:
            err(f"row {index}: date is in the future — skipped")
            counters["skipped"] += 1
            continue
        row_ccy = csv_io.normalize_currency(_get(row, "currency"))
        if row_ccy and row_ccy != acct_currency:
            conflict(f"row {index}: {row_ccy} differs from account currency {acct_currency}")
            counters["skipped"] += 1
            continue

        currency = acct_currency
        tx_type = (_get(row, "type") or "other").lower()
        symbol = _get(row, "symbol") or None
        description = _get(row, "description") or None

        # Category: explicit Parent > Child -> manual (sign coerced); else rule chain.
        category_id = None
        category_source = None
        category_path = _get(row, "category")
        explicit_source = _get(row, "category_source").lower()
        if category_path:
            leaf = await _resolve_category_leaf(db, user_id, category_path, category_cache)
            if leaf is not None:
                category_id = leaf[0]
                # CSV-provided categories are tagged "import": protected from auto
                # re-evaluation, but overwritable by a newer re-import — unlike an
                # in-app "manual" override, which a re-import must never clobber.
                category_source = explicit_source if explicit_source in ("auto", "rule", "manual") else "import"
                if category_source in ("manual", "import"):
                    amount = canonical_amount_for_classification(amount, leaf[1])
            else:
                warn(f"row {index}: category '{category_path}' did not resolve to a leaf")
        if category_id is None:
            category_id, category_source = resolver.resolve(
                description=description,
                amount=amount,
                provider=None,
                account_type=account.account_type,
                transaction_type=tx_type,
                mcc=_get(row, "mcc") or None,
                transaction_date=date_dt,
                cash_tracking_start=institution.cash_tracking_start,
            )

        if not external_id:
            date_iso = date_dt.astimezone(user_tz).date().isoformat()
            occ_key = (
                date_iso, tx_type, round(float(amount), 2), currency,
                " ".join((description or "").split()).lower(), (symbol or "").upper(),
            )
            occurrence = occurrence_counter[occ_key]
            occurrence_counter[occ_key] += 1
            external_id = csv_io.mint_external_id(
                institution_name=institution_name,
                account_name=account.name,
                date_iso=date_iso,
                tx_type=tx_type,
                amount=amount,
                currency=currency,
                description=description,
                symbol=symbol,
                occurrence=occurrence,
            )

        existing = (
            await db.execute(
                select(Transaction).where(
                    Transaction.user_id == user_id,
                    Transaction.account_id == account.id,
                    Transaction.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            # Update in place; preserve the immutable provider description.
            existing.account_id = account.id
            existing.date = date_dt.astimezone(user_tz).date()
            existing.type = tx_type
            existing.symbol = symbol
            existing.amount = amount
            existing.currency = currency
            existing.quantity = csv_io.parse_decimal(_get(row, "quantity"))
            existing.price = csv_io.parse_decimal(_get(row, "price"))
            existing.commission = csv_io.parse_decimal(_get(row, "commission"))
            if _get(row, "user_description"):
                existing.user_description = _get(row, "user_description")
            if _get(row, "user_notes"):
                existing.user_notes = _get(row, "user_notes")
            # In-app manual categorizations win — a re-import never clobbers them.
            if category_id is not None and existing.category_source != "manual":
                existing.category_id = category_id
                existing.category_source = category_source
            counters["updated"] += 1
            continue

        currencies.add(currency)
        db.add(Transaction(
            user_id=user_id,
            account_id=account.id,
            date=date_dt.astimezone(user_tz).date(),
            type=tx_type,
            symbol=symbol,
            description=description,
            amount=amount,
            currency=currency,
            quantity=csv_io.parse_decimal(_get(row, "quantity")),
            price=csv_io.parse_decimal(_get(row, "price")),
            commission=csv_io.parse_decimal(_get(row, "commission")),
            mcc=_get(row, "mcc") or None,
            provider_recurring=csv_io.parse_bool(_get(row, "provider_recurring")),
            external_id=external_id,
            category_id=category_id,
            category_source=category_source,
            user_description=_get(row, "user_description") or None,
            user_notes=_get(row, "user_notes") or None,
        ))
        counters["inserted"] += 1


async def _backfill_fx(db, user_id: int, currencies: set[str], dataset: str) -> None:
    from app.services.fx_history import backfill_fx_rates

    non_primary = {c for c in currencies if c and c != "CAD"}
    if not non_primary:
        return
    model = BalanceHistory if dataset == "balances" else Transaction
    span = (
        await db.execute(
            select(func.min(model.date), func.max(model.date)).where(model.user_id == user_id)
        )
    ).first()
    if not span or span[0] is None:
        return
    start = span[0].astimezone(timezone.utc).date().isoformat() if span[0].tzinfo else span[0].date().isoformat()
    end = span[1].astimezone(timezone.utc).date().isoformat() if span[1].tzinfo else span[1].date().isoformat()
    await backfill_fx_rates(db, sorted(non_primary), start, end)
