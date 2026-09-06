"""backend job integrity and recurring category deletion

Revision ID: 0002_backend_job_integrity
Revises: 0001
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_backend_job_integrity"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("recurring_series", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_recurring_series_category_delete",
            "categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_transaction_import_jobs_lease_expires_at"),
            ["lease_expires_at"],
            unique=False,
        )

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, institution_id, provider
                           ORDER BY created_at, id
                       ) AS active_rank
                FROM transaction_import_jobs
                WHERE status IN ('queued', 'running')
            )
            UPDATE transaction_import_jobs
            SET status = 'failed',
                last_error = 'Superseded while enforcing one active transaction import job.',
                lease_token = NULL,
                lease_expires_at = NULL,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN (SELECT id FROM ranked WHERE active_rank > 1)
            """
        )
    )
    op.create_index(
        "uq_transaction_import_jobs_active_connection_provider",
        "transaction_import_jobs",
        ["user_id", "institution_id", "provider"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "provider_sync_leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("institution_key", sa.String(), nullable=False),
        sa.Column("owner_token", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_token", name="uq_provider_sync_leases_owner_token"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "institution_key",
            name="uq_provider_sync_leases_user_provider_connection",
        ),
    )
    with op.batch_alter_table("provider_sync_leases", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_provider_sync_leases_expires_at"), ["expires_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_provider_sync_leases_provider"), ["provider"], unique=False)
        batch_op.create_index(batch_op.f("ix_provider_sync_leases_user_id"), ["user_id"], unique=False)

    op.create_table(
        "pending_sync_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status_provider", sa.String(), nullable=False),
        sa.Column("institution_key", sa.String(), nullable=False),
        sa.Column("final_status", sa.String(), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "status_provider",
            "institution_key",
            name="uq_pending_sync_statuses_user_provider_connection",
        ),
    )
    with op.batch_alter_table("pending_sync_statuses", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pending_sync_statuses_status_provider"),
            ["status_provider"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_pending_sync_statuses_user_id"), ["user_id"], unique=False)

    op.create_table(
        "sync_batch_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("connection_signature", sa.Text(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("lease_token", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", name="uq_sync_batch_jobs_batch_id"),
    )
    with op.batch_alter_table("sync_batch_jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_batch_jobs_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sync_batch_jobs_lease_expires_at"),
            ["lease_expires_at"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_sync_batch_jobs_lease_token"), ["lease_token"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_batch_jobs_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_batch_jobs_updated_at"), ["updated_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_batch_jobs_user_id"), ["user_id"], unique=False)
    op.create_index(
        "uq_sync_batch_jobs_active_signature",
        "sync_batch_jobs",
        ["user_id", "mode", "connection_signature"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_batch_jobs_active_signature", table_name="sync_batch_jobs")
    with op.batch_alter_table("sync_batch_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_user_id"))
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_updated_at"))
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_status"))
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_lease_token"))
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_lease_expires_at"))
        batch_op.drop_index(batch_op.f("ix_sync_batch_jobs_batch_id"))
    op.drop_table("sync_batch_jobs")

    with op.batch_alter_table("pending_sync_statuses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pending_sync_statuses_user_id"))
        batch_op.drop_index(batch_op.f("ix_pending_sync_statuses_status_provider"))
    op.drop_table("pending_sync_statuses")

    with op.batch_alter_table("provider_sync_leases", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_sync_leases_user_id"))
        batch_op.drop_index(batch_op.f("ix_provider_sync_leases_provider"))
        batch_op.drop_index(batch_op.f("ix_provider_sync_leases_expires_at"))
    op.drop_table("provider_sync_leases")

    op.drop_index(
        "uq_transaction_import_jobs_active_connection_provider",
        table_name="transaction_import_jobs",
    )
    with op.batch_alter_table("transaction_import_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_import_jobs_lease_expires_at"))
        batch_op.drop_column("lease_expires_at")

    with op.batch_alter_table("recurring_series", schema=None) as batch_op:
        batch_op.drop_constraint("fk_recurring_series_category_delete", type_="foreignkey")
