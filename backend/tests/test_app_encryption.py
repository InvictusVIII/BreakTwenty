from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.services.app_encryption import (
    ENCRYPTED_PAYLOAD_AAD_SCHEMA,
    ENCRYPTED_PAYLOAD_MARKER,
    ENCRYPTED_PAYLOAD_VERSION,
    encrypt_json_payload,
    is_encrypted_payload,
    load_json_payload,
)
from tests.key_material_test_utils import reset_cached_key_material


class AppEncryptionV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._app_key = b"\x42" * 32
        self._key_patch = patch.dict(
            os.environ,
            {
                "BREAKTWENTY_DATABASE_ENCRYPTION_KEY": base64.b64encode(
                    b"\x41" * 32
                ).decode("ascii"),
                "BREAKTWENTY_APP_ENCRYPTION_KEY": base64.b64encode(
                    self._app_key
                ).decode("ascii"),
            },
        )
        self._key_patch.start()
        reset_cached_key_material()

    def tearDown(self) -> None:
        reset_cached_key_material()
        self._key_patch.stop()

    def test_v2_round_trip_requires_the_exact_identity_context(self) -> None:
        context = {
            "storage": "connection_auth_artifacts",
            "user_id": 7,
            "institution_id": 12,
            "provider": "rbc",
            "artifact_kind": "api_credentials",
            "slot": "active",
        }
        payload_json = encrypt_json_payload(
            {"api_token": "secret"},
            aad_context=context,
        )
        envelope = json.loads(payload_json)

        self.assertTrue(is_encrypted_payload(payload_json))
        self.assertEqual(envelope[ENCRYPTED_PAYLOAD_MARKER], ENCRYPTED_PAYLOAD_VERSION)
        self.assertEqual(envelope["aad_schema"], ENCRYPTED_PAYLOAD_AAD_SCHEMA)
        self.assertEqual(
            load_json_payload(payload_json, aad_context=context),
            {"api_token": "secret"},
        )

        for changed_context in (
            {**context, "user_id": 8},
            {**context, "institution_id": 13},
            {**context, "provider": "bmo"},
            {**context, "artifact_kind": "scraper_credentials"},
            {**context, "slot": "quarantine"},
        ):
            with self.subTest(context=changed_context), self.assertRaises(InvalidTag):
                load_json_payload(payload_json, aad_context=changed_context)

    def test_v1_ciphertext_is_rejected_without_a_legacy_reader(self) -> None:
        nonce = b"\x24" * 12
        plaintext = json.dumps({"api_token": "legacy"}, separators=(",", ":")).encode()
        ciphertext = AESGCM(self._app_key).encrypt(nonce, plaintext, None)
        payload_json = json.dumps(
            {
                ENCRYPTED_PAYLOAD_MARKER: 1,
                "alg": "AES-256-GCM",
                "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        )

        with self.assertRaisesRegex(ValueError, "encrypted payload expected"):
            load_json_payload(
                payload_json,
                aad_context={"storage": "user_secret_artifacts", "user_id": 1},
            )
        self.assertFalse(is_encrypted_payload(payload_json))

    def test_identity_context_is_mandatory_and_canonical(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity context is required"):
            encrypt_json_payload({"secret": "value"}, aad_context={})
        with self.assertRaisesRegex(ValueError, "must be strings or integers"):
            encrypt_json_payload(
                {"secret": "value"},
                aad_context={"storage": "test", "user_id": True},
            )

        first = {
            "storage": "user_secret_artifacts",
            "user_id": 1,
            "secret_kind": "market_data_api_key",
        }
        reordered = {
            "secret_kind": "market_data_api_key",
            "user_id": 1,
            "storage": "user_secret_artifacts",
        }
        payload_json = encrypt_json_payload({"secret": "value"}, aad_context=first)
        self.assertEqual(
            load_json_payload(payload_json, aad_context=reordered),
            {"secret": "value"},
        )


if __name__ == "__main__":
    unittest.main()
