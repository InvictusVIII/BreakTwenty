import asyncio
import faulthandler
import logging
import os
import signal
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.brand import APP_BRAND_NAME
from app.database import close_database_connections, verify_database_connection
from app.logging_setup import configure_breaktwenty_logging
from app.launch_auth import LaunchAuthMiddleware
from app.api.routes import router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.connection_auth_storage import (
    recover_api_credential_replacements,
    start_api_credential_replacement_watchdog,
    stop_api_credential_replacement_watchdog,
)
from app.services.event_bus import recover_expired_event_stream_leases
from app.services.market_strip import start_market_strip_sampler, stop_market_strip_sampler
from app.services.runtime_service_leases import recover_expired_runtime_service_leases
from app.services.sync_batch import (
    resume_sync_batch_jobs,
    start_sync_batch_watchdog,
    stop_sync_batch_watchdog,
)
from app.services.sync_utils import recover_expired_sync_leases, start_sync_lease_watchdog
from app.services.task_supervisor import start_task_supervisor, stop_task_supervisor
from app.services.startup_health import (
    mark_startup_component_failed,
    mark_startup_component_ok,
    reset_startup_health,
)
from app.services.transaction_import_jobs import (
    resume_transaction_import_jobs,
    start_transaction_import_watchdog,
    stop_transaction_import_watchdog,
)

configure_breaktwenty_logging()
logger = logging.getLogger("breaktwenty.app")

DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:32100",
    "http://127.0.0.1:32100",
)
LIFESPAN_SERVICE_STOP_TIMEOUT_SECONDS = 5.0
RECOVERY_RESTART = os.getenv("BREAKTWENTY_RECOVERY_RESTART", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _enable_embedded_stack_dump_signal() -> None:
    dump_signal = getattr(signal, "SIGUSR2", None)
    if dump_signal is None or os.getenv("BREAKTWENTY_BACKEND_MODE") != "embedded":
        return
    try:
        faulthandler.register(dump_signal, all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("backend stack-dump signal unavailable error_type=%s", type(exc).__name__)


_enable_embedded_stack_dump_signal()


async def _stop_lifespan_service(
    name: str,
    stop_callback,
    *,
    timeout_seconds: float = LIFESPAN_SERVICE_STOP_TIMEOUT_SECONDS,
) -> None:
    try:
        async with asyncio.timeout(max(0.0, timeout_seconds)):
            await stop_callback()
    except TimeoutError:
        logger.error("backend service shutdown deadline exceeded service=%s", name)
    except Exception as exc:
        logger.error(
            "backend service shutdown failed service=%s error_type=%s",
            name,
            type(exc).__name__,
        )


def get_cors_origins() -> list[str]:
    configured_origins = []
    for value in os.getenv("BREAKTWENTY_CORS_ALLOW_ORIGINS", "").split(","):
        origin = value.strip()
        if not origin:
            continue
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError("BreakTwenty CORS origins must use private local frontend origins") from exc
        if (
            parsed.scheme != "http"
            or str(parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/")
            or port is None
            or port <= 0
        ):
            raise RuntimeError("BreakTwenty CORS origins must use private local frontend origins")
        configured_origins.append(origin.rstrip("/"))
    return list(dict.fromkeys((*DEFAULT_CORS_ORIGINS, *configured_origins)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    reset_startup_health()
    if RECOVERY_RESTART:
        logger.warning("embedded backend controlled recovery startup")
    await verify_database_connection()
    mark_startup_component_ok("database")
    recovered_replacements = await recover_api_credential_replacements()
    if recovered_replacements:
        logger.warning(
            "restored %s interrupted API credential replacement(s)",
            recovered_replacements,
        )
    start_task_supervisor()
    start_api_credential_replacement_watchdog()
    try:
        await recover_expired_sync_leases(force=RECOVERY_RESTART)
        start_sync_lease_watchdog()
        mark_startup_component_ok("sync_lease_recovery")
    except Exception as e:
        mark_startup_component_failed("sync_lease_recovery", e)
        logger.warning("sync lease recovery failed: %s", e)
    try:
        await recover_expired_runtime_service_leases(force=RECOVERY_RESTART)
        await recover_expired_event_stream_leases(force=RECOVERY_RESTART)
        mark_startup_component_ok("runtime_lease_recovery")
    except Exception as e:
        mark_startup_component_failed("runtime_lease_recovery", e)
        logger.warning("runtime lease recovery failed: %s", e)
    try:
        await start_scheduler()
        mark_startup_component_ok("scheduler")
    except Exception as e:
        mark_startup_component_failed("scheduler", e)
        logger.warning("scheduler startup failed: %s", e)
    try:
        start_market_strip_sampler()
        mark_startup_component_ok("market_strip_sampler")
    except Exception as e:
        mark_startup_component_failed("market_strip_sampler", e)
        logger.warning("market strip sampler startup failed: %s", e)
    try:
        await resume_transaction_import_jobs(force_running=RECOVERY_RESTART)
        mark_startup_component_ok("transaction_import_resume")
    except Exception as e:
        mark_startup_component_failed("transaction_import_resume", e)
        logger.warning("transaction import resume failed: %s", e)
    try:
        await resume_sync_batch_jobs(force_running=RECOVERY_RESTART)
        start_sync_batch_watchdog()
        mark_startup_component_ok("sync_batch_resume")
    except Exception as e:
        mark_startup_component_failed("sync_batch_resume", e)
        logger.warning("sync batch resume failed: %s", e)
    try:
        start_transaction_import_watchdog()
        mark_startup_component_ok("transaction_import_watchdog")
    except Exception as e:
        mark_startup_component_failed("transaction_import_watchdog", e)
        logger.warning("transaction import watchdog startup failed: %s", e)
    try:
        yield
    finally:
        stop_scheduler()
        await _stop_lifespan_service(
            "api_credential_replacement_watchdog",
            stop_api_credential_replacement_watchdog,
        )
        await _stop_lifespan_service("market_strip_sampler", stop_market_strip_sampler)
        await _stop_lifespan_service(
            "transaction_import_watchdog",
            stop_transaction_import_watchdog,
        )
        await _stop_lifespan_service("sync_batch_watchdog", stop_sync_batch_watchdog)
        await _stop_lifespan_service(
            "task_supervisor",
            stop_task_supervisor,
            timeout_seconds=25.0,
        )
        await _stop_lifespan_service("database_pool", close_database_connections)


app = FastAPI(
    title=APP_BRAND_NAME,
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Added before CORS so CORSMiddleware remains the outer wrapper and includes CORS
# response headers on authentication failures.
app.add_middleware(LaunchAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Let the browser read server-chosen CSV and support-archive download names
    # cross-origin; otherwise the frontend must fall back to a local name.
    expose_headers=["Content-Disposition"],
)

app.include_router(router)
