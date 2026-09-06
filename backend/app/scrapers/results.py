"""Shared result helpers for scraper module entrypoints."""

from __future__ import annotations

from typing import Any, Literal

ScraperStatus = Literal["ok", "auth_required", "network_error", "error"]

SCRAPER_STATUSES: set[str] = {"ok", "auth_required", "network_error", "error"}
SCRAPER_RESULT_CORE_KEYS = {"status", "accounts", "holdings", "transactions", "message"}


def scraper_result(
    status: ScraperStatus,
    *,
    accounts: list[dict[str, Any]] | None = None,
    holdings: Any = None,
    transactions: Any = None,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if status not in SCRAPER_STATUSES:
        raise ValueError(f"Unsupported scraper result status: {status}")
    payload: dict[str, Any] = {
        "status": status,
        "accounts": accounts or [],
        "holdings": {} if holdings is None else holdings,
        "transactions": {} if transactions is None else transactions,
        "message": message,
    }
    payload.update(extra)
    return payload


def scraper_ok(
    accounts: list[dict[str, Any]] | None = None,
    *,
    holdings: Any = None,
    transactions: Any = None,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return scraper_result(
        "ok",
        accounts=accounts,
        holdings=holdings,
        transactions=transactions,
        message=message,
        **extra,
    )


def scraper_auth_required(message: str | None = None, **extra: Any) -> dict[str, Any]:
    return scraper_result("auth_required", message=message, **extra)


def scraper_network_error(message: str | None = "Connection failed", **extra: Any) -> dict[str, Any]:
    return scraper_result("network_error", message=message, **extra)


def scraper_error(message: str | None = "Sync failed", **extra: Any) -> dict[str, Any]:
    return scraper_result("error", message=message, **extra)


def normalize_scraper_result(
    payload: Any,
    *,
    auth_message: str | None = None,
    unexpected_message: str = "Unexpected scraper response",
) -> dict[str, Any]:
    if payload is None:
        return scraper_auth_required(auth_message)
    if isinstance(payload, list):
        return scraper_ok(accounts=payload)
    if not isinstance(payload, dict):
        return scraper_error(unexpected_message)

    status = payload.get("status")
    if status not in SCRAPER_STATUSES:
        return scraper_error(str(payload.get("message") or unexpected_message))

    extra = {key: value for key, value in payload.items() if key not in SCRAPER_RESULT_CORE_KEYS}
    return scraper_result(
        status,
        accounts=payload.get("accounts") or [],
        holdings=payload.get("holdings"),
        transactions=payload.get("transactions"),
        message=payload.get("message"),
        **extra,
    )
