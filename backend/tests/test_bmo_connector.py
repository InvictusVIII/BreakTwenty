from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx

from app.connectors.bmo import BMOConnector
from app.connectors.types import SyncStatus, account_result_key
from app.scrapers import bmo as bmo_scraper


class BMOConnectorTests(unittest.TestCase):
    def test_interac_received_transaction_shape_normalizes_as_deposit(self) -> None:
        result = BMOConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:ba:35648869681",
                        "ocifAccountName": "Savings Amplifier Account",
                        "category_name": "BA",
                        "accountNumber": "35648869681",
                        "accountBalance": "100.00",
                    }
                ],
                "transactions": {
                    "account:ba:35648869681": [
                        {
                            "txnDate": "2026-05-06",
                            "txnAmount": "12.34",
                            "descr": "INTERAC ETRNSFR RECVD TEST SENDER",
                            "code": "CW",
                            "cimgRef": "ref-123",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:ba:35648869681"],
            }
        )

        self.assertEqual(SyncStatus.OK, result.status)
        self.assertEqual(1, len(result.transactions))
        tx = result.transactions[0]
        self.assertEqual("bmo_ref-123", tx.external_id)
        self.assertEqual("deposit", tx.type)
        self.assertEqual(12.34, tx.amount)
        self.assertEqual("INTERAC ETRNSFR RECVD TEST SENDER", tx.description)
        self.assertEqual("CAD", tx.currency)

        account_key = account_result_key(result.accounts[0])
        self.assertEqual([tx], result.transactions_by_account[account_key])
        self.assertEqual({account_key}, result.transaction_fetch_succeeded_accounts)

    def test_withdrawal_description_controls_positive_bmo_amount(self) -> None:
        result = BMOConnector()._build_from_payload(
            {
                "status": "ok",
                "accounts": [
                    {
                        "external_id": "account:ba:35648869681",
                        "ocifAccountName": "Savings Amplifier Account",
                        "category_name": "BA",
                        "accountNumber": "35648869681",
                        "accountBalance": "100.00",
                    }
                ],
                "transactions": {
                    "account:ba:35648869681": [
                        {
                            "txnDate": "2026-05-06",
                            "txnAmount": "12.34",
                            "descr": "ATM withdrawal",
                            "code": "CW",
                            "cimgRef": "ref-456",
                            "_account_currency": "CAD",
                        }
                    ]
                },
                "transaction_fetch_succeeded_accounts": ["account:ba:35648869681"],
            }
        )

        tx = result.transactions[0]
        self.assertEqual("withdrawal", tx.type)
        self.assertEqual(-12.34, tx.amount)


class BMODirectBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_bootstrap_matches_cdb_flow_before_entitlements(self) -> None:
        calls: list[str] = []

        async def fake_direct_request(_client, _url, **kwargs):
            request_key = kwargs["request_key"]
            calls.append(request_key)
            if request_key == "EntitlementsRq":
                return {
                    "status": 200,
                    "url": "https://www1.bmo.com/banking/services/accounts/entitlements",
                    "payload": {
                        "EntitlementsRs": {
                            "HdrRs": {"callStatus": "Success"},
                            "BodyRs": {"bankAccounts": []},
                        }
                    },
                }
            response_key = request_key.replace("Rq", "Rs")
            return {
                "status": 200,
                "url": "https://www1.bmo.com/",
                "payload": {response_key: {"HdrRs": {"callStatus": "Success"}, "BodyRs": {}}},
            }

        with patch.object(
            bmo_scraper,
            "_bmo_direct_request",
            new=AsyncMock(side_effect=fake_direct_request),
        ):
            entitlements = await bmo_scraper._bmo_direct_bootstrap(
                object(),
                mfa_device_token=None,
                user_agent="test-agent",
                user_id=1,
            )

        self.assertEqual(
            ["InitISAMSessionRq", "EntitlementsRq"],
            calls,
        )
        self.assertEqual({"bankAccounts": []}, entitlements)

    async def test_direct_bootstrap_entitlements_failure_includes_http_status(self) -> None:
        async def fake_direct_request(_client, _url, **kwargs):
            request_key = kwargs["request_key"]
            if request_key == "EntitlementsRq":
                return {
                    "status": 400,
                    "url": "https://www1.bmo.com/banking/services/accounts/entitlements",
                    "payload": None,
                }
            response_key = request_key.replace("Rq", "Rs")
            return {
                "status": 200,
                "url": "https://www1.bmo.com/",
                "payload": {response_key: {"HdrRs": {"callStatus": "Success"}, "BodyRs": {}}},
            }

        with patch.object(
            bmo_scraper,
            "_bmo_direct_request",
            new=AsyncMock(side_effect=fake_direct_request),
        ):
            with self.assertRaisesRegex(RuntimeError, r"BMO entitlements request failed \(HTTP 400\)"):
                await bmo_scraper._bmo_direct_bootstrap(
                    object(),
                    mfa_device_token=None,
                    user_agent="test-agent",
                    user_id=1,
                )

    async def test_saved_artifact_account_sync_validates_captured_entitlements(self) -> None:
        session_artifact = {
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "groupTotal": [{"summaryBalance": "10.00", "currency": "CAD"}],
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                                "availableAmount": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
                "loanAccounts": [],
                "creditCardAccounts": [],
                "investmentAccounts": [],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=session_artifact["entitlements"]),
            ) as direct_bootstrap,
            patch.object(bmo_scraper, "_bmo_fetch_bank_account_window_direct", new=AsyncMock()) as bank_details,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="accounts",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, len(payload["accounts"]))
        self.assertTrue(payload["accounts"][0]["balance_is_fresh"])
        self.assertEqual({"allowDownloadDetails": True, "accountNumber": "3564 8869-681"}, payload["accounts"][0]["entitlements"])
        direct_bootstrap.assert_awaited_once()
        bank_details.assert_not_called()

    async def test_saved_artifact_account_sync_falls_back_to_captured_entitlements_after_replay_auth_required(self) -> None:
        today = bmo_scraper.date.today()
        history_floor = today - timedelta(days=bmo_scraper.BMO_MAX_HISTORY_DAYS)
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "attempt_id": "attempt-visible",
            "user_agent": "Mozilla/5.0 test",
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "accountIndex": 0,
                        "productName": "Savings",
                        "allowDownloadDetails": True,
                    }
                ],
                "loanAccounts": [],
                "creditCardAccounts": [],
                "investmentAccounts": [],
            },
            "browser_transaction_history": {
                "captured": True,
                "attempt_id": "attempt-visible",
                "expected_windows": 1,
                "windows": [
                    {
                        "account_index": "0",
                        "start_date": history_floor.isoformat(),
                        "end_date": today.isoformat(),
                        "details": {
                            "accountBalance": "42.00",
                            "currency": "CAD",
                            "moreTxns": "N",
                        },
                        "transactions": [],
                    }
                ],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(
                bmo_scraper,
                "_bmo_current_visible_auth_attempt_id",
                return_value="attempt-visible",
            ),
            patch.object(bmo_scraper, "_bmo_fetch_bank_account_window_direct", new=AsyncMock()) as bank_details,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="accounts",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, len(payload["accounts"]))
        self.assertEqual("42.00", payload["accounts"][0]["accountBalance"])
        self.assertTrue(payload["accounts"][0]["balance_is_fresh"])
        direct_bootstrap.assert_awaited_once()
        bank_details.assert_not_called()

    async def test_saved_artifact_account_sync_returns_auth_required_for_unscoped_cached_entitlements(self) -> None:
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "user_agent": "Mozilla/5.0 test",
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "accountIndex": 0,
                        "productName": "Savings",
                    }
                ],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(bmo_scraper, "_bmo_fetch_bank_account_window_direct", new=AsyncMock()) as bank_details,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="accounts",
            )

        self.assertIsNone(payload)
        direct_bootstrap.assert_awaited_once()
        bank_details.assert_not_called()

    async def test_saved_artifact_account_sync_keeps_entitlement_accounts_when_balance_refresh_requires_auth(self) -> None:
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "attempt_id": "attempt-visible",
            "user_agent": "Mozilla/5.0 test",
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "accountIndex": 0,
                        "productName": "Savings",
                    }
                ],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                bmo_scraper,
                "_bmo_current_visible_auth_attempt_id",
                return_value="attempt-visible",
            ),
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_window_direct",
                new=AsyncMock(side_effect=PermissionError("BMO auth required")),
            ) as bank_details,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="accounts",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, len(payload["accounts"]))
        self.assertFalse(payload["accounts"][0]["balance_is_fresh"])
        bank_details.assert_awaited_once()

    async def test_saved_artifact_transaction_sync_bootstraps_even_with_captured_entitlements(self) -> None:
        session_artifact = {
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
            },
        }
        refreshed_entitlements = {
            "bankAccounts": [
                {
                    "accountNumber": "3564 8869-681",
                    "allowDownloadDetails": True,
                    "allowStopPayment": True,
                }
            ],
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=refreshed_entitlements),
            ) as direct_bootstrap,
            patch.object(
                bmo_scraper,
                "_bmo_transaction_history_preflight",
                new=AsyncMock(return_value=True),
            ) as transaction_preflight,
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_resolved_window_direct",
                new=AsyncMock(return_value=({"moreTxns": "N", "currency": "CAD"}, [])),
            ),
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="transactions",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual(
            {
                "accountNumber": "3564 8869-681",
                "allowDownloadDetails": True,
                "allowStopPayment": True,
            },
            payload["accounts"][0]["entitlements"],
        )
        direct_bootstrap.assert_awaited_once()
        transaction_preflight.assert_awaited_once()

    async def test_saved_artifact_transaction_sync_uses_browser_transaction_history(self) -> None:
        external_id = "account:ba:35648869681"
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "handoff_sync_id": "sync-visible",
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
            },
            "browser_transaction_history": {
                "captured": True,
                "handoff_sync_id": "sync-visible",
                "expected_windows": 2,
                "windows": [
                    {
                        "account_index": "0",
                        "start_date": "2025-08-23",
                        "end_date": "2026-08-22",
                        "details": {"currency": "CAD", "moreTxns": "N"},
                        "transactions": [
                            {
                                "txnDate": "2026-05-05",
                                "txnAmount": "10.00",
                                "descr": "INTERAC ETRNSFR RECVD TEST",
                                "cimgRef": "ref-browser",
                            }
                        ],
                    },
                    {
                        "account_index": "0",
                        "start_date": "2024-08-23",
                        "end_date": "2025-08-22",
                        "details": {"currency": "CAD", "moreTxns": "N"},
                        "transactions": [],
                    },
                ],
            },
        }
        recorded_events: list[tuple[str, str, str, int]] = []

        async def recorder(event):
            recorded_events.append(
                (
                    event["event"],
                    event["start_date"],
                    event["end_date"],
                    len(event.get("transactions") or []),
                )
            )

        async def resolver(_accounts):
            return {
                external_id: {
                    "mode": "backfill",
                    "end_date": "2026-08-22",
                    "backfill_start_date": "2024-08-23",
                    "backfill_end_date": "2026-08-22",
                    "pending_backfill_windows": [
                        {"start": "2025-08-23", "end": "2026-08-22"},
                        {"start": "2024-08-23", "end": "2025-08-22"},
                    ],
                }
            }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(bmo_scraper, "_bmo_current_sync_id", return_value="sync-visible"),
            patch.object(bmo_scraper, "_bmo_transaction_history_preflight", new=AsyncMock()) as transaction_preflight,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                mode_resolver=resolver,
                sync_scope="transactions",
                transaction_window_recorder=recorder,
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual([external_id], payload["transaction_fetch_succeeded_accounts"])
        self.assertEqual(1, len(payload["transactions"][external_id]))
        self.assertEqual(
            [
                ("started", "2025-08-23", "2026-08-22", 0),
                ("completed", "2025-08-23", "2026-08-22", 1),
                ("started", "2024-08-23", "2025-08-22", 0),
                ("completed", "2024-08-23", "2025-08-22", 0),
            ],
            recorded_events,
        )
        direct_bootstrap.assert_not_called()
        transaction_preflight.assert_not_called()

    async def test_saved_artifact_transaction_sync_uses_browser_history_without_revalidating_entitlements(self) -> None:
        external_id = "account:ba:35648869681"
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "handoff_sync_id": "sync-visible",
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "browser_transaction_history": {
                "captured": True,
                "handoff_sync_id": "sync-visible",
                "expected_windows": 1,
                "windows": [
                    {
                        "account_index": "0",
                        "start_date": "2025-08-23",
                        "end_date": "2026-08-22",
                        "details": {
                            "accountBalance": "10.00",
                            "currency": "CAD",
                            "moreTxns": "N",
                        },
                        "transactions": [
                            {
                                "txnDate": "2026-05-05",
                                "txnAmount": "10.00",
                                "descr": "INTERAC ETRNSFR RECVD TEST",
                                "cimgRef": "ref-browser-no-entitlements",
                            }
                        ],
                    },
                ],
            },
        }

        async def resolver(_accounts):
            return {
                external_id: {
                    "mode": "backfill",
                    "end_date": "2026-08-22",
                    "backfill_start_date": "2025-08-23",
                    "backfill_end_date": "2026-08-22",
                    "pending_backfill_windows": [
                        {"start": "2025-08-23", "end": "2026-08-22"},
                    ],
                }
            }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(bmo_scraper, "_bmo_current_sync_id", return_value="sync-visible"),
            patch.object(bmo_scraper, "_bmo_transaction_history_preflight", new=AsyncMock()) as transaction_preflight,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                mode_resolver=resolver,
                sync_scope="transactions",
            )

        self.assertEqual("ok", payload["status"])
        self.assertEqual([external_id], payload["transaction_fetch_succeeded_accounts"])
        self.assertEqual(1, len(payload["transactions"][external_id]))
        direct_bootstrap.assert_not_called()
        transaction_preflight.assert_not_called()

    async def test_saved_artifact_transaction_sync_ignores_unmatched_browser_history_before_recording_windows(self) -> None:
        external_id = "account:ba:35648869681"
        session_artifact = {
            "captured_at": "2026-08-26T22:28:00Z",
            "handoff_sync_id": "old-sync",
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
            },
            "browser_transaction_history": {
                "captured": True,
                "captured_at": "2026-08-26T22:28:00Z",
                "handoff_sync_id": "old-sync",
                "expected_windows": 1,
                "windows": [
                    {
                        "account_index": "0",
                        "start_date": "2026-08-11",
                        "end_date": "2026-08-25",
                        "details": {"currency": "CAD", "moreTxns": "N"},
                        "transactions": [],
                    },
                ],
            },
        }
        recorded_events: list[str] = []

        async def recorder(event):
            recorded_events.append(event["event"])

        async def resolver(_accounts):
            return {
                external_id: {
                    "mode": "incremental",
                    "start_date": "2026-08-11",
                    "end_date": "2026-08-26",
                }
            }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(bmo_scraper, "_bmo_current_sync_id", return_value="new-sync"),
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_history_from_browser_capture",
                new=AsyncMock(),
            ) as browser_fetch,
            patch.object(bmo_scraper, "_bmo_transaction_history_preflight", new=AsyncMock()) as transaction_preflight,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                mode_resolver=resolver,
                sync_scope="transactions",
                transaction_window_recorder=recorder,
            )

        self.assertIsNone(payload)
        self.assertEqual([], recorded_events)
        direct_bootstrap.assert_awaited_once()
        browser_fetch.assert_not_called()
        transaction_preflight.assert_not_called()

    async def test_saved_artifact_transaction_sync_auth_required_does_not_fetch_browser_history(self) -> None:
        session_artifact = {
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
            },
            "browser_transaction_history": {
                "captured": True,
                "windows": [
                    {
                        "account_index": "0",
                        "start_date": "2025-08-23",
                        "end_date": "2026-08-22",
                        "details": {"currency": "CAD", "moreTxns": "N"},
                        "transactions": [],
                    }
                ],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=None),
            ) as direct_bootstrap,
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_history_from_browser_capture",
                new=AsyncMock(),
            ) as browser_fetch,
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_history_direct",
                new=AsyncMock(),
            ) as direct_fetch,
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="transactions",
            )

        self.assertIsNone(payload)
        direct_bootstrap.assert_awaited_once()
        browser_fetch.assert_not_called()
        direct_fetch.assert_not_called()

    async def test_browser_transaction_history_accepts_oldest_edge_gap(self) -> None:
        account = {
            "external_id": "account:ba:35648869681",
            "accountIndex": 0,
            "currency": "CAD",
            "sync_window": {
                "mode": "backfill",
                "end_date": "2026-08-22",
                "pending_backfill_windows": [
                    {"start": "2025-08-23", "end": "2026-08-22"},
                    {"start": "2016-08-24", "end": "2016-08-24"},
                ],
            },
        }
        browser_history = {
            "captured": True,
            "windows": [
                {
                    "account_index": "0",
                    "start_date": "2025-08-23",
                    "end_date": "2026-08-22",
                    "details": {"currency": "CAD", "moreTxns": "N"},
                    "transactions": [
                        {
                            "txnDate": "2026-05-05",
                            "txnAmount": "10.00",
                            "descr": "INTERAC ETRNSFR RECVD TEST",
                            "cimgRef": "ref-browser",
                        }
                    ],
                },
                {
                    "account_index": "0",
                    "start_date": "2016-08-25",
                    "end_date": "2017-08-24",
                    "details": {"currency": "CAD", "moreTxns": "N"},
                    "transactions": [],
                },
            ],
        }
        recorded_events: list[tuple[str, str, str, int]] = []

        async def recorder(event):
            recorded_events.append(
                (
                    event["event"],
                    event["start_date"],
                    event["end_date"],
                    len(event.get("transactions") or []),
                )
            )

        details, transactions, succeeded = await bmo_scraper._bmo_fetch_bank_account_history_from_browser_capture(
            browser_history,
            account,
            transaction_window_recorder=recorder,
        )

        self.assertTrue(succeeded)
        self.assertEqual({"currency": "CAD", "moreTxns": "N"}, details)
        self.assertEqual(1, len(transactions))
        self.assertEqual(
            [
                ("started", "2025-08-23", "2026-08-22", 0),
                ("completed", "2025-08-23", "2026-08-22", 1),
                ("started", "2016-08-24", "2016-08-24", 0),
                ("completed", "2016-08-24", "2016-08-24", 0),
            ],
            recorded_events,
        )

    async def test_bank_account_window_uses_current_cdb_details_endpoint(self) -> None:
        calls: list[str] = []

        async def fake_direct_request(_client, url, **_kwargs):
            calls.append(url)
            return {
                "status": 200,
                "url": url,
                "payload": {
                    "GetBankAccountDetailsRs": {
                        "HdrRs": {"callStatus": "Success"},
                        "BodyRs": {
                            "bankAccountDetails": {"moreTxns": "N", "currency": "CAD"},
                            "bankAccountTransactions": [
                                {
                                    "txnDate": "2026-05-05",
                                    "txnAmount": "10.00",
                                    "descr": "Deposit",
                                    "cimgRef": "ref-1",
                                }
                            ],
                        },
                    }
                },
            }

        with patch.object(
            bmo_scraper,
            "_bmo_direct_request",
            new=AsyncMock(side_effect=fake_direct_request),
        ):
            details, transactions, more_txns = await bmo_scraper._bmo_fetch_bank_account_window_direct(
                object(),
                account={"accountIndex": 0, "accountNumber": "3564 8869-681"},
                start_date=bmo_scraper.date(2026, 1, 1),
                end_date=bmo_scraper.date(2026, 8, 22),
                mfa_device_token="token",
                user_agent="test-agent",
            )

        self.assertEqual(
            ["https://www1.bmo.com/api/cdb/current-account/accountdetails/getBankAccountDetails"],
            calls,
        )
        self.assertEqual({"moreTxns": "N", "currency": "CAD"}, details)
        self.assertEqual(1, len(transactions))
        self.assertFalse(more_txns)

    async def test_direct_request_sends_cdb_mfa_device_token_header(self) -> None:
        captured_headers: dict[str, str] = {}

        class FakeResponse:
            status_code = 200
            text = "{}"
            headers = {"content-type": "application/json"}
            url = bmo_scraper.BMO_BANK_DETAILS_URL

            @property
            def is_success(self):
                return True

        class FakeClient:
            cookies = httpx.Cookies()

            async def post(self, _url, *, headers=None, json=None):
                captured_headers.update(headers or {})
                return FakeResponse()

        await bmo_scraper._bmo_direct_request(
            FakeClient(),
            bmo_scraper.BMO_BANK_DETAILS_URL,
            request_key="GetBankAccountDetailsRq",
            body={},
            current_path="/banking/digital/account-details/ba/0",
            referrer="https://www1.bmo.com/banking/digital/account-details/ba/0",
            mfa_device_token="device-token",
            user_agent="test-agent",
        )

        self.assertEqual("device-token", captured_headers.get("x-bmo-mfa-device-token"))

    async def test_saved_artifact_transaction_sync_errors_when_all_bank_fetches_fail(self) -> None:
        session_artifact = {
            "user_agent": "Mozilla/5.0 test",
            "summary": {
                "categories": [
                    {
                        "categoryName": "BA",
                        "groupHeadTitle": "Bank Accounts",
                        "products": [
                            {
                                "accountType": "BANK_ACCOUNT",
                                "productName": "Savings",
                                "ocifAccountName": "Savings Amplifier Account",
                                "accountNumber": "3564 8869-681",
                                "currency": "CAD",
                                "accountIndex": 0,
                                "accountBalance": "10.00",
                            }
                        ],
                    }
                ],
            },
            "entitlements": {
                "bankAccounts": [
                    {
                        "accountNumber": "3564 8869-681",
                        "allowDownloadDetails": True,
                    }
                ],
            },
        }

        with (
            patch.object(
                bmo_scraper,
                "_bmo_direct_bootstrap",
                new=AsyncMock(return_value=session_artifact["entitlements"]),
            ),
            patch.object(
                bmo_scraper,
                "_bmo_transaction_history_preflight",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                bmo_scraper,
                "_bmo_fetch_bank_account_resolved_window_direct",
                new=AsyncMock(side_effect=RuntimeError("BMO bank account details request failed")),
            ),
        ):
            payload = await bmo_scraper._sync_bmo_with_saved_artifacts(
                {"cookies": [], "origins": []},
                session_artifact,
                user_id=None,
                sync_scope="transactions",
            )

        self.assertEqual("error", payload["status"])
        self.assertEqual("BMO bank account details request failed", payload["message"])
        self.assertEqual([], payload["transaction_fetch_succeeded_accounts"])


if __name__ == "__main__":
    unittest.main()
