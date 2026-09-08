from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.services import replay_diagnostics
from app.services.support_logging import (
    DEFAULT_SUPPORT_CAPTURE_LEVEL,
    SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
)


class SdkRecord:
    def __init__(self) -> None:
        self.id = "1234567890123456"
        self.description = "SDK row"


class ReplayDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_diagnostic_dir = replay_diagnostics.DIAGNOSTIC_DIR
        replay_diagnostics.DIAGNOSTIC_DIR = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        replay_diagnostics.DIAGNOSTIC_DIR = self.original_diagnostic_dir
        self.tmpdir.cleanup()

    def test_capture_writes_run_matched_sanitized_fetch_sample(self) -> None:
        phase1_path = Path(self.tmpdir.name) / "CIBC" / "cibc_visible_auth_user1_20260507T222243Z_sync-cibc-abc_attempt-attempt-123_succeeded.har"
        phase1_path.parent.mkdir(parents=True, exist_ok=True)
        phase1_path.write_text("{}", encoding="utf-8")

        capture = replay_diagnostics.ReplayDiagnosticsCapture(
            "cibc",
            1,
            sync_id="cibc-abc",
            attempt_id="attempt-123",
            provider_dir="CIBC",
            phase1_har_path=phase1_path,
            capture_level=SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )
        capture.record_http_fetch(
            stage="transactions.window",
            method="GET",
            url="https://www.cibconline.cibc.com/ebm-ai/api/v1/json/accountDetails/1234567890123456",
            status=200,
            params={"accountId": "1234567890", "authToken": "secret", "fromDate": "2026-05-01"},
            payload={"transactions": []},
            records=[
                {
                    "accountId": "1234567890",
                    "external_id": "account:1234567890123456",
                    "id": "1234567890123456",
                    "number": "1234567890",
                    "transactionId": "0",
                    "description": "DEPOSIT Tangerine",
                    "creditSortValue": "12.34",
                    "authToken": "secret",
                }
            ],
            record_source="transactions",
            account_ref="1234567890",
        )
        path = capture.finish("succeeded")

        self.assertIn("sync-cibc-abc", path.name)
        self.assertIn("attempt-attempt-123", path.name)

        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            replay_diagnostics.runtime_display_path(phase1_path),
            entries[0]["phase1_har_path"],
        )
        fetch = next(entry for entry in entries if entry["event"] == "http_fetch")
        self.assertEqual("transactions.window", fetch["stage"])
        self.assertIn("<redacted:", fetch["url"]["path"])
        self.assertTrue(str(fetch["url"]["query"]["accountId"]).startswith("<redacted:"))
        self.assertEqual("<redacted>", fetch["url"]["query"]["authToken"])
        sample = fetch["response"]["sample_records"][0]
        self.assertEqual("DEPOSIT Tangerine", sample["description"])
        self.assertEqual("12.34", sample["creditSortValue"])
        self.assertTrue(str(sample["accountId"]).startswith("<redacted:"))
        self.assertTrue(str(sample["external_id"]).startswith("<redacted:"))
        self.assertTrue(str(sample["id"]).startswith("<redacted:"))
        self.assertTrue(str(sample["number"]).startswith("<redacted:"))
        self.assertEqual("0", sample["transactionId"])
        self.assertEqual("<redacted>", sample["authToken"])

    def test_capture_sanitizes_sdk_records_and_payload_sections(self) -> None:
        capture = replay_diagnostics.ReplayDiagnosticsCapture(
            "eqbank",
            1,
            sync_id="eqbank-abc",
            capture_level=SUPPORT_CAPTURE_LEVEL_DEVELOPER_LOCAL,
        )
        replay_diagnostics.record_provider_payload_sections(
            capture,
            {
                "status": "ok",
                "accounts": [SdkRecord()],
                "transactions": {"account:1234567890123456": [SdkRecord()]},
            },
            source="scraper.saved_artifact",
            transport="scraper",
        )
        path = capture.finish("succeeded")

        self.assertEqual("EQBank", path.parent.name)
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        stages = [entry.get("stage") for entry in entries if entry["event"] == "http_fetch"]
        self.assertIn("accounts.list", stages)
        self.assertIn("transactions.window", stages)
        account_fetch = next(entry for entry in entries if entry.get("stage") == "accounts.list")
        sample = account_fetch["response"]["sample_records"][0]
        self.assertEqual("SDK row", sample["description"])
        self.assertTrue(str(sample["id"]).startswith("<redacted:"))

    def test_redacted_rich_baseline_redacts_narrative_pii(self) -> None:
        # Default construction (no capture_level) is the always-on baseline.
        capture = replay_diagnostics.ReplayDiagnosticsCapture(
            "scotiabank",
            1,
            sync_id="scotia-1",
            provider_dir="Scotiabank",
        )
        capture.record_http_fetch(
            stage="transactions.window",
            method="POST",
            url="https://api.scotiabank.example/transactions",
            status=200,
            payload={"transactions": []},
            records=[
                {
                    "description": "TIM HORTONS #123",
                    "merchant": {"name": "TIM HORTONS", "categoryCode": "5814"},
                    "memo": "lunch with team",
                    "amount": -4.5,
                    "currency": "CAD",
                    "type": "debit",
                    "mcc": "5814",
                    "accountId": "1234567890",
                    "transactionId": "abc123def456",
                    "authToken": "secret",
                }
            ],
            record_source="transactions",
            account_ref="1234567890",
        )
        path = capture.finish("succeeded")

        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        fetch = next(entry for entry in entries if entry["event"] == "http_fetch")
        self.assertEqual(
            DEFAULT_SUPPORT_CAPTURE_LEVEL, fetch["capture_level"]
        )
        sample = fetch["response"]["sample_records"][0]
        # Narrative PII is redacted at the baseline...
        self.assertEqual("<redacted>", sample["description"])
        self.assertEqual("<redacted>", sample["memo"])
        self.assertEqual("<redacted>", sample["merchant"]["name"])
        # ...while structural / numeric fields (incl. category codes) survive.
        self.assertEqual("5814", sample["merchant"]["categoryCode"])
        self.assertEqual("5814", sample["mcc"])
        self.assertEqual(-4.5, sample["amount"])
        self.assertEqual("CAD", sample["currency"])
        self.assertEqual("debit", sample["type"])
        # Identifiers are fingerprinted; credentials fully redacted.
        self.assertTrue(str(sample["accountId"]).startswith("<redacted:"))
        self.assertTrue(str(sample["transactionId"]).startswith("<redacted:"))
        self.assertEqual("<redacted>", sample["authToken"])

    def test_redacted_rich_baseline_redacts_moomoo_sdk_identifiers(self) -> None:
        capture = replay_diagnostics.ReplayDiagnosticsCapture(
            "moomoo",
            1,
            sync_id="moomoo-1",
            provider_dir="Moomoo",
        )
        capture.record_http_fetch(
            stage="accounts.list",
            method="SDK",
            url="moomoo://get_acc_list",
            status=200,
            records=[
                {
                    "acc_id": 286823027201204118,
                    "card_num": "1019328561125756",
                    "uni_card_num": "1019259920916098",
                    "acc_role": "NORMAL",
                    "acc_status": "ACTIVE",
                    "trd_env": "REAL",
                }
            ],
            record_source="accounts",
        )
        path = capture.finish("ok")

        text = path.read_text(encoding="utf-8")
        self.assertNotIn("286823027201204118", text)
        self.assertNotIn("1019328561125756", text)
        self.assertNotIn("1019259920916098", text)
        entries = [json.loads(line) for line in text.splitlines()]
        fetch = next(entry for entry in entries if entry["event"] == "http_fetch")
        sample = fetch["response"]["sample_records"][0]
        self.assertTrue(str(sample["acc_id"]).startswith("<redacted:"))
        self.assertTrue(str(sample["card_num"]).startswith("<redacted:"))
        self.assertTrue(str(sample["uni_card_num"]).startswith("<redacted:"))
        self.assertEqual("NORMAL", sample["acc_role"])
        self.assertEqual("ACTIVE", sample["acc_status"])
        self.assertEqual("REAL", sample["trd_env"])

    def test_redacted_rich_baseline_redacts_nested_rbc_link_identifiers(self) -> None:
        payload = {
            "link_params": {
                "ACCOUNT_DISPLAY": "CAD Demo Savings 12345-6789012",
                "acctNum": "123456789012",
                "encodedAccountNumber": "V001opaque-account-reference",
                "CARD_ACTV_ID": "11111111-2222-3333-4444-555555555555",
                "PLOAN": "opaque-legacy-routing-material",
                "ACCOUNT_TYPE": "Visa",
                "REQUEST": "AcctTransactionInquiry",
            }
        }

        sanitized = replay_diagnostics.sanitize_payload(payload)
        text = json.dumps(sanitized)
        for sensitive_value in payload["link_params"].values():
            if sensitive_value in {"Visa", "AcctTransactionInquiry"}:
                continue
            self.assertNotIn(sensitive_value, text)

        link_params = sanitized["link_params"]
        self.assertTrue(str(link_params["ACCOUNT_DISPLAY"]).startswith("<redacted:"))
        self.assertTrue(str(link_params["acctNum"]).startswith("<redacted:"))
        self.assertTrue(str(link_params["encodedAccountNumber"]).startswith("<redacted:"))
        self.assertTrue(str(link_params["CARD_ACTV_ID"]).startswith("<redacted:"))
        self.assertEqual("<redacted>", link_params["PLOAN"])
        self.assertEqual("Visa", link_params["ACCOUNT_TYPE"])
        self.assertEqual("AcctTransactionInquiry", link_params["REQUEST"])

    def test_moomoo_route_names_remain_readable_while_ids_are_redacted(self) -> None:
        summary = replay_diagnostics._url_summary(
            "https://webapi.moomoo.com/api/v1.0/accounts/authorized_trd_accs/1234567890123456/orders_history",
            None,
        )

        self.assertIn("authorized_trd_accs", summary["path"])
        self.assertIn("orders_history", summary["path"])
        self.assertNotIn("1234567890123456", summary["path"])
        self.assertIn("<redacted:", summary["path"])

    def test_retention_keeps_newest_three_phase2_files(self) -> None:
        directory = Path(self.tmpdir.name) / "CIBC"
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(4):
            path = directory / f"cibc_phase2_replay_user1_20260507T22224{index}Z_succeeded.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            os.utime(path, (100 + index, 100 + index))
            paths.append(path)

        replay_diagnostics.prune_phase2_replay_diagnostics("cibc", 1, provider_dir="CIBC")

        remaining = {path.name for path in directory.glob("*.jsonl")}
        self.assertEqual(3, len(remaining))
        self.assertNotIn(paths[0].name, remaining)


if __name__ == "__main__":
    unittest.main()
