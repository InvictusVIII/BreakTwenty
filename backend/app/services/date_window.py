"""Shared date-window filter semantics for transaction queries.

A user-facing timeline window is day-granular: ``start_date`` / ``end_date`` are
calendar days and **both ends are inclusive**. Transactions are stored as calendar
dates, so the end is represented as the exclusive following day.

Centralized here so the transaction list, its CSV export, and any future
windowed query share one definition instead of each re-deriving the boundary
(the prior cause of a charge showing in a category total but vanishing from the
list/tray for the same period).
"""
from __future__ import annotations

from datetime import date, timedelta


def start_bound(raw: str | None) -> date | None:
    """Lower bound for ``Transaction.date >= start_bound(raw)``.

    The whole start day is included. Returns ``None`` for missing/unparseable
    input so callers can skip the filter.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return None


def end_bound_exclusive(raw: str | None) -> date | None:
    """Exclusive upper bound for ``Transaction.date < end_bound_exclusive(raw)``.

    The whole ``end_date`` calendar day is included, so a transaction stamped at
    any time on that day is kept. Returns ``None`` for missing/unparseable input.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]) + timedelta(days=1)
    except (ValueError, TypeError):
        return None
