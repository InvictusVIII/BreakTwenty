from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Setting
from app.services.user_secret_storage import (
    MARKET_DATA_API_KEY_SECRET_KIND,
    delete_user_secret_artifact,
    get_user_secret_artifact,
    upsert_user_secret_artifact,
)

MARKET_DATA_API_KEY_SETTING = "market_data_api_key"
MARKET_DATA_CONFIG_REVISION_SETTING = "market_data_config_revision"
MARKET_DATA_MODE_SETTING = "market_data_mode"
MARKET_DATA_PROVIDER_SETTING = "market_data_provider"

MARKET_DATA_MODE_KEYLESS = "keyless"
MARKET_DATA_MODE_USER_KEY = "user_key"
MARKET_DATA_MODE_APP_DEFAULT = "app_default"
DEFAULT_MARKET_DATA_MODE = MARKET_DATA_MODE_KEYLESS

MARKET_DATA_PROVIDER_FMP = "fmp"
MARKET_DATA_PROVIDER_POLYGON = "polygon_massive"
MARKET_DATA_PROVIDER_TWELVE_DATA = "twelve_data"
DEFAULT_MARKET_DATA_PROVIDER = MARKET_DATA_PROVIDER_FMP

MARKET_DATA_PROVIDER_LABELS = {
    MARKET_DATA_PROVIDER_FMP: "FMP",
    MARKET_DATA_PROVIDER_POLYGON: "Polygon / Massive",
    MARKET_DATA_PROVIDER_TWELVE_DATA: "Twelve Data",
}


def get_supported_market_data_providers() -> list[dict[str, str]]:
    return [
        {"key": MARKET_DATA_PROVIDER_FMP, "label": MARKET_DATA_PROVIDER_LABELS[MARKET_DATA_PROVIDER_FMP]},
        {"key": MARKET_DATA_PROVIDER_POLYGON, "label": MARKET_DATA_PROVIDER_LABELS[MARKET_DATA_PROVIDER_POLYGON]},
        {"key": MARKET_DATA_PROVIDER_TWELVE_DATA, "label": MARKET_DATA_PROVIDER_LABELS[MARKET_DATA_PROVIDER_TWELVE_DATA]},
    ]


def normalize_market_data_provider(value: str | None, fallback: str = DEFAULT_MARKET_DATA_PROVIDER) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in MARKET_DATA_PROVIDER_LABELS:
        return normalized
    return fallback


def normalize_market_data_mode(value: str | None, fallback: str = DEFAULT_MARKET_DATA_MODE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {MARKET_DATA_MODE_KEYLESS, MARKET_DATA_MODE_USER_KEY, MARKET_DATA_MODE_APP_DEFAULT}:
        return normalized
    return fallback


def get_market_data_provider_label(provider: str | None) -> str:
    normalized = normalize_market_data_provider(provider)
    return MARKET_DATA_PROVIDER_LABELS[normalized]


def _get_app_market_data_config() -> tuple[str, str]:
    fmp_key = os.getenv("FMP_API_KEY", "").strip()
    if fmp_key:
        return MARKET_DATA_PROVIDER_FMP, fmp_key

    polygon_key = (
        os.getenv("POLYGON_API_KEY", "").strip()
        or os.getenv("MASSIVE_API_KEY", "").strip()
    )
    if polygon_key:
        return MARKET_DATA_PROVIDER_POLYGON, polygon_key

    twelve_data_key = (
        os.getenv("TWELVE_DATA_API_KEY", "").strip()
        or os.getenv("TWELVEDATA_API_KEY", "").strip()
    )
    if twelve_data_key:
        return MARKET_DATA_PROVIDER_TWELVE_DATA, twelve_data_key

    return DEFAULT_MARKET_DATA_PROVIDER, ""


def mask_market_data_api_key(api_key: str | None) -> str | None:
    value = str(api_key or "").strip()
    if not value:
        return None
    visible_tail = value[-4:] if len(value) > 4 else value
    return f"{'•' * max(4, len(value) - len(visible_tail))}{visible_tail}"


async def _get_user_setting_value(
    db: AsyncSession,
    user_id: int,
    key: str,
) -> str:
    result = await db.execute(
        select(Setting.value).where(
            Setting.user_id == user_id,
            Setting.key == key,
        )
    )
    return (result.scalar_one_or_none() or "").strip()


async def _upsert_user_setting_value(
    db: AsyncSession,
    user_id: int,
    key: str,
    value: str,
) -> None:
    result = await db.execute(
        select(Setting).where(
            Setting.user_id == user_id,
            Setting.key == key,
        )
    )
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(Setting(user_id=user_id, key=key, value=value))
    await db.flush()


async def bump_market_data_config_revision(
    db: AsyncSession,
    user_id: int,
) -> str:
    revision = datetime.now(timezone.utc).isoformat()
    await _upsert_user_setting_value(db, user_id, MARKET_DATA_CONFIG_REVISION_SETTING, revision)
    return revision


async def get_user_market_data_api_key(
    db: AsyncSession,
    user_id: int,
) -> str:
    payload = await get_user_secret_artifact(db, user_id, MARKET_DATA_API_KEY_SECRET_KIND)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("api_key") or "").strip()


async def get_user_market_data_mode(
    db: AsyncSession,
    user_id: int,
) -> str:
    stored_mode = await _get_user_setting_value(db, user_id, MARKET_DATA_MODE_SETTING)
    if stored_mode:
        return normalize_market_data_mode(stored_mode)

    if await get_user_market_data_api_key(db, user_id):
        return MARKET_DATA_MODE_USER_KEY

    _, app_api_key = _get_app_market_data_config()
    if app_api_key:
        return MARKET_DATA_MODE_APP_DEFAULT

    return DEFAULT_MARKET_DATA_MODE


async def get_user_market_data_provider(
    db: AsyncSession,
    user_id: int,
) -> str:
    stored_provider = await _get_user_setting_value(db, user_id, MARKET_DATA_PROVIDER_SETTING)
    if stored_provider:
        return normalize_market_data_provider(stored_provider)

    if await get_user_market_data_api_key(db, user_id):
        return MARKET_DATA_PROVIDER_POLYGON

    return DEFAULT_MARKET_DATA_PROVIDER


async def get_market_data_config_revision(
    db: AsyncSession,
    user_id: int,
) -> str:
    return await _get_user_setting_value(db, user_id, MARKET_DATA_CONFIG_REVISION_SETTING)


async def get_effective_market_data_config(
    db: AsyncSession,
    user_id: int,
) -> dict[str, str]:
    mode = await get_user_market_data_mode(db, user_id)
    provider = await get_user_market_data_provider(db, user_id)
    config_revision = await get_market_data_config_revision(db, user_id)

    if mode == MARKET_DATA_MODE_USER_KEY:
        user_api_key = await get_user_market_data_api_key(db, user_id)
        if not user_api_key:
            return {
                "mode": MARKET_DATA_MODE_KEYLESS,
                "provider": provider,
                "provider_label": get_market_data_provider_label(provider),
                "api_key": "",
                "source": "none",
                "config_revision": config_revision,
            }
        return {
            "mode": mode,
            "provider": provider,
            "provider_label": get_market_data_provider_label(provider),
            "api_key": user_api_key,
            "source": "user",
            "config_revision": config_revision,
        }

    if mode == MARKET_DATA_MODE_APP_DEFAULT:
        app_provider, app_api_key = _get_app_market_data_config()
        if app_api_key:
            return {
                "mode": mode,
                "provider": app_provider,
                "provider_label": get_market_data_provider_label(app_provider),
                "api_key": app_api_key,
                "source": "app",
                "config_revision": config_revision,
            }

    return {
        "mode": MARKET_DATA_MODE_KEYLESS,
        "provider": provider,
        "provider_label": get_market_data_provider_label(provider),
        "api_key": "",
        "source": "none",
        "config_revision": config_revision,
    }


async def get_market_data_key_metadata(
    db: AsyncSession,
    user_id: int,
) -> dict:
    config = await get_effective_market_data_config(db, user_id)
    return {
        "mode": config["mode"],
        "provider": config["provider"],
        "provider_label": config["provider_label"],
        "available_providers": get_supported_market_data_providers(),
        "has_key": bool(config["api_key"]),
        "masked_key": mask_market_data_api_key(config["api_key"]),
        "source": config["source"],
        "config_revision": config["config_revision"],
    }


async def save_user_market_data_settings(
    db: AsyncSession,
    user_id: int,
    provider: str,
    api_key: str,
) -> None:
    await _upsert_user_setting_value(db, user_id, MARKET_DATA_PROVIDER_SETTING, provider)
    await _upsert_user_setting_value(db, user_id, MARKET_DATA_MODE_SETTING, MARKET_DATA_MODE_USER_KEY)
    await upsert_user_secret_artifact(
        db,
        user_id,
        MARKET_DATA_API_KEY_SECRET_KIND,
        {"api_key": api_key},
    )
    await bump_market_data_config_revision(db, user_id)
    await db.commit()


async def delete_user_market_data_settings(
    db: AsyncSession,
    user_id: int,
) -> None:
    await delete_user_secret_artifact(db, user_id, MARKET_DATA_API_KEY_SECRET_KIND)
    provider = await get_user_market_data_provider(db, user_id)
    await _upsert_user_setting_value(db, user_id, MARKET_DATA_PROVIDER_SETTING, provider)
    await _upsert_user_setting_value(db, user_id, MARKET_DATA_MODE_SETTING, MARKET_DATA_MODE_KEYLESS)
    await bump_market_data_config_revision(db, user_id)
    await db.commit()
