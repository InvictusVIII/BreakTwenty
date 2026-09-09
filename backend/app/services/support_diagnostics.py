from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select

from app.database import async_session
from app.models import (
    Account,
    Institution,
    TransactionImportAccountState,
    TransactionImportJob,
    TransactionImportWindow,
)
from app.services.auth_artifact_utils import normalize_provider as _normalize_provider
from app.services.support_logging import (
    DEFAULT_SUPPORT_CAPTURE_LEVEL,
    SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
    normalize_capture_level,
)
from app.services.sync_tracking import get_provider_sync_attempt
from app.services.sync_utils import sync_lock_state_snapshot
from app.services.transaction_import_jobs import (
    TRANSACTION_JOB_RESTART_RECOVERY_MESSAGE,
    transaction_import_task_snapshot,
)

LOG_FIELD_RE = re.compile(r"\b(?P<key>[A-Za-z_][A-Za-z0-9_-]*)=(?P<value>[^\s]+)")
JSON_LOG_FIELD_RE = re.compile(
    r"(?P<prefix>[\"'](?P<key>[A-Za-z_][A-Za-z0-9_-]*)[\"']\s*:\s*[\"'])(?P<value>[^\"']+)(?P<suffix>[\"'])"
)
PROVIDER_IDENTIFIER_RE = re.compile(r"\b[a-z][a-z0-9_:-]{1,32}:[A-Za-z0-9._:=/-]{8,}\b", re.IGNORECASE)
MAC_ADDRESS_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}\b")
WINDOWS_HOME_PATH_RE = re.compile(r"(?i)\b(?P<prefix>[A-Z]:[\\/]+Users[\\/]+)(?P<user>[^\\/\s\"'|]+)")
POSIX_HOME_PATH_RE = re.compile(r"(?P<prefix>/(?:home|Users)/)(?P<user>[^/\s\"'|]+)")
SENSITIVE_LOG_KEYS = frozenset(
    {
        "authorization",
        "auth_token",
        "authtoken",
        "api_key",
        "apikey",
        "cookie",
        "credential",
        "credentials",
        "device_id",
        "deviceid",
        "login_pwd_md5",
        "loginpwdmd5",
        "mac_addr",
        "mac_address",
        "macaddr",
        "macaddress",
        "password",
        "passwd",
        "pwd",
        "secret",
        "session",
        "token",
    }
)
IDENTIFIER_LOG_KEYS = frozenset(
    {
        "acc_id",
        "account",
        "account_id",
        "account_ref",
        "accountid",
        "card_num",
        "external_id",
        "externalid",
        "provider_account_id",
        "uni_card_num",
    }
)
DEVELOPER_LOCAL_SAFE_LOG_KEY_PREFIXES = ("has", "is")
DEVELOPER_LOCAL_SAFE_LOG_KEY_SUFFIXES = (
    "available",
    "count",
    "counts",
    "cookies",
    "enabled",
    "expired",
    "fresh",
    "origins",
    "present",
    "removed",
    "slot",
    "succeeded",
    "used",
    "valid",
)
DEVELOPER_LOCAL_SAFE_LOG_VALUES = frozenset(
    {
        "active",
        "false",
        "missing",
        "no",
        "none",
        "null",
        "present",
        "quarantine",
        "true",
        "yes",
    }
)
DEVELOPER_LOCAL_SAFE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _isoformat_optional(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _format_utc_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value or "")


def _normalized_log_key(key: str | None) -> str:
    return str(key or "").strip().lower().replace("-", "_")


def _fingerprint_log_value(value: str | None) -> str:
    digest = hashlib.sha1(
        str(value or "").encode("utf-8", errors="replace"),
        usedforsecurity=False,
    ).hexdigest()[:10]
    return f"<redacted:{digest}>"


def _safe_log_replacement(value: str) -> str:
    return _fingerprint_log_value(value)


def _developer_local_safe_log_scalar(key: str, value: str) -> bool:
    normalized_key = _normalized_log_key(key)
    if any(normalized_key.startswith(prefix) for prefix in DEVELOPER_LOCAL_SAFE_LOG_KEY_PREFIXES):
        return True
    if any(normalized_key.endswith(suffix) for suffix in DEVELOPER_LOCAL_SAFE_LOG_KEY_SUFFIXES):
        return True
    normalized_value = str(value or "").strip().lower()
    if normalized_value in DEVELOPER_LOCAL_SAFE_LOG_VALUES:
        return True
    if DEVELOPER_LOCAL_SAFE_NUMBER_RE.fullmatch(normalized_value):
        return True
    return False


def _sanitize_support_log_text(text: str, capture_level: str) -> str:
    normalized_level = normalize_capture_level(capture_level)

    def replace_field(match: re.Match[str]) -> str:
        key = match.group("key")
        value = match.group("value")
        normalized_key = _normalized_log_key(key)
        if any(_normalized_log_key(item) in normalized_key for item in SENSITIVE_LOG_KEYS):
            if normalized_level == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL and _developer_local_safe_log_scalar(key, value):
                return f"{key}={value}"
            return f"{key}={_safe_log_replacement(value)}"
        if normalized_level == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
            return f"{key}={value}"
        if _normalized_log_key(key) in IDENTIFIER_LOG_KEYS:
            return f"{key}={_safe_log_replacement(value)}"
        return f"{key}={value}"

    def replace_json_field(match: re.Match[str]) -> str:
        key = match.group("key")
        value = match.group("value")
        normalized_key = _normalized_log_key(key)
        should_redact = any(_normalized_log_key(item) in normalized_key for item in SENSITIVE_LOG_KEYS)
        should_fingerprint = _normalized_log_key(key) in IDENTIFIER_LOG_KEYS
        if normalized_level == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL and not should_redact:
            return match.group(0)
        if should_redact or should_fingerprint:
            return f"{match.group('prefix')}{_safe_log_replacement(value)}{match.group('suffix')}"
        return match.group(0)

    def replace_user_path(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}<user>"

    sanitized = LOG_FIELD_RE.sub(replace_field, text)
    sanitized = JSON_LOG_FIELD_RE.sub(replace_json_field, sanitized)
    if normalized_level != SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
        sanitized = MAC_ADDRESS_RE.sub(lambda match: _safe_log_replacement(match.group(0)), sanitized)
        sanitized = WINDOWS_HOME_PATH_RE.sub(replace_user_path, sanitized)
        sanitized = POSIX_HOME_PATH_RE.sub(replace_user_path, sanitized)
    if normalized_level == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
        return sanitized

    def replace_provider_identifier(match: re.Match[str]) -> str:
        token = match.group(0)
        # Don't re-fingerprint the field pass's own `<redacted:hash>` markers.
        if token.startswith("redacted:"):
            return token
        return _safe_log_replacement(token)

    return PROVIDER_IDENTIFIER_RE.sub(replace_provider_identifier, sanitized)


def _mask_account_external_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 6:
        return f"{text[:1]}…{text[-2:]}" if len(text) >= 3 else "…"
    return f"{text[:3]}…{text[-3:]}"


def _snapshot_account_external_id(value: Any, *, capture_level: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if normalize_capture_level(capture_level) == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
        return _mask_account_external_id(text)
    return _fingerprint_log_value(text)


def _snapshot_display_name(value: Any, *, capture_level: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if normalize_capture_level(capture_level) == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
        return text
    return "<redacted>"


def _diagnostic_job_message_fields(
    value: Any,
    *,
    error_field: str = "last_error",
) -> dict[str, Any]:
    if str(value or "").strip() == TRANSACTION_JOB_RESTART_RECOVERY_MESSAGE:
        return {
            error_field: None,
            "recovery_message": TRANSACTION_JOB_RESTART_RECOVERY_MESSAGE,
        }
    return {error_field: value}


def _job_row_to_dict(row: TransactionImportJob) -> dict[str, Any]:
    return {
        "id": int(row.id) if row.id is not None else None,
        "job_id": row.job_id,
        "provider": row.provider,
        "institution_id": int(row.institution_id) if row.institution_id is not None else None,
        "status": row.status,
        "reason": row.reason,
        "sync_id": row.sync_id,
        "source_sync_id": row.source_sync_id,
        "attempt_id": row.attempt_id,
        **_diagnostic_job_message_fields(row.last_error),
        "last_progress_at": _isoformat_optional(row.last_progress_at),
        "last_progress_label": row.last_progress_label,
        "stale_recovery_count": int(row.stale_recovery_count or 0),
        "lease_token_set": bool(row.lease_token),
        "created_at": _isoformat_optional(row.created_at),
        "started_at": _isoformat_optional(row.started_at),
        "finished_at": _isoformat_optional(row.finished_at),
        "updated_at": _isoformat_optional(row.updated_at),
    }


def _window_row_to_dict(
    row: TransactionImportWindow,
    *,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> dict[str, Any]:
    return {
        "id": int(row.id) if row.id is not None else None,
        "provider": row.provider,
        "account_external_id_mask": _snapshot_account_external_id(
            row.account_external_id,
            capture_level=capture_level,
        ),
        "mode": row.mode,
        "window_start_date": row.window_start_date,
        "window_end_date": row.window_end_date,
        "status": row.status,
        "transaction_count": int(row.transaction_count or 0),
        "attempt_count": int(row.attempt_count or 0),
        "sync_id": row.sync_id,
        "last_error": row.last_error,
        "started_at": _isoformat_optional(row.started_at),
        "completed_at": _isoformat_optional(row.completed_at),
        "updated_at": _isoformat_optional(row.updated_at),
    }


def _state_row_to_dict(
    row: TransactionImportAccountState,
    *,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> dict[str, Any]:
    return {
        "id": int(row.id) if row.id is not None else None,
        "provider": row.provider,
        "account_external_id_mask": _snapshot_account_external_id(
            row.account_external_id,
            capture_level=capture_level,
        ),
        "account_type": row.account_type,
        "is_liability": bool(row.is_liability),
        "backfill_status": row.backfill_status,
        "target_start_date": row.target_start_date,
        "target_end_date": row.target_end_date,
        "backfill_completed_at": _isoformat_optional(row.backfill_completed_at),
        "incremental_start_date": row.incremental_start_date,
        "incremental_end_date": row.incremental_end_date,
        "last_successful_window_start_date": row.last_successful_window_start_date,
        "last_successful_window_end_date": row.last_successful_window_end_date,
        "last_successful_fetch_at": _isoformat_optional(row.last_successful_fetch_at),
        "last_error": row.last_error,
        "updated_at": _isoformat_optional(row.updated_at),
    }


def _institution_row_to_dict(
    row: Institution,
    *,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
    sync_status_override: str | None = None,
) -> dict[str, Any]:
    persisted_sync_status = str(row.sync_status or "") or None
    authoritative_sync_status = str(sync_status_override or "").strip() or None
    payload = {
        "id": int(row.id) if row.id is not None else None,
        "name": _snapshot_display_name(row.name, capture_level=capture_level),
        "type": row.type,
        "provider": row.provider,
        "enabled": bool(row.enabled),
        "hidden": bool(row.hidden),
        "sync_status": authoritative_sync_status or persisted_sync_status,
        "sync_status_source": (
            "terminal_attempt_result"
            if authoritative_sync_status
            else "persisted_current_state"
        ),
        "created_at": _isoformat_optional(row.created_at),
    }
    if authoritative_sync_status and authoritative_sync_status != persisted_sync_status:
        payload["persisted_sync_status_at_capture"] = persisted_sync_status
    return payload


def _account_row_to_dict(
    row: Account,
    *,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> dict[str, Any]:
    return {
        "id": int(row.id) if row.id is not None else None,
        "institution_id": int(row.institution_id) if row.institution_id is not None else None,
        "external_id_mask": _snapshot_account_external_id(
            row.external_id,
            capture_level=capture_level,
        ),
        "name": _snapshot_display_name(row.name, capture_level=capture_level),
        "account_type": row.account_type,
        "currency": row.currency,
        "is_liability": bool(row.is_liability),
        "hidden": bool(row.hidden),
        "last_synced": _isoformat_optional(row.last_synced),
    }


async def _collect_state_snapshots(
    user_id: int,
    provider: str,
    *,
    sync_id: str | None,
    attempt_id: str | None = None,
    job_id: str | None = None,
    institution_id: int | None = None,
    result_status: str | None = None,
    initiation_source: str | None = None,
    since: datetime,
    until: datetime,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> dict[str, dict[str, Any]]:
    normalized_provider = _normalize_provider(provider)
    normalized_capture_level = normalize_capture_level(capture_level)
    normalized_sync_id = str(sync_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    normalized_job_id = str(job_id or "").strip()
    normalized_result_status = str(result_status or "").strip().lower()
    normalized_initiation_source = str(initiation_source or "").strip().lower()
    snapshots: dict[str, dict[str, Any]] = {}

    try:
        async with async_session() as db:
            job_scope_filters = []
            if normalized_sync_id:
                job_scope_filters.append(
                    or_(
                        TransactionImportJob.sync_id == normalized_sync_id,
                        TransactionImportJob.source_sync_id == normalized_sync_id,
                    )
                )
            elif normalized_job_id:
                job_scope_filters.append(TransactionImportJob.job_id == normalized_job_id)
            job_rows: list[TransactionImportJob] = []
            if job_scope_filters:
                job_rows = (
                    await db.execute(
                        select(TransactionImportJob)
                        .where(
                            TransactionImportJob.user_id == user_id,
                            TransactionImportJob.provider == normalized_provider,
                            or_(*job_scope_filters),
                        )
                        .order_by(TransactionImportJob.updated_at.desc())
                        .limit(50)
                    )
                ).scalars().all()
            window_rows: list[TransactionImportWindow] = []
            if normalized_sync_id:
                window_rows = (
                    await db.execute(
                        select(TransactionImportWindow)
                        .where(
                            TransactionImportWindow.user_id == user_id,
                            TransactionImportWindow.provider == normalized_provider,
                            TransactionImportWindow.sync_id == normalized_sync_id,
                        )
                        .order_by(TransactionImportWindow.updated_at.desc())
                        .limit(200)
                    )
                ).scalars().all()
            state_rows = (
                await db.execute(
                    select(TransactionImportAccountState)
                    .where(
                        TransactionImportAccountState.user_id == user_id,
                        TransactionImportAccountState.provider == normalized_provider,
                    )
                    .order_by(TransactionImportAccountState.updated_at.desc())
                )
            ).scalars().all()
            institution_rows = (
                await db.execute(
                    select(Institution)
                    .where(
                        Institution.user_id == user_id,
                        Institution.provider == normalized_provider,
                    )
                    .order_by(Institution.hidden.asc(), Institution.id.desc())
                )
            ).scalars().all()
            account_rows: list[Account] = []
            institution_ids = [int(row.id) for row in institution_rows]
            if institution_ids:
                account_rows = (
                    await db.execute(
                        select(Account)
                        .where(
                            Account.user_id == user_id,
                            Account.institution_id.in_(institution_ids),
                        )
                        .order_by(Account.institution_id.asc(), Account.id.asc())
                    )
                ).scalars().all()
    except Exception as exc:
        snapshots["collection_errors.json"] = {
            "error": f"{type(exc).__name__}: {exc}",
        }
        return snapshots

    attempt_scope = {
        "scope_kind": "sync_attempt",
        "attempt_sync_id": normalized_sync_id or None,
        "attempt_id": normalized_attempt_id or None,
        "provider": normalized_provider,
        "same_sync_id_sibling_phases_included": True,
        "provider_history_included": False,
        "initiation_source": normalized_initiation_source or None,
    }
    snapshots["jobs.json"] = {
        "schema_version": 2,
        **attempt_scope,
        "user_id": user_id,
        "window_since": _format_utc_timestamp(since),
        "window_until": _format_utc_timestamp(until),
        "matching_rule": (
            "sync_id or source_sync_id equals attempt_sync_id; "
            "an explicit job_id is used only when the archive has no sync ID"
        ),
        "jobs": [_job_row_to_dict(row) for row in job_rows],
    }
    snapshots["windows.json"] = {
        "schema_version": 2,
        **attempt_scope,
        "user_id": user_id,
        "matching_rule": "sync_id equals attempt_sync_id",
        "windows": [
            _window_row_to_dict(row, capture_level=normalized_capture_level)
            for row in window_rows
        ],
    }
    snapshots["institution_snapshot.json"] = {
        "schema_version": 2,
        "scope_kind": "current_app_state",
        "historical_attempt_evidence": False,
        "label": (
            "Attempt result plus institution, account, and transaction-import account state "
            "captured at archive time"
        ),
        "attempt_sync_id_context": normalized_sync_id or None,
        "attempt_id": normalized_attempt_id or None,
        "attempt_result_status": normalized_result_status or None,
        "initiation_source": normalized_initiation_source or None,
        "user_id": user_id,
        "provider": normalized_provider,
        "institutions": [
            _institution_row_to_dict(
                row,
                capture_level=normalized_capture_level,
                sync_status_override=(
                    normalized_result_status
                    if normalized_result_status
                    and institution_id is not None
                    and row.id is not None
                    and int(row.id) == int(institution_id)
                    else None
                ),
            )
            for row in institution_rows
        ],
        "accounts": [
            _account_row_to_dict(row, capture_level=normalized_capture_level)
            for row in account_rows
        ],
        "transaction_import_account_states": [
            _state_row_to_dict(row, capture_level=normalized_capture_level)
            for row in state_rows
        ],
    }

    institution_ids = {
        int(row.id) for row in institution_rows if row.id is not None
    }
    institution_ids.update(
        int(row.institution_id)
        for row in job_rows
        if row.institution_id is not None
    )
    candidate_attempts: list[dict[str, Any]] = []
    seen_attempts: set[tuple[int | None, str]] = set()
    for institution_id in (None, *sorted(institution_ids)):
        attempt = get_provider_sync_attempt(
            user_id,
            normalized_provider,
            institution_id=institution_id,
        )
        if not attempt or str(attempt.get("sync_id") or "") != normalized_sync_id:
            continue
        key = (institution_id, str(attempt.get("sync_id") or ""))
        if key in seen_attempts:
            continue
        seen_attempts.add(key)
        candidate_attempts.append({"institution_id": institution_id, **attempt})

    relevant_institution_ids = {
        int(item["institution_id"])
        for item in candidate_attempts
        if item.get("institution_id") is not None
    }
    relevant_institution_ids.update(
        int(row.institution_id)
        for row in job_rows
        if row.institution_id is not None and str(row.status or "") in {"queued", "running"}
    )
    filtered_locks = {
        key: value
        for key, value in sync_lock_state_snapshot().items()
        if str(value.get("user_id") or "") == str(user_id)
        and _normalize_provider(value.get("provider")) == normalized_provider
        and bool(relevant_institution_ids)
        and int(value.get("institution_id") or 0) in relevant_institution_ids
    }
    scoped_job_ids = {
        str(row.job_id)
        for row in job_rows
        if row.job_id and str(row.status or "") in {"queued", "running"}
    }
    filtered_tasks = {
        task_job_id: value
        for task_job_id, value in transaction_import_task_snapshot().items()
        if task_job_id in scoped_job_ids
    }
    snapshots["attempts.json"] = {
        "schema_version": 2,
        **attempt_scope,
        "user_id": user_id,
        "matching_rule": "active in-memory state is included only when it can be tied to this attempt",
        "in_memory_sync_attempt": candidate_attempts[0] if candidate_attempts else None,
        "in_memory_sync_attempts": candidate_attempts,
        "active_sync_id": normalized_sync_id if candidate_attempts else None,
        "sync_locks": filtered_locks,
        "live_tximport_tasks": filtered_tasks,
    }
    return snapshots
