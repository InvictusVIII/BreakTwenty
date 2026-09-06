"""separate category colors by theme

Revision ID: 0006_category_theme_colors
Revises: 0005_transfer_cash_movement_categories
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_category_theme_colors"
down_revision: Union[str, Sequence[str], None] = "0005_transfer_cash_movement_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("categories", schema=None) as batch_op:
        batch_op.alter_column(
            "color",
            new_column_name="color_dark",
            existing_type=sa.String(),
            existing_nullable=True,
        )
        batch_op.add_column(sa.Column("color_light", sa.String(), nullable=True))
    op.execute("UPDATE categories SET color_light = color_dark")


def downgrade() -> None:
    op.execute("UPDATE categories SET color_dark = COALESCE(color_dark, color_light)")
    with op.batch_alter_table("categories", schema=None) as batch_op:
        batch_op.drop_column("color_light")
        batch_op.alter_column(
            "color_dark",
            new_column_name="color",
            existing_type=sa.String(),
            existing_nullable=True,
        )
