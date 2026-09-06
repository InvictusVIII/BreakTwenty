"""Import Questrade PDF account statements into balance history.

Questrade exposes no historical-NAV API, so monthly statement PDFs are the only source
of pre-connection month-end value (see :mod:`app.services.questrade_statement_pdf`).
This service turns parsed statement points into ``BalanceHistory`` rows:

- Statements whose account number matches an existing Questrade account (active or
  already-imported) backfill that account's history — idempotent per local day, so
  re-uploading the same statements just dedupes/overwrites, never duplicates.
- Statements for an account number with no match (a closed/transferred account) create
  an ``is_imported`` account under the user's Questrade institution: Questrade-branded,
  never synced or pruned, with its date range derived from the points themselves.

Mirrors the IBKR Flex file-import contract: returns import/skip/error counts (plus
created/updated account counts and warnings) for a direct, IBKR-style result line.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Institution
from app.services.questrade_statement_pdf import parse_statements
from app.services.sqlite_write_gate import sqlite_write_gate

QUESTRADE_PROVIDER = "questrade"


def _account_digits(value) -> str:
    """Digit-only key for an account number. The live Questrade connector stores
    ``external_id`` as ``account:<number>`` (older imports stored bare digits), so matching
    on digits resolves a statement to its synced or previously-imported account regardless
    of the stored format."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _to_normalized_transactions(statement_txns, account):
    """Turn parsed statement transactions into ``NormalizedTransaction``s with a
    deterministic, **symbol-independent** ``external_id``
    (``qtstmt:<acct>:<date>:<type>:<amount>:<seq>``), so re-uploading the same statements upserts
    in place instead of duplicating — even after symbol normalization changes the displayed
    ticker. ``seq`` disambiguates identical same-day rows in stable parse order."""
    from app.connectors.types import NormalizedTransaction

    seen: dict = {}
    normalized = []
    for txn in statement_txns:
        # Symbol-INDEPENDENT: symbols are normalized post-parse, so including one would change
        # the id and duplicate on re-upload. (date, type, amount, seq) identifies a row; seq
        # disambiguates identical same-day rows in stable parse order.
        key = (txn.date, txn.type, f"{txn.amount:.2f}")
        seq = seen.get(key, 0)
        seen[key] = seq + 1
        external_id = f"qtstmt:{account.external_id}:{txn.date}:{txn.type}:{abs(txn.amount):.2f}:{seq}"
        year, month, day = (int(part) for part in txn.date.split("-"))
        normalized.append(NormalizedTransaction(
            external_id=external_id,
            date=datetime(year, month, day, 12, 0, 0),
            type=txn.type,
            amount=txn.amount,
            currency=txn.currency,
            symbol=txn.symbol,
            description=txn.description,
        ))
    return normalized


async def _get_questrade_institution(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
) -> Institution | None:
    institution = (
        await db.execute(
            select(Institution).where(
                Institution.id == int(institution_id),
                Institution.user_id == user_id,
                Institution.provider == QUESTRADE_PROVIDER,
                Institution.enabled.is_(True),
            )
        )
    ).scalars().first()
    return institution


async def import_questrade_statements(
    db: AsyncSession,
    user_id: int,
    institution_id: int,
    items,
) -> dict:
    """*items*: iterable of ``(filename, pdf_bytes)``. Parse, group by account number,
    match or create accounts, and upsert monthly balance points. Returns a result dict
    of ``imported`` (points written) / ``skipped`` / ``errors`` plus account counts and
    warnings. Commits on success."""
    from app.connectors.persistence import (
        _upsert_balance_history_series,
        upsert_transactions_for_account,
    )
    from app.services.categories import CategoryResolver
    from app.services.questrade_statement_transactions import (
        build_symbol_index,
        canonical_symbol_map,
        canonicalize_symbol,
        parse_statement_transactions,
        resolve_symbol,
    )
    from app.services.user_utils import get_user_timezone_info_from_db

    items = list(items)
    points = await asyncio.to_thread(parse_statements, items)

    skipped = errors = 0
    warnings: list[str] = []
    by_account: dict[str, list] = {}
    for point in points:
        label = point.filename or "statement"
        if point.error or point.as_of_date is None or point.balance is None:
            errors += 1
            detail = point.error or "; ".join(point.warnings) or "could not parse statement"
            warnings.append(f"{label}: {detail}")
            continue
        if not point.account_number:
            skipped += 1
            warnings.append(f"{label}: account number not found; skipped")
            continue
        if not _account_digits(point.account_number):
            skipped += 1
            warnings.append(f"{label}: account number contains no digits; skipped")
            continue
        by_account.setdefault(point.account_number, []).append(point)
        if point.source == "empty_statement" and point.warnings:
            warnings.append(f"{label}: {'; '.join(point.warnings)}")

    if not by_account:
        return {
            "status": "ok", "imported": 0, "skipped": skipped, "errors": errors,
            "accounts_created": 0, "accounts_updated": 0, "warnings": warnings,
        }

    # Parse every readable ledger before opening the SQLite writer section. We cannot know
    # whether an account is imported until that section revalidates the connection/accounts;
    # transactions for synced accounts are discarded below because their API sync owns them.
    digits_by_filename = {
        point.filename: _account_digits(point.account_number)
        for point in points
        if point.filename and point.account_number
    }
    global_holdings: dict = {}
    txns_by_digits: dict[str, list] = {}
    parse_items = [
        (name, data, digits_by_filename.get(name))
        for name, data in items
        if digits_by_filename.get(name)
    ]
    parsed_transactions = await asyncio.gather(
        *(
            asyncio.to_thread(parse_statement_transactions, data, filename=name)
            for name, data, _digits in parse_items
        )
    )
    for (_name, _data, digits), tx_parse in zip(parse_items, parsed_transactions, strict=True):
        if tx_parse.error:
            continue
        global_holdings.update(tx_parse.holdings)
        txns_by_digits.setdefault(digits, []).extend(tx_parse.transactions)

    # Normalize every transaction's symbol against the canonical holdings map (the owned
    # tables, aggregated across all uploaded statements). The ledger writes inconsistent forms
    # like ".BTCY"; the owned-table symbol is authoritative, so the same security never splits
    # into separate positions (.RS vs RS) across accounts.
    symbol_index = build_symbol_index(global_holdings)
    suffixed_by_base = canonical_symbol_map(global_holdings)
    for statement_txns in txns_by_digits.values():
        for txn in statement_txns:
            canonical = resolve_symbol(txn.description, symbol_index) if symbol_index else None
            if canonical:
                txn.symbol = canonical                     # canonical holdings ticker
            elif txn.symbol:
                # Not in any holdings table (a churned, never-held-at-month-end security):
                # still strip the ledger's leading-dot quirk so ".RS" lands on "RS" — the form
                # the holdings/API use — and the same security doesn't split across accounts.
                txn.symbol = txn.symbol.lstrip(".") or None
            # Collapse a suffix-less ticker onto its canonical suffixed listing (BTCY -> BTCY.B),
            # since the holdings table keeps the meaningful class/currency suffix.
            txn.symbol = canonicalize_symbol(txn.symbol, suffixed_by_base)

    async with sqlite_write_gate():
        async with db.begin():
            institution = await _get_questrade_institution(db, user_id, institution_id)
            if institution is None:
                return {
                    "status": "error",
                    "message": "The selected Questrade connection was not found",
                }
            user_tz = await get_user_timezone_info_from_db(db, user_id)

            # Match a statement to an existing account (synced or previously-imported) by
            # account-number digits. Re-load under the writer gate before mutating anything.
            existing_accounts = (
                await db.execute(
                    select(Account).where(
                        Account.user_id == user_id,
                        Account.institution_id == institution.id,
                    )
                )
            ).scalars().all()
            account_by_digits: dict[str, Account] = {}
            for existing in existing_accounts:
                account_by_digits.setdefault(_account_digits(existing.external_id), existing)

            inserted = updated_points = unchanged = accounts_created = accounts_updated = 0
            for account_number, account_points in by_account.items():
                digits = _account_digits(account_number)
                account = account_by_digits.get(digits)
                if account is None:
                    detected_type = next(
                        (point.account_type for point in account_points if point.account_type),
                        None,
                    )
                    detected_currency = next(
                        (point.currency for point in account_points if point.currency),
                        None,
                    )
                    account = Account(
                        user_id=user_id,
                        institution_id=institution.id,
                        external_id=f"account:{digits}",
                        name=f"QT-{account_number}",
                        account_type=detected_type,
                        currency=(detected_currency or "CAD").split("/")[0],
                        is_liability=False,
                        is_imported=True,
                    )
                    db.add(account)
                    await db.flush()
                    account_by_digits[digits] = account
                    accounts_created += 1
                else:
                    accounts_updated += 1

                series = []
                for point in account_points:
                    year, month, day = (int(part) for part in point.as_of_date.split("-"))
                    series.append(
                        (
                            datetime(year, month, day, 12, 0, 0, tzinfo=user_tz),
                            float(point.balance),
                        )
                    )
                counts = await _upsert_balance_history_series(
                    db,
                    user_id=user_id,
                    account_id=account.id,
                    points=series,
                    user_tz=user_tz,
                )
                inserted += counts.get("inserted", 0)
                updated_points += counts.get("updated", 0)
                unchanged += counts.get("unchanged", 0)

            transactions_imported = 0
            category_resolver = None
            for digits, statement_txns in txns_by_digits.items():
                account = account_by_digits.get(digits)
                if account is None or not account.is_imported:
                    continue
                normalized = _to_normalized_transactions(statement_txns, account)
                if not normalized:
                    continue
                if category_resolver is None:
                    category_resolver = await CategoryResolver.build(db, user_id)
                transactions_imported += await upsert_transactions_for_account(
                    db,
                    user_id=user_id,
                    account=account,
                    transactions=normalized,
                    category_resolver=category_resolver,
                    provider="questrade",
                )

    return {
        "status": "ok",
        # "imported" = balance points newly written or changed; "skipped" = unchanged
        # duplicates (re-upload of the same statement) plus any file whose account # was unreadable.
        "imported": inserted + updated_points,
        "skipped": skipped + unchanged,
        "errors": errors,
        "transactions_imported": transactions_imported,
        "accounts_created": accounts_created,
        "accounts_updated": accounts_updated,
        "warnings": warnings,
    }
