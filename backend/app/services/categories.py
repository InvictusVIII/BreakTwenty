"""Categories service: seeded taxonomy, CRUD, and type-to-category resolution.

Migration `f1a2b3c4d5e6` carries its own frozen taxonomy snapshot for the
initial seed/backfill. This module owns the *current* canonical taxonomy used
for any future user creation. The two are intentionally separate so historical
migrations stay reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, CategoryRule, RecurringSeries, Transaction
from app.services.categories_rules import (
    BUNDLED_RULES_SORTED,
    BundledRule,
    bundled_rule_matches,
    seed_key_for_mcc,
)


VALID_CLASSIFICATIONS = {"income", "expense", "transfer", "investment"}


@dataclass(frozen=True)
class SeedLeaf:
    """One leaf entry in the canonical ``SEED_TAXONOMY``.

    Attributes:
        name: User-facing display name. Freely renamable from the UI per-user;
            does not affect ``seed_key``-based bundled-rule resolution.
        icon: Emoji codepoint string (for emoji icon sets) or finance-pack slug
            (when ``icon_set='finance'``).
        type_backfill: Connector ``transaction.type`` value that auto-maps to
            this leaf (e.g. ``'buy'`` → Buy leaf). Only set for ~12 leaves.
        icon_set: ``'twemoji'`` (default if None) / ``'noto'`` / ``'fluent'`` /
            ``'finance'``. Selects which bundled SVG pack renders the icon.
        seed_key: Explicit stable identifier. Required when the display
            ``name`` slugifies to something other than the canonical key
            (e.g. ``Apps`` → derived ``apps`` but canonical key is
            ``apps_and_saas``). Most leaves leave this as ``None`` and rely on
            ``seed_key_from_name(name)``.
        color_dark/color_light: Per-theme leaf overrides. ``None`` inherits
            that theme's parent-group color. Used for semantic-color leaves
            under Investments and Transfers and for deliberately distinct
            child colors in the canonical palette.
    """

    name: str
    icon: str
    type_backfill: str | None = None
    icon_set: str | None = None
    seed_key: str | None = None
    color_dark: str | None = None
    color_light: str | None = None
    classification: str | None = None  # per-leaf override; None inherits the group's classification


@dataclass(frozen=True)
class SeedGroup:
    """One top-level group entry in the canonical ``SEED_TAXONOMY``."""

    name: str
    icon: str
    color_dark: str
    color_light: str
    classification: str
    leaves: tuple[SeedLeaf, ...]
    icon_set: str | None = None
    seed_key: str | None = None


def seed_key_from_name(name: str) -> str:
    """Slugify a seed name into a stable identifier.

    Lowercase, ``&`` becomes ``and``, non-alphanumerics collapse to ``_``,
    leading/trailing underscores stripped. Used as the default seed_key when
    a ``SeedGroup`` / ``SeedLeaf`` doesn't carry an explicit override.
    """
    s = name.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _seed_key_for_group(group: SeedGroup) -> str:
    return group.seed_key or seed_key_from_name(group.name)


def _seed_key_for_leaf(leaf: SeedLeaf) -> str:
    return leaf.seed_key or seed_key_from_name(leaf.name)


# IMPORTANT: ``seed_key`` (explicit or derived from ``name``) is the
# load-bearing identifier — bundled rules in ``categories_rules.py``
# reference seed_keys, and ``categories.seed_key`` on existing rows is
# pinned at seed time. Renaming ``name`` in code without preserving the
# seed_key would orphan every bundled rule + every existing row that
# depends on the old key. To rename a seeded category for display purposes,
# either:
#   * Pass an explicit ``seed_key='old_canonical_value'`` on the SeedLeaf
#     (preferred — keeps the structure declarative), or
#   * Add a migration that updates ``categories.name`` for existing rows
#     without touching ``seed_key``.
# The per-user UI rename is always safe — only ``Category.name`` changes
# and ``seed_key`` stays anchored.
#
# Adding a NEW seeded leaf:
#   1. Append a SeedLeaf to the appropriate SeedGroup's ``leaves``.
#   2. Write a migration that INSERTs the row for every existing user AND
#      sets ``seed_key`` in the INSERT (the Tax Prep / Government migrations
#      ``b5c6d7e8f9a0`` / ``c6d7e8f9a0b1`` are the canonical templates).
#   3. Reference the new leaf from bundled rules via its seed_key.
# User-created categories from the UI intentionally leave ``seed_key`` NULL
# — they're orthogonal to the bundled-rule system and only addressable via
# user rules (CategoryRule rows referencing ``category_id`` directly).
#
# This is the canonical golden-standard taxonomy as customized by the
# original BreakTwenty maintainer (names, icons, colors, sort order). Future
# new users seeded into a fresh BreakTwenty instance get exactly this layout.
SEED_TAXONOMY: list[SeedGroup] = [
    SeedGroup(
        name='Income', icon="\U0001f4b0", color_dark='#00ce31', color_light='#01b400', classification='income',
        leaves=(
            SeedLeaf('Paycheck', "\U0001f4b5", icon_set='twemoji', color_light='#00ce31'),
            SeedLeaf('Dividend', "\U0001f4b0", type_backfill='dividend', icon_set='noto', color_dark='#00ff35', color_light='#00ff35'),
            SeedLeaf('Interest Earned', "coin-stack", type_backfill='interest', icon_set='finance', seed_key='interest', color_dark='#14ff00', color_light='#14ff00'),
            SeedLeaf('Refund', "\U0001f4b5", icon_set='noto', color_dark='#00c86a', color_light='#00c86a'),
            SeedLeaf('Reimburse', "money-stack", icon_set='finance', color_dark='#45ff6c', color_light='#45ff6c'),
            SeedLeaf('Misc Income', "\U0001fa99", icon_set='noto', color_light='#00ce31'),
            SeedLeaf('Gifts Received', "\U0001f381", icon_set='noto', seed_key='gifts_received', color_light='#00ce31'),
            SeedLeaf('E-Transfer Received', "send", icon_set='finance', seed_key='etransfer_received', color_dark='#a855f7', color_light='#d2b5ff'),
        ),
    ),
    SeedGroup(
        name='Investments', icon="\U0001f4ca", color_dark='#4f87ff', color_light='#006eff', classification='investment',
        leaves=(
            SeedLeaf('Buy', "sales-performance", type_backfill='buy', icon_set='finance', color_dark='#ffff18', color_light='#ffff18'),
            SeedLeaf('Sell', "\U0001f4b2", type_backfill='sell', icon_set='noto', color_dark='#2bff31', color_light='#2bff31'),
            SeedLeaf('Expired', "\U0000231b", type_backfill='expired', icon_set='fluent', color_dark='#c4c4c4', color_light='#888888'),
        ),
    ),
    SeedGroup(
        name='Transfers', icon="\U0001f504", color_dark='#ff9927', color_light='#ff9927', classification='transfer',
        leaves=(
            SeedLeaf('Transfer', "\U0001f501", type_backfill='transfer', icon_set='noto', color_dark='#ff9400', color_light='#f09000'),
            SeedLeaf('Deposit', "\U0001f53c", type_backfill='deposit', icon_set='noto', color_dark='#00ff28', color_light='#00ff28'),
            SeedLeaf('Withdrawal', "\U0001f53d", type_backfill='withdrawal', icon_set='noto', color_dark='#ff2935', color_light='#ff0000'),
            SeedLeaf('Credit Card Payment', "\U0001f4b3", icon_set='noto', seed_key='cc_payment', color_dark='#259fde', color_light='#21a7ff'),
            SeedLeaf('Loan Payment', "\U0001f4b8", icon_set='noto', color_dark='#259fde', color_light='#21a7ff'),
            SeedLeaf('Loan Advance', "\U0001f5d2\U0000fe0f", icon_set='noto', color_dark='#259fde', color_light='#21a7ff'),
        ),
    ),
    SeedGroup(
        name='Financial', icon="\U0001f3e6", color_dark='#ff2935', color_light='#ff0000', classification='expense',
        leaves=(
            SeedLeaf('Fee', "\U00002796", type_backfill='fee'),
            SeedLeaf('Interest Charged', "\U0001f4b8", type_backfill='interest_paid', icon_set='noto', seed_key='interest_paid'),
            SeedLeaf('Bank Fees', "\U0001f3e6", icon_set='fluent'),
            SeedLeaf('ATM Fees', "\U0001f3e7"),
        ),
    ),
    SeedGroup(
        name='Food & Drink', icon="\U0001f37d\U0000fe0f", color_dark='#ff4a18', color_light='#ff4a18', classification='expense',
        leaves=(
            SeedLeaf('Groceries', "\U0001f6d2", icon_set='noto'),
            SeedLeaf('Restaurants', "\U0001f37d\U0000fe0f"),
            SeedLeaf('Coffee & Tea', "\U00002615", icon_set='noto', seed_key='coffee'),
            SeedLeaf('Fast Food', "\U0001f354", icon_set='noto'),
            SeedLeaf('Alcohol', "\U0001f942", icon_set='noto'),
            SeedLeaf('Delivery', "\U0001f6f5"),
            SeedLeaf('Online Groceries', "\U0001f4e6", icon_set='noto'),
            SeedLeaf('Treats', "\U0001f36b", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Housing', icon="\U0001f3e0", color_dark='#ffe700', color_light='#ffe700', classification='expense',
        leaves=(
            SeedLeaf('Rent', "building-home", icon_set='finance'),
            SeedLeaf('Utilities', "\U0001f4a1", icon_set='noto'),
            SeedLeaf('Internet', "\U0001f310", icon_set='noto'),
            SeedLeaf('Phone', "\U0001f4f1", icon_set='noto'),
            SeedLeaf('Maintenance', "\U0001f527", icon_set='noto'),
            SeedLeaf('Insurance', "\U0001f3da\U0000fe0f"),
            SeedLeaf('Mortgage', "\U0001f3e1", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Transportation', icon="\U0001f697", color_dark='#11cdef', color_light='#11cdef', classification='expense',
        leaves=(
            SeedLeaf('Gas', "\U000026fd"),
            SeedLeaf('Transit', "\U0001f68b", icon_set='noto'),
            SeedLeaf('Ride Share', "\U0001f695", icon_set='noto'),
            SeedLeaf('Parking', "\U0001f17f\U0000fe0f", icon_set='fluent'),
            SeedLeaf('Car Insurance', "\U0001f699", icon_set='noto'),
            SeedLeaf('Car Repair', "\U0001f529", icon_set='noto'),
            SeedLeaf('Tolls', "\U0001f6e3\U0000fe0f", icon_set='fluent'),
            SeedLeaf('Car Loan', "\U0001f697", icon_set='noto'),
            SeedLeaf('Shipping', "\U0001f69a", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Shopping', icon="\U0001f6cd\U0000fe0f", color_dark='#2e61ff', color_light='#0082ff', classification='expense',
        leaves=(
            SeedLeaf('Misc', "\U0001f6cd\U0000fe0f", icon_set='noto'),
            SeedLeaf('Clothing & Footwear', "\U0001f455", icon_set='noto', seed_key='clothing'),
            SeedLeaf('Electronics', "\U0001f4bb", icon_set='noto'),
            SeedLeaf('Home Goods', "\U0001f6cb\U0000fe0f", icon_set='noto'),
            SeedLeaf('Online Shopping', "globe", icon_set='finance'),
        ),
    ),
    SeedGroup(
        name='Subscriptions', icon="\U0001f4fa", color_dark='#ffa801', color_light='#ffa801', classification='expense',
        leaves=(
            SeedLeaf('Streaming', "\U0001f4fa", icon_set='twemoji'),
            SeedLeaf('Apps', "\U0001f4f1", icon_set='fluent', seed_key='apps_and_saas'),
            SeedLeaf('Memberships', "person", icon_set='finance'),
            SeedLeaf('Other Subscriptions', "\U0001f4b8", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Entertainment', icon="\U0001f3ac", color_dark='#9035ff', color_light='#b569ff', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Movies', "\U0001f4fd\U0000fe0f", icon_set='noto'),
            SeedLeaf('Events', "\U0001f3ab", icon_set='noto'),
            SeedLeaf('Games', "\U0001f3ae", icon_set='noto'),
            SeedLeaf('Hobbies', "\U0001f3a8", icon_set='noto'),
            SeedLeaf('Books', "\U0001f4da", icon_set='noto'),
            SeedLeaf('Music', "\U0001f3a7", icon_set='noto'),
            SeedLeaf('Recreational', "cannabis-leaf", icon_set='finance', seed_key='cannabis'),
        ),
    ),
    SeedGroup(
        name='Health & Wellness', icon="\U00002695\U0000fe0f", color_dark='#72ff67', color_light='#72ff67', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Pharmacy', "\U0001f48a", icon_set='noto'),
            SeedLeaf('Doctor', "\U0001fa7a", icon_set='noto'),
            SeedLeaf('Dental', "\U0001f9b7", icon_set='noto'),
            SeedLeaf('Vision', "\U0001f453", icon_set='noto'),
            SeedLeaf('Fitness', "\U0001f4aa", icon_set='noto'),
            SeedLeaf('Therapy', "\U0001f9e0", icon_set='noto'),
            SeedLeaf('Other', "\U0001f489", icon_set='noto', seed_key='health_other'),
        ),
    ),
    SeedGroup(
        name='Personal Care', icon="\U0001f6c0", color_dark='#a370f0', color_light='#e248f7', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Hair', "\U0001f488", icon_set='noto'),
            SeedLeaf('Personal Care', "\U0001faae", icon_set='noto', seed_key='beauty'),
            SeedLeaf('Spa', "\U0001fae7", icon_set='noto'),
            SeedLeaf('Laundry', "\U0001f9fa", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Travel', icon="\U00002708\U0000fe0f", color_dark='#ffff00', color_light='#ffff00', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Flights', "\U00002708\U0000fe0f", icon_set='noto'),
            SeedLeaf('Lodging', "\U0001f3e8", icon_set='noto'),
            SeedLeaf('Travel Food', "\U0001f371", icon_set='noto', seed_key='travel_eats'),
            SeedLeaf('Travel Transport', "\U0001f685", icon_set='noto', seed_key='travel_transit'),
            SeedLeaf('Travel Misc', "\U0001f9f3", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Life', icon="\U0001f3d9\U0000fe0f", color_dark='#fb6e94', color_light='#ff99b1', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Gifts Given', "\U0001f380", icon_set='noto'),
            SeedLeaf('Charity', "donate", icon_set='finance'),
            SeedLeaf('Education', "\U0001f3eb", icon_set='noto'),
            SeedLeaf('Kids', "\U0001f476", icon_set='noto'),
            SeedLeaf('Pets', "\U0001f415", icon_set='noto'),
            SeedLeaf('Student Loan', "\U0001f393", icon_set='noto'),
        ),
    ),
    SeedGroup(
        name='Taxes', icon="\U0001f4d2", color_dark='#ff2935', color_light='#ff0000', classification='expense', icon_set='twemoji',
        leaves=(
            SeedLeaf('Income Tax', "\U0001f9fe", icon_set='noto'),
            SeedLeaf('Property Tax', "\U0001f3db\U0000fe0f", icon_set='noto'),
            SeedLeaf('Other Tax', "\U0001f4cb", icon_set='noto'),
            SeedLeaf('Tax Prep', "\U0001f9ee"),
            # Foreign dividend withholding tax: a tax paid (expense), grouped with
            # the other taxes. type_backfill maps the connector's withholding_tax
            # transactions here; seed_key 'withheld' is kept stable across the move.
            SeedLeaf('Witholding tax', "tax-form", type_backfill='withholding_tax', icon_set='finance', seed_key='withheld'),
        ),
    ),
    SeedGroup(
        name='Other', icon="\U00002753", color_dark='#ffffff', color_light='#969696', classification='expense', icon_set='noto',
        leaves=(
            SeedLeaf('Uncategorized', "\U00002753", icon_set='noto', color_dark='#8d8d8d', color_light='#8d8d8d'),
            SeedLeaf('Other', "\U00002754", type_backfill='other', icon_set='noto', color_dark='#ffffff', color_light='#c3c3c3'),
            SeedLeaf('Cash', "\U0001f4b5", icon_set='twemoji', color_dark='#adffc2', color_light='#77ffa1', classification='transfer'),
            SeedLeaf('Business', "\U0001f4bc", icon_set='noto', color_dark='#c67050', color_light='#d7d7d7'),
            SeedLeaf('Government', "\U0001faaa", color_dark='#ffffff', color_light='#e7e7e7'),
            SeedLeaf('E-Transfer Sent', "send", icon_set='finance', seed_key='etransfer_sent', color_dark='#a855f7', color_light='#d2b5ff'),
        ),
    ),
]


# Connector persistence + auto-assignment: transaction.type → seeded leaf
# seed_key. Built from the SeedLeaf.type_backfill markers on the canonical
# taxonomy. Resolved per-user via Category.seed_key (stable across UI
# renames), not Category.name.
TYPE_TO_SEED_KEY: dict[str, str] = {
    leaf.type_backfill: _seed_key_for_leaf(leaf)
    for group in SEED_TAXONOMY
    for leaf in group.leaves
    if leaf.type_backfill
}
# Brokers don't surface dividend-vs-distribution at transaction time (a
# covered-call ETF and a true dividend both come through as "CASH DIVIDEND").
# BreakTwenty doesn't try to distinguish them either — both roll up into Dividend.
# Users who care can recreate a Distribution leaf and route via user rules.
TYPE_TO_SEED_KEY["distribution"] = "dividend"

# The `transfer` and `other` types are withheld from the type-mapping fallback. A
# bare `transfer` with no rule and no MCC is genuinely ambiguous (own-account move
# vs external money vs a mis-typed purchase); a bare `other` (the connector / CSV-
# import "unknown" type) can't be classified from its type at all. Both fall through
# to Uncategorized for the user to resolve, rather than the catch-all Other category.
# Authoritative types (buy/sell/dividend/interest/fee/...) keep the fallback because
# the connector's type is the category.
TYPE_FALLBACK_REQUIRES_RULE = frozenset({"transfer", "other"})

# On transaction-style bank and liability accounts, providers commonly use
# `deposit`/`withdrawal` as generic credit/debit directions. Those types do not
# establish purpose: an AMEX purchase can arrive as `withdrawal`, for example.
# Rules and MCC still run first; an unmatched directional row is Uncategorized.
# Investment/crypto/cash-style accounts retain the type fallback because their
# activity feeds use Deposit/Withdrawal as authoritative cash-movement actions.
DIRECTION_ONLY_TYPES = frozenset({"deposit", "withdrawal"})
DIRECTION_ONLY_ACCOUNT_TYPES = frozenset(
    {
        "chequing",
        "checking",
        "savings",
        "credit_card",
        "credit",
        "loc",
        "line_of_credit",
        "heloc",
        "mortgage",
        "loan",
        "student_loan",
        "auto_loan",
        "personal_loan",
        "other_debt",
    }
)

# Authoritative brokerage actions whose *description is a security name* (e.g.
# "AMAZON.COM INC", "ADOBE INC", "NVDA(US67066G1040) CASH DIVIDEND", an option
# symbol). The connector's type IS the category here, so generic merchant /
# description rules must NOT override them — otherwise a user "AMAZON" -> Online
# Shopping rule (legitimately created from a real Amazon purchase) bleeds onto an
# AMZN stock buy and mis-files it. For these types the resolver honors only rules
# EXPLICITLY scoped to the same `transaction_type`; plain name rules are skipped
# and the row falls through to the authoritative type-mapping (Buy/Sell/Dividend…).
SECURITY_NAME_TYPES = frozenset(
    {"buy", "sell", "dividend", "distribution", "withholding_tax", "expired"}
)


async def seed_default_categories_for_user(db: AsyncSession, user_id: int) -> bool:
    """Idempotently seed the default taxonomy for a user.

    Returns True if seeding ran, False if the user already had system categories.
    Caller is responsible for committing.
    """
    existing = (
        await db.execute(
            select(Category.id)
            .where(Category.user_id == user_id, Category.is_system.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False

    now = datetime.now(timezone.utc)
    for group_idx, group in enumerate(SEED_TAXONOMY):
        group_row = Category(
            user_id=user_id,
            parent_id=None,
            name=group.name,
            icon=group.icon,
            icon_set=group.icon_set,
            color_dark=group.color_dark,
            color_light=group.color_light,
            classification=group.classification,
            is_system=True,
            sort_order=group_idx,
            seed_key=_seed_key_for_group(group),
            created_at=now,
        )
        db.add(group_row)
        await db.flush()
        for leaf_idx, leaf in enumerate(group.leaves):
            leaf_color_dark = leaf.color_dark or group.color_dark
            leaf_color_light = leaf.color_light or group.color_light
            db.add(
                Category(
                    user_id=user_id,
                    parent_id=group_row.id,
                    name=leaf.name,
                    icon=leaf.icon,
                    icon_set=leaf.icon_set,
                    color_dark=leaf_color_dark,
                    color_light=leaf_color_light,
                    classification=leaf.classification or group.classification,
                    is_system=True,
                    sort_order=leaf_idx,
                    seed_key=_seed_key_for_leaf(leaf),
                    created_at=now,
                )
            )
        await db.flush()
    return True


async def list_categories_for_user(db: AsyncSession, user_id: int) -> list[Category]:
    rows = (
        await db.execute(
            select(Category)
            .where(Category.user_id == user_id)
            .order_by(
                Category.parent_id.is_(None).desc(),
                Category.parent_id,
                Category.sort_order,
                Category.id,
            )
        )
    ).scalars().all()
    return list(rows)


async def get_category_for_user(
    db: AsyncSession, user_id: int, category_id: int
) -> Category | None:
    return (
        await db.execute(
            select(Category).where(
                Category.user_id == user_id,
                Category.id == category_id,
            )
        )
    ).scalar_one_or_none()


async def create_category(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    parent_id: int | None,
    icon: str | None,
    icon_set: str | None,
    color_dark: str | None,
    color_light: str | None,
    classification: str | None = None,
) -> Category:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    parent = None
    if parent_id is not None:
        parent = await get_category_for_user(db, user_id, parent_id)
        if parent is None:
            raise HTTPException(status_code=400, detail="Invalid parent_id")
        if parent.parent_id is not None:
            raise HTTPException(status_code=400, detail="Cannot nest categories beyond two levels")

    # Soft default: a subcategory inherits its parent group's classification when
    # the caller omits one (mirrors the create-leaf UI, which never shows a
    # classification picker). An explicit value still wins, so intentional
    # transfer/investment overrides (e.g. seeded Loan Payment under Financial)
    # stay possible. A top-level group must declare its own classification.
    if classification is None:
        if parent is None:
            raise HTTPException(status_code=400, detail="classification is required for a top-level group")
        classification = parent.classification
    if classification not in VALID_CLASSIFICATIONS:
        raise HTTPException(status_code=400, detail="Invalid classification")

    next_sort = (
        await db.execute(
            select(func.coalesce(func.max(Category.sort_order), -1))
            .where(Category.user_id == user_id, Category.parent_id.is_(parent_id) if parent_id is None else Category.parent_id == parent_id)
        )
    ).scalar() or 0
    sort_order = int(next_sort) + 1

    category = Category(
        user_id=user_id,
        parent_id=parent_id,
        name=name,
        icon=(icon or None),
        icon_set=(icon_set or None),
        color_dark=(color_dark or None),
        color_light=(color_light or None),
        classification=classification,
        is_system=False,
        sort_order=sort_order,
        created_at=datetime.now(timezone.utc),
    )
    db.add(category)
    await db.flush()
    return category


async def update_category(
    db: AsyncSession,
    user_id: int,
    category_id: int,
    *,
    name: str | None = None,
    icon: str | None = None,
    icon_set: str | None = None,
    color_dark: str | None = None,
    color_light: str | None = None,
    classification: str | None = None,
) -> Category:
    category = await get_category_for_user(db, user_id, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")

    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        category.name = cleaned
    if icon is not None:
        category.icon = icon or None
    if icon_set is not None:
        category.icon_set = icon_set or None
    if color_dark is not None:
        category.color_dark = color_dark or None
    if color_light is not None:
        category.color_light = color_light or None
    if classification is not None and classification != category.classification:
        if category.is_system:
            raise HTTPException(status_code=400, detail="Cannot change classification of system category")
        if classification not in VALID_CLASSIFICATIONS:
            raise HTTPException(status_code=400, detail="Invalid classification")
        category.classification = classification

    await db.flush()
    return category


async def reset_category_to_seed(
    db: AsyncSession, user_id: int, category_id: int
) -> Category:
    """Reset a seeded system category's icon / icon_set / theme colors / name back to
    the SEED_TAXONOMY defaults. Errors on user-created categories (no seed
    baseline to restore)."""
    category = await get_category_for_user(db, user_id, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    if not category.is_system:
        raise HTTPException(status_code=400, detail="Only seeded system categories can be reset")

    # Resolve only by the stable seed key so user-renamed categories still map
    # to exactly one canonical seed definition.
    seed_entry: dict | None = None
    if category.parent_id is None:
        for group in SEED_TAXONOMY:
            if category.seed_key is not None and _seed_key_for_group(group) == category.seed_key:
                seed_entry = {
                    "name": group.name,
                    "icon": group.icon,
                    "icon_set": group.icon_set,
                    "color_dark": group.color_dark,
                    "color_light": group.color_light,
                    "classification": group.classification,
                }
                break
    else:
        parent = await get_category_for_user(db, user_id, category.parent_id)
        parent_seed_key = parent.seed_key if parent else None
        for group in SEED_TAXONOMY:
            if parent_seed_key is None or _seed_key_for_group(group) != parent_seed_key:
                continue
            for leaf in group.leaves:
                if category.seed_key is not None and _seed_key_for_leaf(leaf) == category.seed_key:
                    seed_entry = {
                        "name": leaf.name,
                        "icon": leaf.icon,
                        "icon_set": leaf.icon_set,
                        "color_dark": leaf.color_dark or group.color_dark,
                        "color_light": leaf.color_light or group.color_light,
                        "classification": leaf.classification or group.classification,
                    }
                    break
            if seed_entry is not None:
                break

    if seed_entry is None:
        raise HTTPException(
            status_code=400,
            detail="Cannot resolve seed defaults for this category.",
        )

    category.name = seed_entry["name"]
    category.icon = seed_entry["icon"]
    category.icon_set = seed_entry["icon_set"]
    category.color_dark = seed_entry["color_dark"]
    category.color_light = seed_entry["color_light"]
    category.classification = seed_entry["classification"]
    await db.flush()
    return category


async def delete_category(db: AsyncSession, user_id: int, category_id: int) -> None:
    category = await get_category_for_user(db, user_id, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    if category.is_system:
        raise HTTPException(status_code=400, detail="Cannot delete system category")

    children_count = (
        await db.execute(
            select(func.count(Category.id)).where(
                Category.user_id == user_id, Category.parent_id == category_id
            )
        )
    ).scalar() or 0
    if children_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete category with {children_count} subcategories. Delete or reassign them first.",
        )

    # A manual re-tag auto-creates a per-user rule pointing at the chosen category
    # (see update_transaction). Its non-null FK would otherwise block this delete,
    # so drop any rules targeting this category first — they're meaningless once
    # the target is gone.
    await db.execute(
        delete(CategoryRule).where(
            CategoryRule.user_id == user_id, CategoryRule.category_id == category_id
        )
    )

    # Preserve detected recurring-series history while dropping only this user's
    # reference to the category being deleted. This is explicit because SQLite's
    # composite owner FK does not provide SET NULL semantics.
    await db.execute(
        update(RecurringSeries)
        .where(
            RecurringSeries.user_id == user_id,
            RecurringSeries.category_id == category_id,
        )
        .values(category_id=None)
    )

    # Clear the manual override on rows tagged here so the re-eval below resolves
    # them to their auto/initial category instead of leaving a dangling id. Restore
    # the true provider sign first (`raw_amount`) so sign-routed types like interest
    # re-resolve to the correct direction (Interest Earned vs Charged) rather than
    # the flipped one the manual expense tag left behind.
    await db.execute(
        update(Transaction)
        .where(Transaction.user_id == user_id, Transaction.category_id == category_id)
        .values(
            category_id=None,
            category_source=None,
            amount=func.coalesce(Transaction.raw_amount, Transaction.amount),
            raw_amount=None,
        )
    )
    await db.delete(category)
    await db.flush()

    # Revert affected rows (and anything the removed rule used to catch) to the
    # rule/type auto-resolution — their initial category — rather than Uncategorized.
    await re_evaluate_all_transactions_for_user(db, user_id)


async def reset_transaction_category_to_auto(
    db: AsyncSession, user_id: int, transaction_id: int
) -> Transaction:
    """Revert a transaction to its initial auto category.

    Manually tagging a row records a per-descriptor rule and pins the row as
    ``manual`` (see update_transaction). This is the inverse: forget that rule,
    drop the manual override, and re-run the rule chain — so the row (and any
    siblings the forgotten rule had caught) fall back to bundled-rule/type
    auto-resolution. Caller commits.
    """
    from app.services.manual_accounts import manual_kind_for_external_id

    tx = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.id == transaction_id
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if manual_kind_for_external_id(tx.external_id) in ("cash_mirror", "cash_opening"):
        raise HTTPException(
            status_code=400,
            detail="This is a system-managed cash entry and can't be re-categorized.",
        )

    pattern = normalize_rule_pattern((tx.description or "").strip())
    if pattern:
        await db.execute(
            delete(CategoryRule).where(
                CategoryRule.user_id == user_id,
                CategoryRule.is_regex.is_(False),
                func.lower(CategoryRule.description_pattern) == pattern.lower(),
            )
        )

    tx.category_id = None
    tx.category_source = None
    # Restore the true provider sign before re-resolving so sign-routed types
    # (interest) re-resolve to the correct direction instead of the flipped one.
    restored_raw_amount = tx.raw_amount is not None
    if restored_raw_amount:
        tx.amount = tx.raw_amount
        tx.raw_amount = None
    await db.flush()

    await re_evaluate_all_transactions_for_user(db, user_id)
    await db.refresh(tx)

    return tx


async def compute_default_category_for_transaction(
    db: AsyncSession, user_id: int, transaction_id: int
) -> tuple[int | None, str | None]:
    """Preview the category a transaction would revert to, without mutating anything.

    Uses the same resolution `reset_transaction_category_to_auto` applies — the rule
    chain with this descriptor's auto-created user rule excluded — so the tray can
    show the default category before the user saves. Returns (category_id, source).
    """
    from app.models import Account, Institution

    tx = (
        await db.execute(
            select(Transaction).where(
                Transaction.user_id == user_id, Transaction.id == transaction_id
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    meta = (
        await db.execute(
            select(Account.account_type, Institution.provider, Institution.cash_tracking_start)
            .join(Institution, Institution.id == Account.institution_id)
            .where(Account.id == tx.account_id)
        )
    ).first()
    account_type = meta.account_type if meta else None
    provider = meta.provider if meta else None
    cash_cutoff = meta.cash_tracking_start if meta else None

    user_rules = await load_user_rules(db, user_id)
    pattern = normalize_rule_pattern((tx.description or "").strip())
    if pattern:
        user_rules = [
            r
            for r in user_rules
            if r.is_regex or (r.description_pattern or "").lower() != pattern.lower()
        ]

    category_seed_key_to_id = await build_category_seed_key_to_id_map(db, user_id)
    type_to_category_id = await build_type_to_category_id_map(db, user_id)

    cat_id, source, _ = resolve_category_for_transaction(
        description=tx.description,
        amount=tx.amount,
        provider=provider,
        account_type=account_type,
        transaction_type=tx.type,
        mcc=tx.mcc,
        user_rules=user_rules,
        category_seed_key_to_id=category_seed_key_to_id,
        type_to_category_id=type_to_category_id,
        transaction_date=tx.date,
        cash_tracking_start=cash_cutoff,
    )
    return cat_id, source


# Structural transaction types whose cash direction is unambiguous. Used to
# restore the correct sign when a row is reclassified back to an investment/
# transfer category — whose classification alone doesn't imply a sign — after an
# income/expense reclassification flipped it. (`amount` is stored signed and
# mutated in place, so the raw provider sign isn't otherwise recoverable in-app.)
_INFLOW_TYPES = frozenset({"sell", "dividend", "distribution", "deposit"})
_OUTFLOW_TYPES = frozenset({"buy", "withholding_tax", "withdrawal"})


def canonical_amount_for_classification(
    amount: float, classification: str | None, transaction_type: str | None = None
) -> float:
    """Coerce a transaction's signed amount to match its category/type.

    Income → positive, expense → negative. For transfer/investment (and unknown)
    classifications the classification alone doesn't fix a sign, so when the
    structural ``transaction_type`` has an unambiguous direction (sell/dividend/
    deposit → +, buy/withholding_tax/withdrawal → −) that is applied — this
    restores a sign an earlier income/expense flip overwrote. Genuinely ambiguous
    types (transfer, …) keep their current sign. Idempotent, so it can be
    re-applied on every sync without reverting a user's cross-classification flip.
    """
    if classification == "income":
        return abs(amount)
    if classification == "expense":
        return -abs(amount)
    if transaction_type in _INFLOW_TYPES:
        return abs(amount)
    if transaction_type in _OUTFLOW_TYPES:
        return -abs(amount)
    return amount


async def build_type_to_category_id_map(
    db: AsyncSession, user_id: int
) -> dict[str, int]:
    """Resolve transaction.type → seeded category_id for a user.

    Used by connector persistence to auto-assign category on new
    transactions. Resolves via the stable ``Category.seed_key`` column so
    user renames of the seeded leaf (Coffee → Coffee & Tea, Withheld →
    Witholding tax, etc.) don't break the type-mapping fallback.

    Returns empty dict if the user has no seeded categories yet.
    """
    rows = (
        await db.execute(
            select(Category.id, Category.seed_key).where(
                Category.user_id == user_id,
                Category.seed_key.is_not(None),
                Category.seed_key.in_(list(TYPE_TO_SEED_KEY.values())),
            )
        )
    ).all()
    seed_key_to_id = {sk: cid for cid, sk in rows}
    return {
        type_value: seed_key_to_id[sk]
        for type_value, sk in TYPE_TO_SEED_KEY.items()
        if sk in seed_key_to_id
    }


# ---------------------------------------------------------------------------
# Rule application
# ---------------------------------------------------------------------------


_USER_REGEX_CACHE: dict[tuple[int, str], re.Pattern[str]] = {}


# ---------------------------------------------------------------------------
# Retag -> rule pattern normalization
# ---------------------------------------------------------------------------
# When a user recategorizes a transaction we auto-create a CategoryRule from its
# descriptor so the same merchant sticks on future syncs. Raw bank descriptions
# carry per-charge noise (transaction ids, amounts, store numbers, order refs),
# so saving the full string yields a rule that only ever matches that one row
# (the source of one-off rule pollution). ``normalize_rule_pattern`` reduces a
# description to a stable merchant substring that recurs on future charges, or
# ``None`` when it's too generic to make a safe rule (a bare cheque / transfer /
# "Card transaction of ..."). Downstream matching is plain substring, so the
# result must stay a CONTIGUOUS slice of the original; we fail to ``None`` rather
# than ever emit an over-broad pattern.

# Wise: "Card transaction of <amt> <cur> issued by <merchant>" — the merchant is
# the stable tail; the amount up front is the per-charge part.
_RULE_WISE_ISSUED_BY = re.compile(r"\bissued by\s+(.+)$", re.IGNORECASE)
# Leading connector wrappers (Tangerine debit etc.) peeled before extraction.
_RULE_LEADING_WRAPPER = re.compile(
    r"^\s*(?:interac|visa debit)\s*-\s*(?:purchase|funds transfer)\s*-\s*",
    re.IGNORECASE,
)
# First per-charge noise token marks where the merchant name ends.
_RULE_NOISE_TOKEN = re.compile(
    r"\*(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3,}"  # *225136839 / *8R2VV82N3 — id-like
                                            # (has a digit) so "*Amazon" / "SQ
                                            # *Store" merchant tails survive
    r"|#\s*\d+"               # #631 store numbers
    r"|\b\d[\d\-]{3,}\b"      # 7022801098 / 701-8715545-18 long refs (>=4 chars)
    r"|\b\d+\.\d{2}\b"        # 12.00 amounts
)
# Generic banking descriptors that must not become a (broad) merchant rule.
# Trailing spaces on short tokens avoid clipping real merchants (e.g. "atm "
# won't reject ATMOSPHERE; "eft " / "fee " stay scoped).
_RULE_GENERIC_PREFIXES = (
    "card transaction of", "cheque", "withdrawal", "deposit", "transfer",
    "converted", "bill payment", "payment", "pay to", "online banking",
    "internet withdrawal", "internet deposit", "eft ", "interac", "e-transfer",
    "etransfer", "abm ", "atm ", "pre-authorized", "preauthorized",
    "loan advance", "sent money", "received money", "topped up",
    "direct deposit", "fee ", "cash advance", "reversal", "adjustment", "wire ",
    "www ", "br to br", "inter-fi",
)


def normalize_rule_pattern(raw_description: str | None) -> str | None:
    """Reduce a transaction description to a stable merchant substring for an
    auto-created user rule, or ``None`` if it's too generic to rule on safely."""
    if not raw_description:
        return None
    text = " ".join(raw_description.split())
    wise = _RULE_WISE_ISSUED_BY.search(text)
    if wise:
        core = wise.group(1)
    else:
        core = _RULE_LEADING_WRAPPER.sub("", text)
        noise = _RULE_NOISE_TOKEN.search(core)
        if noise:
            core = core[: noise.start()]
    core = re.sub(r"\s+", " ", core).strip(" -*,#.|/")
    if len(core) < 4:
        return None
    low = core.lower()
    if any(low.startswith(prefix) for prefix in _RULE_GENERIC_PREFIXES):
        return None
    return core


def _user_rule_matches(
    rule: CategoryRule,
    *,
    description: str | None,
    amount: float | None,
    provider: str | None,
    account_type: str | None,
    transaction_type: str | None,
) -> bool:
    if not rule.enabled:
        return False
    if rule.provider and rule.provider.lower() != (provider or "").lower():
        return False
    if rule.account_type and rule.account_type != account_type:
        return False
    if rule.transaction_type and rule.transaction_type != transaction_type:
        return False
    if rule.amount_sign and amount is not None:
        if rule.amount_sign == "positive" and amount <= 0:
            return False
        if rule.amount_sign == "negative" and amount >= 0:
            return False
    if not description:
        return False
    if rule.is_regex:
        key = (rule.id, rule.description_pattern)
        compiled = _USER_REGEX_CACHE.get(key)
        if compiled is None:
            try:
                compiled = re.compile(rule.description_pattern, re.IGNORECASE)
                _USER_REGEX_CACHE[key] = compiled
            except re.error:
                return False
        return bool(compiled.search(description))
    desc_lower = description.lower()
    for alt in rule.description_pattern.lower().split("|"):
        alt = alt.strip()
        if alt and alt in desc_lower:
            return True
    return False


async def load_user_rules(db: AsyncSession, user_id: int) -> list[CategoryRule]:
    rows = (
        await db.execute(
            select(CategoryRule)
            .where(CategoryRule.user_id == user_id)
            .order_by(CategoryRule.priority, CategoryRule.id)
        )
    ).scalars().all()
    return list(rows)


async def build_category_seed_key_to_id_map(
    db: AsyncSession, user_id: int
) -> dict[str, int]:
    """Map stable seed_key -> current category_id for this user.

    Seeded system rows carry an immutable ``seed_key`` written at seed time
    (and backfilled for pre-migration rows in ``a4b5c6d7e8f9``). User
    renames touch ``name`` only, so the same ``seed_key`` keeps pointing at
    the same row forever. Bundled rules in ``categories_rules.py`` resolve
    targets through this map exclusively — no more heuristics, no more
    rename-breaks-rules.
    """
    rows = (
        await db.execute(
            select(Category.id, Category.seed_key).where(
                Category.user_id == user_id,
                Category.seed_key.is_not(None),
                Category.parent_id.is_not(None),
            )
        )
    ).all()
    return {seed_key: cid for cid, seed_key in rows}


def resolve_category_for_transaction(
    *,
    description: str | None,
    amount: float | None,
    provider: str | None,
    account_type: str | None,
    transaction_type: str,
    mcc: str | None = None,
    user_rules: list[CategoryRule],
    bundled_rules: tuple[BundledRule, ...] = BUNDLED_RULES_SORTED,
    category_seed_key_to_id: dict[str, int],
    type_to_category_id: dict[str, int],
    transaction_date: datetime | None = None,
    cash_tracking_start: date | None = None,
) -> tuple[int | None, str | None, int | None]:
    """Resolve (category_id, source, matched_rule_id) for a transaction.

    Order of precedence: user rules (sorted by priority) -> bundled rules
    (sorted by priority) -> type-mapping fallback.

    Returns (None, None, None) if nothing matches and no type-mapping exists.

    Cash gating: auto-resolution to the **Cash** leaf is suppressed for
    transactions dated before ``cash_tracking_start`` (so an initial sync of old
    ATM withdrawals stays ordinary spend instead of inflating the cash balance).
    A direct manual tag bypasses this — it never goes through this resolver.
    """
    cash_id = category_seed_key_to_id.get("cash")

    def _cash_gated(cat_id: int | None) -> bool:
        if cat_id is None or cash_id is None or cat_id != cash_id:
            return False
        if cash_tracking_start is None or transaction_date is None:
            return False
        tx_day = transaction_date.date() if hasattr(transaction_date, "date") else transaction_date
        return tx_day < cash_tracking_start

    # For authoritative security-name types, only honor rules explicitly scoped
    # to that type — a plain merchant rule must not retag a stock buy/sell.
    security_typed = transaction_type in SECURITY_NAME_TYPES

    for rule in user_rules:
        if security_typed and rule.transaction_type != transaction_type:
            continue
        if _user_rule_matches(
            rule,
            description=description,
            amount=amount,
            provider=provider,
            account_type=account_type,
            transaction_type=transaction_type,
        ) and not _cash_gated(rule.category_id):
            return rule.category_id, "rule", rule.id

    for rule in bundled_rules:
        if security_typed and rule.transaction_type != transaction_type:
            continue
        if bundled_rule_matches(
            rule,
            description=description,
            amount=amount,
            provider=provider,
            account_type=account_type,
            transaction_type=transaction_type,
        ):
            cat_id = category_seed_key_to_id.get(rule.category_seed_key)
            if cat_id is not None and not _cash_gated(cat_id):
                return cat_id, "rule", None

    # MCC fallback: a card-network merchant category code resolves spending that
    # the name rules missed, ahead of the generic type-mapping fallback (which
    # would otherwise file a card purchase as a Withdrawal). Spending MCCs only.
    mcc_seed = seed_key_for_mcc(mcc)
    if mcc_seed:
        cat_id = category_seed_key_to_id.get(mcc_seed)
        if cat_id is not None:
            return cat_id, "auto", None

    # Type-mapping fallback. Globally ambiguous types always require a rule; on
    # banking/liability accounts, deposit/withdrawal also describe direction only.
    normalized_account_type = (account_type or "").strip().lower()
    directional_guess = (
        transaction_type in DIRECTION_ONLY_TYPES
        and normalized_account_type in DIRECTION_ONLY_ACCOUNT_TYPES
    )
    if transaction_type not in TYPE_FALLBACK_REQUIRES_RULE and not directional_guess:
        cat_id = type_to_category_id.get(transaction_type)
        if cat_id is not None:
            return cat_id, "auto", None

    # Nothing matched — assign the explicit Uncategorized leaf so every
    # transaction carries a category (no NULL holes) and shows under the
    # Uncategorized filter / ❓ pill for the user to resolve.
    uncat_id = category_seed_key_to_id.get("uncategorized")
    if uncat_id is not None:
        return uncat_id, "auto", None
    return None, None, None


class CategoryResolver:
    """Per-sync context bundling rule chain + category lookups for a user.

    Built once per provider sync (`build`) and called per-transaction (`resolve`).
    """

    def __init__(
        self,
        user_rules: list[CategoryRule],
        category_seed_key_to_id: dict[str, int],
        type_to_category_id: dict[str, int],
    ):
        self.user_rules = user_rules
        self.category_seed_key_to_id = category_seed_key_to_id
        self.type_to_category_id = type_to_category_id

    @classmethod
    async def build(cls, db: AsyncSession, user_id: int) -> "CategoryResolver":
        return cls(
            user_rules=await load_user_rules(db, user_id),
            category_seed_key_to_id=await build_category_seed_key_to_id_map(db, user_id),
            type_to_category_id=await build_type_to_category_id_map(db, user_id),
        )

    def resolve(
        self,
        *,
        description: str | None,
        amount: float | None,
        provider: str | None,
        account_type: str | None,
        transaction_type: str,
        mcc: str | None = None,
        transaction_date: datetime | None = None,
        cash_tracking_start: date | None = None,
    ) -> tuple[int | None, str | None]:
        # cash_tracking_start is the SOURCE institution's cutoff — the caller passes the
        # cutoff of the account's institution (per-institution gating).
        cat_id, source, _ = resolve_category_for_transaction(
            description=description,
            amount=amount,
            provider=provider,
            account_type=account_type,
            transaction_type=transaction_type,
            mcc=mcc,
            user_rules=self.user_rules,
            category_seed_key_to_id=self.category_seed_key_to_id,
            type_to_category_id=self.type_to_category_id,
            transaction_date=transaction_date,
            cash_tracking_start=cash_tracking_start,
        )
        return cat_id, source


async def re_evaluate_all_transactions_for_user(
    db: AsyncSession, user_id: int
) -> dict[str, int]:
    """Run the rule chain over every non-manual transaction and update
    `category_id` / `category_source` where the resolution changes.

    Returns counters: {"updated": N, "unchanged": N, "manual_skipped": N}.
    Caller commits.
    """
    from app.services.manual_accounts import (
        CASH_MIRROR_PREFIX,
        reconcile_cash_mirror,
    )

    user_rules = await load_user_rules(db, user_id)
    category_seed_key_to_id = await build_category_seed_key_to_id_map(db, user_id)
    type_to_category_id = await build_type_to_category_id_map(db, user_id)
    cash_category_id = category_seed_key_to_id.get("cash")

    # Pull the joined rows needed for matching (provider, account_type live on Account/Institution)
    from app.models import Account, Institution

    rows = (
        await db.execute(
            select(
                Transaction.id,
                Transaction.external_id,
                Transaction.description,
                Transaction.amount,
                Transaction.type,
                Transaction.mcc,
                Transaction.date,
                Transaction.category_id,
                Transaction.category_source,
                Account.account_type,
                Institution.provider,
                Institution.cash_tracking_start,
            )
            .join(Account, Account.id == Transaction.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .where(Transaction.user_id == user_id)
        )
    ).all()

    updated = 0
    unchanged = 0
    manual_skipped = 0
    cash_affected_ids: set[int] = set()

    for row in rows:
        # Auto-spawned cash mirror legs are managed by reconcile, not the rule chain.
        if (row.external_id or "").startswith(CASH_MIRROR_PREFIX):
            continue
        # "manual" = in-app override; "import" = user-provided via CSV. Both are
        # user intent and must survive auto re-evaluation.
        if row.category_source in ("manual", "import"):
            manual_skipped += 1
            continue
        new_cat_id, new_source, _ = resolve_category_for_transaction(
            description=row.description,
            amount=row.amount,
            provider=row.provider,
            account_type=row.account_type,
            transaction_type=row.type,
            mcc=row.mcc,
            user_rules=user_rules,
            category_seed_key_to_id=category_seed_key_to_id,
            type_to_category_id=type_to_category_id,
            transaction_date=row.date,
            cash_tracking_start=row.cash_tracking_start,
        )
        if new_cat_id == row.category_id and new_source == row.category_source:
            unchanged += 1
            continue
        await db.execute(
            update(Transaction)
            .where(Transaction.id == row.id)
            .values(category_id=new_cat_id, category_source=new_source)
        )
        updated += 1
        if cash_category_id is not None and cash_category_id in (new_cat_id, row.category_id):
            cash_affected_ids.add(row.id)

    # Spawn/remove cash mirror legs for rows whose Cash categorization changed.
    for cash_tx_id in cash_affected_ids:
        await reconcile_cash_mirror(db, user_id, cash_tx_id)

    return {
        "updated": updated,
        "unchanged": unchanged,
        "manual_skipped": manual_skipped,
        "total": len(rows),
    }


async def ensure_categorization_ruleset_current(user_id: int) -> bool:
    """One-time, version-gated re-evaluation against the current bundled rulepack.

    Syncs are incremental (backfill once, then only a recent window), so a
    code-shipped rulepack update otherwise only reaches transactions a later sync
    re-fetches. This re-resolves the user's full non-manual history once per
    ``RULESET_VERSION`` bump — at app startup — so rule improvements apply across
    the board without re-adding institutions. It runs the normal resolution chain,
    which **skips manual tags** and lets **user rules win over bundled rules**, so a
    user's own categorizations are never clobbered. No-op (single SELECT) when the
    stored version already matches. Returns True if a re-evaluation actually ran.

    NOTE: a hosted multi-user deployment should make this lazy/per-user (or a
    background queue) rather than a synchronous startup sweep over every user.
    """
    from app.database import async_session
    from app.models import Setting
    from app.services.categories_rules import RULESET_VERSION
    from app.services.sqlite_write_gate import sqlite_write_gate

    target = str(RULESET_VERSION)
    async with async_session() as db:
        row = (
            await db.execute(
                select(Setting).where(
                    Setting.user_id == user_id,
                    Setting.key == "categorization_ruleset_version",
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.value == target:
            return False
        # Hold the write gate across the re-eval AND the commit so SQLite write
        # locks serialize correctly (gate enters first, releases after commit).
        async with sqlite_write_gate():
            await re_evaluate_all_transactions_for_user(db, user_id)
            if row is None:
                db.add(
                    Setting(
                        user_id=user_id,
                        key="categorization_ruleset_version",
                        value=target,
                    )
                )
            else:
                row.value = target
            await db.commit()
    return True


def serialize_category(category: Category) -> dict:
    return {
        "id": category.id,
        "parent_id": category.parent_id,
        "name": category.name,
        "icon": category.icon,
        "icon_set": category.icon_set,
        "color_dark": category.color_dark,
        "color_light": category.color_light,
        "classification": category.classification,
        "is_system": bool(category.is_system),
        "sort_order": category.sort_order,
        "seed_key": category.seed_key,
    }
