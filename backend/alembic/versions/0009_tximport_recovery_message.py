"""store transaction import restart recovery separately from errors

Revision ID: 0009_tximport_recovery_message
Revises: 0008_canonical_category_palette
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_tximport_recovery_message"
down_revision: Union[str, Sequence[str], None] = "0008_canonical_category_palette"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RESTART_RECOVERY_MESSAGE = "Transaction import resumed after backend restart."


def upgrade() -> None:
    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recovery_message", sa.Text(), nullable=True))

    jobs = sa.table(
        "transaction_import_jobs",
        sa.column("last_error", sa.Text()),
        sa.column("recovery_message", sa.Text()),
    )
    op.get_bind().execute(
        jobs.update()
        .where(jobs.c.last_error == RESTART_RECOVERY_MESSAGE)
        .values(
            last_error=None,
            recovery_message=RESTART_RECOVERY_MESSAGE,
        )
    )


def downgrade() -> None:
    jobs = sa.table(
        "transaction_import_jobs",
        sa.column("last_error", sa.Text()),
        sa.column("recovery_message", sa.Text()),
    )
    op.get_bind().execute(
        jobs.update()
        .where(
            jobs.c.last_error.is_(None),
            jobs.c.recovery_message.is_not(None),
        )
        .values(last_error=jobs.c.recovery_message)
    )

    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.drop_column("recovery_message")
