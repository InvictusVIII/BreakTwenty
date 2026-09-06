"""classify deposit and withdrawal leaves as transfers

Revision ID: 0005_transfer_cash_movement_categories
Revises: 0004_amex_backfill_recovery
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op


revision: str = "0005_transfer_cash_movement_categories"
down_revision: Union[str, Sequence[str], None] = "0004_amex_backfill_recovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE categories SET classification = 'transfer' "
        "WHERE seed_key IN ('deposit', 'withdrawal') "
        "AND classification IN ('income', 'expense')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE categories SET classification = 'income' "
        "WHERE seed_key = 'deposit' AND classification = 'transfer'"
    )
    op.execute(
        "UPDATE categories SET classification = 'expense' "
        "WHERE seed_key = 'withdrawal' AND classification = 'transfer'"
    )
