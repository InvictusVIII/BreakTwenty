"""Parse Questrade monthly account-statement PDFs into balance-history points.

Questrade exposes no historical-NAV API — the v1 ``accounts/{id}/balances`` endpoint
returns only a current snapshot (``combinedBalances``/``sodBalances``), so the PDF
account statements are the *only* source of pre-connection month-end value for a
Questrade account, active or closed. Each monthly statement states, on page 1, a
single authoritative figure plus its as-of date:

    Current month balance: $11,080.19
    Current month:         June 30, 2022
    Type:                  Individual Tax-Free Savings Account (TFSA)

The balance is already "Combined in CAD" (USD holdings converted), so it needs no FX.
This module extracts one ``(account_number, as_of_date, balance, account_type)`` point
per statement.

A fully-emptied/closed account's final statement renders the balance *blank* (the
page-1 figure and the page-3 Balances table are both empty). That is reported as
``$0.00`` with a warning and ``source="empty_statement"`` so a genuine parse miss can
never masquerade as a silent zero — the caller surfaces the warning for confirmation.

Pure parsing — no DB and no I/O beyond reading the provided PDF. Callers route the
points through :mod:`app.services.questrade_statement_import`, which upserts
``BalanceHistory`` rows into a (matched or imported) account; this module never touches
the database.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import date

from pdfminer.high_level import extract_text

# Page-1 anchors. ``extract_text`` keeps each label and its value on one logical line
# (verified across 2020-2024 statements for both individual and combined TFSA layouts).
_BALANCE_RE = re.compile(r"Current month balance:\s*\$?\s*(-?[\d,]+\.\d{2})", re.IGNORECASE)
_ASOF_RE = re.compile(r"Current month:\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_ACCOUNT_RE = re.compile(r"Account #:\s*(\d+)")
_CURRENCY_RE = re.compile(r"Currency:\s*([A-Za-z]+(?:/[A-Za-z]+)?)")
_TYPE_RE = re.compile(r"Type:\s*([^\n]+)")
# Registered-plan acronym in parentheses, e.g. "(TFSA)", "(RRSP)", "(FHSA)".
_TYPE_PAREN_RE = re.compile(r"\(([A-Za-z]{2,6})\)")

_MONTHS = {
    name: index
    for index, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}


@dataclass
class StatementPoint:
    """One month-end balance point extracted from a single statement PDF."""

    account_number: str | None
    as_of_date: str | None  # ISO ``YYYY-MM-DD``
    balance: float | None
    currency: str | None
    source: str  # "header" | "empty_statement" | "error"
    account_type: str | None = None  # normalized, e.g. "tfsa" / "rrsp" / "margin"
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    filename: str | None = None


def _detect_account_type(text: str) -> str | None:
    """Normalize the statement's ``Type:`` line to a short account-type key.

    Prefers the registered-plan acronym in parentheses ("(TFSA)" -> "tfsa"); falls back
    to the margin/cash keywords for non-registered accounts. Returns None if unknown,
    leaving the account type unset rather than guessing.
    """
    match = _TYPE_RE.search(text)
    if not match:
        return None
    type_text = match.group(1)
    paren = _TYPE_PAREN_RE.search(type_text)
    if paren:
        return paren.group(1).lower()
    lowered = type_text.lower()
    if "margin" in lowered:
        return "margin"
    if "cash" in lowered:
        return "cash"
    return None


def _extract_page1_text(source) -> str:
    """Read page 1 of *source* (path, bytes, or file-like) as text."""
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    elif isinstance(source, (str, os.PathLike)):
        source = os.fspath(source)
    # Page 1 carries the balance header, as-of date, account #, currency and type.
    return extract_text(source, page_numbers=[0]) or ""


def parse_statement(source, *, filename: str | None = None) -> StatementPoint:
    """Parse a single Questrade statement PDF into a :class:`StatementPoint`.

    *source* may be a filesystem path, raw ``bytes``, or a binary file-like object.
    Never raises for a malformed/unreadable PDF — returns a point with ``error`` set.
    """
    try:
        text = _extract_page1_text(source)
    except Exception as exc:  # unreadable / corrupt / not a PDF
        return StatementPoint(
            None, None, None, None, "error",
            error=f"Could not read PDF: {exc}", filename=filename,
        )

    warnings: list[str] = []

    acct_match = _ACCOUNT_RE.search(text)
    account_number = acct_match.group(1) if acct_match else None
    if account_number is None:
        warnings.append("Account number not found on statement")

    currency_match = _CURRENCY_RE.search(text)
    currency = currency_match.group(1) if currency_match else None

    account_type = _detect_account_type(text)

    as_of_date: str | None = None
    date_match = _ASOF_RE.search(text)
    if date_match:
        month = _MONTHS.get(date_match.group(1).lower())
        if month:
            try:
                as_of_date = date(int(date_match.group(3)), month, int(date_match.group(2))).isoformat()
            except ValueError:
                as_of_date = None
    if as_of_date is None:
        warnings.append("Statement date not found")

    balance_match = _BALANCE_RE.search(text)
    if balance_match:
        balance: float | None = float(balance_match.group(1).replace(",", ""))
        source_kind = "header"
    elif "current month balance" in text.lower():
        # Label present but no figure: an emptied/closed account's final statement.
        # Treat as $0.00 but flag it so a real parse miss isn't read as a true zero.
        balance = 0.0
        source_kind = "empty_statement"
        warnings.append("Balance is blank — treated as $0.00 (closed/empty statement); please verify")
    else:
        balance = None
        source_kind = "error"
        warnings.append("Balance figure not found")

    return StatementPoint(
        account_number=account_number,
        as_of_date=as_of_date,
        balance=balance,
        currency=currency,
        source=source_kind,
        account_type=account_type,
        warnings=warnings,
        filename=filename,
    )


def parse_statements(items) -> list[StatementPoint]:
    """Parse many statements. *items* is an iterable of ``(filename, source)`` pairs.

    Points are returned in input order; deduplication of same-day balances is left to
    the downstream balances import, which already upserts one row per account per day.
    """
    return [parse_statement(source, filename=name) for name, source in items]
