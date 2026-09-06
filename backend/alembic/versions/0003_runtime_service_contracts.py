"""runtime service leases and deployment-wide event-stream quotas

Revision ID: 0003_runtime_service_contracts
Revises: 0002_backend_job_integrity
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_runtime_service_contracts"
down_revision: Union[str, Sequence[str], None] = "0002_backend_job_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_service_leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_name", sa.String(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_token",
            name="uq_runtime_service_leases_owner_token",
        ),
        sa.UniqueConstraint(
            "service_name",
            name="uq_runtime_service_leases_service_name",
        ),
    )
    with op.batch_alter_table("runtime_service_leases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_runtime_service_leases_expires_at"),
            ["expires_at"],
            unique=False,
        )

    op.create_table(
        "event_stream_leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("user_slot", sa.Integer(), nullable=False),
        sa.Column("global_slot", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "global_slot >= 1 AND global_slot <= 128",
            name="ck_event_stream_leases_global_slot",
        ),
        sa.CheckConstraint(
            "user_slot >= 1 AND user_slot <= 4",
            name="ck_event_stream_leases_user_slot",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "global_slot",
            name="uq_event_stream_leases_global_slot",
        ),
        sa.UniqueConstraint(
            "lease_id",
            name="uq_event_stream_leases_lease_id",
        ),
        sa.UniqueConstraint(
            "user_id",
            "user_slot",
            name="uq_event_stream_leases_user_slot",
        ),
    )
    with op.batch_alter_table("event_stream_leases", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_event_stream_leases_expires_at"),
            ["expires_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_event_stream_leases_user_id"),
            ["user_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("event_stream_leases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_event_stream_leases_user_id"))
        batch_op.drop_index(batch_op.f("ix_event_stream_leases_expires_at"))
    op.drop_table("event_stream_leases")

    with op.batch_alter_table("runtime_service_leases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runtime_service_leases_expires_at"))
    op.drop_table("runtime_service_leases")
