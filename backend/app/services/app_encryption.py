from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.key_material import get_key_material

ENCRYPTED_PAYLOAD_MARKER = "__breaktwenty_encrypted__"
ENCRYPTED_PAYLOAD_VERSION = 2
ENCRYPTED_PAYLOAD_AAD_SCHEMA = "breaktwenty.encrypted-payload-aad.v2"
NONCE_BYTES = 12


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def get_app_encryption_key() -> bytes:
    return get_key_material().app_encryption_key


def _associated_data(context: Mapping[str, str | int]) -> bytes:
    if not isinstance(context, Mapping) or not context:
        raise ValueError("BreakTwenty encrypted payload identity context is required")
    normalized: dict[str, str | int] = {}
    for raw_key, raw_value in context.items():
        key = str(raw_key).strip()
        if not key or key in normalized:
            raise ValueError("BreakTwenty encrypted payload identity context is invalid")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int)):
            raise ValueError("BreakTwenty encrypted payload identity values must be strings or integers")
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if value == "":
            raise ValueError("BreakTwenty encrypted payload identity values cannot be empty")
        normalized[key] = value
    return json.dumps(
        {
            "schema": ENCRYPTED_PAYLOAD_AAD_SCHEMA,
            "context": normalized,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def is_encrypted_payload(payload_json: str | None) -> bool:
    try:
        payload = json.loads(payload_json or "")
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get(ENCRYPTED_PAYLOAD_MARKER) == ENCRYPTED_PAYLOAD_VERSION
        and payload.get("alg") == "AES-256-GCM"
        and payload.get("aad_schema") == ENCRYPTED_PAYLOAD_AAD_SCHEMA
    )


def encrypt_json_payload(
    payload: Any,
    *,
    aad_context: Mapping[str, str | int],
) -> str:
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(get_app_encryption_key()).encrypt(
        nonce,
        plaintext,
        _associated_data(aad_context),
    )
    return json.dumps(
        {
            ENCRYPTED_PAYLOAD_MARKER: ENCRYPTED_PAYLOAD_VERSION,
            "alg": "AES-256-GCM",
            "aad_schema": ENCRYPTED_PAYLOAD_AAD_SCHEMA,
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
        },
        separators=(",", ":"),
    )


def load_json_payload(
    payload_json: str,
    *,
    aad_context: Mapping[str, str | int],
) -> Any:
    payload = json.loads(payload_json)
    if (
        not isinstance(payload, dict)
        or payload.get(ENCRYPTED_PAYLOAD_MARKER) != ENCRYPTED_PAYLOAD_VERSION
        or payload.get("alg") != "AES-256-GCM"
        or payload.get("aad_schema") != ENCRYPTED_PAYLOAD_AAD_SCHEMA
    ):
        raise ValueError("BreakTwenty encrypted payload expected")

    nonce = _b64decode(str(payload.get("nonce") or ""))
    ciphertext = _b64decode(str(payload.get("ciphertext") or ""))
    plaintext = AESGCM(get_app_encryption_key()).decrypt(
        nonce,
        ciphertext,
        _associated_data(aad_context),
    )
    return json.loads(plaintext.decode("utf-8"))
