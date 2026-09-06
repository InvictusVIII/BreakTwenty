"""reopen false-complete zero-row AMEX backfills

Revision ID: 0004_amex_backfill_recovery
Revises: 0003_runtime_service_contracts
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_amex_backfill_recovery"
down_revision: Union[str, Sequence[str], None] = "0003_runtime_service_contracts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RETRY_ERROR = "Retry required after correcting AMEX zero-row backfill handling."


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE transaction_import_account_states
            SET backfill_status = 'error',
                backfill_completed_at = NULL,
                last_error = :retry_error,
                updated_at = CURRENT_TIMESTAMP
            WHERE transaction_import_account_states.provider = 'amex'
              AND transaction_import_account_states.backfill_status = 'complete'
              AND EXISTS (
                  SELECT 1
                  FROM transaction_import_windows AS tiw
                  WHERE tiw.user_id = transaction_import_account_states.user_id
                    AND tiw.institution_id = transaction_import_account_states.institution_id
                    AND tiw.account_external_id = transaction_import_account_states.account_external_id
                    AND tiw.provider = 'amex'
                    AND tiw.mode = 'backfill'
                    AND tiw.status = 'complete'
                    AND tiw.transaction_count = 0
              )
            """
        ).bindparams(retry_error=RETRY_ERROR)
    )
    op.execute(
        sa.text(
            """
            UPDATE transaction_import_windows
            SET status = 'error',
                completed_at = NULL,
                last_error = :retry_error,
                updated_at = CURRENT_TIMESTAMP
            WHERE provider = 'amex'
              AND mode = 'backfill'
              AND status = 'complete'
              AND transaction_count = 0
            """
        ).bindparams(retry_error=RETRY_ERROR)
    )


def downgrade() -> None:
    pass
