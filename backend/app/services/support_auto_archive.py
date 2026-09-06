from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from app.runtime_paths import DESKTOP_AUTH_DIR, DIAGNOSTIC_DIR, LOG_DIR, runtime_display_path
from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.services.log_buffer import buffer_metadata, render_buffer_snapshot
from app.services.time_utils import utc_now as _utc_now

RUNS_DIR = LOG_DIR / "runs"
ATTEMPTS_PER_PROVIDER_LIMIT = 3
ATTEMPT_RETENTION_DAYS = 7
RUN_BUFFER_WINDOW_BEFORE = timedelta(minutes=10)
RUN_BUFFER_WINDOW_AFTER = timedelta(minutes=2)
EXPORTED_RUNS_PER_PROVIDER_LIMIT = 2
EXPORTED_ALL_RECENT_LIMIT = 2
VISIBLE_AUTH_RUNNER_LOG_MAX_BYTES = 128 * 1024
BROWSER_RUNTIME_PREWARM_LOG_MAX_BYTES = 192 * 1024
DIAGNOSTIC_FILE_MAX_BYTES = 8 * 1024 * 1024
DIAGNOSTIC_HAR_FILE_MAX_BYTES = 32 * 1024 * 1024
DIAGNOSTIC_FILE_COUNT_LIMIT = 32
DIAGNOSTIC_AGGREGATE_MAX_BYTES = 64 * 1024 * 1024
SUPPORT_ZIP_FILE_COUNT_LIMIT = 256
SUPPORT_ZIP_AGGREGATE_MAX_BYTES = 64 * 1024 * 1024
SUPPORT_DIRECTORY_SCAN_LIMIT = 4_096
SUPPORT_RUN_SCAN_LIMIT = 1_024
ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT = 4_096
ALL_RECENT_SUPPORT_RUN_SCAN_LIMIT = 4_096
ALL_RECENT_SUPPORT_TREE_SCAN_LIMIT = 16_384
SUPPORT_ARCHIVE_METADATA_MAX_BYTES = 16 * 1024
SANITIZER_FAILURE_TEXT = "[support-log content omitted: sanitizer unavailable]"
RUN_COMPLETION_FILENAME = "completion.json"

logger = logging.getLogger("breaktwenty.support_auto_archive")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_pending_archive_lock = Lock()
_pending_archive_identities: Counter[tuple[int | None, str, str]] = Counter()


def _archive_identity_key(
    *,
    user_id: int | None,
    sync_id: str | None,
    attempt_id: str | None,
) -> tuple[int | None, str, str] | None:
    normalized_sync_id = str(sync_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    if not normalized_sync_id and not normalized_attempt_id:
        return None
    return (
        int(user_id) if user_id is not None else None,
        normalized_sync_id,
        normalized_attempt_id if not normalized_sync_id else "",
    )


def _mark_archive_pending(key: tuple[int | None, str, str] | None) -> None:
    if key is None:
        return
    with _pending_archive_lock:
        _pending_archive_identities[key] += 1


def _clear_archive_pending(key: tuple[int | None, str, str] | None) -> None:
    if key is None:
        return
    with _pending_archive_lock:
        remaining = _pending_archive_identities.get(key, 0) - 1
        if remaining > 0:
            _pending_archive_identities[key] = remaining
        else:
            _pending_archive_identities.pop(key, None)


def _archive_identity_is_pending(
    *,
    user_id: int | None,
    sync_id: str | None,
    attempt_id: str | None,
) -> bool:
    key = _archive_identity_key(user_id=user_id, sync_id=sync_id, attempt_id=attempt_id)
    if key is None:
        return False
    with _pending_archive_lock:
        return _pending_archive_identities.get(key, 0) > 0


@dataclass(frozen=True)
class SupportArchive:
    archive_id: str
    filename: str
    path: Path


@dataclass(frozen=True)
class AttemptArchiveFiles:
    entries: tuple[tuple[Path, str], ...]
    deduplicated_files: tuple[dict[str, Any], ...]
    original_file_count: int
    original_size_bytes: int
    examined_tree_entries: int


class SupportArchiveLimitError(RuntimeError):
    """The requested evidence window cannot be exported completely and safely."""

    def __init__(
        self,
        *,
        reason: str,
        message: str,
        required_file_count: int | None = None,
        required_bytes: int | None = None,
        limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.required_file_count = required_file_count
        self.required_bytes = required_bytes
        self.limit = limit

    def response_payload(self) -> dict[str, Any]:
        return {
            "status": "too_large",
            "reason": self.reason,
            "message": self.message,
            "required_file_count": self.required_file_count,
            "required_bytes": self.required_bytes,
            "limit": self.limit,
        }


def _normalize_archive_id(value: object) -> str | None:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None
    normalized = str(parsed)
    return normalized if raw == normalized else None


def _archive_paths(archive_id: str) -> tuple[Path, Path]:
    archives_dir = LOG_DIR / "exports" / "archives"
    return (
        archives_dir / f"{archive_id}.zip",
        archives_dir / f"{archive_id}.json",
    )


def _remove_registered_archive(archive_id: str) -> None:
    zip_path, metadata_path = _archive_paths(archive_id)
    metadata = _read_archive_metadata(metadata_path)
    if metadata:
        _remove_materialized_archive(metadata)
    for path in (zip_path, metadata_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("support archive cleanup failed archive_id=%s", archive_id)


def _remove_materialized_archive(metadata: dict[str, Any]) -> bool:
    relative_value = str(metadata.get("materialized_relative_path") or "").strip()
    if not relative_value:
        return False
    relative_path = Path(relative_value)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        return False

    filename = Path(str(metadata.get("filename") or "")).name
    kind = str(metadata.get("kind") or "").strip()
    if kind == "provider_run":
        provider = _normalize_provider(metadata.get("provider"))
        if not provider:
            return False
        expected = Path(_safe_provider_dir_name(provider)) / filename
    elif kind == "all_recent":
        expected = Path(filename)
    else:
        return False
    if relative_path != expected or not filename.lower().endswith(".zip"):
        return False

    target = LOG_DIR / "exports" / relative_path
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("materialized support archive cleanup failed path=%s", relative_path)
        return False
    try:
        if target.parent != LOG_DIR / "exports" and not any(target.parent.iterdir()):
            target.parent.rmdir()
    except OSError:
        pass
    return True


def _set_private_archive_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    path.chmod(0o600)


def _read_archive_metadata(metadata_path: Path) -> dict[str, Any] | None:
    try:
        stat = metadata_path.lstat()
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        if stat.st_size <= 0 or stat.st_size > SUPPORT_ARCHIVE_METADATA_MAX_BYTES:
            return None
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _register_support_archive(
    *,
    user_id: int,
    filename: str,
    kind: str,
    provider: str | None = None,
) -> SupportArchive:
    archive_id = str(uuid.uuid4())
    safe_filename = Path(str(filename or "support-bundle.zip")).name
    if not safe_filename.lower().endswith(".zip"):
        safe_filename = f"{safe_filename}.zip"
    archives_dir = LOG_DIR / "exports" / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        archives_dir.chmod(0o700)
    except OSError:
        pass
    zip_path, metadata_path = _archive_paths(archive_id)
    metadata = {
        "schema_version": 1,
        "archive_id": archive_id,
        "user_id": int(user_id),
        "filename": safe_filename,
        "kind": str(kind),
        "provider": str(provider or "") or None,
        "created_at": _utc_now().isoformat(),
    }
    metadata_payload = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    try:
        zip_fd = os.open(zip_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(zip_fd)
        metadata_fd = os.open(
            metadata_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as handle:
            handle.write(metadata_payload)
    except Exception:
        for path in (zip_path, metadata_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return SupportArchive(archive_id=archive_id, filename=safe_filename, path=zip_path)


def resolve_support_archive(archive_id: object, *, user_id: int) -> SupportArchive | None:
    normalized_id = _normalize_archive_id(archive_id)
    if not normalized_id:
        return None
    zip_path, metadata_path = _archive_paths(normalized_id)
    metadata = _read_archive_metadata(metadata_path)
    if not metadata:
        return None
    if metadata.get("archive_id") != normalized_id or metadata.get("user_id") != int(user_id):
        return None
    filename = Path(str(metadata.get("filename") or "")).name
    if not filename or not filename.lower().endswith(".zip"):
        return None
    try:
        archive_root = (LOG_DIR / "exports" / "archives").resolve()
        if zip_path.resolve().parent != archive_root:
            return None
        if zip_path.is_symlink() or not zip_path.is_file():
            return None
    except OSError:
        return None
    return SupportArchive(archive_id=normalized_id, filename=filename, path=zip_path)


def _iter_registered_archive_metadata() -> Iterator[tuple[str, dict[str, Any]]]:
    archives_dir = LOG_DIR / "exports" / "archives"
    if not archives_dir.exists() or not archives_dir.is_dir():
        return
    for index, metadata_path in enumerate(archives_dir.glob("*.json")):
        if index >= SUPPORT_DIRECTORY_SCAN_LIMIT:
            break
        archive_id = _normalize_archive_id(metadata_path.stem)
        if not archive_id:
            continue
        metadata = _read_archive_metadata(metadata_path)
        if metadata and metadata.get("archive_id") == archive_id:
            yield archive_id, metadata


def _prune_registered_archives(
    *,
    user_id: int,
    kind: str,
    keep: int,
    provider: str | None = None,
) -> None:
    matches: list[tuple[str, str]] = []
    for archive_id, metadata in _iter_registered_archive_metadata():
        if metadata.get("user_id") != int(user_id) or metadata.get("kind") != kind:
            continue
        if provider is not None and metadata.get("provider") != provider:
            continue
        matches.append((str(metadata.get("created_at") or ""), archive_id))
    matches.sort(reverse=True)
    for _created_at, archive_id in matches[max(0, int(keep)):]:
        _remove_registered_archive(archive_id)


def _sanitize_log_blob(text: str) -> str:
    try:
        from app.services.support_diagnostics import _sanitize_support_log_text
        from app.services.support_logging import DEFAULT_SUPPORT_CAPTURE_LEVEL
    except Exception:
        logger.error("support-log sanitizer unavailable; omitting archive content")
        return SANITIZER_FAILURE_TEXT
    return "\n".join(
        _sanitize_support_log_text(line, DEFAULT_SUPPORT_CAPTURE_LEVEL)
        for line in str(text or "").splitlines()
    )


def _sanitize_error_text(text: str | None) -> str | None:
    if text is None:
        return None
    return _sanitize_log_blob(str(text))


def _render_sanitized_buffer_snapshot(
    provider: str,
    *,
    user_id: int | None,
    sync_id: str | None,
    attempt_id: str | None,
    since: datetime,
    until: datetime,
) -> str:
    buffer_text = render_buffer_snapshot(
        provider,
        since=since,
        until=until,
        user_id=user_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
    )
    return _sanitize_log_blob(buffer_text) if buffer_text else ""


def _read_text_tail(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max(0, int(max_bytes))))
        return handle.read(max(0, int(max_bytes))).decode("utf-8", errors="replace")


def _diagnostic_file_size_limit(path: Path) -> int:
    if path.suffix.lower() == ".har":
        return DIAGNOSTIC_HAR_FILE_MAX_BYTES
    return DIAGNOSTIC_FILE_MAX_BYTES


def _windows_extended_path_text(path_text: str) -> str:
    if path_text.startswith("\\\\?\\"):
        return path_text
    if path_text.startswith("\\\\"):
        return f"\\\\?\\UNC\\{path_text[2:]}"
    return f"\\\\?\\{path_text}"


def _filesystem_access_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    return Path(_windows_extended_path_text(os.path.abspath(os.fspath(path))))


def _collect_attempt_archive_files(
    runs: Iterable[tuple[Path, str]],
    *,
    attempt_key: str,
    tree_scan_limit: int,
) -> AttemptArchiveFiles:
    entries: list[tuple[Path, str]] = []
    deduplicated_files: list[dict[str, Any]] = []
    canonical_diagnostics: dict[tuple[str, int, str], str] = {}
    original_file_count = 0
    original_size_bytes = 0
    examined_tree_entries = 0

    for run_dir, archive_root in runs:
        try:
            accessible_run_dir = _filesystem_access_path(run_dir)
            if accessible_run_dir.is_symlink() or not accessible_run_dir.is_dir():
                raise OSError("run directory is unavailable")
            run_root = accessible_run_dir.resolve(strict=True)
            for child in accessible_run_dir.rglob("*"):
                examined_tree_entries += 1
                if examined_tree_entries > tree_scan_limit:
                    raise SupportArchiveLimitError(
                        reason="tree_scan_limit",
                        message=(
                            f"This complete sync attempt contains more than {tree_scan_limit:,} "
                            "diagnostic filesystem entries. No partial ZIP was created; report "
                            "this limit reason so the archive safety bound can be reviewed."
                        ),
                        limit=tree_scan_limit,
                    )
                if child.is_dir():
                    continue
                if child.is_symlink() or not child.is_file():
                    raise SupportArchiveLimitError(
                        reason="unsafe_evidence_path",
                        message=(
                            "This sync attempt contains an unsafe or unsupported evidence path. "
                            "No partial ZIP was created; retry after regenerating its diagnostics."
                        ),
                    )
                resolved_child = child.resolve(strict=True)
                if resolved_child.parent != run_root and run_root not in resolved_child.parents:
                    raise SupportArchiveLimitError(
                        reason="unsafe_evidence_path",
                        message=(
                            "This sync attempt contains evidence outside its attempt directory. "
                            "No partial ZIP was created."
                        ),
                    )
                relative_child = child.relative_to(accessible_run_dir).as_posix()
                if relative_child == RUN_COMPLETION_FILENAME:
                    continue
                size = int(child.lstat().st_size)
                file_size_limit = _diagnostic_file_size_limit(child)
                if size < 0 or size > file_size_limit:
                    raise SupportArchiveLimitError(
                        reason="per_file_size_limit",
                        message=(
                            "A required diagnostic file in this sync attempt is larger than the "
                            f"{file_size_limit / (1024 * 1024):.0f} MiB per-file safety limit. "
                            "No partial ZIP was created; report this limit reason so the evidence "
                            "capture bound can be reviewed."
                        ),
                        required_bytes=size,
                        limit=file_size_limit,
                    )

                archive_name = "/".join(
                    part for part in (archive_root.strip("/"), relative_child) if part
                )
                original_file_count += 1
                original_size_bytes += size
                if relative_child.startswith("diagnostics/"):
                    digest = hashlib.sha256()
                    with child.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    digest_hex = digest.hexdigest()
                    # Phase archives can carry the same finalized provider
                    # diagnostic under different run-relative filenames. Treat
                    # identical diagnostic bytes of the same media type as one
                    # attempt-level artifact.
                    dedup_key = (child.suffix.lower(), size, digest_hex)
                    canonical_name = canonical_diagnostics.get(dedup_key)
                    if canonical_name is not None:
                        deduplicated_files.append(
                            {
                                "attempt_key": attempt_key,
                                "canonical_path": canonical_name,
                                "duplicate_path": archive_name,
                                "size_bytes": size,
                                "sha256": digest_hex,
                            }
                        )
                        continue
                    canonical_diagnostics[dedup_key] = archive_name
                entries.append((child, archive_name))
        except SupportArchiveLimitError:
            raise
        except OSError as exc:
            raise SupportArchiveLimitError(
                reason="evidence_unavailable",
                message=(
                    "The diagnostic evidence changed while this complete sync attempt was being "
                    "checked. No partial ZIP was created; retry the export."
                ),
            ) from exc

    return AttemptArchiveFiles(
        entries=tuple(entries),
        deduplicated_files=tuple(deduplicated_files),
        original_file_count=original_file_count,
        original_size_bytes=original_size_bytes,
        examined_tree_entries=examined_tree_entries,
    )


def _diagnostic_filename_owned_by(filename: str, user_id: int) -> bool:
    return bool(
        re.search(
            rf"(?:^|[_-])user-?{int(user_id)}(?=$|[_\-.])",
            str(filename or ""),
            flags=re.IGNORECASE,
        )
    )


def _bounded_children(directory: Path, *, limit: int = SUPPORT_DIRECTORY_SCAN_LIMIT) -> Iterator[Path]:
    for index, child in enumerate(directory.iterdir()):
        if index >= limit:
            break
        yield child


def _bounded_tree(directory: Path, *, limit: int = SUPPORT_DIRECTORY_SCAN_LIMIT) -> Iterator[Path]:
    for index, child in enumerate(directory.rglob("*")):
        if index >= limit:
            break
        yield child


def _provider_context_payload(provider: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        from app.provider_catalog import (
            get_provider_metadata,
            get_runtime_state_metadata,
            get_transaction_import_metadata,
        )
    except Exception:
        return payload
    try:
        metadata = get_provider_metadata(provider)
    except Exception:
        return payload
    backend = metadata.get("backend") or {}
    connector = backend.get("connector") or {}
    route = backend.get("route") or {}
    payload["provider"] = provider
    payload["display_name"] = metadata.get("displayName")
    payload["institution_type"] = metadata.get("institutionType")
    payload["connector"] = {
        "module": connector.get("modulePath"),
        "class": connector.get("className"),
        "provider_type": connector.get("providerType"),
    }
    payload["route"] = {
        "auth_handler": route.get("authHandler"),
        "sync_path": route.get("syncPath"),
        "lock_provider": route.get("lockProvider"),
    }
    try:
        payload["transaction_import"] = get_transaction_import_metadata(provider)
    except Exception:
        payload["transaction_import"] = {}
    try:
        payload["runtime_state"] = get_runtime_state_metadata(provider)
    except Exception:
        payload["runtime_state"] = {}
    return payload


async def _user_timezone_info(user_id: int | None) -> ZoneInfo | None:
    if user_id is None:
        return None
    try:
        from app.database import async_session
        from app.services.user_utils import get_user_timezone_info_from_db
    except Exception:
        return None
    try:
        async with async_session() as db:
            return await get_user_timezone_info_from_db(db, int(user_id))
    except Exception:
        return None


def _app_version() -> str:
    try:
        from app.main import app as fastapi_app
        return str(getattr(fastapi_app, "version", "") or "")
    except Exception:
        return ""


def _scrub_state_last_errors(snapshots: dict[str, dict[str, Any]]) -> None:
    for snapshot_name, payload in list(snapshots.items()):
        if not isinstance(payload, dict):
            continue
        for collection_name in (
            "jobs",
            "windows",
            "account_states",
            "transaction_import_account_states",
        ):
            collection = payload.get(collection_name)
            if not isinstance(collection, list):
                continue
            for row in collection:
                if isinstance(row, dict) and "last_error" in row:
                    row["last_error"] = _sanitize_error_text(row.get("last_error"))


def _isoformat_optional(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def _collect_current_state_at_export(user_id: int | None) -> dict[str, Any]:
    generated_at = _utc_now()
    payload: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": generated_at.isoformat(),
        "user_id": user_id,
        "scope_kind": "all_providers_current_app_state",
        "historical_attempt_evidence": False,
        "provider_history_scope": "explicit_all_providers_recent_export_context",
        "purpose": (
            "Explicit provider-wide current app-state context for the all-providers recent export. "
            "Per-run attempt-scoped state/*.json files remain separate historical evidence."
        ),
        "providers": {},
        "active_transaction_import_jobs": [],
    }
    if user_id is None:
        return payload

    try:
        from sqlalchemy import func, select

        from app.database import async_session
        from app.models import (
            Account,
            Institution,
            Transaction,
            TransactionImportJob,
            TransactionImportWindow,
        )
    except Exception as exc:
        payload["collection_error"] = _sanitize_error_text(f"{type(exc).__name__}: {exc}")
        return payload

    try:
        async with async_session() as db:
            institution_rows = (
                await db.execute(
                    select(
                        Institution.provider,
                        Institution.id,
                        Institution.sync_status,
                        Institution.enabled,
                    )
                    .where(Institution.user_id == int(user_id))
                    .order_by(Institution.provider.asc(), Institution.id.asc())
                )
            ).all()
            account_counts = dict(
                (
                    await db.execute(
                        select(Institution.provider, func.count(Account.id))
                        .select_from(Institution)
                        .join(Account, Account.institution_id == Institution.id)
                        .where(
                            Institution.user_id == int(user_id),
                            Account.user_id == int(user_id),
                        )
                        .group_by(Institution.provider)
                    )
                ).all()
            )
            transaction_counts = dict(
                (
                    await db.execute(
                        select(Institution.provider, func.count(Transaction.id))
                        .select_from(Institution)
                        .join(Account, Account.institution_id == Institution.id)
                        .join(Transaction, Transaction.account_id == Account.id)
                        .where(
                            Institution.user_id == int(user_id),
                            Account.user_id == int(user_id),
                            Transaction.user_id == int(user_id),
                        )
                        .group_by(Institution.provider)
                    )
                ).all()
            )
            job_rows = (
                await db.execute(
                    select(TransactionImportJob)
                    .where(TransactionImportJob.user_id == int(user_id))
                    .order_by(TransactionImportJob.updated_at.desc())
                    .limit(100)
                )
            ).scalars().all()
            window_rows = (
                await db.execute(
                    select(
                        TransactionImportWindow.provider,
                        TransactionImportWindow.status,
                        func.count(TransactionImportWindow.id),
                        func.coalesce(func.sum(TransactionImportWindow.transaction_count), 0),
                    )
                    .where(TransactionImportWindow.user_id == int(user_id))
                    .group_by(TransactionImportWindow.provider, TransactionImportWindow.status)
                )
            ).all()
    except Exception as exc:
        payload["collection_error"] = _sanitize_error_text(f"{type(exc).__name__}: {exc}")
        return payload

    providers: dict[str, dict[str, Any]] = {}
    for provider, institution_id, sync_status, enabled in institution_rows:
        normalized_provider = _current_state_provider_key(provider)
        if not normalized_provider:
            continue
        summary = providers.setdefault(
            normalized_provider,
            {
                "institution_count": 0,
                "institution_ids": [],
                "enabled_count": 0,
                "sync_status_counts": {},
                "account_count": 0,
                "transaction_count": 0,
                "transaction_import_job_status_counts": {},
                "transaction_import_window_status_counts": {},
                "transaction_import_window_transaction_count": 0,
                "latest_transaction_import_job": None,
            },
        )
        summary["institution_count"] += 1
        summary["institution_ids"].append(int(institution_id))
        if enabled:
            summary["enabled_count"] += 1
        status_counts = Counter(summary["sync_status_counts"])
        status_counts[str(sync_status or "unknown")] += 1
        summary["sync_status_counts"] = dict(sorted(status_counts.items()))

    for provider, count in account_counts.items():
        normalized_provider = _current_state_provider_key(provider)
        if normalized_provider in providers:
            providers[normalized_provider]["account_count"] += int(count or 0)
    for provider, count in transaction_counts.items():
        normalized_provider = _current_state_provider_key(provider)
        if normalized_provider in providers:
            providers[normalized_provider]["transaction_count"] += int(count or 0)
    for provider, status, count, transaction_count in window_rows:
        normalized_provider = _current_state_provider_key(provider)
        if normalized_provider not in providers:
            continue
        summary = providers[normalized_provider]
        window_counts = Counter(summary["transaction_import_window_status_counts"])
        window_counts[str(status or "unknown")] += int(count or 0)
        summary["transaction_import_window_status_counts"] = dict(sorted(window_counts.items()))
        summary["transaction_import_window_transaction_count"] += int(transaction_count or 0)

    for job in job_rows:
        normalized_provider = _current_state_provider_key(job.provider)
        if normalized_provider not in providers:
            providers[normalized_provider] = {
                "institution_count": 0,
                "institution_ids": [],
                "enabled_count": 0,
                "sync_status_counts": {},
                "account_count": 0,
                "transaction_count": 0,
                "transaction_import_job_status_counts": {},
                "transaction_import_window_status_counts": {},
                "transaction_import_window_transaction_count": 0,
                "latest_transaction_import_job": None,
            }
        summary = providers[normalized_provider]
        job_counts = Counter(summary["transaction_import_job_status_counts"])
        job_counts[str(job.status or "unknown")] += 1
        summary["transaction_import_job_status_counts"] = dict(sorted(job_counts.items()))
        job_payload = {
            "job_id": job.job_id,
            "status": job.status,
            "reason": job.reason,
            "sync_id": job.sync_id,
            "source_sync_id": job.source_sync_id,
            "attempt_id": job.attempt_id,
            "last_error": _sanitize_error_text(job.last_error),
            "lease_token_set": bool(job.lease_token),
            "created_at": _isoformat_optional(job.created_at),
            "started_at": _isoformat_optional(job.started_at),
            "finished_at": _isoformat_optional(job.finished_at),
            "updated_at": _isoformat_optional(job.updated_at),
        }
        if summary["latest_transaction_import_job"] is None:
            summary["latest_transaction_import_job"] = job_payload
        if str(job.status or "") in {"queued", "running"}:
            payload["active_transaction_import_jobs"].append(
                {
                    "provider": normalized_provider,
                    "institution_id": int(job.institution_id) if job.institution_id is not None else None,
                    **job_payload,
                }
            )

    payload["providers"] = dict(sorted(providers.items()))
    payload["active_transaction_import_job_count"] = len(payload["active_transaction_import_jobs"])
    return payload


def _current_state_provider_key(provider: Any) -> str:
    """Use the user-facing institution key for split internal provider routes."""
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return ""
    try:
        from app.provider_catalog import get_status_provider_for_result

        return _normalize_provider(get_status_provider_for_result(normalized_provider))
    except Exception:
        return normalized_provider


def _safe_name_component(value: Any, *, fallback: str = "item") -> str:
    text = _SAFE_NAME_RE.sub("-", str(value or "").strip()).strip("-._")
    return text[:80] or fallback


def _format_local_filename_timestamp(now: datetime, tz: ZoneInfo | None) -> str:
    """Format `now` as a filesystem-safe local timestamp tagged with tz abbr.

    Output: ``YYYY-MM-DD_HH-MM-SS_ABBR`` (e.g. ``2026-05-23_13-18-25_EDT``).
    Falls back to UTC when no user timezone is available.
    """
    aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if tz is None:
        local_now = aware.astimezone(timezone.utc)
        safe_abbr = "UTC"
    else:
        local_now = aware.astimezone(tz)
        raw_abbr = local_now.strftime("%Z") or ""
        safe_abbr = re.sub(r"[^A-Za-z0-9]+", "", raw_abbr)[:8] or "LCL"
    return f"{local_now.strftime('%Y-%m-%d_%H-%M-%S')}_{safe_abbr}"


async def archive_run(
    *,
    user_id: int | None,
    provider: str,
    sync_id: str | None,
    trigger: str,
    error: str | None = None,
    extra_fields: dict[str, Any] | None = None,
    traceback_text: str | None = None,
    attempt_id: str | None = None,
) -> Path | None:
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return None

    try:
        from app.services.support_diagnostics import _collect_state_snapshots
    except Exception as exc:
        logger.warning("auto-archive state snapshot import failed: %s", exc)
        return None

    now = _utc_now()
    try:
        user_tz = await _user_timezone_info(user_id)
    except Exception:
        user_tz = None
    capture_level = "redacted_rich"
    if user_id is not None:
        try:
            from app.services.support_logging import get_support_capture_level

            capture_level = get_support_capture_level(
                normalized_provider,
                user_id=int(user_id),
            )
        except Exception as exc:
            logger.warning(
                "auto-archive capture level resolve failed provider=%s: %s",
                normalized_provider,
                type(exc).__name__,
            )
    safe_provider = _safe_name_component(normalized_provider, fallback="provider")
    sync_id_text = _safe_name_component(sync_id or "no-sync-id", fallback="no-sync-id")
    folder_name = f"{_format_local_filename_timestamp(now, user_tz)}_{sync_id_text}_{_safe_name_component(trigger, fallback='trigger')}"
    sync_scope_value = (extra_fields or {}).get("sync_scope") if isinstance(extra_fields, dict) else None
    if isinstance(sync_scope_value, str) and sync_scope_value.strip():
        folder_name = f"{folder_name}_{_safe_name_component(sync_scope_value, fallback='scope')}"
    provider_root = RUNS_DIR / safe_provider
    final_run_dir = provider_root / folder_name
    run_dir = LOG_DIR / ".runs-staging" / safe_provider / str(uuid.uuid4())

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("auto-archive mkdir failed provider=%s: %s", normalized_provider, exc)
        return None

    since = now - RUN_BUFFER_WINDOW_BEFORE
    until = now + RUN_BUFFER_WINDOW_AFTER

    trigger_payload: dict[str, Any] = {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "provider": normalized_provider,
        "user_id": user_id,
        "sync_id": sync_id,
        "attempt_id": attempt_id,
        "capture_level": capture_level,
        "export_scope": {
            "kind": "sync_attempt",
            "attempt_sync_id": str(sync_id or "") or None,
            "attempt_id": str(attempt_id or "") or None,
            "same_sync_id_sibling_phases_included": True,
            "provider_history_included": False,
        },
        "trigger": trigger,
        "initiation_source": (
            str((extra_fields or {}).get("sync_source") or "").strip().lower() or None
            if isinstance(extra_fields, dict)
            else None
        ),
        "error": await asyncio.to_thread(_sanitize_error_text, error),
        "buffer_metadata": buffer_metadata(
            normalized_provider,
            user_id=user_id,
            sync_id=sync_id,
            attempt_id=attempt_id,
        ),
        "buffer_window_start": since.isoformat(),
        "buffer_window_end": until.isoformat(),
    }
    if extra_fields:
        trigger_payload["extra"] = extra_fields

    try:
        await asyncio.to_thread(
            (run_dir / "trigger.json").write_text,
            json.dumps(trigger_payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("auto-archive trigger.json write failed provider=%s: %s", normalized_provider, exc)

    timezone_label = str(user_tz) if user_tz is not None else None
    context_payload = {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "app_version": _app_version(),
        "user_id": user_id,
        "user_timezone": timezone_label,
        "sync_id": str(sync_id or "") or None,
        "attempt_id": str(attempt_id or "") or None,
        "capture_level": capture_level,
        "provider": _provider_context_payload(normalized_provider),
    }
    try:
        await asyncio.to_thread(
            (run_dir / "context.json").write_text,
            json.dumps(context_payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("auto-archive context.json write failed provider=%s: %s", normalized_provider, exc)

    try:
        sanitized_buffer = await asyncio.to_thread(
            _render_sanitized_buffer_snapshot,
            normalized_provider,
            user_id=user_id,
            sync_id=sync_id,
            attempt_id=attempt_id,
            since=since,
            until=until,
        )
    except Exception as exc:
        logger.warning("auto-archive buffer render failed provider=%s: %s", normalized_provider, exc)
        sanitized_buffer = ""
    if sanitized_buffer:
        try:
            await asyncio.to_thread(
                (run_dir / "buffer.log").write_text,
                sanitized_buffer + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("auto-archive buffer.log write failed provider=%s: %s", normalized_provider, exc)

    if traceback_text:
        try:
            sanitized_traceback = await asyncio.to_thread(_sanitize_log_blob, traceback_text)
            await asyncio.to_thread(
                (run_dir / "traceback.log").write_text,
                sanitized_traceback + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("auto-archive traceback.log write failed provider=%s: %s", normalized_provider, exc)

    if user_id is not None:
        try:
            snapshots = await _collect_state_snapshots(
                int(user_id),
                normalized_provider,
                sync_id=sync_id,
                attempt_id=attempt_id,
                job_id=(extra_fields or {}).get("job_id") if isinstance(extra_fields, dict) else None,
                institution_id=(
                    (extra_fields or {}).get("institution_id")
                    if isinstance(extra_fields, dict)
                    else None
                ),
                result_status=(
                    (extra_fields or {}).get("result_status")
                    if isinstance(extra_fields, dict)
                    else None
                ),
                initiation_source=(
                    (extra_fields or {}).get("sync_source")
                    if isinstance(extra_fields, dict)
                    else None
                ),
                since=since,
                until=until,
                capture_level=capture_level,
            )
        except Exception as exc:
            logger.warning("auto-archive state snapshot collect failed provider=%s: %s", normalized_provider, exc)
            snapshots = {"collection_errors.json": {"error": _sanitize_error_text(str(exc))}}
        else:
            _scrub_state_last_errors(snapshots)
        state_dir = run_dir / "state"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("auto-archive state dir failed provider=%s: %s", normalized_provider, exc)
            snapshots = {}
        for snapshot_name, snapshot_payload in snapshots.items():
            try:
                await asyncio.to_thread(
                    (state_dir / snapshot_name).write_text,
                    json.dumps(snapshot_payload, indent=2, default=str) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning(
                    "auto-archive state %s write failed provider=%s: %s",
                    snapshot_name,
                    normalized_provider,
                    exc,
                )

    await asyncio.to_thread(
        _copy_provider_diagnostics_into_run,
        run_dir=run_dir,
        provider=normalized_provider,
        user_id=user_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
        since=since,
        until=until,
    )
    await asyncio.to_thread(
        _copy_visible_auth_runner_log_into_run,
        run_dir=run_dir,
        provider=normalized_provider,
        attempt_id=attempt_id,
    )
    if user_id is None:
        await asyncio.to_thread(_copy_browser_runtime_prewarm_log_into_run, run_dir=run_dir)

    completion_payload = {
        "schema_version": 1,
        "status": "complete",
        "completed_at": _utc_now().isoformat(),
        "provider": normalized_provider,
        "user_id": user_id,
        "sync_id": sync_id,
        "attempt_id": attempt_id,
        "evidence_collection_finished": True,
    }
    try:
        await asyncio.to_thread(
            (run_dir / RUN_COMPLETION_FILENAME).write_text,
            json.dumps(completion_payload, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("auto-archive completion marker failed provider=%s: %s", normalized_provider, exc)
        await asyncio.to_thread(shutil.rmtree, run_dir, True)
        return None

    try:
        await asyncio.to_thread(provider_root.mkdir, parents=True, exist_ok=True)
        if await asyncio.to_thread(final_run_dir.exists):
            final_run_dir = provider_root / f"{folder_name}_{uuid.uuid4().hex[:8]}"
        await asyncio.to_thread(os.replace, run_dir, final_run_dir)
    except OSError as exc:
        logger.warning("auto-archive publish failed provider=%s: %s", normalized_provider, exc)
        await asyncio.to_thread(shutil.rmtree, run_dir, True)
        return None

    await asyncio.to_thread(_prune_runs, provider_root, user_id=user_id)
    logger.info(
        "auto-archive run written provider=%s sync_id=%s trigger=%s path=%s",
        normalized_provider,
        sync_id,
        trigger,
        runtime_display_path(final_run_dir),
    )
    return final_run_dir


def archive_run_fire_and_forget(
    *,
    user_id: int | None,
    provider: str,
    sync_id: str | None,
    trigger: str,
    error: str | None = None,
    extra_fields: dict[str, Any] | None = None,
    traceback_text: str | None = None,
    attempt_id: str | None = None,
    diagnostics_settle_seconds: float = 0,
) -> None:
    pending_key = _archive_identity_key(
        user_id=user_id,
        sync_id=sync_id,
        attempt_id=attempt_id,
    )
    _mark_archive_pending(pending_key)

    async def _archive() -> None:
        try:
            await asyncio.sleep(max(0.05, diagnostics_settle_seconds))
            await archive_run(
                user_id=user_id,
                provider=provider,
                sync_id=sync_id,
                trigger=trigger,
                error=error,
                extra_fields=extra_fields,
                traceback_text=traceback_text,
                attempt_id=attempt_id,
            )
        finally:
            _clear_archive_pending(pending_key)

    try:
        from app.services.task_supervisor import create_tracked_task

        create_tracked_task(
            _archive(),
            name=f"support-archive:{_safe_name_component(provider)}:{sync_id or 'none'}",
        )
    except (RuntimeError, ImportError):
        _clear_archive_pending(pending_key)
        return
    except Exception as exc:
        _clear_archive_pending(pending_key)
        logger.warning(
            "auto-archive task admission failed provider=%s sync_id=%s: %s",
            provider,
            sync_id,
            type(exc).__name__,
        )


def _diagnostic_filename_has_identity(filename: str, *, label: str, value: str) -> bool:
    component = _SAFE_NAME_RE.sub("-", str(value or "").strip()).strip("-._")[:96]
    if not component:
        return False
    return f"_{label}-{component}_" in f"_{Path(filename).name}_"


def _jsonl_attempt_identity(path: Path) -> tuple[str, str] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for _index in range(8):
                line = handle.readline(64 * 1024)
                if not line:
                    break
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    return None
                return (
                    str(payload.get("sync_id") or "").strip(),
                    str(payload.get("attempt_id") or "").strip(),
                )
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def _diagnostic_file_matches_attempt(
    path: Path,
    *,
    sync_id: str | None,
    attempt_id: str | None,
) -> bool:
    normalized_sync_id = str(sync_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    if not normalized_sync_id and not normalized_attempt_id:
        return False
    if path.suffix.lower() == ".jsonl":
        identity = _jsonl_attempt_identity(path)
        if identity is None:
            return False
        file_sync_id, file_attempt_id = identity
        if normalized_sync_id:
            return file_sync_id == normalized_sync_id
        return file_attempt_id == normalized_attempt_id
    if path.suffix.lower() == ".har":
        if normalized_sync_id:
            return _diagnostic_filename_has_identity(
                path.name,
                label="sync",
                value=normalized_sync_id,
            )
        return _diagnostic_filename_has_identity(
            path.name,
            label="attempt",
            value=normalized_attempt_id,
        )
    return False


def _copy_provider_diagnostics_into_run(
    *,
    run_dir: Path,
    provider: str,
    user_id: int | None,
    sync_id: str | None,
    attempt_id: str | None,
    since: datetime,
    until: datetime,
) -> None:
    try:
        from app.services.replay_diagnostics import PROVIDER_DIAGNOSTIC_DIRS
    except Exception:
        provider_diag_dirs: dict[str, str] = {}
    else:
        provider_diag_dirs = PROVIDER_DIAGNOSTIC_DIRS

    provider_dir_name = provider_diag_dirs.get(provider) or provider.upper()
    source_dir = DIAGNOSTIC_DIR / provider_dir_name
    if not source_dir.exists():
        return

    diagnostics_dir = run_dir / "diagnostics"
    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("auto-archive diagnostics dir failed provider=%s: %s", provider, exc)
        return

    allowlist = {".har", ".jsonl"}
    since_ts = since.timestamp()
    until_ts = until.timestamp()
    copied = 0
    copied_bytes = 0
    for path in _bounded_tree(source_dir):
        if not path.is_file() or path.suffix.lower() not in allowlist:
            continue
        if path.suffix.lower() == ".jsonl" and path.name.endswith("_in_progress.jsonl"):
            # Active JSONL files are still mutable. Linking one into a finalized
            # run would let later writes rewrite historical evidence in place.
            continue
        if user_id is not None and not _diagnostic_filename_owned_by(path.name, int(user_id)):
            continue
        if not _diagnostic_file_matches_attempt(
            path,
            sync_id=sync_id,
            attempt_id=attempt_id,
        ):
            continue
        try:
            path_stat = path.stat()
            modified_at = path_stat.st_mtime
            file_size = int(path_stat.st_size)
        except OSError:
            continue
        if modified_at < since_ts or modified_at > until_ts:
            continue
        if file_size < 0 or file_size > _diagnostic_file_size_limit(path):
            continue
        if copied >= DIAGNOSTIC_FILE_COUNT_LIMIT:
            break
        if copied_bytes + file_size > DIAGNOSTIC_AGGREGATE_MAX_BYTES:
            break
        target = diagnostics_dir / path.relative_to(source_dir)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Finalized HAR/JSONL evidence is immutable. A hard link avoids
                # charging the same bytes to both provider retention and every
                # phase snapshot on the common same-volume desktop layout.
                os.link(path, target)
            except OSError:
                # Cross-volume filesystems and restricted Windows setups may
                # reject hard links; retain the portable copy behavior there.
                shutil.copy2(path, target)
            copied += 1
            copied_bytes += file_size
        except OSError as exc:
            logger.warning(
                "auto-archive diagnostics copy failed provider=%s path=%s: %s",
                provider,
                path.name,
                exc,
            )
    if copied:
        logger.info("auto-archive copied %s diagnostics files provider=%s", copied, provider)


def _copy_visible_auth_runner_log_into_run(
    *,
    run_dir: Path,
    provider: str,
    attempt_id: str | None,
) -> None:
    safe_attempt_id = _safe_name_component(attempt_id or "", fallback="")
    if not safe_attempt_id:
        return
    source_path = (
        DESKTOP_AUTH_DIR
        / "logs"
        / "visible-auth"
        / f"{_safe_name_component(provider, fallback='provider')}-{safe_attempt_id}.log"
    )
    if not source_path.is_file():
        return
    try:
        raw_text = _read_text_tail(source_path, VISIBLE_AUTH_RUNNER_LOG_MAX_BYTES)
    except OSError as exc:
        logger.warning("auto-archive visible-auth runner log read failed provider=%s: %s", provider, exc)
        return
    sanitized_text = _sanitize_log_blob(raw_text)
    if not sanitized_text:
        return
    diagnostics_dir = run_dir / "diagnostics"
    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (diagnostics_dir / "visible-auth-runner.log").write_text(sanitized_text + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("auto-archive visible-auth runner log write failed provider=%s: %s", provider, exc)


def _copy_browser_runtime_prewarm_log_into_run(*, run_dir: Path) -> None:
    candidates = (
        LOG_DIR / "runtimes" / "browser-runtime" / "prewarm.log",
        DESKTOP_AUTH_DIR / "logs" / "runtimes" / "browser-runtime" / "prewarm.log",
    )
    source_path = next((path for path in candidates if path.is_file()), None)
    if source_path is None:
        return
    try:
        raw_text = _read_text_tail(source_path, BROWSER_RUNTIME_PREWARM_LOG_MAX_BYTES)
    except OSError as exc:
        logger.warning("auto-archive browser runtime prewarm log read failed: %s", exc)
        return
    sanitized_text = _sanitize_log_blob(raw_text)
    if not sanitized_text:
        return
    diagnostics_dir = run_dir / "diagnostics"
    try:
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (diagnostics_dir / "browser-runtime-prewarm.log").write_text(
            sanitized_text + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("auto-archive browser runtime prewarm log write failed: %s", exc)


def _attempt_unit_key(payload: dict[str, Any], *, fallback: str) -> str:
    sync_id = str(payload.get("sync_id") or "").strip()
    if sync_id:
        return f"sync:{sync_id}"
    attempt_id = str(payload.get("attempt_id") or "").strip()
    if attempt_id:
        return f"attempt:{attempt_id}"
    return f"run:{fallback}"


def _prune_runs(provider_root: Path, *, user_id: int | None) -> None:
    if not provider_root.exists():
        return
    cutoff = _utc_now() - timedelta(days=ATTEMPT_RETENTION_DAYS)
    family_roots = []
    for family_member in _provider_family(provider_root.name) or (provider_root.name,):
        candidate = RUNS_DIR / _safe_provider_dir_name(family_member)
        if candidate.exists() and candidate.is_dir() and candidate not in family_roots:
            family_roots.append(candidate)
    if provider_root not in family_roots:
        family_roots.append(provider_root)

    units: dict[str, dict[str, Any]] = {}
    for family_root in family_roots:
        try:
            children = list(family_root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            payload = _trigger_payload_for(child) or {}
            if not _trigger_user_matches(payload, user_id):
                continue
            try:
                modified_at = child.stat().st_mtime
            except OSError:
                continue
            unit_key = _attempt_unit_key(
                payload,
                fallback=f"{family_root.name}/{child.name}",
            )
            unit = units.setdefault(unit_key, {"modified_at": 0.0, "paths": []})
            unit["modified_at"] = max(float(unit["modified_at"]), modified_at)
            unit["paths"].append(child)

    ordered_units = sorted(
        units.values(),
        key=lambda unit: float(unit["modified_at"]),
        reverse=True,
    )
    for index, unit in enumerate(ordered_units):
        modified_dt = datetime.fromtimestamp(float(unit["modified_at"]), tz=timezone.utc)
        if index < ATTEMPTS_PER_PROVIDER_LIMIT and modified_dt >= cutoff:
            continue
        for path in unit["paths"]:
            _remove_run_dir(path)


def _remove_run_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _safe_provider_dir_name(provider: str) -> str:
    safe = _safe_name_component(provider, fallback="provider")
    return safe


def _safe_run_dir_name(run_id: str) -> str:
    return _safe_name_component(run_id, fallback="run")


def _trigger_payload_for(run_dir: Path) -> dict[str, Any] | None:
    trigger_path = run_dir / "trigger.json"
    if not trigger_path.exists():
        return None
    try:
        return json.loads(trigger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run_is_complete(run_dir: Path) -> bool:
    completion_path = run_dir / RUN_COMPLETION_FILENAME
    try:
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "complete" and payload.get(
        "evidence_collection_finished"
    ) is True


def _trigger_user_matches(payload: dict[str, Any], user_id: int | None) -> bool:
    if user_id is None:
        return True
    try:
        return int(payload.get("user_id")) == int(user_id)
    except (TypeError, ValueError):
        return False


def _summarize_run(provider_root: Path, run_dir: Path) -> dict[str, Any]:
    try:
        accessible_run_dir = _filesystem_access_path(run_dir)
        stat = accessible_run_dir.stat()
    except OSError:
        accessible_run_dir = run_dir
        stat = None
    trigger_payload = _trigger_payload_for(run_dir) or {}
    files: list[dict[str, Any]] = []
    files_truncated = False
    examined = 0
    for child in accessible_run_dir.rglob("*"):
        examined += 1
        if examined > SUPPORT_DIRECTORY_SCAN_LIMIT:
            files_truncated = True
            break
        if not child.is_file():
            continue
        if len(files) >= SUPPORT_ZIP_FILE_COUNT_LIMIT:
            files_truncated = True
            break
        try:
            relative = child.relative_to(accessible_run_dir).as_posix()
            size = child.stat().st_size
        except OSError:
            continue
        files.append({"path": relative, "bytes": int(size)})
    return {
        "provider": provider_root.name,
        "run_id": run_dir.name,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat() if stat else None,
        "size_bytes": sum(int(entry["bytes"]) for entry in files),
        "file_count": len(files),
        "files_truncated": files_truncated,
        "trigger": trigger_payload.get("trigger"),
        "sync_id": trigger_payload.get("sync_id"),
        "attempt_id": trigger_payload.get("attempt_id"),
        "capture_level": trigger_payload.get("capture_level"),
        "user_id": trigger_payload.get("user_id"),
        "error": trigger_payload.get("error"),
        "generated_at": trigger_payload.get("generated_at"),
        "files": files,
    }


def _provider_family(provider: str) -> tuple[str, ...]:
    """Return all route_providers sharing this provider's syncStatusProvider
    (e.g. ('ibkr', 'ibkr_flex') for either input). Lets the panel surface
    runs for a split institution under a single UI provider key."""
    if not provider:
        return ()
    try:
        from app.provider_catalog import get_status_provider_for_result
        from app.services.sync_utils import _related_route_providers_for_status
        status_provider = get_status_provider_for_result(provider)
        family = _related_route_providers_for_status(status_provider)
    except Exception:
        family = (provider,)
    if provider not in family:
        family = (*family, provider)
    return tuple(dict.fromkeys(family))


def list_runs(*, provider: str | None = None, user_id: int | None = None) -> list[dict[str, Any]]:
    if not RUNS_DIR.exists():
        return []
    provider_roots: list[Path] = []
    if provider:
        for family_member in _provider_family(provider):
            candidate = RUNS_DIR / _safe_provider_dir_name(family_member)
            if candidate.exists() and candidate.is_dir():
                provider_roots.append(candidate)
    else:
        provider_roots = [child for child in _bounded_children(RUNS_DIR) if child.is_dir()]

    runs: list[dict[str, Any]] = []
    examined_runs = 0
    for provider_root in provider_roots:
        _prune_runs(provider_root, user_id=user_id)
        for child in _bounded_children(provider_root, limit=SUPPORT_RUN_SCAN_LIMIT):
            examined_runs += 1
            if examined_runs > SUPPORT_RUN_SCAN_LIMIT:
                break
            if not child.is_dir():
                continue
            if not _run_is_complete(child):
                continue
            if user_id is not None:
                payload = _trigger_payload_for(child) or {}
                if not _trigger_user_matches(payload, user_id):
                    continue
            summary = _summarize_run(provider_root, child)
            if _archive_identity_is_pending(
                user_id=user_id,
                sync_id=summary.get("sync_id"),
                attempt_id=summary.get("attempt_id"),
            ):
                continue
            runs.append(summary)
        if examined_runs > SUPPORT_RUN_SCAN_LIMIT:
            break
    runs.sort(key=lambda item: item.get("modified_at") or "", reverse=True)
    return runs


def get_run(provider: str, run_id: str, *, user_id: int | None = None) -> dict[str, Any] | None:
    safe_run = _safe_run_dir_name(run_id)
    for family_member in _provider_family(provider):
        provider_root = RUNS_DIR / _safe_provider_dir_name(family_member)
        if not provider_root.exists():
            continue
        run_dir = provider_root / safe_run
        if run_dir.exists() and run_dir.is_dir() and _run_is_complete(run_dir):
            if user_id is not None:
                payload = _trigger_payload_for(run_dir) or {}
                if not _trigger_user_matches(payload, user_id):
                    continue
            summary = _summarize_run(provider_root, run_dir)
            if _archive_identity_is_pending(
                user_id=user_id,
                sync_id=summary.get("sync_id"),
                attempt_id=summary.get("attempt_id"),
            ):
                continue
            return summary
    return None


def purge_provider_diagnostic_artifacts(*, user_id: int, provider: str) -> dict[str, int]:
    """Remove a user's auto-archive folders, exported zips, and provider replay
    HAR/JSONL files (under data/logs/providers/) for a given provider AND every
    related route_provider in its family (e.g. removing 'ibkr' also sweeps
    'ibkr_flex'). Multi-user safe: filters by user_id on archive folders and by
    `user{N}` marker on provider-log filenames."""
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return {"runs_removed": 0, "exports_removed": 0, "diagnostics_removed": 0}
    family = _provider_family(normalized_provider) or (normalized_provider,)
    target_user_id = int(user_id)
    counts = {"runs_removed": 0, "exports_removed": 0, "diagnostics_removed": 0}

    for family_member in family:
        safe_provider = _safe_provider_dir_name(family_member)

        # 1. Auto-archive folders under data/logs/runs/<provider>/
        provider_runs_root = RUNS_DIR / safe_provider
        if provider_runs_root.exists() and provider_runs_root.is_dir():
            for run_dir in _bounded_children(provider_runs_root, limit=SUPPORT_RUN_SCAN_LIMIT):
                if not run_dir.is_dir():
                    continue
                payload = _trigger_payload_for(run_dir) or {}
                run_user_id = payload.get("user_id")
                if run_user_id is not None and int(run_user_id) == target_user_id:
                    _remove_run_dir(run_dir)
                    counts["runs_removed"] += 1
            try:
                if not any(provider_runs_root.iterdir()):
                    provider_runs_root.rmdir()
            except OSError:
                pass

        # 2. Opaque, owner-registered exports for this provider.
        for archive_id, metadata in list(_iter_registered_archive_metadata()):
            if (
                metadata.get("user_id") == target_user_id
                and metadata.get("kind") == "provider_run"
                and metadata.get("provider") == family_member
            ):
                _remove_registered_archive(archive_id)
                counts["exports_removed"] += 1

    # 3. Sanitized HAR + Phase 2 JSONL artifacts under data/logs/providers/<PROVIDER>/
    try:
        from app.services.replay_diagnostics import PROVIDER_DIAGNOSTIC_DIRS
    except Exception:
        PROVIDER_DIAGNOSTIC_DIRS = {}
    diagnostic_dirs = {
        DIAGNOSTIC_DIR / (PROVIDER_DIAGNOSTIC_DIRS.get(member) or member.upper())
        for member in family
    }
    for diag_dir in diagnostic_dirs:
        if not diag_dir.exists() or not diag_dir.is_dir():
            continue
        for path in _bounded_tree(diag_dir):
            if not path.is_file():
                continue
            if not _diagnostic_filename_owned_by(path.name, target_user_id):
                continue
            try:
                path.unlink()
                counts["diagnostics_removed"] += 1
            except OSError:
                continue
        try:
            if not any(diag_dir.iterdir()):
                diag_dir.rmdir()
        except OSError:
            pass

    if any(counts.values()):
        logger.info(
            "diagnostic purge user_id=%s provider=%s runs=%s exports=%s diagnostics=%s",
            target_user_id,
            normalized_provider,
            counts["runs_removed"],
            counts["exports_removed"],
            counts["diagnostics_removed"],
        )
    return counts


def _sibling_run_dirs_for_attempt(
    provider: str,
    sync_id: str | None,
    attempt_id: str | None,
    *,
    user_id: int,
) -> list[Path]:
    normalized_sync_id = str(sync_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    if not normalized_sync_id and not normalized_attempt_id:
        return []
    matches: list[Path] = []
    examined = 0
    for family_member in _provider_family(provider):
        provider_root = RUNS_DIR / _safe_provider_dir_name(family_member)
        if not provider_root.exists():
            continue
        try:
            provider_runs = list(provider_root.iterdir())
        except OSError as exc:
            raise SupportArchiveLimitError(
                reason="evidence_unavailable",
                message=(
                    "The diagnostic evidence changed while the complete attempt was being "
                    "checked. No partial ZIP was created; retry the export."
                ),
            ) from exc
        for run_dir in provider_runs:
            examined += 1
            if examined > SUPPORT_RUN_SCAN_LIMIT:
                raise SupportArchiveLimitError(
                    reason="attempt_run_scan_limit",
                    message=(
                        f"More than {SUPPORT_RUN_SCAN_LIMIT:,} retained run directories must be "
                        "checked to prove this attempt is complete. No partial ZIP was created; "
                        "report this limit reason so the scan bound can be reviewed."
                    ),
                    limit=SUPPORT_RUN_SCAN_LIMIT,
                )
            if not run_dir.is_dir():
                continue
            if not _run_is_complete(run_dir):
                continue
            trigger_payload = _trigger_payload_for(run_dir) or {}
            if not _trigger_user_matches(trigger_payload, user_id):
                continue
            candidate_sync_id = str(trigger_payload.get("sync_id") or "").strip()
            candidate_attempt_id = str(trigger_payload.get("attempt_id") or "").strip()
            if normalized_sync_id and candidate_sync_id == normalized_sync_id:
                matches.append(run_dir)
            elif (
                not normalized_sync_id
                and normalized_attempt_id
                and candidate_attempt_id == normalized_attempt_id
            ):
                matches.append(run_dir)
    matches.sort(
        key=lambda path: (
            path.stat().st_mtime if path.exists() else 0,
            path.parent.name,
            path.name,
        )
    )
    return matches


def package_run_zip(provider: str, run_id: str, *, user_id: int) -> SupportArchive | None:
    for family_member in _provider_family(provider):
        provider_root = RUNS_DIR / _safe_provider_dir_name(family_member)
        if provider_root.exists() and provider_root.is_dir():
            _prune_runs(provider_root, user_id=user_id)
    summary = get_run(provider, run_id, user_id=user_id)
    if not summary:
        return None
    sync_id = str(summary.get("sync_id") or "").strip() or None
    attempt_id = str(summary.get("attempt_id") or "").strip() or None
    actual_provider = summary.get("provider") or provider
    primary_run_dir = RUNS_DIR / _safe_provider_dir_name(actual_provider) / _safe_run_dir_name(run_id)
    siblings = _sibling_run_dirs_for_attempt(
        actual_provider,
        sync_id,
        attempt_id,
        user_id=user_id,
    )
    if primary_run_dir not in siblings:
        siblings = [primary_run_dir] + [path for path in siblings if path != primary_run_dir]
    if not siblings:
        siblings = [primary_run_dir]

    bundle_anchor = max(siblings, key=lambda path: path.name)
    timestamp_prefix = "_".join(bundle_anchor.name.split("_")[:3])
    zip_name = (
        f"BreakTwenty_{_safe_provider_dir_name(actual_provider)}_attempt_diagnostics_"
        f"{timestamp_prefix}.zip"
    )
    archive_runs: list[tuple[Path, str]] = []
    cross_provider = len({path.parent.name for path in siblings}) > 1
    sibling_attempt_ids: list[str] = []
    sibling_capture_levels: list[str] = []
    for run_dir in siblings:
        trigger_payload = _trigger_payload_for(run_dir) or {}
        if not _trigger_user_matches(trigger_payload, user_id):
            continue
        sibling_attempt_id = str(trigger_payload.get("attempt_id") or "").strip()
        if sibling_attempt_id and sibling_attempt_id not in sibling_attempt_ids:
            sibling_attempt_ids.append(sibling_attempt_id)
        sibling_capture_level = str(trigger_payload.get("capture_level") or "").strip()
        if sibling_capture_level and sibling_capture_level not in sibling_capture_levels:
            sibling_capture_levels.append(sibling_capture_level)
        if len(siblings) == 1:
            archive_root = ""
        elif cross_provider:
            archive_root = f"{run_dir.parent.name}/{run_dir.name}"
        else:
            archive_root = run_dir.name
        archive_runs.append((run_dir, archive_root))
    attempt_key = (
        f"sync:{sync_id}"
        if sync_id
        else f"attempt:{attempt_id}"
        if attempt_id
        else f"run:{actual_provider}/{run_id}"
    )
    attempt_files = _collect_attempt_archive_files(
        archive_runs,
        attempt_key=attempt_key,
        tree_scan_limit=SUPPORT_DIRECTORY_SCAN_LIMIT,
    )
    archive_entries = sorted(attempt_files.entries, key=lambda entry: entry[1])
    deduplicated_bytes = sum(
        int(item["size_bytes"]) for item in attempt_files.deduplicated_files
    )
    aggregate_bytes = attempt_files.original_size_bytes - deduplicated_bytes
    if len(archive_entries) > SUPPORT_ZIP_FILE_COUNT_LIMIT:
        raise SupportArchiveLimitError(
            reason="attempt_file_count_limit",
            message=(
                f"This complete sync attempt needs {len(archive_entries):,} files after exact "
                f"duplicate removal, above the {SUPPORT_ZIP_FILE_COUNT_LIMIT:,}-file safety "
                "limit. No partial ZIP was created; report this limit reason so the attempt "
                "export bound can be reviewed."
            ),
            required_file_count=len(archive_entries),
            limit=SUPPORT_ZIP_FILE_COUNT_LIMIT,
        )
    if aggregate_bytes > SUPPORT_ZIP_AGGREGATE_MAX_BYTES:
        raise SupportArchiveLimitError(
            reason="attempt_aggregate_bytes_limit",
            message=(
                f"This complete sync attempt needs {aggregate_bytes / (1024 * 1024):.1f} MiB "
                "after exact duplicate removal, above the "
                f"{SUPPORT_ZIP_AGGREGATE_MAX_BYTES / (1024 * 1024):.0f} MiB safety limit. "
                "No partial ZIP was created; report this limit reason so the attempt export "
                "bound can be reviewed."
            ),
            required_bytes=aggregate_bytes,
            limit=SUPPORT_ZIP_AGGREGATE_MAX_BYTES,
        )

    support_archive = _register_support_archive(
        user_id=user_id,
        filename=zip_name,
        kind="provider_run",
        provider=str(actual_provider),
    )
    export_manifest = {
        "schema_version": 1,
        "generated_at": _utc_now().isoformat(),
        "export_scope": "sync_attempt",
        "attempt_boundary": (
            "Every finalized run sharing this sync_id (or attempt_id fallback) is included."
        ),
        "complete": True,
        "truncated": False,
        "attempt_key": attempt_key,
        "sync_id": sync_id,
        "attempt_id": sibling_attempt_ids[0] if sibling_attempt_ids else attempt_id,
        "capture_level": (
            sibling_capture_levels[0]
            if len(sibling_capture_levels) == 1
            else "mixed"
            if sibling_capture_levels
            else "unknown"
        ),
        "capture_levels": sorted(sibling_capture_levels),
        "providers": sorted({run_dir.parent.name for run_dir, _archive_root in archive_runs}),
        "run_ids": [run_dir.name for run_dir, _archive_root in archive_runs],
        "included_file_count": len(archive_entries),
        "included_size_bytes": aggregate_bytes,
        "original_file_count": attempt_files.original_file_count,
        "original_size_bytes": attempt_files.original_size_bytes,
        "deduplicated_file_count": len(attempt_files.deduplicated_files),
        "deduplicated_bytes": deduplicated_bytes,
        "deduplicated_files": list(attempt_files.deduplicated_files),
        "limits": {
            "file_count": SUPPORT_ZIP_FILE_COUNT_LIMIT,
            "aggregate_bytes": SUPPORT_ZIP_AGGREGATE_MAX_BYTES,
            "per_file_bytes": DIAGNOSTIC_FILE_MAX_BYTES,
            "har_per_file_bytes": DIAGNOSTIC_HAR_FILE_MAX_BYTES,
            "note": "Generated manifest metadata is outside the attempt-file limits.",
        },
    }
    try:
        with zipfile.ZipFile(support_archive.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "export_manifest.json",
                json.dumps(export_manifest, indent=2, default=str) + "\n",
            )
            for child, archive_name in archive_entries:
                archive.write(child, arcname=archive_name)
        _set_private_archive_permissions(support_archive.path)
    except Exception:
        _remove_registered_archive(support_archive.archive_id)
        raise
    _prune_registered_archives(
        user_id=user_id,
        kind="provider_run",
        provider=str(actual_provider),
        keep=EXPORTED_RUNS_PER_PROVIDER_LIMIT,
    )
    return support_archive


async def package_all_recent_runs(
    *,
    user_id: int | None,
    since_minutes: int = 60,
    excluded_sync_ids: set[str] | None = None,
) -> tuple[SupportArchive | None, list[dict[str, Any]], dict[str, Any]]:
    user_timezone = await _user_timezone_info(user_id)
    current_state_at_export = await _collect_current_state_at_export(user_id)
    return await asyncio.to_thread(
        _package_all_recent_runs_sync,
        user_id=user_id,
        since_minutes=since_minutes,
        user_timezone=user_timezone,
        current_state_at_export=current_state_at_export,
        excluded_sync_ids=excluded_sync_ids,
    )


def _package_all_recent_runs_sync(
    *,
    user_id: int | None,
    since_minutes: int = 60,
    user_timezone: ZoneInfo | None = None,
    current_state_at_export: dict[str, Any] | None = None,
    excluded_sync_ids: set[str] | None = None,
) -> tuple[SupportArchive | None, list[dict[str, Any]], dict[str, Any]]:
    if since_minutes <= 0:
        since_minutes = 60
    if user_id is None:
        raise ValueError("User-owned support exports require a user ID")
    excluded_attempts = {
        str(sync_id).strip()
        for sync_id in (excluded_sync_ids or set())
        if str(sync_id).strip()
    }
    cutoff = _utc_now() - timedelta(minutes=int(since_minutes))
    if not RUNS_DIR.exists():
        return None, [], {}

    retained: list[tuple[Path, dict[str, Any], float]] = []
    examined_runs = 0
    try:
        provider_roots = list(RUNS_DIR.iterdir())
    except OSError as exc:
        raise SupportArchiveLimitError(
            reason="evidence_unavailable",
            message=(
                "The diagnostic evidence changed while the complete export was being checked. "
                "No partial ZIP was created; retry the export."
            ),
        ) from exc
    if len(provider_roots) > SUPPORT_DIRECTORY_SCAN_LIMIT:
        raise SupportArchiveLimitError(
            reason="provider_scan_limit",
            message=(
                "The retained diagnostics contain too many provider directories to verify a "
                "complete export safely. No partial ZIP was created; export an individual "
                "attempt from its timestamp pill or remove expired diagnostics and retry."
            ),
            limit=SUPPORT_DIRECTORY_SCAN_LIMIT,
        )
    for provider_root in sorted(provider_roots, key=lambda path: path.name):
        if not provider_root.is_dir():
            continue
        _prune_runs(provider_root, user_id=user_id)
        try:
            provider_runs = list(provider_root.iterdir())
        except OSError as exc:
            raise SupportArchiveLimitError(
                reason="evidence_unavailable",
                message=(
                    "The diagnostic evidence changed while the complete export was being checked. "
                    "No partial ZIP was created; retry the export."
                ),
            ) from exc
        for run_dir in sorted(provider_runs, key=lambda path: path.name):
            examined_runs += 1
            if examined_runs > ALL_RECENT_SUPPORT_RUN_SCAN_LIMIT:
                raise SupportArchiveLimitError(
                    reason="run_scan_limit",
                    message=(
                        f"More than {ALL_RECENT_SUPPORT_RUN_SCAN_LIMIT:,} retained diagnostic "
                        "runs exist, so this window cannot be verified as complete safely. No "
                        "partial ZIP was created; export an individual attempt from its "
                        "timestamp pill or remove expired diagnostics and retry."
                    ),
                    limit=ALL_RECENT_SUPPORT_RUN_SCAN_LIMIT,
                )
            if not run_dir.is_dir():
                continue
            if not _run_is_complete(run_dir):
                continue
            trigger_payload = _trigger_payload_for(run_dir) or {}
            if not _trigger_user_matches(trigger_payload, user_id):
                continue
            try:
                modified_timestamp = float(run_dir.stat().st_mtime)
            except OSError as exc:
                raise SupportArchiveLimitError(
                    reason="evidence_unavailable",
                    message=(
                        "The diagnostic evidence changed while the complete export was being "
                        "checked. No partial ZIP was created; retry the export."
                    ),
                ) from exc
            summary = _summarize_run(provider_root, run_dir)
            summary_sync_id = str(summary.get("sync_id") or "").strip()
            if summary_sync_id and summary_sync_id in excluded_attempts:
                continue
            if _archive_identity_is_pending(
                user_id=user_id,
                sync_id=summary_sync_id,
                attempt_id=summary.get("attempt_id"),
            ):
                continue
            retained.append((run_dir, summary, modified_timestamp))

    if not retained:
        return None, [], {}

    # A terminal attempt is the export boundary, whether its outcome was success,
    # failure, timeout, or cancellation. Group before applying the time cutoff so
    # a phase that began just outside the window stays with its terminal sibling.
    units_by_key: dict[str, dict[str, Any]] = {}
    for run_dir, summary, modified_timestamp in retained:
        sync_id = str(summary.get("sync_id") or "").strip()
        attempt_id = str(summary.get("attempt_id") or "").strip()
        if sync_id:
            unit_key = f"sync:{sync_id}"
        elif attempt_id:
            unit_key = f"attempt:{attempt_id}"
        else:
            unit_key = f"run:{run_dir.parent.name}/{run_dir.name}"
        unit = units_by_key.setdefault(
            unit_key,
            {
                "attempt_key": unit_key,
                "sync_id": sync_id or None,
                "attempt_id": attempt_id or None,
                "capture_levels": set(),
                "providers": set(),
                "runs": [],
                "modified_timestamp": 0.0,
            },
        )
        unit["providers"].add(str(summary.get("provider") or run_dir.parent.name))
        capture_level = str(summary.get("capture_level") or "").strip()
        if capture_level:
            unit["capture_levels"].add(capture_level)
        unit["runs"].append((run_dir, summary))
        unit["modified_timestamp"] = max(unit["modified_timestamp"], modified_timestamp)
        if unit["attempt_id"] is None and attempt_id:
            unit["attempt_id"] = attempt_id

    units = sorted(
        (
            unit
            for unit in units_by_key.values()
            if datetime.fromtimestamp(unit["modified_timestamp"], tz=timezone.utc) >= cutoff
        ),
        key=lambda unit: (unit["modified_timestamp"], unit["attempt_key"]),
        reverse=True,
    )
    if not units:
        return None, [], {}

    prepared_units: list[dict[str, Any]] = []
    examined_tree_entries = 0
    for unit in units:
        unit_runs = sorted(
            unit["runs"],
            key=lambda item: (item[0].parent.name, item[0].name),
        )
        manifest_unit = {
            "attempt_key": unit["attempt_key"],
            "sync_id": unit["sync_id"],
            "attempt_id": unit["attempt_id"],
            "capture_level": (
                next(iter(unit["capture_levels"]))
                if len(unit["capture_levels"]) == 1
                else "mixed"
                if unit["capture_levels"]
                else "unknown"
            ),
            "capture_levels": sorted(unit["capture_levels"]),
            "providers": sorted(unit["providers"]),
            "run_ids": [run_dir.name for run_dir, _summary in unit_runs],
            "terminal_evidence_at": datetime.fromtimestamp(
                unit["modified_timestamp"],
                tz=timezone.utc,
            ).isoformat(),
        }
        attempt_files = _collect_attempt_archive_files(
            (
                (run_dir, f"{run_dir.parent.name}/{run_dir.name}")
                for run_dir, _summary in unit_runs
            ),
            attempt_key=unit["attempt_key"],
            tree_scan_limit=ALL_RECENT_SUPPORT_TREE_SCAN_LIMIT - examined_tree_entries,
        )
        examined_tree_entries += attempt_files.examined_tree_entries
        unit_entries = list(attempt_files.entries)
        unit_deduplicated_bytes = sum(
            int(item["size_bytes"]) for item in attempt_files.deduplicated_files
        )
        unit_bytes = attempt_files.original_size_bytes - unit_deduplicated_bytes

        manifest_unit.update(
            file_count=len(unit_entries),
            size_bytes=unit_bytes,
            original_file_count=attempt_files.original_file_count,
            original_size_bytes=attempt_files.original_size_bytes,
            deduplicated_file_count=len(attempt_files.deduplicated_files),
            deduplicated_bytes=unit_deduplicated_bytes,
        )
        prepared_units.append(
            {
                "manifest": manifest_unit,
                "entries": unit_entries,
                "deduplicated_files": list(attempt_files.deduplicated_files),
                "runs": unit_runs,
            }
        )

    newest = prepared_units[0]
    if newest["manifest"]["file_count"] > ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT:
        raise SupportArchiveLimitError(
            reason="newest_attempt_file_count_limit",
            message=(
                "The newest complete sync attempt cannot fit within the all-provider "
                f"{ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT:,}-file safety limit. No partial "
                "attempt was exported; report this limit reason so the archive bound can be "
                "reviewed."
            ),
            required_file_count=newest["manifest"]["file_count"],
            limit=ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT,
        )
    if newest["manifest"]["size_bytes"] > SUPPORT_ZIP_AGGREGATE_MAX_BYTES:
        raise SupportArchiveLimitError(
            reason="newest_attempt_aggregate_bytes_limit",
            message=(
                "The newest complete sync attempt cannot fit within the all-provider "
                f"{SUPPORT_ZIP_AGGREGATE_MAX_BYTES / (1024 * 1024):.0f} MiB safety limit. "
                "No partial attempt was exported; report this limit reason so the archive bound "
                "can be reviewed."
            ),
            required_bytes=newest["manifest"]["size_bytes"],
            limit=SUPPORT_ZIP_AGGREGATE_MAX_BYTES,
        )

    included_prepared: list[dict[str, Any]] = []
    omitted_prepared: list[dict[str, Any]] = []
    archive_entries: list[tuple[Path, str]] = []
    aggregate_bytes = 0
    file_count = 0
    capacity_reason: str | None = None
    for prepared in prepared_units:
        unit_file_count = int(prepared["manifest"]["file_count"])
        unit_bytes = int(prepared["manifest"]["size_bytes"])
        if capacity_reason is None:
            if file_count + unit_file_count > ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT:
                capacity_reason = "export_file_count_limit"
            elif aggregate_bytes + unit_bytes > SUPPORT_ZIP_AGGREGATE_MAX_BYTES:
                capacity_reason = "export_aggregate_bytes_limit"
        if capacity_reason is not None:
            omitted_prepared.append(prepared)
            continue
        included_prepared.append(prepared)
        archive_entries.extend(prepared["entries"])
        file_count += unit_file_count
        aggregate_bytes += unit_bytes

    included_units = [prepared["manifest"] for prepared in included_prepared]
    omitted_units = [
        {
            **prepared["manifest"],
            "reason": capacity_reason,
            "message": (
                "Omitted as a whole attempt because newer complete attempts exhausted the "
                "bounded export capacity. Export this attempt from its provider timestamp pill."
            ),
        }
        for prepared in omitted_prepared
    ]
    deduplicated_files = [
        item
        for prepared in included_prepared
        for item in prepared["deduplicated_files"]
    ]

    timestamp = _format_local_filename_timestamp(_utc_now(), user_timezone)
    support_archive = _register_support_archive(
        user_id=user_id,
        filename=f"BreakTwenty_all_providers_recent_diagnostics_{timestamp}.zip",
        kind="all_recent",
    )
    archive_entries.sort(key=lambda entry: entry[1])
    included = [
        {
            "provider": summary.get("provider"),
            "run_id": summary.get("run_id"),
            "trigger": summary.get("trigger"),
            "sync_id": summary.get("sync_id"),
            "modified_at": summary.get("modified_at"),
        }
        for prepared in included_prepared
        for _run_dir, summary in sorted(
            prepared["runs"],
            key=lambda item: (item[0].parent.name, item[0].name),
        )
    ]
    omitted_run_count = sum(len(prepared["runs"]) for prepared in omitted_prepared)
    omitted_providers = sorted(
        {
            provider_name
            for prepared in omitted_prepared
            for provider_name in prepared["manifest"]["providers"]
        }
    )
    included_original_file_count = sum(
        int(unit["original_file_count"]) for unit in included_units
    )
    included_original_bytes = sum(int(unit["original_size_bytes"]) for unit in included_units)
    requested_file_count = sum(
        int(prepared["manifest"]["file_count"]) for prepared in prepared_units
    )
    requested_size_bytes = sum(
        int(prepared["manifest"]["size_bytes"]) for prepared in prepared_units
    )
    export_manifest = {
        "schema_version": 1,
        "generated_at": _utc_now().isoformat(),
        "export_scope": "all_providers_recent",
        "scope_label": "Explicitly provider-wide recent diagnostics export",
        "attempt_boundary": (
            "All finalized runs sharing one sync_id (or attempt_id fallback) are one export unit, "
            "regardless of terminal success or failure."
        ),
        "selection_policy": (
            "Terminal attempts whose latest finalized evidence lands in the requested window are "
            "ordered newest-first. Whole attempts are included until the next-oldest attempt would "
            "exceed a safety limit; that attempt and every older attempt remain individually "
            "exportable from their provider timestamp pills."
        ),
        "since_minutes": int(since_minutes),
        "capture_level": (
            next(iter({
                level
                for unit in included_units
                for level in unit.get("capture_levels", [])
            }))
            if len({
                level
                for unit in included_units
                for level in unit.get("capture_levels", [])
            }) == 1
            else "mixed"
            if any(unit.get("capture_levels") for unit in included_units)
            else "unknown"
        ),
        "capture_levels": sorted({
            level
            for unit in included_units
            for level in unit.get("capture_levels", [])
        }),
        "limits": {
            "run_file_count": ALL_RECENT_SUPPORT_ZIP_FILE_COUNT_LIMIT,
            "run_aggregate_bytes": SUPPORT_ZIP_AGGREGATE_MAX_BYTES,
            "per_file_bytes": DIAGNOSTIC_FILE_MAX_BYTES,
            "har_per_file_bytes": DIAGNOSTIC_HAR_FILE_MAX_BYTES,
            "note": "Generated manifest/current-state metadata are outside the run-file limits.",
        },
        "complete": not omitted_units,
        "truncated": bool(omitted_units),
        "requested_attempt_count": len(prepared_units),
        "requested_run_count": sum(len(prepared["runs"]) for prepared in prepared_units),
        "requested_file_count_after_deduplication": requested_file_count,
        "requested_size_bytes_after_deduplication": requested_size_bytes,
        "included_attempt_count": len(included_units),
        "included_run_count": len(included),
        "included_file_count": len(archive_entries),
        "included_size_bytes": aggregate_bytes,
        "original_file_count": included_original_file_count,
        "original_size_bytes": included_original_bytes,
        "deduplicated_file_count": len(deduplicated_files),
        "deduplicated_bytes": included_original_bytes - aggregate_bytes,
        "deduplicated_files": deduplicated_files,
        "omitted_attempt_count": len(omitted_units),
        "omitted_run_count": omitted_run_count,
        "included_attempts": included_units,
        "omitted_attempts": omitted_units,
    }
    export_summary = {
        "complete": not omitted_units,
        "truncated": bool(omitted_units),
        "requested_attempt_count": len(prepared_units),
        "included_attempt_count": export_manifest["included_attempt_count"],
        "omitted_attempt_count": len(omitted_units),
        "omitted_run_count": omitted_run_count,
        "providers_with_omitted_attempts": omitted_providers,
        "included_file_count": len(archive_entries),
        "deduplicated_file_count": len(deduplicated_files),
        "deduplicated_bytes": included_original_bytes - aggregate_bytes,
        "manifest_path": "export_manifest.json",
    }
    try:
        with zipfile.ZipFile(support_archive.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if current_state_at_export is not None:
                archive.writestr(
                    "current_state_at_export.json",
                    json.dumps(current_state_at_export, indent=2, default=str) + "\n",
                )
            archive.writestr(
                "export_manifest.json",
                json.dumps(export_manifest, indent=2, default=str) + "\n",
            )
            for child, archive_name in archive_entries:
                archive.write(child, arcname=archive_name)
        _set_private_archive_permissions(support_archive.path)
    except Exception:
        _remove_registered_archive(support_archive.archive_id)
        raise
    _prune_registered_archives(
        user_id=user_id,
        kind="all_recent",
        keep=EXPORTED_ALL_RECENT_LIMIT,
    )
    return support_archive, included, export_summary
