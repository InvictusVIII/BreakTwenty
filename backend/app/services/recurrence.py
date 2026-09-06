"""Recurrence detection.

Derives recurring transaction *series* (subscriptions, rent, insurance,
utilities, salary, …) from a user's transaction history. We get no
"recurring" flag from any connector, so recurrence is inferred:

  1. Group transactions by a normalized merchant fingerprint + direction.
  2. Within each group, look at the gaps between consecutive charges and
     decide whether they cluster around a known cadence (weekly … annual).
  3. Score confidence from interval regularity + occurrence count + amount
     stability, with an optional soft boost from description keywords.

The authoritative signal is the cadence math (dates + amounts — structured
data we own). Description keywords are only a tie-breaker, never the sole
basis for classifying something as recurring.

``detect_series`` is pure (operates on lightweight rows, no DB) so it is
unit-testable in isolation. ``run_detection_for_user`` is the DB-bound
orchestrator that upserts ``RecurringSeries`` and links matched
transactions; it preserves user overrides (rename / recategorize / confirm
/ dismiss) across re-detection.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median, pstdev

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, RecurringSeries, Transaction


# --- Tunables ---------------------------------------------------------------
MIN_OCCURRENCES = 3          # need >= 3 charges (>= 2 intervals) to see a rhythm
CONFIDENCE_THRESHOLD = 0.5   # below this we don't treat a group as recurring
DEFAULT_LOOKBACK_DAYS = 730  # 2 years of history feeds detection
LAPSE_MISSED_CYCLES = 2.5    # mark active->lapsed after this many missed intervals
AMOUNT_STABILITY_MAX = 0.20  # max amount coefficient-of-variation to call charges "fixed"

# Tokens that are payment plumbing, not merchant identity. Stripped from the
# fingerprint so "PREAUTH NETFLIX" and "NETFLIX.COM 866-..." group together.
_NOISE_TOKENS = frozenset({
    "PREAUTH", "PREAUTHORIZED", "PREAUTHDEBIT", "RECURRING", "POS", "PURCHASE",
    "PAYMENT", "PMT", "BILLPMT", "BILL", "PAD", "AUTOPAY", "AUTOPAYMENT", "EFT",
    "SQ", "TST", "PAYPAL", "PP", "VISA", "MASTERCARD", "MC", "AMEX", "DEBIT",
    "CREDIT", "WWW", "HTTP", "HTTPS", "COM", "CA", "INC", "LTD", "LLC", "CORP",
    "REF", "ID", "NO", "THE",
    # Generic charge-type descriptors merchants sometimes insert mid-description
    # (e.g. a relabel from "CHEXY TORONTO" to "CHEXY RENT TORONTO") — not
    # identity, so stripping them keeps a renamed merchant as one series.
    "RENT", "SUBSCRIPTION", "SUBSCR",
})
# Canadian province codes — dropped as trailing location noise.
_PROVINCE_CODES = frozenset({
    "ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU",
})
# Soft recurrence hints occasionally present in bank descriptions.
_RECURRING_KEYWORDS = (
    "PREAUTH", "PRE-AUTH", "PRE AUTH", "RECURRING", "AUTOPAY", "AUTO PAY",
    "PAD", "MONTHLY", "SUBSCRIPTION", "MEMBERSHIP", "BILL PMT",
)

# --- Auto-draw precision gate ----------------------------------------------
# The Recurring panel is for *auto-drawn* subscriptions/bills (so users can
# cancel forgotten ones), not habitual manual spending that merely recurs on a
# cadence (coffee, restaurants, a weekly pastry run). No connector gives an
# "auto-draw" flag, so we infer it from category + amount stability + keywords.

# Point-of-sale / manual-spend leaves — never an auto-subscription, dropped
# even when perfectly cadenced.
_DISCRETIONARY_SEED_KEYS = frozenset({
    "alcohol", "coffee", "delivery", "fast_food", "groceries", "cannabis",
    "restaurants", "treats",
    "dental", "doctor", "pharmacy", "therapy", "vision",  # manual health visits
    "beauty", "hair", "spa",
    "clothing", "electronics", "home_goods", "misc", "online_shopping",
    "gas", "parking", "ride_share", "tolls", "car_repair",
    "flights", "lodging", "travel_eats", "travel_misc", "travel_transit",
    "books", "events", "games", "hobbies", "movies", "music",
    "gifts_given", "kids", "pets",
    "etransfer_sent",  # person-to-person money movement, never an auto-draw
})
# Whole groups that are manual spend — covers leaves with no seed_key.
_DISCRETIONARY_PARENT_SEED_KEYS = frozenset({
    "food_and_drink", "personal_care", "shopping", "travel", "entertainment",
})
# Bills/subscriptions that are auto-drawn by nature — eligible on category
# alone, so variable-amount bills (hydro, phone) still qualify.
_SUBSCRIPTION_SEED_KEYS = frozenset({
    "apps_and_saas", "memberships", "other_subscriptions", "streaming",
    "insurance", "internet", "mortgage", "phone", "rent", "utilities",
    "loan_payment", "interest_paid", "student_loan",
    "fitness", "car_insurance", "car_loan",
})


def category_bucket(seed_key, parent_seed_key, classification):
    """Bucket a category for the auto-draw gate: ``subscription`` (always
    eligible), ``discretionary`` (always excluded), or ``neutral`` (eligible
    only with a near-constant amount or an auto-pay keyword)."""
    if classification == "income":
        return "subscription"  # recurring auto-deposits (paycheck) are auto by nature
    if seed_key in _SUBSCRIPTION_SEED_KEYS:
        return "subscription"
    if seed_key in _DISCRETIONARY_SEED_KEYS:
        return "discretionary"
    if parent_seed_key in _DISCRETIONARY_PARENT_SEED_KEYS:
        return "discretionary"
    return "neutral"


@dataclass
class TxnLite:
    """Minimal transaction row used by the pure detector."""
    id: int
    date: date
    amount: float
    currency: str
    description: str
    category_id: int | None
    provider_recurring: bool = False


@dataclass
class SeriesCandidate:
    merchant_key: str
    direction: str            # "inflow" | "outflow"
    display_name: str
    category_id: int | None
    cadence: str
    interval_days: float | None
    avg_amount: float
    amount_min: float
    amount_max: float
    currency: str
    last_seen: date
    next_expected: date | None
    occurrence_count: int
    confidence: float
    txn_ids: list[int] = field(default_factory=list)


def normalize_merchant(description: str) -> str:
    """Collapse a raw transaction description to a stable merchant fingerprint.

    Uppercase, drop punctuation, strip standalone numbers (store/ref ids,
    dates, phone fragments) and payment-plumbing tokens, then keep the first
    few meaningful words. Best-effort — the goal is a *stable grouping key*,
    not a perfect merchant name.
    """
    if not description:
        return ""
    upper = description.upper()
    # Preserve a masked account/card suffix (e.g. ``******8919``) as a stable
    # per-account token so genuinely different services billed under one brand —
    # Rogers phone (``******8919``) vs Rogers internet (``******0228``) — don't
    # collapse into one series. The suffix is stable month-to-month, so monthly
    # charges of the same account still group; only distinct accounts split.
    # (The raw digits are stripped below, so the suffix is rewritten to an
    # alphanumeric ``ACCT<digits>`` token that survives.)
    upper = re.sub(r"\*{2,}\s*(\d{2,})", r" ACCT\1 ", upper)
    cleaned = []
    for ch in upper:
        cleaned.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    tokens = "".join(cleaned).split()
    meaningful: list[str] = []
    for tok in tokens:
        if tok.isdigit():
            continue
        if len(tok) <= 1:
            continue
        if tok in _NOISE_TOKENS:
            continue
        if tok in _PROVINCE_CODES and meaningful:
            # trailing province code — location noise once we already have a name
            continue
        meaningful.append(tok)
        if len(meaningful) >= 4:
            break
    return " ".join(meaningful)


def _has_recurring_keyword(descriptions: list[str]) -> bool:
    joined = " ".join(descriptions).upper()
    return any(kw in joined for kw in _RECURRING_KEYWORDS)


def _classify_cadence(median_days: float) -> tuple[str, float | None]:
    """Map a median inter-charge gap to a cadence + canonical interval."""
    if 5 <= median_days <= 9:
        return "weekly", 7.0
    if 12 <= median_days <= 16:
        return "biweekly", 14.0
    if 26 <= median_days <= 35:
        return "monthly", 30.0
    if 84 <= median_days <= 96:
        return "quarterly", 91.0
    if 350 <= median_days <= 385:
        return "annual", 365.0
    return "irregular", None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_confidence(intervals: list[float], amounts: list[float], has_keyword: bool, provider_recurring: bool = False) -> float:
    n = len(amounts)
    # Interval regularity: tight clustering of gaps -> high score.
    if len(intervals) >= 2 and mean(intervals) > 0:
        cv_interval = pstdev(intervals) / mean(intervals)
    else:
        cv_interval = 1.0
    regularity = _clamp01(1.0 - cv_interval)
    # Occurrence factor: 3 charges -> 0.25, 6+ -> 1.0.
    occ_factor = _clamp01((n - 2) / 4.0)
    # Amount stability: stable charges score higher (but variable bills still
    # earn credit from cadence).
    abs_amounts = [abs(a) for a in amounts]
    if abs_amounts and mean(abs_amounts) > 0:
        cv_amount = pstdev(abs_amounts) / mean(abs_amounts)
    else:
        cv_amount = 1.0
    amount_stability = _clamp01(1.0 - cv_amount)
    keyword_boost = 0.1 if has_keyword else 0.0
    # The provider's own recurring/pre-auth flag is authoritative — boost it
    # harder than the soft description-keyword hint.
    provider_boost = 0.2 if provider_recurring else 0.0
    return _clamp01(
        0.45 * regularity + 0.3 * occ_factor + 0.2 * amount_stability
        + keyword_boost + provider_boost
    )


def _analyze_group(merchant_key: str, direction: str, txns: list[TxnLite], category_buckets: dict[int, str] | None = None) -> SeriesCandidate | None:
    if direction == "inflow":
        return None  # Recurring Expenses panel: outflows only (no income/refunds/incoming transfers)
    if len(txns) < MIN_OCCURRENCES:
        return None
    ordered = sorted(txns, key=lambda t: t.date)
    # Unique calendar days for cadence (collapse same-day double charges).
    unique_days = sorted({t.date for t in ordered})
    if len(unique_days) < MIN_OCCURRENCES:
        return None
    intervals = [
        float((unique_days[i + 1] - unique_days[i]).days)
        for i in range(len(unique_days) - 1)
    ]
    med = median(intervals)
    cadence, canonical_interval = _classify_cadence(med)
    if cadence == "irregular":
        return None

    amounts = [t.amount for t in ordered]
    descriptions = [t.description for t in ordered]
    has_keyword = _has_recurring_keyword(descriptions)
    has_provider_recurring = any(bool(t.provider_recurring) for t in ordered)
    confidence = _score_confidence(intervals, amounts, has_keyword, has_provider_recurring)
    if confidence < CONFIDENCE_THRESHOLD:
        return None

    interval_days = canonical_interval if canonical_interval is not None else med
    last_seen = ordered[-1].date
    next_expected = last_seen + timedelta(days=interval_days)

    # Display name: most common raw description, else the fingerprint.
    name_counts = Counter(d for d in descriptions if d)
    display_name = name_counts.most_common(1)[0][0] if name_counts else merchant_key
    # Dominant category among the group's transactions.
    cat_counts = Counter(t.category_id for t in ordered if t.category_id is not None)
    category_id = cat_counts.most_common(1)[0][0] if cat_counts else None
    currency = Counter(t.currency for t in ordered).most_common(1)[0][0]

    # Auto-draw precision gate: keep subscriptions/bills, drop habitual manual
    # spending that merely recurs (coffee, restaurants, a weekly pastry run).
    bucket = (category_buckets or {}).get(category_id, "neutral")
    if bucket == "discretionary":
        return None
    abs_amounts = [abs(a) for a in amounts]
    amount_cv = (
        pstdev(abs_amounts) / mean(abs_amounts)
        if len(abs_amounts) >= 2 and mean(abs_amounts) > 0
        else 1.0
    )
    if not (
        amount_cv <= AMOUNT_STABILITY_MAX
        or bucket == "subscription"
        or has_keyword
        or has_provider_recurring
    ):
        return None

    return SeriesCandidate(
        merchant_key=merchant_key,
        direction=direction,
        display_name=display_name,
        category_id=category_id,
        cadence=cadence,
        interval_days=interval_days,
        avg_amount=mean(amounts),
        amount_min=min(amounts),
        amount_max=max(amounts),
        currency=currency,
        last_seen=last_seen,
        next_expected=next_expected,
        occurrence_count=len(ordered),
        confidence=confidence,
        txn_ids=[t.id for t in ordered],
    )


def detect_series(
    transactions: list[TxnLite],
    category_buckets: dict[int, str] | None = None,
) -> list[SeriesCandidate]:
    """Pure detector: group transactions and return recurring candidates."""
    groups: dict[tuple[str, str], list[TxnLite]] = {}
    for txn in transactions:
        key = normalize_merchant(txn.description)
        if not key:
            continue
        direction = "inflow" if txn.amount > 0 else "outflow"
        groups.setdefault((key, direction), []).append(txn)

    candidates: list[SeriesCandidate] = []
    for (merchant_key, direction), group in groups.items():
        candidate = _analyze_group(merchant_key, direction, group, category_buckets)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


async def run_detection_for_user(
    db: AsyncSession,
    user_id: int,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    commit: bool = True,
) -> int:
    """Detect recurring series for one user and persist the result.

    Upserts ``RecurringSeries`` keyed by (user_id, merchant_key, direction)
    and links matched transactions via ``recurring_series_id``. User
    overrides are preserved: a dismissed series stays dismissed, and
    ``is_confirmed`` is never cleared. Returns the number of
    active series after the run.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff = today - timedelta(days=lookback_days)
    rows = (
        await db.execute(
            select(
                Transaction.id,
                Transaction.date,
                Transaction.amount,
                Transaction.currency,
                Transaction.description,
                Transaction.user_description,
                Transaction.category_id,
                Transaction.provider_recurring,
            )
            .select_from(Transaction)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == user_id,
                Transaction.date >= cutoff,
                # Securities events (dividends/interest tied to a symbol) belong
                # in Investment Activity, not Recurring — drop anything with a
                # symbol. Bank/loan interest carries no symbol and stays.
                Transaction.symbol.is_(None),
                # Exclude investment/transfer to match the Cash Flow page's
                # contract (CC/AMEX paydowns are money movement, not spend).
                # Keep uncategorized rows so unrecognized subscriptions count.
                (Category.classification.is_(None))
                | (Category.classification.notin_(("investment", "transfer"))),
            )
        )
    ).all()
    txns = [
        TxnLite(
            id=r.id,
            date=r.date,
            amount=float(r.amount or 0.0),
            currency=r.currency or "CAD",
            description=(r.user_description or r.description or ""),
            category_id=r.category_id,
            provider_recurring=bool(r.provider_recurring),
        )
        for r in rows
        if r.date is not None
    ]
    existing = (
        await db.execute(select(RecurringSeries).where(RecurringSeries.user_id == user_id))
    ).scalars().all()
    by_key: dict[tuple[str, str], RecurringSeries] = {
        (s.merchant_key, s.direction): s for s in existing
    }
    cat_rows = (
        await db.execute(
            select(
                Category.id,
                Category.seed_key,
                Category.classification,
                Category.parent_id,
            ).where(Category.user_id == user_id)
        )
    ).all()
    cat_seed = {c.id: c.seed_key for c in cat_rows}
    category_buckets = {
        c.id: category_bucket(c.seed_key, cat_seed.get(c.parent_id), c.classification)
        for c in cat_rows
    }

    candidates = detect_series(txns, category_buckets=category_buckets)

    seen_keys: set[tuple[str, str]] = set()
    series_txn_ids: list[tuple[RecurringSeries, list[int]]] = []
    for cand in candidates:
        key = (cand.merchant_key, cand.direction)
        seen_keys.add(key)
        series = by_key.get(key)
        if series is None:
            series = RecurringSeries(
                user_id=user_id,
                merchant_key=cand.merchant_key,
                direction=cand.direction,
                display_name=cand.display_name,
                status="active",
            )
            db.add(series)
            by_key[key] = series

        # Recency-based lifecycle: a re-detected pattern whose last charge is
        # long overdue is lapsed, not active.
        if series.status != "dismissed":
            overdue = (
                cand.interval_days is not None
                and (today - cand.last_seen).days > cand.interval_days * LAPSE_MISSED_CYCLES
            )
            series.status = "lapsed" if overdue else "active"
        elif (
            # A dismissed series that went quiet and then charged again after a
            # long gap is a genuine restart (e.g. a cancelled subscription the
            # user re-subscribed to) — resurface it. Consecutive charges of a
            # still-active dismissed series (gap ~ one cycle) do NOT revive it,
            # so a deliberate dismissal of an active expense stays hidden.
            series.last_seen_date is not None
            and cand.interval_days is not None
            and (cand.last_seen - series.last_seen_date).days > cand.interval_days * LAPSE_MISSED_CYCLES
        ):
            series.status = "active"

        series.display_name = cand.display_name
        if cand.category_id is not None:
            series.category_id = cand.category_id
        series.cadence = cand.cadence
        series.interval_days = cand.interval_days
        series.avg_amount = cand.avg_amount
        series.amount_min = cand.amount_min
        series.amount_max = cand.amount_max
        series.currency = cand.currency
        series.last_seen_date = cand.last_seen
        series.next_expected_date = cand.next_expected
        series.occurrence_count = cand.occurrence_count
        series.confidence = cand.confidence
        series.updated_at = now
        series_txn_ids.append((series, cand.txn_ids))

    # Lapse active series that were not re-detected and are overdue.
    for key, series in by_key.items():
        if key in seen_keys or series.status != "active":
            continue
        interval = series.interval_days or 30.0
        last_seen = series.last_seen_date
        if last_seen is None or (today - last_seen).days > interval * LAPSE_MISSED_CYCLES:
            series.status = "lapsed"
            series.updated_at = now

    await db.flush()  # assign ids to freshly-added series

    for series, txn_ids in series_txn_ids:
        if txn_ids:
            await db.execute(
                update(Transaction)
                .where(Transaction.id.in_(txn_ids))
                .values(recurring_series_id=series.id)
            )

    # An active series that was NOT re-detected this run is stale: its charges
    # were recategorized out of the panel's eligibility (e.g. tagged as a
    # transfer / e-Transfer / discretionary spend), re-grouped under a new
    # fingerprint, or deleted. Unlink its transactions so the orphan cleanup
    # below removes it — otherwise it lingers on the panel with a frozen
    # snapshot (the recategorized row keeps showing its old category pill).
    # Overdue series were already moved to 'lapsed' above (kept as history);
    # dismissed series are left untouched.
    stale_active_ids = [
        s.id
        for key, s in by_key.items()
        if key not in seen_keys and s.status == "active" and s.id is not None
    ]
    if stale_active_ids:
        await db.execute(
            update(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.recurring_series_id.in_(stale_active_ids),
            )
            .values(recurring_series_id=None)
        )

    # Drop active series left with no linked transactions — e.g. a merchant whose
    # fingerprint changed (Rogers splitting into one series per masked account) so
    # its charges re-grouped under new keys, orphaning the old row. Dismissed /
    # lapsed orphans are kept (a dismissal stays suppressed; lapsed stays history).
    await db.execute(
        delete(RecurringSeries).where(
            RecurringSeries.user_id == user_id,
            RecurringSeries.status == "active",
            RecurringSeries.id.notin_(
                select(Transaction.recurring_series_id)
                .where(
                    Transaction.user_id == user_id,
                    Transaction.recurring_series_id.isnot(None),
                )
                .distinct()
            ),
        )
    )

    if commit:
        await db.commit()
    return sum(1 for c in candidates)
