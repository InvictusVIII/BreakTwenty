import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.database import async_session
from app.models import Institution
from app.provider_catalog import (
    get_error_status_for_provider,
    iter_nightly_providers,
)
from app.services.sync_utils import (
    get_persisted_sync_status,
)
from app.services.fx_history import run_fx_daily_refresh
from app.services.task_supervisor import create_tracked_task
from app.services.user_utils import ensure_default_dev_user_exists, list_scheduler_users

logger = logging.getLogger("breaktwenty.scheduler")

scheduler = AsyncIOScheduler()


def _scheduler_job_id(user_id: int) -> str:
    return f"nightly_sync_user_{user_id}"


def nightly_provider_sync_enabled() -> bool:
    raw = os.getenv("BREAKTWENTY_ENABLE_NIGHTLY_SYNC", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _persist_user_sync_statuses(user_id: int, results: dict[str, dict]) -> None:
    async with async_session() as db:
        for payload in results.values():
            if str(payload.get("status") or "") == "network_blocked":
                continue
            institution_id = int(payload.get("institution_id") or 0)
            institution = await db.get(Institution, institution_id) if institution_id else None
            if institution is not None and int(institution.user_id) != int(user_id):
                institution = None
            if institution:
                institution.sync_status = get_persisted_sync_status(
                    payload,
                    error_status=get_error_status_for_provider(payload.get("route_provider") or payload.get("provider")),
                )

        await db.commit()


async def nightly_sync_user(user_id: int):
    logger.info("Starting nightly sync for user %s", user_id)
    from app.services.sync_batch import run_sync_batch_now

    nightly_providers = {
        *iter_nightly_providers("api"),
        *iter_nightly_providers("scraper"),
    }
    async with async_session() as db:
        institutions = (
            await db.execute(
                select(Institution).where(
                    Institution.user_id == user_id,
                    Institution.enabled.is_(True),
                    Institution.hidden.is_(False),
                    Institution.provider.in_(nightly_providers),
                )
            )
        ).scalars().all()
    connections = [
        {"provider": institution.provider, "institution_id": institution.id}
        for institution in institutions
    ]
    results = await run_sync_batch_now(user_id, connections, mode="nightly")

    try:
        await _persist_user_sync_statuses(user_id, results)
    except Exception as e:
        logger.error("Failed to save nightly sync statuses for user %s: %s", user_id, e)

    logger.info("Nightly sync complete for user %s: %s", user_id, results)
    return results


async def start_scheduler():
    await ensure_default_dev_user_exists()
    scheduled_users = await list_scheduler_users()

    # One-time, version-gated re-evaluation so code-shipped rulepack updates reach
    # each user's already-imported (non-manual) transactions on app update. No-op
    # when the stored ruleset version already matches; preserves manual tags and
    # user rules. (A hosted multi-user deployment should make this lazy/background
    # instead of a startup sweep.)
    from app.services.categories import ensure_categorization_ruleset_current
    for scheduled_user_id, _tz in scheduled_users:
        try:
            if await ensure_categorization_ruleset_current(scheduled_user_id):
                logger.info(
                    "re-evaluated categorization for user %s after ruleset version bump",
                    scheduled_user_id,
                )
        except Exception as e:
            logger.warning(
                "categorization ruleset refresh failed for user %s: %s",
                scheduled_user_id,
                e,
            )

    if scheduler.running:
        scheduler.remove_all_jobs()

    nightly_enabled = nightly_provider_sync_enabled()
    if nightly_enabled:
        for user_id, timezone_name in scheduled_users:
            scheduler.add_job(
                nightly_sync_user,
                trigger=CronTrigger(hour=2, minute=0, timezone=timezone_name),
                args=[user_id],
                id=_scheduler_job_id(user_id),
                name=f"Nightly sync user {user_id}",
                replace_existing=True,
            )

    # Global daily historical-FX refresh (not user-scoped).
    scheduler.add_job(
        run_fx_daily_refresh,
        trigger=CronTrigger(hour=5, minute=0, timezone="UTC"),
        id="fx_rates_daily_refresh",
        name="Daily FX rates refresh",
        replace_existing=True,
    )

    if not scheduler.running:
        scheduler.start()

    # Kick off an initial backfill in the background so historical rates are
    # available without blocking startup.
    create_tracked_task(run_fx_daily_refresh(), name="initial-fx-refresh")

    logger.info(
        "Scheduler started — nightly provider sync %s; scheduler users: %s",
        "enabled" if nightly_enabled else "disabled",
        ", ".join(f"{user_id}@{timezone_name}" for user_id, timezone_name in scheduled_users),
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
