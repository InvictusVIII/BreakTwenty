"""apply the canonical category palette to existing users

Revision ID: 0008_canonical_category_palette
Revises: 0007_transaction_import_attempt_id
Create Date: 2026-09-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_canonical_category_palette"
down_revision: Union[str, Sequence[str], None] = "0007_transaction_import_attempt_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_GROUP_COLORS = {
    "income": ("#00ce31", "#01b400"),
    "investments": ("#4f87ff", "#006eff"),
    "transfers": ("#ff9927", "#ff9927"),
    "financial": ("#ff2935", "#ff0000"),
    "food_and_drink": ("#ff4a18", "#ff4a18"),
    "housing": ("#ffe700", "#ffe700"),
    "transportation": ("#11cdef", "#11cdef"),
    "shopping": ("#2e61ff", "#0082ff"),
    "subscriptions": ("#ffa801", "#ffa801"),
    "entertainment": ("#9035ff", "#b569ff"),
    "health_and_wellness": ("#72ff67", "#72ff67"),
    "personal_care": ("#a370f0", "#e248f7"),
    "travel": ("#ffff00", "#ffff00"),
    "life": ("#fb6e94", "#ff99b1"),
    "taxes": ("#ff2935", "#ff0000"),
    "other": ("#ffffff", "#969696"),
}

_LEAF_COLOR_OVERRIDES = {
    ("income", "paycheck"): ("#00ce31", "#00ce31"),
    ("income", "dividend"): ("#00ff35", "#00ff35"),
    ("income", "interest"): ("#14ff00", "#14ff00"),
    ("income", "refund"): ("#00c86a", "#00c86a"),
    ("income", "reimburse"): ("#45ff6c", "#45ff6c"),
    ("income", "misc_income"): ("#00ce31", "#00ce31"),
    ("income", "gifts_received"): ("#00ce31", "#00ce31"),
    ("income", "etransfer_received"): ("#a855f7", "#d2b5ff"),
    ("investments", "buy"): ("#ffff18", "#ffff18"),
    ("investments", "sell"): ("#2bff31", "#2bff31"),
    ("investments", "expired"): ("#c4c4c4", "#888888"),
    ("transfers", "transfer"): ("#ff9400", "#f09000"),
    ("transfers", "deposit"): ("#00ff28", "#00ff28"),
    ("transfers", "withdrawal"): ("#ff2935", "#ff0000"),
    ("transfers", "cc_payment"): ("#259fde", "#21a7ff"),
    ("transfers", "loan_payment"): ("#259fde", "#21a7ff"),
    ("transfers", "loan_advance"): ("#259fde", "#21a7ff"),
    ("other", "uncategorized"): ("#8d8d8d", "#8d8d8d"),
    ("other", "other"): ("#ffffff", "#c3c3c3"),
    ("other", "cash"): ("#adffc2", "#77ffa1"),
    ("other", "business"): ("#c67050", "#d7d7d7"),
    ("other", "government"): ("#ffffff", "#e7e7e7"),
    ("other", "etransfer_sent"): ("#a855f7", "#d2b5ff"),
}


def upgrade() -> None:
    categories = sa.table(
        "categories",
        sa.column("id", sa.Integer()),
        sa.column("parent_id", sa.Integer()),
        sa.column("seed_key", sa.String()),
        sa.column("is_system", sa.Boolean()),
        sa.column("color_dark", sa.String()),
        sa.column("color_light", sa.String()),
    )
    connection = op.get_bind()

    for group_seed_key, (color_dark, color_light) in _GROUP_COLORS.items():
        parent_ids = sa.select(categories.c.id).where(
            categories.c.parent_id.is_(None),
            categories.c.is_system.is_(True),
            categories.c.seed_key == group_seed_key,
        )
        connection.execute(
            categories.update()
            .where(categories.c.id.in_(parent_ids))
            .values(color_dark=color_dark, color_light=color_light)
        )
        connection.execute(
            categories.update()
            .where(
                categories.c.parent_id.in_(parent_ids),
                categories.c.is_system.is_(True),
            )
            .values(color_dark=color_dark, color_light=color_light)
        )

    for (group_seed_key, leaf_seed_key), (color_dark, color_light) in _LEAF_COLOR_OVERRIDES.items():
        parent_ids = sa.select(categories.c.id).where(
            categories.c.parent_id.is_(None),
            categories.c.is_system.is_(True),
            categories.c.seed_key == group_seed_key,
        )
        connection.execute(
            categories.update()
            .where(
                categories.c.parent_id.in_(parent_ids),
                categories.c.is_system.is_(True),
                categories.c.seed_key == leaf_seed_key,
            )
            .values(color_dark=color_dark, color_light=color_light)
        )


def downgrade() -> None:
    pass
