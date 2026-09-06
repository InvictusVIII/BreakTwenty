"""Transaction-driven daily balance reconstruction for transaction-backed accounts.

For accounts whose balance moves ONLY via transactions (cash, chequing, savings,
credit cards, lines of credit, loans — i.e. NO market-valued holdings), the balance
on a day with no sync can be reconstructed exactly from a real ("anchor") balance
point plus the signed transactions around it:

    balance(day D) = anchor_balance - sign * Σ(amount of txns dated after D, up to the anchor)
    sign = -1 for liabilities (an outflow raises what's owed), +1 for assets
    amount is +inflow / -outflow (see Transaction.amount)

This is the backward dual of the forward derivation in
``manual_institutions.recompute_manual_account_balance`` (balance = opening +
sign*cumulative), solved from the latest known balance instead of an opening anchor.

Reconstruction is ANCHORED to every real balance point: gaps between consecutive anchors
are filled exactly, and the span BEFORE the earliest anchor is walked back through the
signed transactions to the account's first transaction — so each real point stays exact,
the series reaches the account's true start (not just its first sync), and any
transaction-completeness shortfall stays contained to its own gap.

Pure — no DB, no I/O, no timezone logic. Callers pass already-localized
``(YYYY-MM-DD, value)`` pairs. Liability history follows the ledger's magnitude contract:
every reconstructed point is non-negative and presentation layers apply the debt sign.
Holdings-backed (brokerage) accounts must NOT be passed here: their value moves with the
market, not transactions, so they stay step-on-sync.
"""
from __future__ import annotations

from datetime import date, timedelta


def _parse(d: str) -> date:
    y, m, dd = d.split("-")
    return date(int(y), int(m), int(dd))


def _amounts_by_day(transactions) -> dict[str, float]:
    by_day: dict[str, float] = {}
    for d, amt in transactions:
        by_day[d] = by_day.get(d, 0.0) + float(amt or 0.0)
    return by_day


def _sorted_anchors(anchors):
    # One balance per local day (last value for a day wins, matching the read payload).
    return sorted({str(d): float(b) for d, b in anchors}.items())


def reconstruct_daily_balances(anchors, transactions, is_liability):
    """Dense ``{YYYY-MM-DD: balance}`` covering every day from the earliest *transaction*
    through the latest anchor. Gaps between real anchors are walked back from the upper
    anchor; the span before the earliest anchor is walked back from that first real balance
    through the signed transactions, so the series reaches the account's true start (its
    first transaction), not merely its first sync. Each real anchor is preserved exactly.
    """
    sign = -1 if is_liability else 1
    pts = _sorted_anchors(anchors)
    if not pts:
        return {}
    by_day = _amounts_by_day(transactions)
    series: dict[str, float] = {}
    # Fill BETWEEN consecutive anchors (each anchor stays exact; residuals stay in-gap).
    for (d_lo, b_lo), (d_hi, b_hi) in zip(pts, pts[1:]):
        lo, hi = _parse(d_lo), _parse(d_hi)
        running = b_hi
        cur = hi
        series[d_hi] = b_hi
        while cur > lo:
            running = running - sign * by_day.get(cur.isoformat(), 0.0)
            cur = cur - timedelta(days=1)
            series[cur.isoformat()] = running
        series[d_lo] = b_lo
    series[pts[0][0]] = pts[0][1]  # earliest anchor (also covers the single-anchor case)
    # Extend BEFORE the earliest anchor, back to the earliest transaction: walk the same
    # signed flow backward from the first real balance. (No earlier transactions → no extension.)
    # These pre-anchor points are inferred from transaction coverage; providers without a
    # dated balance API cannot independently verify them.
    earliest_day, earliest_bal = pts[0]
    if by_day:
        first_txn_day = min(by_day)
        if first_txn_day < earliest_day:
            lo = _parse(first_txn_day)
            cur = _parse(earliest_day)
            running = earliest_bal
            while cur > lo:
                running = running - sign * by_day.get(cur.isoformat(), 0.0)
                cur = cur - timedelta(days=1)
                series[cur.isoformat()] = running
    if is_liability:
        return {day: abs(balance) for day, balance in series.items()}
    return series
