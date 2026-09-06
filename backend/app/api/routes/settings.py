import asyncio
from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import CurrentUser, get_current_user, get_db
from app.auth import get_auth_principal
from app.launch_auth import AuthPrincipal, require_runner_binding
from app.models import Account, FxRate, Institution, Setting, TransactionImportJob
from app.provider_catalog import (
    get_desktop_visible_auth_metadata,
    iter_api_credential_setting_keys,
    get_runtime_state_metadata,
    get_setting_invalidation_provider,
    iter_desktop_visible_auth_providers,
    iter_sync_route_providers,
)
from app.scrapers.scraper_logging import log_scraper_event
from app.services.currency import get_fx_rates, get_cached_rates_entry
from app.services.institution_cleanup import invalidate_provider_auth_state
from app.services.market_data_settings import (
    MARKET_DATA_API_KEY_SETTING,
    MARKET_DATA_CONFIG_REVISION_SETTING,
    MARKET_DATA_MODE_SETTING,
    MARKET_DATA_PROVIDER_SETTING,
    delete_user_market_data_settings,
    get_market_data_key_metadata,
    normalize_market_data_provider,
    save_user_market_data_settings,
)
from app.services.holdings_sector_enrichment import enrich_existing_user_holdings_with_sectors
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.sync_tracking import (
    ensure_provider_sync_attempt,
    finalize_provider_sync_attempt,
    list_user_sync_attempt_ids,
)
from app.services.transaction_import_jobs import transaction_import_task_snapshot
from app.services.support_auto_archive import (
    SupportArchiveLimitError,
    archive_run_fire_and_forget,
    get_run,
    list_runs,
    package_all_recent_runs,
    package_run_zip,
    resolve_support_archive,
)
from app.services.connection_auth_storage import (
    ApiCredentialReplacementExpiredError,
    begin_api_credential_replacement,
    ProviderAlreadyConnectedError,
    delete_scraper_credentials,
    ensure_api_credentials_institution,
    get_api_credentials,
    get_scraper_credentials,
    upsert_api_credentials,
    is_scraper_username_placeholder,
    recover_expired_api_credential_replacements,
    settle_api_credential_replacement,
    upsert_scraper_credentials,
)
from app.services.user_utils import (
    DEFAULT_USER_TIMEZONE,
    normalize_user_timezone,
)
HIDDEN_SETTINGS_KEYS = {
    MARKET_DATA_API_KEY_SETTING,
    MARKET_DATA_CONFIG_REVISION_SETTING,
    MARKET_DATA_MODE_SETTING,
    MARKET_DATA_PROVIDER_SETTING,
}
CONTROL_SETTINGS_KEYS = {"add_flow", "institution_id"}
GENERAL_SETTINGS_KEYS = frozenset({"primary_currency", "user_timezone", "user_time_format"})
SUPPORTED_PRIMARY_CURRENCIES = frozenset({"CAD", "USD", "EUR", "GBP", "CHF", "CZK", "BTC", "ETH"})
MAX_SETTING_VALUE_LENGTH = 16_384
SCRAPER_CREDENTIAL_REQUEST_KEYS = frozenset(
    {"username", "password", "institution_id", "preserve_pending_session"}
)
DEFAULT_USER_TIME_FORMAT = "24h"
USER_TIME_FORMAT_SETTING = "user_time_format"
USER_TIME_FORMATS = frozenset({"12h", "24h"})
CLIENT_EVENT_SUPPORT_LOG_FINAL_RESULTS = frozenset({
    "ok",
    "succeeded",
    "error",
    "failed",
    "cancelled",
})
CLIENT_EVENT_SUPPORT_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})
CLIENT_EVENT_SUPPORT_LOG_SOURCES = frozenset({"desktop_visible_auth"})
CLIENT_EVENT_SUPPORT_DETAIL_FIELD_LIMIT = 80
CLIENT_EVENT_SUPPORT_DETAIL_VALUE_LIMIT = 240
CLIENT_EVENT_SUPPORT_RESERVED_DETAIL_KEYS = frozenset(
    {
        "provider",
        "user_id",
        "sync_id",
        "source",
        "stage",
        "result",
        "level",
        "debug",
        "flow",
        "add_flow",
        "attempt_id",
        "request_status",
        "message",
        "last_output",
    }
)


def _support_logging_providers() -> frozenset[str]:
    return frozenset(iter_sync_route_providers())


def _desktop_visible_auth_support_log_providers() -> frozenset[str]:
    return frozenset(iter_desktop_visible_auth_providers())

router = APIRouter()
logger = logging.getLogger("breaktwenty.settings")


def normalize_user_time_format(value) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in USER_TIME_FORMATS else DEFAULT_USER_TIME_FORMAT


def _validate_settings_payload(settings: dict) -> dict:
    """Validate the generic settings surface before any database side effects."""
    if not isinstance(settings, dict):
        raise HTTPException(status_code=422, detail="Settings payload must be an object")

    credential_key_providers = {
        key: provider
        for provider, keys in iter_api_credential_setting_keys(include_transient=False)
        for key in keys
        if get_setting_invalidation_provider(key)
    }
    allowed_keys = GENERAL_SETTINGS_KEYS | CONTROL_SETTINGS_KEYS | credential_key_providers.keys()
    invalid_keys = sorted(
        str(key)
        for key in settings
        if not isinstance(key, str) or key not in allowed_keys
    )
    reserved_keys = sorted(str(key) for key in settings if key in HIDDEN_SETTINGS_KEYS)
    if reserved_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Reserved settings must use their dedicated endpoint: {', '.join(reserved_keys)}",
        )
    if invalid_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported settings keys: {', '.join(invalid_keys)}",
        )

    normalized = dict(settings)
    if "add_flow" in normalized and not isinstance(normalized["add_flow"], bool):
        raise HTTPException(status_code=422, detail="add_flow must be a boolean")
    if "institution_id" in normalized:
        institution_id = normalized["institution_id"]
        if isinstance(institution_id, bool) or not isinstance(institution_id, int) or institution_id <= 0:
            raise HTTPException(status_code=422, detail="institution_id must be a positive integer")

    credential_providers = {
        credential_key_providers[key]
        for key in normalized
        if key in credential_key_providers
    }
    if len(credential_providers) > 1:
        raise HTTPException(status_code=422, detail="Credentials for multiple providers cannot be saved together")
    if (CONTROL_SETTINGS_KEYS & normalized.keys()) and not credential_providers:
        raise HTTPException(status_code=422, detail="Connection controls require provider credentials")

    for key in normalized.keys() & credential_key_providers.keys():
        value = normalized[key]
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{key} must be a string")
        if len(value) > MAX_SETTING_VALUE_LENGTH:
            raise HTTPException(status_code=422, detail=f"{key} is too long")

    if "primary_currency" in normalized:
        currency = str(normalized["primary_currency"] or "").strip().upper()
        if currency not in SUPPORTED_PRIMARY_CURRENCIES:
            raise HTTPException(status_code=422, detail="Unsupported primary currency")
        normalized["primary_currency"] = currency
    if "user_timezone" in normalized:
        timezone_name = str(normalized["user_timezone"] or "").strip()
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise HTTPException(status_code=422, detail="Invalid user timezone") from exc
        normalized["user_timezone"] = timezone_name
    if USER_TIME_FORMAT_SETTING in normalized:
        time_format = str(normalized[USER_TIME_FORMAT_SETTING] or "").strip().lower()
        if time_format not in USER_TIME_FORMATS:
            raise HTTPException(status_code=422, detail="user_time_format must be 12h or 24h")
        normalized[USER_TIME_FORMAT_SETTING] = time_format
    return normalized


def _validate_scraper_credential_payload(body: dict) -> tuple[str, str, int, bool]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Credential payload must be an object")
    unknown_keys = sorted(str(key) for key in body if key not in SCRAPER_CREDENTIAL_REQUEST_KEYS)
    if unknown_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported credential fields: {', '.join(unknown_keys)}",
        )
    missing_keys = sorted(key for key in ("username", "password", "institution_id") if key not in body)
    if missing_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Missing credential fields: {', '.join(missing_keys)}",
        )

    username = body["username"]
    password = body["password"]
    for key, value in (("username", username), ("password", password)):
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{key} must be a string")
        if not value:
            raise HTTPException(status_code=422, detail=f"{key} is required")
        if len(value) > MAX_SETTING_VALUE_LENGTH:
            raise HTTPException(status_code=422, detail=f"{key} is too long")

    institution_id = body["institution_id"]
    if (
        type(institution_id) is not int
        or institution_id <= 0
        or institution_id > 2_147_483_647
    ):
        raise HTTPException(status_code=422, detail="institution_id must be a positive integer")

    preserve_pending_session = body.get("preserve_pending_session", False)
    if type(preserve_pending_session) is not bool:
        raise HTTPException(status_code=422, detail="preserve_pending_session must be a boolean")
    return username, password, institution_id, preserve_pending_session


async def _provider_has_institution(db: AsyncSession, user_id: int, provider: str) -> bool:
    result = await db.execute(
        select(Institution.id).where(
            Institution.provider == provider,
            Institution.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


@router.get("/settings")
async def get_settings(
    response: Response,
    institution_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(select(Setting).where(Setting.user_id == current_user.id))
    settings = result.scalars().all()
    api_credential_keys = {
        key
        for _provider, keys in iter_api_credential_setting_keys(include_transient=False)
        for key in keys
    }
    payload = {
        s.key: s.value
        for s in settings
        if s.key not in HIDDEN_SETTINGS_KEYS and s.key not in api_credential_keys
    }
    user_timezone_configured = "user_timezone" in payload
    credential_presence: dict[str, bool] = {}
    credential_values: dict[str, str] = {}
    if institution_id is not None:
        response.headers["Cache-Control"] = "no-store"
        for provider, keys in iter_api_credential_setting_keys(include_transient=False):
            try:
                credentials = await get_api_credentials(
                    db,
                    current_user.id,
                    provider,
                    institution_id=institution_id,
                    setting_keys=keys,
                )
            except ValueError:
                continue
            for key in keys:
                credential_presence[key] = bool(credentials.get(key))
                if key in credentials:
                    credential_values[key] = credentials[key]
    payload["user_timezone"] = normalize_user_timezone(
        payload.get("user_timezone"),
        default=DEFAULT_USER_TIMEZONE,
    )
    payload[USER_TIME_FORMAT_SETTING] = normalize_user_time_format(
        payload.get(USER_TIME_FORMAT_SETTING)
    )
    payload["user_timezone_configured"] = user_timezone_configured
    if institution_id is not None:
        payload["credential_presence"] = credential_presence
        payload.update(credential_values)
    return payload


def _normalize_support_logging_provider(provider: str | None) -> str:
    normalized = str(provider or "").strip().lower()
    return normalized if normalized in _support_logging_providers() else ""


def _normalize_desktop_visible_auth_support_provider(provider: str | None) -> str:
    normalized = _normalize_support_logging_provider(provider)
    if normalized not in _desktop_visible_auth_support_log_providers():
        return ""
    metadata = get_desktop_visible_auth_metadata(normalized)
    return normalized if metadata.get("enabled") and (metadata.get("addFlow") or metadata.get("manualFlow")) else ""


def _normalize_client_event_support_source(source: str | None) -> str:
    normalized = str(source or "").strip().lower()
    return normalized if normalized in CLIENT_EVENT_SUPPORT_LOG_SOURCES else "desktop_visible_auth"


def _normalize_client_event_support_provider(provider: str | None, source: str) -> str:
    if source == "desktop_visible_auth":
        return _normalize_desktop_visible_auth_support_provider(provider)
    return ""


def _client_event_support_logger(provider: str, source: str) -> logging.Logger:
    return logging.getLogger(f"breaktwenty.scrapers.{provider}")


def _normalize_client_event_support_stage(stage: str | None) -> str:
    return " ".join(str(stage or "").strip().lower().split())[:80]


def _normalize_client_event_support_level(level: str | None) -> str:
    normalized = str(level or "").strip().lower()
    return normalized if normalized in CLIENT_EVENT_SUPPORT_LOG_LEVELS else "info"


def _normalize_client_event_support_result(result: str | None) -> str:
    return str(result or "").strip().lower()[:60]


def _normalize_client_event_support_text(value: object, *, limit: int = 320) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[:limit]


def _normalize_client_event_support_detail_key(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    normalized = "".join(ch for ch in normalized if ch.isalnum() or ch == "_")
    return normalized[:CLIENT_EVENT_SUPPORT_DETAIL_FIELD_LIMIT]


def _normalize_client_event_support_details(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    details: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _normalize_client_event_support_detail_key(raw_key)
        if not key or key in CLIENT_EVENT_SUPPORT_RESERVED_DETAIL_KEYS:
            continue
        if raw_value is None or raw_value == "":
            continue
        details[key] = _normalize_client_event_support_text(
            raw_value,
            limit=CLIENT_EVENT_SUPPORT_DETAIL_VALUE_LIMIT,
        )
    return details


def _client_event_support_final_status(result: str) -> str | None:
    if result not in CLIENT_EVENT_SUPPORT_LOG_FINAL_RESULTS:
        return None
    if result in {"ok", "succeeded"}:
        return "ok"
    return "error"


def _client_event_support_archive_trigger(source: str, stage: str, result: str) -> str | None:
    if result not in {"error", "failed", "cancelled"}:
        return None
    if source == "desktop_visible_auth" and stage == "runner finalized":
        return f"visible_auth_{result}"
    return None


@router.post("/settings/support-logs/client-event")
async def log_support_client_event(
    body: dict,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    principal: AuthPrincipal = Depends(get_auth_principal),
):
    source = _normalize_client_event_support_source(body.get("source"))
    provider = _normalize_client_event_support_provider(body.get("provider"), source)
    if not provider:
        return {"status": "error", "message": f"A valid {source.replace('_', '-')} provider is required"}

    stage = _normalize_client_event_support_stage(body.get("stage"))
    if not stage:
        return {"status": "error", "message": "A stage is required"}

    result = _normalize_client_event_support_result(body.get("result"))

    text_limit = 1200

    attempt_id = _normalize_client_event_support_text(body.get("attempt_id"), limit=80)
    if principal.kind == "runner-session":
        require_runner_binding(
            request,
            provider=provider,
            attempt_id=attempt_id,
            add_flow=bool(body.get("add_flow")),
            purpose="visible-auth",
        )
        if source != "desktop_visible_auth":
            raise HTTPException(status_code=403, detail="Runner support-event source mismatch")
    sync_id = ensure_provider_sync_attempt(
        current_user.id,
        provider,
        sync_id=body.get("sync_id"),
    )["sync_id"]
    message = _normalize_client_event_support_text(body.get("message"), limit=text_limit)

    log_scraper_event(
        _client_event_support_logger(provider, source),
        provider=provider,
        stage=stage,
        level=_normalize_client_event_support_level(body.get("level")),
        debug=bool(body.get("debug")),
        user_id=current_user.id,
        sync_id=sync_id,
        source=source,
        flow="add" if bool(body.get("add_flow")) else "existing",
        request_status=_normalize_client_event_support_text(body.get("request_status"), limit=80),
        result=result,
        attempt_id=attempt_id,
        message=message,
        last_output=_normalize_client_event_support_text(body.get("last_output"), limit=text_limit),
        **_normalize_client_event_support_details(body.get("details")),
    )

    final_status = _client_event_support_final_status(result)
    if source == "desktop_visible_auth" and stage != "runner finalized":
        final_status = None
    if final_status:
        finalize_provider_sync_attempt(
            current_user.id,
            provider,
            sync_id=sync_id,
            status=final_status,
        )
    archive_trigger = _client_event_support_archive_trigger(source, stage, result)
    if archive_trigger:
        requested_sync_source = str(body.get("sync_source") or "").strip().lower()
        initiation_source = requested_sync_source if requested_sync_source in {
            "autosync",
            "sync_all",
            "individual_sync",
            "add_connection",
            "manual_reauthentication",
            "scheduled_sync",
        } else (
            "add_connection"
            if bool(body.get("add_flow"))
            else "manual_reauthentication"
        )
        archive_run_fire_and_forget(
            user_id=current_user.id,
            provider=provider,
            sync_id=sync_id,
            attempt_id=attempt_id or None,
            trigger=archive_trigger,
            error=message or None,
            extra_fields={
                "source": source,
                "flow": "add" if bool(body.get("add_flow")) else "existing",
                "attempt_id": attempt_id or None,
                "result": result,
                "result_status": final_status,
                "sync_source": initiation_source,
            },
            diagnostics_settle_seconds=0,
        )

    return {"status": "ok", "sync_id": sync_id}


async def _active_support_sync_ids(db: AsyncSession, user_id: int) -> set[str]:
    active_sync_ids = list_user_sync_attempt_ids(user_id)
    active_task_job_ids = [
        job_id
        for job_id, task_state in transaction_import_task_snapshot().items()
        if not bool(task_state.get("done"))
    ]
    active_job_filter = TransactionImportJob.status.in_(("queued", "running"))
    if active_task_job_ids:
        active_job_filter = or_(
            active_job_filter,
            TransactionImportJob.job_id.in_(active_task_job_ids),
        )
    active_job_rows = (
        await db.execute(
            select(TransactionImportJob.sync_id, TransactionImportJob.source_sync_id).where(
                TransactionImportJob.user_id == user_id,
                active_job_filter,
            )
        )
    ).all()
    for job_sync_id, source_sync_id in active_job_rows:
        active_sync_ids.update(
            value
            for value in (
                str(job_sync_id or "").strip(),
                str(source_sync_id or "").strip(),
            )
            if value
        )
    return active_sync_ids


@router.get("/settings/support-logs/runs")
async def list_support_runs(
    provider: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    normalized_provider = (
        _normalize_support_logging_provider(provider) if provider else None
    )
    runs = await asyncio.to_thread(
        list_runs,
        provider=normalized_provider or None,
        user_id=current_user.id,
    )
    active_sync_ids = await _active_support_sync_ids(db, current_user.id)
    if active_sync_ids:
        runs = [run for run in runs if str(run.get("sync_id") or "").strip() not in active_sync_ids]
    return {"status": "ok", "runs": runs}


@router.post("/settings/support-logs/runs/{provider}/{run_id}/zip")
async def zip_support_run(
    provider: str,
    run_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    normalized_provider = _normalize_support_logging_provider(provider)
    if not normalized_provider:
        return {"status": "error", "message": "A valid provider is required"}
    run = await asyncio.to_thread(
        get_run,
        normalized_provider,
        run_id,
        user_id=current_user.id,
    )
    active_sync_ids = await _active_support_sync_ids(db, current_user.id)
    if run and str(run.get("sync_id") or "").strip() in active_sync_ids:
        return {
            "status": "not_ready",
            "message": "This sync attempt is still finishing. Try again when its timestamp appears.",
        }
    try:
        support_archive = await asyncio.to_thread(
            package_run_zip,
            normalized_provider,
            run_id,
            user_id=current_user.id,
        )
    except SupportArchiveLimitError as exc:
        return exc.response_payload()
    if support_archive is None:
        return {"status": "not_found", "message": "Run not found"}
    return {
        "status": "ok",
        "export_scope": "sync_attempt",
        "archive_id": support_archive.archive_id,
        "archive_filename": support_archive.filename,
    }


@router.get("/settings/support-logs/archives/{archive_id}")
async def download_support_archive(
    archive_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    support_archive = await asyncio.to_thread(
        resolve_support_archive,
        archive_id,
        user_id=current_user.id,
    )
    if support_archive is None:
        raise HTTPException(status_code=404, detail="Support archive not found")
    return FileResponse(
        support_archive.path,
        media_type="application/zip",
        filename=support_archive.filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/settings/dev-diagnostics/capture-level")
async def get_dev_diagnostics_capture_level(
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.support_logging import get_user_capture_level
    return {"status": "ok", **get_user_capture_level(current_user.id)}


@router.put("/settings/dev-diagnostics/capture-level")
async def set_dev_diagnostics_capture_level(
    body: dict | None = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    from app.services.support_logging import (
        DEFAULT_SUPPORT_CAPTURE_LEVEL,
        SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        set_support_capture_level,
    )
    payload = body if isinstance(body, dict) else {}
    raw_level = str(payload.get("capture_level") or "").strip().lower()
    # Only developer_local enables Dev capture; any other value (including the
    # baseline name or "off") reverts to redacted_rich.
    target_level = (
        SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL
        if raw_level == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL
        else DEFAULT_SUPPORT_CAPTURE_LEVEL
    )
    result = set_support_capture_level(
        current_user.id,
        target_level,
    )
    return {"status": "ok", **result}


@router.post("/settings/support-logs/runs/export-recent")
async def export_recent_support_runs(
    body: dict | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    minutes_value = (body or {}).get("minutes") if isinstance(body, dict) else None
    try:
        minutes = int(minutes_value) if minutes_value is not None else 60
    except (TypeError, ValueError):
        minutes = 60
    minutes = max(5, min(minutes, 24 * 60))
    active_sync_ids = await _active_support_sync_ids(db, current_user.id)
    try:
        support_archive, included, export_summary = await package_all_recent_runs(
            user_id=current_user.id,
            since_minutes=minutes,
            excluded_sync_ids=active_sync_ids,
        )
    except SupportArchiveLimitError as exc:
        return {**exc.response_payload(), "minutes": minutes}
    if support_archive is None:
        return {"status": "empty", "message": "No recent diagnostic snapshots found.", "minutes": minutes}
    return {
        "status": "ok",
        "export_scope": "all_providers_recent",
        "archive_id": support_archive.archive_id,
        "archive_filename": support_archive.filename,
        "minutes": minutes,
        "included": included,
        "run_count": len(included),
        **export_summary,
    }


@router.post("/settings")
async def save_settings(
    settings: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    settings = _validate_settings_payload(settings)
    await recover_expired_api_credential_replacements(db, user_id=current_user.id)
    providers_to_invalidate = set()
    user_timezone_changed = False
    primary_currency_changed = False
    add_flow = bool(settings.get("add_flow"))
    requested_institution_id = settings.get("institution_id")
    institution_id = int(requested_institution_id) if requested_institution_id else None
    saved_connection_ids: dict[str, int] = {}
    credential_replacement_id: str | None = None
    api_credentials_by_provider: dict[str, dict[str, str]] = {}
    for key, value in settings.items():
        if key in CONTROL_SETTINGS_KEYS:
            continue
        provider = get_setting_invalidation_provider(key)
        if provider:
            api_credentials_by_provider.setdefault(provider, {})[key] = str(value)
            continue

        result = await db.execute(
            select(Setting).where(Setting.key == key, Setting.user_id == current_user.id)
        )
        existing = result.scalar_one_or_none()
        if key == "user_timezone":
            new_value = normalize_user_timezone(value, default=DEFAULT_USER_TIMEZONE)
        elif key == USER_TIME_FORMAT_SETTING:
            new_value = normalize_user_time_format(value)
        else:
            new_value = str(value)
        if existing and existing.value == new_value:
            continue
        if key == "user_timezone":
            user_timezone_changed = True
        if key == "primary_currency":
            primary_currency_changed = True
        if existing:
            existing.value = new_value
        else:
            db.add(Setting(user_id=current_user.id, key=key, value=new_value))

    if len(api_credentials_by_provider) > 1:
        raise HTTPException(
            status_code=422,
            detail="Update credentials for one provider at a time",
        )
    for provider, credentials in api_credentials_by_provider.items():
        try:
            connection_id = await ensure_api_credentials_institution(
                db,
                current_user.id,
                provider,
                pending_add=add_flow,
                institution_id=institution_id,
            )
        except ProviderAlreadyConnectedError as exc:
            await db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if connection_id is None:
            raise ValueError("Could not resolve the institution connection for these credentials.")
        saved_connection_ids[provider] = connection_id
        existing = await get_api_credentials(
            db,
            current_user.id,
            provider,
            institution_id=connection_id,
        )
        if any(existing.get(key) != value for key, value in credentials.items()):
            providers_to_invalidate.add(provider)
            if not add_flow:
                try:
                    credential_replacement_id = await begin_api_credential_replacement(
                        db,
                        current_user.id,
                        provider,
                        institution_id=connection_id,
                        previous_credentials=existing,
                        candidate_credentials={**existing, **credentials},
                    )
                except ValueError as exc:
                    await db.rollback()
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        await upsert_api_credentials(
            db,
            current_user.id,
            provider,
            credentials,
            institution_id=connection_id,
            ensure_institution=True,
            pending_add=add_flow,
        )

    for provider in providers_to_invalidate:
        await invalidate_provider_auth_state(
            db,
            current_user.id,
            provider,
            institution_id=saved_connection_ids.get(provider),
        )
    await db.commit()
    if user_timezone_changed:
        try:
            from app.services.scheduler import start_scheduler

            await start_scheduler()
        except Exception as exc:
            logger.warning(
                "Failed to refresh scheduler after timezone change for user %s: %s",
                current_user.id,
                exc,
            )
    if primary_currency_changed:
        # Backfill historical FX for the new primary currency in the background so
        # past balances/flows convert at real historical rates, not today's rate.
        try:
            from app.services.fx_history import run_fx_daily_refresh
            from app.services.task_supervisor import create_tracked_task

            create_tracked_task(
                run_fx_daily_refresh(),
                name=f"primary-currency-fx-refresh:{current_user.id}",
            )
        except Exception as exc:
            logger.warning(
                "Failed to trigger FX backfill after primary currency change for user %s: %s",
                current_user.id,
                exc,
            )
    return {
        "status": "ok",
        "institution_id": next(iter(saved_connection_ids.values()), institution_id),
        "credential_replacement_id": credential_replacement_id,
    }


@router.post("/settings/credential-replacements/{replacement_id}/{action}")
async def settle_credential_replacement(
    replacement_id: str,
    action: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if action not in {"commit", "rollback"}:
        raise HTTPException(status_code=404, detail="Credential replacement action not found")
    try:
        settled = await settle_api_credential_replacement(
            db,
            current_user.id,
            replacement_id,
            commit=action == "commit",
        )
    except ApiCredentialReplacementExpiredError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not settled:
        raise HTTPException(status_code=404, detail="Credential replacement not found")
    await db.commit()
    return {"status": "ok"}


@router.get("/settings/market-data")
async def get_market_data_settings(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    metadata = await get_market_data_key_metadata(db, current_user.id)
    return {"status": "ok", **metadata}


@router.put("/settings/market-data")
async def save_market_data_settings(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    provider = normalize_market_data_provider(body.get("provider"), fallback="")
    api_key = str(body.get("api_key", "")).strip()
    if not provider:
        return {"status": "error", "message": "A market data provider is required"}
    if not api_key:
        return {"status": "error", "message": "API key is required"}

    await save_user_market_data_settings(db, current_user.id, provider, api_key)
    try:
        sectors_changed = await enrich_existing_user_holdings_with_sectors(db, current_user.id)
        if sectors_changed:
            async with sqlite_write_gate():
                await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "market data holdings enrichment failed user_id=%s error_type=%s",
            current_user.id,
            type(exc).__name__,
        )
    metadata = await get_market_data_key_metadata(db, current_user.id)
    return {"status": "ok", **metadata}


@router.delete("/settings/market-data")
async def delete_market_data_settings(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    await delete_user_market_data_settings(db, current_user.id)
    metadata = await get_market_data_key_metadata(db, current_user.id)
    return {"status": "ok", **metadata}


@router.get("/fx-rates")
async def fx_rates(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    # Get primary currency from settings, default to CAD
    result = await db.execute(
        select(Setting).where(
            Setting.key == "primary_currency",
            Setting.user_id == current_user.id,
        )
    )
    setting = result.scalar_one_or_none()
    base = setting.value if setting and setting.value else "CAD"

    rates = await get_fx_rates(base)

    cached_entry = get_cached_rates_entry(base)
    cached_at = None
    if cached_entry:
        cached_at = datetime.fromtimestamp(cached_entry[0], tz=timezone.utc).isoformat()

    return {"base": base.upper(), "rates": rates, "cached_at": cached_at}


@router.get("/fx-rates/history")
async def fx_rates_history(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Historical daily CAD-per-unit rates for the user's held currencies, so
    the client can convert past balances/flows at their own date. Shape:
    {primary, base: 'CAD', rates: {CUR: {'YYYY-MM-DD': cad_per_unit}}}."""
    setting = (
        await db.execute(
            select(Setting).where(
                Setting.key == "primary_currency",
                Setting.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    primary = (setting.value if setting and setting.value else "CAD").upper()
    currencies = {
        str(c).upper()
        for c in (
            await db.execute(
                select(func.distinct(Account.currency)).where(Account.user_id == current_user.id)
            )
        ).scalars().all()
        if c
    }
    currencies.add(primary)
    currencies.discard("CAD")
    out: dict[str, dict[str, float]] = {}
    if currencies:
        rows = (
            await db.execute(
                select(FxRate.currency, FxRate.date, FxRate.cad_per_unit)
                .where(FxRate.currency.in_(currencies))
                .order_by(FxRate.currency, FxRate.date)
            )
        ).all()
        for row in rows:
            out.setdefault(row.currency, {})[row.date] = float(row.cad_per_unit)
    return {"base": "CAD", "primary": primary, "rates": out}


@router.get("/credentials/{provider}/status")
async def get_credential_status(
    provider: str,
    response: Response,
    institution_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store"
    if not await _provider_has_institution(db, current_user.id, provider):
        return {"status": "not_found"}
    try:
        cred = await get_scraper_credentials(
            db,
            current_user.id,
            provider,
            institution_id=institution_id,
        )
    except ValueError:
        return {"status": "not_found"}
    if not cred:
        return {"status": "not_found"}
    return {
        "status": "ok",
        "has_saved_credentials": True,
        "username": cred.username,
        "password": cred.password,
    }


@router.put("/credentials/{provider}")
async def save_credential(
    provider: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    username, password, institution_id, preserve_pending_session = (
        _validate_scraper_credential_payload(body)
    )
    if not await _provider_has_institution(db, current_user.id, provider):
        return {"status": "error", "message": "Institution not found"}

    if is_scraper_username_placeholder(username):
        return {"status": "error", "message": "Credential username looked like a placeholder and was not saved"}
    cred = await get_scraper_credentials(
        db,
        current_user.id,
        provider,
        institution_id=institution_id,
    )
    changed = True
    if cred:
        changed = cred.username != username or cred.password != password
    if changed:
        # During an in-flight scraper 2FA flow, the frontend may persist newly
        # validated credentials after login starts. Preserve the live pending
        # auth session in that narrow case so push/SMS continuation can finish.
        runtime_metadata = get_runtime_state_metadata(provider)
        await invalidate_provider_auth_state(
            db,
            current_user.id,
            provider,
            preserve_pending_session=preserve_pending_session,
            quarantine_runtime=bool(runtime_metadata.get("credentialChangeQuarantine")) and not preserve_pending_session,
            institution_id=institution_id,
        )
    saved = await upsert_scraper_credentials(
        db,
        current_user.id,
        provider,
        username,
        password,
        institution_id=institution_id,
    )
    if not saved:
        return {"status": "error", "message": "Credentials were not saved"}
    await db.commit()
    return {"status": "ok"}


@router.delete("/credentials/{provider}")
async def delete_credential(
    provider: str,
    institution_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    await invalidate_provider_auth_state(
        db,
        current_user.id,
        provider,
        institution_id=institution_id,
    )
    await delete_scraper_credentials(
        db,
        current_user.id,
        provider,
        institution_id=institution_id,
    )
    await db.commit()
    return {"status": "ok"}
