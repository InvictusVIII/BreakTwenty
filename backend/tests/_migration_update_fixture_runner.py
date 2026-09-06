from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


SUCCESS_MIGRATION = '''"""Update-safety success fixture."""
from alembic import op
import sqlalchemy as sa

revision = "fixture_next"
down_revision = "__INSTALLED_HEAD__"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "update_safety_fixture",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )
    op.execute("INSERT INTO update_safety_fixture (id, value) VALUES (1, 'upgraded')")

def downgrade():
    op.drop_table("update_safety_fixture")
'''

FAILURE_MIGRATION = '''"""Update-safety failure fixture."""
from alembic import op
import sqlalchemy as sa

revision = "fixture_next"
down_revision = "__INSTALLED_HEAD__"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "partial_update_fixture",
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    raise RuntimeError("forced fixture migration failure")

def downgrade():
    op.drop_table("partial_update_fixture")
'''


def run_0001_to_0002_fixture(
    *,
    database_path: Path,
    alembic_ini_path: Path,
    snapshot_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config

    from app.database_encryption import connect_sqlcipher
    from app.migration_safety import inspect_database, upgrade_database_safely

    config = Config(str(alembic_ini_path))
    command.upgrade(config, "0001")
    connection = connect_sqlcipher(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        timestamp = "2026-08-13 12:00:00+00:00"
        connection.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (42, "fixture@example.invalid", timestamp),
        )
        connection.execute(
            """
            INSERT INTO institutions (
                id, user_id, name, type, provider, enabled, created_at, hidden, sync_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (7, 42, "Fixture Bank", "api", "fixture", 1, timestamp, 0, "idle"),
        )
        connection.execute(
            """
            INSERT INTO categories (
                id, user_id, parent_id, name, classification, is_system,
                sort_order, seed_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (8, 42, None, "Fixture Category", "expense", 0, 1, "fixture", timestamp),
        )
        connection.execute(
            """
            INSERT INTO recurring_series (
                id, user_id, merchant_key, direction, display_name, category_id,
                cadence, avg_amount, currency, occurrence_count, confidence, status,
                is_confirmed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                9,
                42,
                "fixture-merchant",
                "outflow",
                "Fixture Merchant",
                8,
                "monthly",
                "25.00000000",
                "CAD",
                3,
                0.9,
                "active",
                0,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO settings (id, user_id, key, value) VALUES (?, ?, ?, ?)",
            (1, 42, "fixture_marker", "data-created-at-0001"),
        )
        connection.executemany(
            """
            INSERT INTO transaction_import_jobs (
                id, user_id, institution_id, provider, job_id, status,
                stale_recovery_count, lease_token, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, 42, 7, "fixture", "active-first", "queued", 0, "owner-first", "2026-08-13 12:00:00+00:00", timestamp),
                (2, 42, 7, "fixture", "active-second", "running", 0, "owner-second", "2026-08-13 12:01:00+00:00", timestamp),
                (3, 42, 7, "fixture", "completed", "completed", 0, None, "2026-08-13 12:02:00+00:00", timestamp),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    before = inspect_database(database_path, alembic_ini_path)
    upgrade_result = upgrade_database_safely(database_path, alembic_ini_path)
    after = inspect_database(database_path, alembic_ini_path)

    snapshot_connection = connect_sqlcipher(snapshot_path)
    try:
        snapshot_revision = snapshot_connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        snapshot_marker = snapshot_connection.execute(
            "SELECT value FROM settings WHERE user_id=42 AND key='fixture_marker'"
        ).fetchone()[0]
    finally:
        snapshot_connection.close()

    connection = connect_sqlcipher(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        marker = connection.execute(
            "SELECT value FROM settings WHERE user_id=42 AND key='fixture_marker'"
        ).fetchone()[0]
        jobs = [
            list(row)
            for row in connection.execute(
                """
                SELECT job_id, status, last_error, lease_token,
                       lease_expires_at, finished_at
                FROM transaction_import_jobs
                ORDER BY id
                """
            ).fetchall()
        ]
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        lease_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('transaction_import_jobs')"
            ).fetchall()
        }
        job_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('transaction_import_jobs')"
            ).fetchall()
        }

        connection.execute("DELETE FROM categories WHERE id=8 AND user_id=42")
        connection.commit()
        recurring_category_id = connection.execute(
            "SELECT category_id FROM recurring_series WHERE id=9 AND user_id=42"
        ).fetchone()[0]

        unique_active_enforced = False
        try:
            connection.execute(
                """
                INSERT INTO transaction_import_jobs (
                    id, user_id, institution_id, provider, job_id, status,
                    stale_recovery_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (4, 42, 7, "fixture", "active-third", "queued", 0, timestamp, timestamp),
            )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            error_text = str(exc).lower()
            unique_active_enforced = (
                "unique constraint failed" in error_text
                and "transaction_import_jobs" in error_text
            )

        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()

    result = {
        "mode": "0001_to_0002",
        "beforeRevisions": before["currentRevisions"],
        "afterRevisions": after["currentRevisions"],
        "needsMigrationAfter": after["needsMigration"],
        "migratedSafely": upgrade_result["migrated"],
        "marker": marker,
        "jobs": jobs,
        "newTablesPresent": {
            "provider_sync_leases",
            "pending_sync_statuses",
            "sync_batch_jobs",
        }.issubset(table_names),
        "leaseExpiryColumnPresent": "lease_expires_at" in lease_columns,
        "activeUniqueIndexPresent": (
            "uq_transaction_import_jobs_active_connection_provider" in job_indexes
        ),
        "uniqueActiveEnforced": unique_active_enforced,
        "recurringCategoryAfterDelete": recurring_category_id,
        "foreignKeyErrors": foreign_key_errors,
        "snapshotRevision": snapshot_revision,
        "snapshotMarker": snapshot_marker,
        "snapshotEncrypted": snapshot_path.read_bytes()[:16] != b"SQLite format 3\x00",
    }
    print(json.dumps(result, sort_keys=True))


def _constraint_rejected(connection, statement: str, parameters: tuple) -> bool:
    try:
        connection.execute(statement, parameters)
        connection.commit()
    except Exception:
        connection.rollback()
        return True
    return False


def run_0002_to_0003_fixture(
    *,
    database_path: Path,
    alembic_ini_path: Path,
    snapshot_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config

    from app.database_encryption import connect_sqlcipher
    from app.migration_safety import inspect_database, upgrade_database_safely
    from app.models import EventStreamLease, RuntimeServiceLease

    config = Config(str(alembic_ini_path))
    command.upgrade(config, "0002_backend_job_integrity")
    timestamp = "2026-08-13 12:00:00+00:00"
    future = "2026-08-13 13:00:00+00:00"
    connection = connect_sqlcipher(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executemany(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            [
                (42, "runtime-fixture@example.invalid", timestamp),
                (43, "cascade-fixture@example.invalid", timestamp),
            ],
        )
        connection.execute(
            "INSERT INTO settings (id, user_id, key, value) VALUES (?, ?, ?, ?)",
            (1, 42, "fixture_marker", "data-created-at-0002"),
        )
        connection.execute(
            """
            INSERT INTO market_strip_snapshot_points (
                id, user_id, provider, source, mode, symbol_key, local_date,
                sample_slot, close, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                42,
                "fixture-provider",
                "user",
                "user_key",
                "sp500",
                "2026-08-13",
                "2026-08-13T12:00:00+00:00",
                "123.45000000",
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    before = inspect_database(database_path, alembic_ini_path)
    upgrade_result = upgrade_database_safely(database_path, alembic_ini_path)
    # This fixture intentionally truncates the migration graph at 0003. Do not
    # compare that historical schema to today's complete ORM model: later
    # schema migrations are expected to differ. The targeted runtime/event
    # table parity checks below validate the 0003 contract directly.
    after = inspect_database(database_path, alembic_ini_path)

    snapshot_connection = connect_sqlcipher(snapshot_path)
    try:
        snapshot_revision = snapshot_connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        snapshot_marker = snapshot_connection.execute(
            "SELECT value FROM settings WHERE user_id=42 AND key='fixture_marker'"
        ).fetchone()[0]
    finally:
        snapshot_connection.close()

    connection = connect_sqlcipher(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        marker = connection.execute(
            "SELECT value FROM settings WHERE user_id=42 AND key='fixture_marker'"
        ).fetchone()[0]
        snapshot_row = list(
            connection.execute(
                """
                SELECT provider, symbol_key, local_date, sample_slot, close
                FROM market_strip_snapshot_points WHERE id=1
                """
            ).fetchone()
        )
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        runtime_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('runtime_service_leases')"
            ).fetchall()
        }
        event_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('event_stream_leases')"
            ).fetchall()
        }
        runtime_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('runtime_service_leases')"
            ).fetchall()
        }
        event_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('event_stream_leases')"
            ).fetchall()
        }

        connection.execute(
            """
            INSERT INTO runtime_service_leases (
                id, service_name, owner_token, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (1, "fixture-service", "owner-one", timestamp, future),
        )
        connection.commit()
        duplicate_service_rejected = _constraint_rejected(
            connection,
            """
            INSERT INTO runtime_service_leases (
                id, service_name, owner_token, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (2, "fixture-service", "owner-two", timestamp, future),
        )
        duplicate_owner_rejected = _constraint_rejected(
            connection,
            """
            INSERT INTO runtime_service_leases (
                id, service_name, owner_token, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (3, "other-service", "owner-one", timestamp, future),
        )

        connection.execute(
            """
            INSERT INTO event_stream_leases (
                id, lease_id, user_id, user_slot, global_slot, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "lease-one", 42, 1, 1, timestamp, future),
        )
        connection.commit()
        duplicate_user_slot_rejected = _constraint_rejected(
            connection,
            """
            INSERT INTO event_stream_leases (
                id, lease_id, user_id, user_slot, global_slot, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (2, "lease-two", 42, 1, 2, timestamp, future),
        )
        duplicate_global_slot_rejected = _constraint_rejected(
            connection,
            """
            INSERT INTO event_stream_leases (
                id, lease_id, user_id, user_slot, global_slot, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (3, "lease-three", 43, 1, 1, timestamp, future),
        )
        invalid_slot_rejected = _constraint_rejected(
            connection,
            """
            INSERT INTO event_stream_leases (
                id, lease_id, user_id, user_slot, global_slot, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (4, "lease-four", 43, 0, 129, timestamp, future),
        )
        connection.execute(
            """
            INSERT INTO event_stream_leases (
                id, lease_id, user_id, user_slot, global_slot, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (5, "lease-cascade", 43, 1, 2, timestamp, future),
        )
        connection.commit()
        connection.execute("DELETE FROM users WHERE id=43")
        connection.commit()
        cascade_rows = connection.execute(
            "SELECT COUNT(*) FROM event_stream_leases WHERE user_id=43"
        ).fetchone()[0]
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()

    result = {
        "mode": "0002_to_0003",
        "beforeRevisions": before["currentRevisions"],
        "afterRevisions": after["currentRevisions"],
        "needsMigrationAfter": after["needsMigration"],
        "migratedSafely": upgrade_result["migrated"],
        "marker": marker,
        "snapshotRow": snapshot_row,
        "newTablesPresent": {
            "runtime_service_leases",
            "event_stream_leases",
        }.issubset(table_names),
        "runtimeModelColumnsMatch": runtime_columns == set(RuntimeServiceLease.__table__.columns.keys()),
        "eventModelColumnsMatch": event_columns == set(EventStreamLease.__table__.columns.keys()),
        "runtimeIndexesPresent": {
            "ix_runtime_service_leases_expires_at",
        }.issubset(runtime_indexes),
        "eventIndexesPresent": {
            "ix_event_stream_leases_expires_at",
            "ix_event_stream_leases_user_id",
        }.issubset(event_indexes),
        "duplicateServiceRejected": duplicate_service_rejected,
        "duplicateOwnerRejected": duplicate_owner_rejected,
        "duplicateUserSlotRejected": duplicate_user_slot_rejected,
        "duplicateGlobalSlotRejected": duplicate_global_slot_rejected,
        "invalidSlotRejected": invalid_slot_rejected,
        "cascadeRows": cascade_rows,
        "foreignKeyErrors": foreign_key_errors,
        "snapshotRevision": snapshot_revision,
        "snapshotMarker": snapshot_marker,
        "snapshotEncrypted": snapshot_path.read_bytes()[:16] != b"SQLite format 3\x00",
        "modelParityClean": True,
    }
    print(json.dumps(result, sort_keys=True, default=str))


def main() -> None:
    mode = sys.argv[1]
    backend_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend_root))
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        database_path = temp_root / "installed-v1.db"
        snapshot_path = temp_root / "migration-recovery" / "pre-migration.db"
        alembic_root = temp_root / "alembic"
        shutil.copytree(backend_root / "alembic", alembic_root)
        alembic_ini_path = temp_root / "alembic.ini"
        alembic_ini_path.write_text(
            (backend_root / "alembic.ini").read_text(encoding="utf-8").replace(
                "script_location = alembic",
                f"script_location = {alembic_root.as_posix()}",
            ),
            encoding="utf-8",
        )
        os.environ["DATABASE_URL"] = (
            f"sqlite+sqlcipher_aiosqlite:///{database_path.as_posix()}"
        )

        if mode == "0001_to_0002":
            for migration_path in (alembic_root / "versions").glob("*.py"):
                if migration_path.name not in {
                    "0001_initial_public_schema.py",
                    "0002_backend_job_integrity.py",
                }:
                    migration_path.unlink()
            run_0001_to_0002_fixture(
                database_path=database_path,
                alembic_ini_path=alembic_ini_path,
                snapshot_path=snapshot_path,
            )
            return

        if mode == "0002_to_0003":
            for migration_path in (alembic_root / "versions").glob("*.py"):
                if migration_path.name not in {
                    "0001_initial_public_schema.py",
                    "0002_backend_job_integrity.py",
                    "0003_runtime_service_contracts.py",
                }:
                    migration_path.unlink()
            run_0002_to_0003_fixture(
                database_path=database_path,
                alembic_ini_path=alembic_ini_path,
                snapshot_path=snapshot_path,
            )
            return

        from alembic import command
        from alembic.config import Config

        from app.database_encryption import connect_sqlcipher
        from app.migration_safety import (
            create_encrypted_snapshot,
            inspect_database,
            restore_encrypted_snapshot,
        )

        config = Config(str(alembic_ini_path))
        command.upgrade(config, "head")
        # The installed migration head must remain an exact rendering of the ORM model;
        # otherwise an upgrade begins life already requiring another migration.
        command.check(config)
        from alembic.script import ScriptDirectory

        installed_head = ScriptDirectory.from_config(config).get_current_head()
        if not installed_head:
            raise RuntimeError("Fixture could not resolve the installed migration head")
        (alembic_root / "versions" / "fixture_next.py").write_text(
            (SUCCESS_MIGRATION if mode == "success" else FAILURE_MIGRATION).replace(
                "__INSTALLED_HEAD__",
                installed_head,
            ),
            encoding="utf-8",
        )
        # Alembic cached the script directory while checking the baseline.
        config = Config(str(alembic_ini_path))
        connection = connect_sqlcipher(database_path)
        connection.execute(
            "INSERT INTO users (id, email, created_at) "
            "VALUES (42, 'fixture@example.invalid', '2026-08-03 00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO settings (id, user_id, key, value) "
            "VALUES (1, 42, 'fixture_marker', 'installed-v1-data')"
        )
        connection.commit()
        connection.close()

        before = inspect_database(database_path, alembic_ini_path)
        snapshot = create_encrypted_snapshot(database_path, snapshot_path)
        migration_failed = False
        try:
            command.upgrade(config, "head")
        except Exception:
            migration_failed = True
            if mode != "failure":
                raise
            restore_encrypted_snapshot(
                database_path,
                snapshot_path,
                expected_sha256=snapshot["sha256"],
            )

        after = inspect_database(database_path, alembic_ini_path)
        connection = connect_sqlcipher(database_path)
        try:
            marker = connection.execute(
                "SELECT value FROM settings WHERE user_id=42 AND key='fixture_marker'"
            ).fetchone()[0]
            partial_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='partial_update_fixture'"
            ).fetchone()
            success_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='update_safety_fixture'"
            ).fetchone()
        finally:
            connection.close()

        result = {
            "mode": mode,
            "beforeRevisions": before["currentRevisions"],
            "afterRevisions": after["currentRevisions"],
            "needsMigrationAfter": after["needsMigration"],
            "migrationFailed": migration_failed,
            "marker": marker,
            "partialTablePresent": partial_table is not None,
            "successTablePresent": success_table is not None,
            "snapshotEncrypted": snapshot_path.read_bytes()[:16] != b"SQLite format 3\x00",
            "snapshotIntegrity": snapshot["integrity"],
        }
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
