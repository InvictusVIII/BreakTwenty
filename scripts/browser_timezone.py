from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_USER_TIMEZONE = "UTC"


def normalize_browser_timezone(timezone_name: str | None) -> str:
    candidate = str(timezone_name or "").strip() or DEFAULT_USER_TIMEZONE
    try:
        ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError):
        return DEFAULT_USER_TIMEZONE
    return candidate
