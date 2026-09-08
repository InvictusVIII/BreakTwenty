from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from app.connectors.connector_logging import (
    CONNECTOR_LOG_INPUT_LIMIT,
    log_connector_event,
    safe_connector_public_message,
    sanitize_connector_log_text,
)
from app.connectors.types import SyncResult, SyncStatus


class ConnectorLoggingRedactionTests(unittest.TestCase):
    def test_normal_text_is_preserved(self) -> None:
        self.assertEqual(
            sanitize_connector_log_text("ordinary provider message"),
            "ordinary provider message",
        )

    def test_complete_private_key_block_is_redacted(self) -> None:
        begin_marker = "-----BEGIN RSA " + "PRIVATE KEY-----"
        end_marker = "-----END RSA " + "PRIVATE KEY-----"
        raw = (
            f"before {begin_marker}\n"
            "private-material\n"
            f"{end_marker} after"
        )

        self.assertEqual(sanitize_connector_log_text(raw), "before <redacted> after")

    def test_multiple_complete_private_key_blocks_are_redacted(self) -> None:
        begin_marker = "-----BEGIN " + "PRIVATE KEY-----"
        end_marker = "-----END " + "PRIVATE KEY-----"
        raw = (
            f"first {begin_marker}\nsecret-one\n{end_marker} "
            "middle -----begin encrypted private key-----\nsecret-two\n"
            "-----end encrypted private key----- last"
        )

        self.assertEqual(
            sanitize_connector_log_text(raw),
            "first <redacted> middle <redacted> last",
        )

    def test_unmatched_private_key_begin_redacts_the_remainder(self) -> None:
        begin_marker = "-----BEGIN " + "PRIVATE KEY-----"
        raw = f"before {begin_marker}\nprivate-material"

        self.assertEqual(sanitize_connector_log_text(raw), "before <redacted>")

    def test_many_repeated_unmatched_private_key_begins_are_redacted(self) -> None:
        begin_marker = "-----BEGIN " + "PRIVATE KEY-----"
        repeated_begins = begin_marker * 500

        self.assertEqual(
            sanitize_connector_log_text(f"before {repeated_begins}", limit=CONNECTOR_LOG_INPUT_LIMIT),
            "before <redacted>",
        )

    def test_input_is_bounded_before_redaction(self) -> None:
        raw = "x" * (CONNECTOR_LOG_INPUT_LIMIT + 500)

        with patch(
            "app.connectors.connector_logging._redact_private_key_blocks",
            side_effect=lambda text: text,
        ) as redact_private_keys:
            sanitized = sanitize_connector_log_text(raw, limit=CONNECTOR_LOG_INPUT_LIMIT + 500)

        bounded_input = redact_private_keys.call_args.args[0]
        self.assertEqual(len(bounded_input), CONNECTOR_LOG_INPUT_LIMIT)
        self.assertEqual(sanitized, f"{'x' * CONNECTOR_LOG_INPUT_LIMIT}...")

    def test_freeform_exception_text_redacts_secret_bearing_material(self) -> None:
        private_key = "-----BEGIN " + "PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
        jwt = "abcdefgh.ijklmnop.qrstuvwx"
        raw = (
            "request failed https://user:pass@api.example.test/v1/accounts?access_token=url-secret&cursor=private "
            "Authorization: Bearer header-secret password=plain-secret "
            f"email=user@example.test jwt={jwt} key={private_key}"
        )

        sanitized = sanitize_connector_log_text(raw)

        for secret in (
            "user:pass",
            "url-secret",
            "private",
            "header-secret",
            "plain-secret",
            "user@example.test",
            jwt,
            "private-material",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertIn("https://api.example.test/v1/accounts", sanitized)
        self.assertIn("access_token=<redacted>", sanitized)
        self.assertNotIn("\n", sanitized)

    def test_structured_fields_are_redacted_before_reaching_logger(self) -> None:
        logger = logging.getLogger("breaktwenty.tests.connector-redaction")
        logger.setLevel(logging.INFO)

        with self.assertLogs(logger, level="INFO") as captured:
            log_connector_event(
                logger,
                provider="example",
                stage="request failed",
                user_id=1,
                url="https://api.example.test/v1/resource?cursor=cursor-secret",
                headers={
                    "Authorization": "Bearer header-secret",
                    "X-Trace-ID": "trace-safe",
                },
                api_key="direct-secret",
                passcode=123456,
                error="password=exception-secret",
                attempt=2,
            )

        rendered = "\n".join(captured.output)
        for secret in ("cursor-secret", "header-secret", "direct-secret", "exception-secret"):
            self.assertNotIn(secret, rendered)
        self.assertIn("trace-safe", rendered)
        self.assertIn("attempt=2", rendered)
        self.assertIn("api_key=<redacted>", rendered)
        self.assertIn("passcode=<redacted>", rendered)
        self.assertNotIn("123456", rendered)

    def test_secret_bearing_exception_message_is_redacted_in_route_payload(self) -> None:
        raw = (
            "provider failed with {'access_token': 'route-secret'} "
            "at https://user:pass@example.test/data?cursor=cursor-secret"
        )

        public_message = safe_connector_public_message(RuntimeError(raw))
        response = SyncResult(
            status=SyncStatus.ERROR,
            message=raw,
        ).to_route_response()

        for secret in ("route-secret", "user:pass", "cursor-secret"):
            self.assertNotIn(secret, public_message)
            self.assertNotIn(secret, response["message"])
        self.assertIn("access_token': <redacted>", response["message"])


if __name__ == "__main__":
    unittest.main()
