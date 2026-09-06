from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import threading
from dataclasses import dataclass

from app.brand import APP_BRAND_NAME

DATABASE_KEY_ENV = "BREAKTWENTY_DATABASE_ENCRYPTION_KEY"
APP_ENCRYPTION_KEY_ENV = "BREAKTWENTY_APP_ENCRYPTION_KEY"
KEY_BOOTSTRAP_ENV = "BREAKTWENTY_KEY_BOOTSTRAP"
KEY_BOOTSTRAP_STDIN_V2 = "stdin-v2"
KEY_BYTES = 32
MAX_BOOTSTRAP_BYTES = 4096


@dataclass(frozen=True)
class KeyMaterial:
    database_key: bytes
    app_encryption_key: bytes
    desktop_launch_token: str
    renderer_launch_token: str
    source: str


_key_material: KeyMaterial | None = None
_key_material_lock = threading.Lock()


def _decode_base64_key(value: str, label: str) -> bytes:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"{APP_BRAND_NAME} {label} is empty")
    try:
        decoded = base64.b64decode(
            text.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise RuntimeError(f"{APP_BRAND_NAME} {label} is invalid") from exc
    if len(decoded) != KEY_BYTES:
        raise RuntimeError(f"{APP_BRAND_NAME} {label} must contain 256 bits")
    return decoded


def _load_stdin_key_material() -> KeyMaterial:
    payload_bytes = sys.stdin.buffer.readline(MAX_BOOTSTRAP_BYTES + 1)
    if not payload_bytes or len(payload_bytes) > MAX_BOOTSTRAP_BYTES or not payload_bytes.endswith(b"\n"):
        raise RuntimeError(f"{APP_BRAND_NAME} secure key bootstrap was missing or invalid")
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{APP_BRAND_NAME} secure key bootstrap was invalid") from exc
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise RuntimeError(f"{APP_BRAND_NAME} secure key bootstrap version is unsupported")
    desktop_launch_token = str(payload.get("desktopLaunchToken") or "")
    renderer_launch_token = str(payload.get("rendererLaunchToken") or "")
    desktop_token_bytes = _decode_base64_key(
        desktop_launch_token,
        "desktop launch authentication token",
    )
    renderer_token_bytes = _decode_base64_key(
        renderer_launch_token,
        "renderer launch authentication token",
    )
    if desktop_token_bytes == renderer_token_bytes:
        raise RuntimeError(f"{APP_BRAND_NAME} launch authentication roles are invalid")
    return KeyMaterial(
        database_key=_decode_base64_key(payload.get("databaseKey", ""), "database key"),
        app_encryption_key=_decode_base64_key(
            payload.get("appEncryptionKey", ""),
            "app encryption key",
        ),
        desktop_launch_token=desktop_launch_token,
        renderer_launch_token=renderer_launch_token,
        source="electron-stdin",
    )


def _load_environment_key_material() -> KeyMaterial:
    return KeyMaterial(
        database_key=_decode_base64_key(
            os.getenv(DATABASE_KEY_ENV, ""),
            "database key",
        ),
        app_encryption_key=_decode_base64_key(
            os.getenv(APP_ENCRYPTION_KEY_ENV, ""),
            "app encryption key",
        ),
        desktop_launch_token="",
        renderer_launch_token="",
        source="development-environment",
    )


def get_key_material() -> KeyMaterial:
    global _key_material
    if _key_material is not None:
        return _key_material
    with _key_material_lock:
        if _key_material is not None:
            return _key_material
        bootstrap = str(os.getenv(KEY_BOOTSTRAP_ENV) or "").strip().lower()
        if bootstrap == KEY_BOOTSTRAP_STDIN_V2:
            _key_material = _load_stdin_key_material()
        elif bootstrap:
            raise RuntimeError(f"{APP_BRAND_NAME} secure key bootstrap mode is unsupported")
        else:
            _key_material = _load_environment_key_material()
        return _key_material
