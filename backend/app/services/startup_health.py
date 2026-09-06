from __future__ import annotations

from typing import Any

from app.services.time_utils import utc_now_string

STARTUP_COMPONENTS = (
    "database",
    "sync_lease_recovery",
    "runtime_lease_recovery",
    "scheduler",
    "market_strip_sampler",
    "transaction_import_resume",
    "sync_batch_resume",
    "transaction_import_watchdog",
)

_component_state: dict[str, dict[str, Any]] = {}


def reset_startup_health() -> None:
    _component_state.clear()
    for component in STARTUP_COMPONENTS:
        _component_state[component] = {"status": "pending"}


def mark_startup_component_ok(component: str) -> None:
    _component_state[component] = {
        "status": "ok",
        "checked_at": utc_now_string(),
    }


def mark_startup_component_failed(component: str, exc: Exception) -> None:
    _component_state[component] = {
        "status": "failed",
        "checked_at": utc_now_string(),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def get_startup_health() -> dict[str, Any]:
    if not _component_state:
        reset_startup_health()
    components = {
        component: dict(state)
        for component, state in _component_state.items()
    }
    degraded_components = [
        component
        for component, state in components.items()
        if state.get("status") == "failed"
    ]
    pending_components = [
        component
        for component, state in components.items()
        if state.get("status") == "pending"
    ]
    status = "degraded" if degraded_components else "starting" if pending_components else "ok"
    return {
        "status": status,
        "components": components,
        "degraded_components": degraded_components,
    }
