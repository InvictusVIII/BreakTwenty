"""persist visible-auth attempt identity on transaction import jobs

Revision ID: 0007_transaction_import_attempt_id
Revises: 0006_category_theme_colors
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_transaction_import_attempt_id"
down_revision: Union[str, Sequence[str], None] = "0006_category_theme_colors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempt_id", sa.String(), nullable=True))
        batch_op.create_index(
            "ix_transaction_import_jobs_attempt_id",
            ["attempt_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_transaction_import_jobs_attempt_id")
        batch_op.drop_column("attempt_id")
