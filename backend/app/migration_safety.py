from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database_encryption import connect_sqlcipher


MIGRATION_RECOVERY_SCHEMA = "breaktwenty.migration-recovery"
MIGRATION_RECOVERY_VERSION = 1


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Directory fsync is not available through Python on every supported OS.
        pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now_string() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.unlink(missing_ok=True)
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    os.replace(temporary_path, path)
    _fsync_directory(path.parent)


def _remove_database_files(database_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        database_path.with_name(f"{database_path.name}{suffix}").unlink(missing_ok=True)


def _recovery_paths(database_path: Path) -> tuple[Path, Path]:
    directory = database_path.parent / "migration-recovery"
    return directory / "pre-migration.db", directory / "manifest.json"


def _read_recovery_manifest(manifest_path: Path, database_path: Path) -> dict[str, object] | None:
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "The database migration recovery manifest is unreadable; recovery files were preserved"
        ) from exc
    valid_statuses = {"prepared", "recovering", "recovery_failed", "recovered", "succeeded"}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != MIGRATION_RECOVERY_SCHEMA
        or manifest.get("version") != MIGRATION_RECOVERY_VERSION
        or manifest.get("databaseFile") != database_path.name
        or manifest.get("snapshotFile") != "pre-migration.db"
        or manifest.get("status") not in valid_statuses
        or not isinstance(manifest.get("fromRevisions"), list)
        or not isinstance(manifest.get("snapshotSha256"), str)
    ):
        raise RuntimeError(
            "The database migration recovery manifest is invalid; recovery files were preserved"
        )
    return manifest


def _run_alembic_upgrade(alembic_ini_path: Path) -> None:
    command.upgrade(Config(str(alembic_ini_path)), "head")


def _database_url_for_path(database_path: Path) -> str:
    return f"sqlite+sqlcipher_aiosqlite:///{database_path.as_posix()}"


def _with_database_url(database_path: Path):
    class _DatabaseUrlContext:
        def __enter__(self):
            self.previous = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = _database_url_for_path(database_path)
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            if self.previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = self.previous

    return _DatabaseUrlContext()


def _database_state(database_path: Path) -> dict[str, object]:
    connection = connect_sqlcipher(database_path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        current_revisions = []
        if has_version_table:
            current_revisions = sorted(
                str(row[0])
                for row in connection.execute("SELECT version_num FROM alembic_version")
            )
        return {
            "currentRevisions": current_revisions,
            "integrity": integrity,
            "foreignKeyErrors": foreign_key_errors,
        }
    finally:
        connection.close()


def inspect_database(database_path: Path, alembic_ini_path: Path) -> dict[str, object]:
    state = _database_state(database_path)
    config = Config(str(alembic_ini_path))
    script = ScriptDirectory.from_config(config)
    head_revisions = sorted(script.get_heads())
    state["headRevisions"] = head_revisions
    state["needsMigration"] = state["currentRevisions"] != head_revisions
    return state


def create_encrypted_snapshot(database_path: Path, snapshot_path: Path) -> dict[str, object]:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = snapshot_path.with_name(f"{snapshot_path.name}.tmp")
    temporary_path.unlink(missing_ok=True)

    source = connect_sqlcipher(database_path)
    destination = connect_sqlcipher(temporary_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    state = _database_state(temporary_path)
    if state["integrity"] != "ok" or state["foreignKeyErrors"]:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError("The encrypted pre-migration snapshot failed validation")

    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, snapshot_path)
    _fsync_directory(snapshot_path.parent)
    return {
        **state,
        "bytes": snapshot_path.stat().st_size,
        "sha256": _sha256_file(snapshot_path),
    }


def restore_encrypted_snapshot(
    database_path: Path,
    snapshot_path: Path,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    if not snapshot_path.is_file():
        raise RuntimeError("The encrypted migration recovery snapshot is missing")
    if expected_sha256 and _sha256_file(snapshot_path) != expected_sha256:
        raise RuntimeError("The encrypted migration recovery snapshot checksum does not match")
    snapshot_state = _database_state(snapshot_path)
    if snapshot_state["integrity"] != "ok" or snapshot_state["foreignKeyErrors"]:
        raise RuntimeError("The encrypted migration recovery snapshot is invalid")

    temporary_path = database_path.with_name(f"{database_path.name}.restore.tmp")
    temporary_path.unlink(missing_ok=True)
    shutil.copyfile(snapshot_path, temporary_path)
    os.chmod(temporary_path, 0o600)
    with temporary_path.open("rb+") as handle:
        os.fsync(handle.fileno())

    restored_state = _database_state(temporary_path)
    if restored_state != snapshot_state:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError("The copied migration recovery snapshot failed validation")

    for suffix in ("-wal", "-shm"):
        database_path.with_name(f"{database_path.name}{suffix}").unlink(missing_ok=True)
    os.replace(temporary_path, database_path)
    _fsync_directory(database_path.parent)
    return restored_state


def upgrade_database_safely(database_path: Path, alembic_ini_path: Path) -> dict[str, object]:
    database_path = database_path.resolve()
    alembic_ini_path = alembic_ini_path.resolve()
    database_existed = database_path.is_file()
    if not database_existed:
        try:
            with _with_database_url(database_path):
                _run_alembic_upgrade(alembic_ini_path)
            state = inspect_database(database_path, alembic_ini_path)
            if state["integrity"] != "ok" or state["foreignKeyErrors"] or state["needsMigration"]:
                raise RuntimeError("New database migration did not produce a healthy schema at head")
            return {"migrated": True, "recovered": False, **state}
        except BaseException:
            _remove_database_files(database_path)
            raise

    snapshot_path, manifest_path = _recovery_paths(database_path)
    existing_manifest = _read_recovery_manifest(manifest_path, database_path)
    if existing_manifest and existing_manifest["status"] == "recovery_failed":
        raise RuntimeError(
            "A prior database recovery failed; its encrypted snapshot was preserved for manual recovery"
        )
    if existing_manifest and existing_manifest["status"] in {"prepared", "recovering"}:
        try:
            restored = restore_encrypted_snapshot(
                database_path,
                snapshot_path,
                expected_sha256=str(existing_manifest["snapshotSha256"]),
            )
            if sorted(restored["currentRevisions"]) != sorted(existing_manifest["fromRevisions"]):
                raise RuntimeError("Interrupted migration recovery restored an unexpected revision")
        except BaseException as exc:
            _atomic_write_json(
                manifest_path,
                {
                    **existing_manifest,
                    "status": "recovery_failed",
                    "recoveryFailedAt": _utc_now_string(),
                    "recoveryFailure": str(exc),
                },
            )
            raise RuntimeError(
                "An interrupted database migration could not be recovered; its snapshot was preserved"
            ) from exc
        _atomic_write_json(
            manifest_path,
            {
                **existing_manifest,
                "status": "recovered",
                "recoveredAt": _utc_now_string(),
                "failure": existing_manifest.get("failure") or "Migration was interrupted",
            },
        )
        raise RuntimeError(
            "An interrupted database migration was restored; restart BreakTwenty to retry the update"
        )

    before = inspect_database(database_path, alembic_ini_path)
    if before["integrity"] != "ok" or before["foreignKeyErrors"]:
        raise RuntimeError("The pre-migration encrypted database failed validation")
    if not before["needsMigration"]:
        return {"migrated": False, "recovered": False, **before}

    snapshot = create_encrypted_snapshot(database_path, snapshot_path)
    manifest: dict[str, object] = {
        "schema": MIGRATION_RECOVERY_SCHEMA,
        "version": MIGRATION_RECOVERY_VERSION,
        "databaseFile": database_path.name,
        "snapshotFile": snapshot_path.name,
        "fromRevisions": before["currentRevisions"],
        "toRevisions": before["headRevisions"],
        "snapshotBytes": snapshot["bytes"],
        "snapshotSha256": snapshot["sha256"],
        "preparedAt": _utc_now_string(),
        "status": "prepared",
    }
    _atomic_write_json(manifest_path, manifest)
    try:
        with _with_database_url(database_path):
            _run_alembic_upgrade(alembic_ini_path)
        after = inspect_database(database_path, alembic_ini_path)
        if (
            after["integrity"] != "ok"
            or after["foreignKeyErrors"]
            or after["needsMigration"]
            or sorted(after["currentRevisions"]) != sorted(before["headRevisions"])
        ):
            raise RuntimeError("Database migration did not reach a healthy schema head")
    except BaseException as migration_error:
        _atomic_write_json(
            manifest_path,
            {
                **manifest,
                "status": "recovering",
                "failedAt": _utc_now_string(),
                "failure": str(migration_error),
            },
        )
        try:
            restored = restore_encrypted_snapshot(
                database_path,
                snapshot_path,
                expected_sha256=str(snapshot["sha256"]),
            )
            if sorted(restored["currentRevisions"]) != sorted(before["currentRevisions"]):
                raise RuntimeError("Restored database revision differs from the pre-migration revision")
        except BaseException as recovery_error:
            _atomic_write_json(
                manifest_path,
                {
                    **manifest,
                    "status": "recovery_failed",
                    "failedAt": _utc_now_string(),
                    "failure": str(migration_error),
                    "recoveryFailedAt": _utc_now_string(),
                    "recoveryFailure": str(recovery_error),
                },
            )
            raise RuntimeError(
                "Database migration and encrypted snapshot recovery both failed"
            ) from recovery_error
        _atomic_write_json(
            manifest_path,
            {
                **manifest,
                "status": "recovered",
                "failedAt": _utc_now_string(),
                "recoveredAt": _utc_now_string(),
                "failure": str(migration_error),
            },
        )
        raise RuntimeError(
            "Database migration failed and the encrypted pre-migration database was restored"
        ) from migration_error

    _atomic_write_json(
        manifest_path,
        {**manifest, "status": "succeeded", "completedAt": _utc_now_string()},
    )
    return {"migrated": True, "recovered": False, **after}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--database", required=True, type=Path)
    inspect_parser.add_argument("--alembic-ini", required=True, type=Path)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--database", required=True, type=Path)
    snapshot_parser.add_argument("--snapshot", required=True, type=Path)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--database", required=True, type=Path)
    restore_parser.add_argument("--snapshot", required=True, type=Path)
    restore_parser.add_argument("--expected-sha256")

    upgrade_parser = subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("--database", required=True, type=Path)
    upgrade_parser.add_argument("--alembic-ini", required=True, type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "inspect":
        result = inspect_database(args.database, args.alembic_ini)
    elif args.command == "snapshot":
        result = create_encrypted_snapshot(args.database, args.snapshot)
    elif args.command == "restore":
        result = restore_encrypted_snapshot(
            args.database,
            args.snapshot,
            expected_sha256=args.expected_sha256,
        )
    else:
        result = upgrade_database_safely(args.database, args.alembic_ini)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
