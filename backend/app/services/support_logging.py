from __future__ import annotations

import json
import logging
from threading import Lock

from app.runtime_paths import DATA_DIR
from app.services.auth_artifact_utils import normalize_provider as _normalize_provider

# BreakTwenty captures provider diagnostics at one of two levels:
#   redacted_rich   - the always-on default. Structural and numeric sample
#                     values are captured (type/status/currency/amount/date/
#                     mcc/category codes), identifiers are fingerprinted, and
#                     narrative PII (description/merchant/memo/notes) is
#                     redacted. Safe to request from end users for support.
#   developer_local - a local developer toggle. Captures full, unredacted
#                     samples for first-party debugging on the developer's own
#                     machine until explicitly turned off.
SUPPORT_CAPTURE_LEVEL_REDACTED_RICH = "redacted_rich"
SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL = "developer_local"
DEFAULT_SUPPORT_CAPTURE_LEVEL = SUPPORT_CAPTURE_LEVEL_REDACTED_RICH
SUPPORT_CAPTURE_LEVELS = frozenset(
    {
        SUPPORT_CAPTURE_LEVEL_REDACTED_RICH,
        SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
    }
)
WILDCARD_PROVIDER = "*"
SUPPORT_CAPTURE_LEVEL_PATH = DATA_DIR / "support_capture_levels.json"

logger = logging.getLogger("breaktwenty.support_logging")

_support_logging_state: dict[int, dict[str, dict[str, str]]] = {}
_support_logging_lock = Lock()
_support_logging_state_loaded = False


def _load_support_logging_state_locked() -> None:
    global _support_logging_state_loaded
    if _support_logging_state_loaded:
        return
    _support_logging_state_loaded = True
    try:
        with SUPPORT_CAPTURE_LEVEL_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning("support capture level state load failed: %s", exc)
        return
    if not isinstance(payload, dict):
        return
    users = payload.get("users")
    if not isinstance(users, dict):
        return
    loaded: dict[int, dict[str, dict[str, str]]] = {}
    for raw_user_id, providers in users.items():
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(providers, dict):
            continue
        provider_entries: dict[str, dict[str, str]] = {}
        for raw_provider, entry in providers.items():
            provider = _normalize_provider(raw_provider) or WILDCARD_PROVIDER
            if not isinstance(entry, dict):
                continue
            if _state_capture_level(entry) != SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
                continue
            provider_entries[provider] = {
                "capture_level": SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
            }
        if provider_entries:
            loaded[user_id] = provider_entries
    _support_logging_state.clear()
    _support_logging_state.update(loaded)


def _persist_support_logging_state_locked() -> None:
    payload = {
        "version": 1,
        "users": {
            str(user_id): providers
            for user_id, providers in sorted(_support_logging_state.items())
            if providers
        },
    }
    try:
        SUPPORT_CAPTURE_LEVEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = SUPPORT_CAPTURE_LEVEL_PATH.with_suffix(
            SUPPORT_CAPTURE_LEVEL_PATH.suffix + ".tmp"
        )
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.replace(SUPPORT_CAPTURE_LEVEL_PATH)
    except Exception as exc:
        logger.warning("support capture level state persist failed: %s", exc)


def normalize_capture_level(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORT_CAPTURE_LEVELS else DEFAULT_SUPPORT_CAPTURE_LEVEL


def _state_capture_level(entry: dict[str, str] | None) -> str:
    # Stored entries only ever hold the elevated developer_local level; the
    # absence of an entry resolves to the always-on redacted_rich baseline.
    if isinstance(entry, dict):
        return normalize_capture_level(str(entry.get("capture_level") or ""))
    return DEFAULT_SUPPORT_CAPTURE_LEVEL


def _resolve_user_capture_entry(
    user_id: int, normalized_provider: str
) -> dict[str, str] | None:
    providers = _support_logging_state.get(user_id) or {}
    entry = providers.get(normalized_provider)
    if entry is not None:
        return entry
    return providers.get(WILDCARD_PROVIDER)


def set_support_capture_level(
    user_id: int,
    capture_level: str,
    *,
    provider: str | None = None,
) -> dict[str, str]:
    """Turn developer_local capture on or off for a user.

    Only ``developer_local`` is stored (optionally scoped to one provider,
    otherwise a wildcard entry covering all providers). Any other level clears
    the elevation and reverts to the always-on redacted_rich baseline.
    """
    normalized_level = normalize_capture_level(capture_level)
    normalized_provider = _normalize_provider(provider) or WILDCARD_PROVIDER
    with _support_logging_lock:
        _load_support_logging_state_locked()
        providers = _support_logging_state.setdefault(user_id, {})
        if normalized_level != SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
            providers.pop(normalized_provider, None)
            if not providers:
                _support_logging_state.pop(user_id, None)
            _persist_support_logging_state_locked()
            return {
                "capture_level": DEFAULT_SUPPORT_CAPTURE_LEVEL,
                "provider": normalized_provider,
            }
        providers[normalized_provider] = {
            "capture_level": SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        }
        _persist_support_logging_state_locked()
    return {
        "capture_level": SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        "provider": normalized_provider,
    }


def get_support_capture_level(
    provider: str,
    *,
    user_id: int | None = None,
) -> str:
    normalized_provider = _normalize_provider(provider)
    if not normalized_provider:
        return DEFAULT_SUPPORT_CAPTURE_LEVEL

    with _support_logging_lock:
        _load_support_logging_state_locked()
        if user_id is not None:
            return _state_capture_level(_resolve_user_capture_entry(user_id, normalized_provider))
        for providers in _support_logging_state.values():
            entry = providers.get(normalized_provider) or providers.get(WILDCARD_PROVIDER)
            if _state_capture_level(entry) == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL:
                return SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL
    return DEFAULT_SUPPORT_CAPTURE_LEVEL


def get_user_capture_level(user_id: int, *, provider: str | None = None) -> dict[str, str]:
    """Inspect the active capture level for a user (used by the dev toggle UI)."""
    normalized_provider = _normalize_provider(provider) or WILDCARD_PROVIDER
    with _support_logging_lock:
        _load_support_logging_state_locked()
        providers = _support_logging_state.get(user_id) or {}
        entry = providers.get(normalized_provider)
        if entry is None and normalized_provider != WILDCARD_PROVIDER:
            entry = providers.get(WILDCARD_PROVIDER)
    if entry is None:
        return {
            "capture_level": DEFAULT_SUPPORT_CAPTURE_LEVEL,
            "provider": normalized_provider,
        }
    return {
        "capture_level": _state_capture_level(entry),
        "provider": normalized_provider,
    }


def support_capture_is_developer_local(capture_level: str | None) -> bool:
    return normalize_capture_level(capture_level) == SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL


def support_logging_enabled(
    provider: str,
    *,
    user_id: int | None = None,
) -> bool:
    """True only while a developer_local capture window is active.

    Used to gate verbose connector/scraper debug logging so the extra detail
    appears only during a local Dev-capture session, not at the baseline.
    """
    return support_capture_is_developer_local(
        get_support_capture_level(provider, user_id=user_id)
    )
