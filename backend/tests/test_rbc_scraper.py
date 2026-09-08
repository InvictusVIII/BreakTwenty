from __future__ import annotations

import asyncio
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from app.scrapers import rbc


class _FakeReplaySession:
    def __init__(self, *args, **kwargs):
        self.storage_state = {"cookies": []}
        self.session_artifact = {"user_agent": "ua"}
        self.refreshed = False

    async def load_async(self):
        return self

    def missing_artifact_result(self):
        raise AssertionError("storage_state should be present")

    async def refresh_runtime_artifacts_async(self, *args, **kwargs):
        self.refreshed = True
        return True

    def normalize_result(self, payload):
        return payload


class RBCCreditCardBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_auth_response_logs_structured_cause(self) -> None:
        response = {
            "status": 401,
            "url": "https://secure.royalbank.com/api/accounts?token=secret",
            "payload": None,
        }
        with (
            patch.object(rbc, "_rbc_direct_request", new=AsyncMock(return_value=response)),
            patch.object(rbc, "_rbc_log_event") as log_event,
        ):
            with self.assertRaises(rbc.RBCAuthRequired):
                await rbc._fetch_rbc_json_direct(
                    {"cookies": []},
                    response["url"],
                    user_agent="ua",
                    label="account list",
                )

        log_event.assert_called_once_with(
            "account list direct fetch auth_required",
            level="warning",
            auth_signal="http_401",
            status_code=401,
            url="https://secure.royalbank.com/api/accounts",
            payload_type="NoneType",
        )

    async def test_account_payload_error_logs_provider_details(self) -> None:
        account_payload = {
            "errorState": {
                "hasError": True,
                "errorCode": "SESSION_EXPIRED",
                "errorMessage": "Session expired",
            }
        }
        with (
            patch.object(
                rbc,
                "_fetch_rbc_json_direct",
                new=AsyncMock(return_value=account_payload),
            ),
            patch.object(rbc, "_rbc_log_event") as log_event,
        ):
            result = await rbc._sync_rbc_with_saved_artifacts(
                {"cookies": []},
                {},
                user_id=1,
            )

        self.assertIsNone(result)
        log_event.assert_any_call(
            "saved-session sync accounts unavailable",
            user_id=1,
            level="warning",
            engine="direct",
            auth_signal="account_payload_error_state",
            payload_type="dict",
            has_error=True,
            error_code="SESSION_EXPIRED",
            error_message="Session expired",
            present_account_categories=[],
        )
    async def test_headless_sync_loads_saved_artifacts_asynchronously(self) -> None:
        payload = {
            "status": "ok",
            "accounts": [{"name": "Chequing", "external_id": "rbc:S001"}],
            "transactions": {},
            "transaction_fetch_succeeded_accounts": [],
        }

        with (
            patch.object(rbc, "SavedArtifactReplaySession", _FakeReplaySession),
            patch.object(rbc, "_sync_rbc_with_saved_artifacts", return_value=payload) as sync_saved,
            patch.object(rbc, "_rbc_log_event"),
        ):
            result = await rbc.try_headless_sync(1)

        self.assertEqual("ok", result["status"])
        sync_saved.assert_awaited_once()

    async def test_loc_history_fetches_legacy_html_page(self) -> None:
        posts: list[tuple[str, dict]] = []
        recorded_events: list[dict] = []

        async def fake_post_form(storage_state, url, payload, *, user_agent):
            del storage_state, user_agent
            posts.append((url, payload))
            return {
                "status": 200,
                "ok": True,
                "url": url,
                "text": """
                    <html>
                      <body>
                        <p>You can view any transaction that has occurred over the last 18 months, to a maximum of 1000 transactions.</p>
                        <form name="RCLStatement">
                          <input type="hidden" name="REQUEST" value="AcctTransactionInquiry">
                        </form>
                        <div id="dlHistSort">
                          <table>
                            <tr>
                              <th></th><th>Date</th><th>Description</th><th>Debits</th><th>Credits</th><th>Balance</th>
                            </tr>
                            <tr class="dataTableLightRow">
                              <td></td><td>Dec 31, 2024</td><td>Before window</td><td>$1.00</td><td></td><td>$99.00</td>
                            </tr>
                            <tr class="dataTableDarkRow">
                              <td></td><td>Jan 03, 2025</td><td>In window</td><td></td><td>$2.00</td><td>$101.00</td>
                            </tr>
                          </table>
                        </div>
                      </body>
                    </html>
                """,
                "payload": None,
            }

        async def recorder(event):
            recorded_events.append(event)

        with patch.object(rbc, "_post_rbc_form_direct", side_effect=fake_post_form):
            transactions, succeeded_accounts, failed_accounts, network_failed_accounts = await rbc.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "encrypted_id": "encrypted",
                        "external_id": "rbc:L001",
                        "category": "linesLoans",
                        "name": "Line of credit",
                        "account_type": "loc",
                        "link_params": {
                            "REQUEST": "AcctTransactionInquiry",
                            "ACCOUNT_TYPE": "R",
                            "PLOAN": "loan-ref",
                            "SHORT_NUMBER": "short-ref",
                        },
                    }
                ],
                user_agent="ua",
                mode_per_account={
                    "rbc:L001": {
                        "mode": "backfill",
                        "backfill_start_date": "2025-01-01",
                        "backfill_end_date": "2025-01-10",
                    }
                },
                transaction_window_recorder=recorder,
            )

        self.assertEqual(rbc.RBC_LOC_HISTORY_URL, posts[0][0])
        self.assertEqual("AcctTransactionInquiry", posts[0][1]["REQUEST"])
        self.assertEqual("R", posts[0][1]["ACCOUNT_TYPE"])
        self.assertEqual("loan-ref", posts[0][1]["PLOAN"])
        self.assertEqual(["In window"], [txn["description"] for txn in transactions["rbc:L001"]])
        self.assertEqual([2.0], [txn["amount"] for txn in transactions["rbc:L001"]])
        self.assertEqual({"rbc:L001"}, succeeded_accounts)
        self.assertEqual(set(), failed_accounts)
        self.assertEqual(set(), network_failed_accounts)
        self.assertEqual(["started", "completed"], [event["event"] for event in recorded_events])
        self.assertEqual(["In window"], [txn["description"] for txn in recorded_events[-1]["transactions"]])

    async def test_loc_history_empty_legacy_page_succeeds(self) -> None:
        recorded_events: list[dict] = []

        async def fake_post_form(storage_state, url, payload, *, user_agent):
            del storage_state, url, payload, user_agent
            return {
                "status": 200,
                "ok": True,
                "url": rbc.RBC_LOC_HISTORY_URL,
                "text": """
                    <html>
                      <body>
                        <p>You can view any transaction that has occurred over the last 18 months, to a maximum of 1000 transactions.</p>
                        <form name="RCLStatement">
                          <select name="DRORCR"><option value="ALL">All Transactions</option></select>
                        </form>
                      </body>
                    </html>
                """,
                "payload": None,
            }

        async def recorder(event):
            recorded_events.append(event)

        with patch.object(rbc, "_post_rbc_form_direct", side_effect=fake_post_form):
            transactions, succeeded_accounts, failed_accounts, network_failed_accounts = await rbc.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "encrypted_id": "encrypted",
                        "external_id": "rbc:L001",
                        "category": "linesLoans",
                        "name": "Line of credit",
                        "account_type": "loc",
                        "link_params": {
                            "REQUEST": "AcctTransactionInquiry",
                            "ACCOUNT_TYPE": "R",
                            "PLOAN": "loan-ref",
                        },
                    }
                ],
                user_agent="ua",
                mode_per_account={
                    "rbc:L001": {
                        "mode": "backfill",
                        "backfill_start_date": "2025-01-01",
                        "backfill_end_date": "2025-01-10",
                    }
                },
                transaction_window_recorder=recorder,
            )

        self.assertEqual({}, transactions)
        self.assertEqual({"rbc:L001"}, succeeded_accounts)
        self.assertEqual(set(), failed_accounts)
        self.assertEqual(set(), network_failed_accounts)
        self.assertEqual(["started", "completed"], [event["event"] for event in recorded_events])
        self.assertEqual([], recorded_events[-1]["transactions"])

    async def test_loc_history_parses_legacy_javascript_rows(self) -> None:
        async def fake_post_form(storage_state, url, payload, *, user_agent):
            del storage_state, url, payload, user_agent
            return {
                "status": 200,
                "ok": True,
                "url": rbc.RBC_LOC_HISTORY_URL,
                "text": """
                    <html>
                      <body>
                        <p>You can view any transaction that has occurred over the last 18 months, to a maximum of 1000 transactions.</p>
                        <div id="dlHistSort"></div>
                        <script>
                          v3m_line[v3m_MC] = new f3mdetrcl_HistLine(
                            v3m_Index,
                            "1 May 2026",
                            "1 May 2026",
                            "INTEREST PAYMENT",
                            "",
                            "36.78",
                            "0.00",
                            "",
                            ""
                          );
                          v3m_MC++;
                          v3m_line[v3m_MC] = new f3mdetrcl_HistLine(
                            v3m_Index,
                            "26 Aug 2025",
                            "26 Aug 2025",
                            "WWW TFR VIN0-08157",
                            "1,000.00",
                            "",
                            "13,400.00",
                            "",
                            ""
                          );
                          v3m_MC++;
                        </script>
                      </body>
                    </html>
                """,
                "payload": None,
            }

        with patch.object(rbc, "_post_rbc_form_direct", side_effect=fake_post_form):
            transactions, succeeded_accounts, failed_accounts, _network_failed_accounts = await rbc.fetch_transactions_direct(
                {"cookies": []},
                [
                    {
                        "encrypted_id": "encrypted",
                        "external_id": "rbc:L001",
                        "category": "linesLoans",
                        "name": "Line of credit",
                        "account_type": "loc",
                        "link_params": {
                            "REQUEST": "AcctTransactionInquiry",
                            "ACCOUNT_TYPE": "R",
                            "PLOAN": "loan-ref",
                        },
                    }
                ],
                user_agent="ua",
                mode_per_account={
                    "rbc:L001": {
                        "mode": "backfill",
                        "backfill_start_date": "2025-01-01",
                        "backfill_end_date": "2026-05-15",
                    }
                },
            )

        self.assertEqual({"rbc:L001"}, succeeded_accounts)
        self.assertEqual(set(), failed_accounts)
        self.assertEqual(["INTEREST PAYMENT", "WWW TFR VIN0-08157"], [txn["description"] for txn in transactions["rbc:L001"]])
        self.assertEqual([36.78, -1000.0], [txn["amount"] for txn in transactions["rbc:L001"]])
        self.assertEqual(["CREDIT", "DEBIT"], [txn["creditDebitIndicator"] for txn in transactions["rbc:L001"]])

    def test_account_list_preserves_nested_loc_link_params(self) -> None:
        accounts = rbc._rbc_accounts_from_account_list_payload(
            {
                "linesLoans": {
                    "accounts": [
                        {
                            "accountId": "L001",
                            "accountNumber": "0002",
                            "accountCurrency": {"currencyCode": "CAD"},
                            "currentBalance": 50,
                            "encryptedAccountNumber": "loc-encrypted",
                            "nickName": "Line of Credit",
                            "product": {
                                "productIdentifier": "LOC",
                                "productName": "Line of Credit",
                                "productTypeName": "Line of Credit",
                            },
                            "linkParams": [
                                {
                                    "linkType": "Navigation",
                                    "formParams": [
                                        {"paramKey": "REQUEST", "paramValue": "AcctTransactionInquiry"},
                                        {"paramKey": "ACCOUNT_TYPE", "paramValue": "R"},
                                        {"paramKey": "PLOAN", "paramValue": "loan-ref"},
                                    ],
                                },
                                {"key": "SHORT_NUMBER", "value": "short-ref"},
                            ],
                        }
                    ],
                }
            }
        )

        self.assertEqual(1, len(accounts))
        self.assertEqual("linesLoans", accounts[0]["category"])
        self.assertEqual("AcctTransactionInquiry", accounts[0]["link_params"]["REQUEST"])
        self.assertEqual("R", accounts[0]["link_params"]["ACCOUNT_TYPE"])
        self.assertEqual("loan-ref", accounts[0]["link_params"]["PLOAN"])
        self.assertEqual("short-ref", accounts[0]["link_params"]["SHORT_NUMBER"])

    async def test_deposit_backfill_fetches_extra_day_and_clamps_to_planned_window(self) -> None:
        urls: list[str] = []
        completed_events: list[dict] = []

        async def fake_fetch_json(storage_state, url, *, user_agent, label):
            del storage_state, user_agent, label
            urls.append(url)
            return {
                "hasError": False,
                "transactionList": [
                    {
                        "id": "too-old",
                        "bookingDate": "2026-05-09",
                        "amount": 1,
                        "description": ["Too old"],
                        "creditDebitIndicator": "CREDIT",
                    },
                    {
                        "id": "in-window",
                        "bookingDate": "2026-05-10",
                        "amount": 2,
                        "description": ["In window"],
                        "creditDebitIndicator": "CREDIT",
                    },
                ],
            }

        async def recorder(event):
            if event.get("event") == "completed":
                completed_events.append(event)

        accounts = [
            {
                "encrypted_id": "encrypted",
                "external_id": "rbc:S001",
                "category": "depositAccounts",
                "name": "Main",
                "account_type": "savings",
            }
        ]
        mode_per_account = {
            "rbc:S001": {
                "mode": "backfill",
                "backfill_start_date": "2026-05-10",
                "backfill_end_date": "2026-05-20",
            }
        }

        with patch.object(rbc, "_fetch_rbc_json_direct", side_effect=fake_fetch_json):
            transactions, succeeded_accounts, failed_accounts, network_failed_accounts = await rbc.fetch_transactions_direct(
                {"cookies": []},
                accounts,
                user_agent="ua",
                mode_per_account=mode_per_account,
                backfill_days=10,
                transaction_window_recorder=recorder,
            )

        self.assertIn("intervalValue=11", urls[0])
        self.assertEqual(["in-window"], [txn["id"] for txn in transactions["rbc:S001"]])
        self.assertEqual(["in-window"], [txn["id"] for txn in completed_events[0]["transactions"]])
        self.assertEqual({"rbc:S001"}, succeeded_accounts)
        self.assertEqual(set(), failed_accounts)
        self.assertEqual(set(), network_failed_accounts)

    async def test_backfill_keeps_older_windows_when_recent_window_fails(self) -> None:
        windows = rbc._rbc_cc_backfill_windows(400)
        self.assertGreaterEqual(len(windows), 2)
        failed_window = windows[0]

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
        ):
            del storage_state, encrypted_id, external_id, user_agent, split_depth, window_results
            del refresh_context
            if (start_date, end_date) == failed_window:
                failure_results.append(
                    (start_date, end_date, rbc.RBC_CC_SEARCH_FAILURE_OTHER)
                )
                return None, False
            return [
                {
                    "transactionId": f"txn-{start_date.isoformat()}",
                    "bookingDate": start_date.isoformat(),
                    "amount": 1,
                    "description": ["sample"],
                    "creditDebitIndicator": "DEBIT",
                }
            ], True

        with patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=400,
                user_agent="ua",
            )

        self.assertFalse(succeeded)
        self.assertEqual(len(windows) - 1, len(transactions))
        self.assertTrue(all(txn["transactionId"].startswith("txn-") for txn in transactions))

    async def test_exact_http_500_leaf_gets_one_deferred_recovery_pass(self) -> None:
        failed_window = (date(2022, 9, 9), date(2023, 3, 9))
        successful_window = (date(2023, 3, 10), date(2023, 9, 8))
        calls: list[tuple[date, date]] = []
        events: list[dict] = []

        def transaction(transaction_id: str, booking_date: date) -> dict:
            return {
                "transactionId": transaction_id,
                "bookingDate": booking_date.isoformat(),
                "amount": 1,
                "description": [transaction_id],
                "creditDebitIndicator": "DEBIT",
            }

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            current_window = (start_date, end_date)
            calls.append(current_window)
            if current_window == failed_window and calls.count(failed_window) == 1:
                window_results.append((*failed_window, [], False))
                failure_results.append((*failed_window, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500))
                return None, False
            recovered = transaction(
                "recovered" if current_window == failed_window else "already-complete",
                start_date,
            )
            window_results.append((start_date, end_date, [recovered], True))
            return [recovered], True

        async def recorder(event):
            events.append(event)

        sleep = AsyncMock()
        warmup = AsyncMock()
        with (
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", new=warmup),
            patch.object(rbc.asyncio, "sleep", new=sleep),
        ):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=365,
                user_agent="ua",
                windows=[failed_window, successful_window],
                transaction_window_recorder=recorder,
            )

        self.assertTrue(succeeded)
        self.assertEqual([failed_window, successful_window, failed_window], calls)
        self.assertEqual(
            {"already-complete", "recovered"},
            {transaction["transactionId"] for transaction in transactions},
        )
        sleep.assert_awaited_once_with(rbc.RBC_CC_SEARCH_DEFERRED_RECOVERY_DELAYS_SECONDS[0])
        warmup.assert_awaited_once()
        failed_window_events = [
            event["event"]
            for event in events
            if (event["start_date"], event["end_date"])
            == tuple(day.isoformat() for day in failed_window)
        ]
        self.assertEqual(["started", "started", "completed"], failed_window_events)

    async def test_deferred_recovery_exhaustion_stays_failed_without_looping(self) -> None:
        failed_window = (date(2022, 9, 9), date(2023, 3, 9))
        calls = 0
        events: list[dict] = []

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            nonlocal calls
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            calls += 1
            window_results.append((start_date, end_date, [], False))
            failure_results.append(
                (start_date, end_date, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500)
            )
            return None, False

        async def recorder(event):
            events.append(event)

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", new=AsyncMock()),
            patch.object(rbc.asyncio, "sleep", new=AsyncMock()),
        ):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=365,
                user_agent="ua",
                windows=[failed_window],
                transaction_window_recorder=recorder,
            )

        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(4, calls)
        self.assertEqual(
            ["started", "started", "started", "started", "failed"],
            [event["event"] for event in events],
        )

    async def test_deferred_recovery_is_round_robin_across_exact_http_500_windows(self) -> None:
        stubborn_window = (date(2021, 9, 9), date(2022, 9, 8))
        recoverable_window = (date(2022, 9, 9), date(2023, 9, 8))
        calls: list[tuple[date, date]] = []
        events: list[dict] = []

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            current_window = (start_date, end_date)
            calls.append(current_window)
            if current_window == recoverable_window and calls.count(recoverable_window) == 2:
                transaction = {
                    "transactionId": "recovered",
                    "bookingDate": start_date.isoformat(),
                    "amount": 1,
                    "description": ["recovered"],
                    "creditDebitIndicator": "DEBIT",
                }
                window_results.append((start_date, end_date, [transaction], True))
                return [transaction], True
            window_results.append((start_date, end_date, [], False))
            failure_results.append(
                (start_date, end_date, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500)
            )
            return None, False

        async def recorder(event):
            events.append(event)

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", new=AsyncMock()),
            patch.object(rbc.asyncio, "sleep", new=AsyncMock()),
        ):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=730,
                user_agent="ua",
                windows=[stubborn_window, recoverable_window],
                transaction_window_recorder=recorder,
            )

        self.assertFalse(succeeded)
        self.assertEqual(["recovered"], [transaction["transactionId"] for transaction in transactions])
        self.assertEqual(
            [
                stubborn_window,
                recoverable_window,
                stubborn_window,
                recoverable_window,
                stubborn_window,
                stubborn_window,
            ],
            calls,
        )
        recoverable_events = [
            event["event"]
            for event in events
            if (event["start_date"], event["end_date"])
            == tuple(day.isoformat() for day in recoverable_window)
        ]
        self.assertEqual(["started", "started", "completed"], recoverable_events)

    async def test_deferred_recovery_budget_exhaustion_preserves_retryable_window(self) -> None:
        failed_window = (date(2022, 9, 9), date(2023, 3, 9))
        now = 0.0
        calls = 0
        events: list[dict] = []

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            nonlocal calls
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            calls += 1
            window_results.append((start_date, end_date, [], False))
            failure_results.append(
                (start_date, end_date, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500)
            )
            return None, False

        async def fake_sleep(seconds):
            nonlocal now
            now += seconds

        async def recorder(event):
            events.append(event)

        with (
            patch.object(rbc, "RBC_CC_SEARCH_DEFERRED_RECOVERY_BUDGET_SECONDS", 10),
            patch.object(rbc, "_rbc_monotonic_seconds", side_effect=lambda: now),
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", new=AsyncMock()),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=365,
                user_agent="ua",
                windows=[failed_window],
                transaction_window_recorder=recorder,
            )

        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(1, calls)
        self.assertEqual(["started", "failed"], [event["event"] for event in events])

    async def test_cancellation_during_deferred_recovery_cooldown_propagates(self) -> None:
        failed_window = (date(2022, 9, 9), date(2023, 3, 9))

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            window_results.append((start_date, end_date, [], False))
            failure_results.append(
                (start_date, end_date, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500)
            )
            return None, False

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc.asyncio, "sleep", side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await rbc._collect_rbc_cc_backward_history_direct(
                    {"cookies": []},
                    account={
                        "encrypted_id": "encrypted",
                        "external_id": "rbc:V001",
                        "category": "creditCards",
                    },
                    encrypted_id="encrypted",
                    external_id="rbc:V001",
                    backfill_days=365,
                    user_agent="ua",
                    windows=[failed_window],
                )

    async def test_deferred_child_recovery_completes_split_parent(self) -> None:
        parent = (date(2022, 9, 9), date(2023, 9, 8))
        left = (date(2022, 9, 9), date(2023, 3, 9))
        right = (date(2023, 3, 10), date(2023, 9, 8))
        events: list[dict] = []
        calls: list[tuple[date, date]] = []
        right_txn = {
            "transactionId": "right",
            "bookingDate": right[0].isoformat(),
            "amount": 1,
            "description": ["right"],
            "creditDebitIndicator": "DEBIT",
        }
        left_txn = {
            "transactionId": "left",
            "bookingDate": left[0].isoformat(),
            "amount": 1,
            "description": ["left"],
            "creditDebitIndicator": "DEBIT",
        }

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
            max_attempts_override=None,
        ):
            del storage_state, encrypted_id, external_id, user_agent, split_depth, refresh_context
            del max_attempts_override
            calls.append((start_date, end_date))
            if (start_date, end_date) == parent:
                window_results.append((*left, [], False))
                window_results.append((*right, [right_txn], True))
                failure_results.append((*left, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500))
                return [right_txn], False
            window_results.append((*left, [left_txn], True))
            return [left_txn], True

        async def recorder(event):
            events.append(event)

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window),
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", new=AsyncMock()),
            patch.object(rbc.asyncio, "sleep", new=AsyncMock()),
        ):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=365,
                user_agent="ua",
                windows=[parent],
                transaction_window_recorder=recorder,
            )

        self.assertTrue(succeeded)
        self.assertEqual([parent, left], calls)
        self.assertEqual({"left", "right"}, {txn["transactionId"] for txn in transactions})
        parent_events = [
            event["event"]
            for event in events
            if (event["start_date"], event["end_date"])
            == tuple(day.isoformat() for day in parent)
        ]
        self.assertEqual(["started", "completed"], parent_events)

    async def test_backfill_closes_split_parent_window_when_children_succeed(self) -> None:
        events: list[dict] = []
        parent = (date(2021, 5, 18), date(2022, 5, 17))
        left = (date(2021, 5, 18), date(2021, 11, 15))
        right = (date(2021, 11, 16), date(2022, 5, 17))
        left_txn = {
            "transactionId": "left",
            "bookingDate": left[0].isoformat(),
            "amount": 1,
            "description": ["left"],
            "creditDebitIndicator": "DEBIT",
        }
        right_txn = {
            "transactionId": "right",
            "bookingDate": right[0].isoformat(),
            "amount": 2,
            "description": ["right"],
            "creditDebitIndicator": "DEBIT",
        }

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
        ):
            del storage_state, encrypted_id, external_id, start_date, end_date, user_agent
            del split_depth, failure_results, refresh_context
            window_results.append((*left, [left_txn], True))
            window_results.append((*right, [right_txn], True))
            return [left_txn, right_txn], True

        async def recorder(event):
            events.append(event)

        with patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=365,
                user_agent="ua",
                windows=[parent],
                transaction_window_recorder=recorder,
            )

        completed_ranges = [
            (event["start_date"], event["end_date"])
            for event in events
            if event["event"] == "completed"
        ]
        self.assertTrue(succeeded)
        self.assertEqual(["left", "right"], [txn["transactionId"] for txn in transactions])
        self.assertIn(tuple(day.isoformat() for day in left), completed_ranges)
        self.assertIn(tuple(day.isoformat() for day in right), completed_ranges)
        self.assertIn(tuple(day.isoformat() for day in parent), completed_ranges)

    async def test_backfill_windows_are_searched_serially(self) -> None:
        active_requests = 0
        max_active_requests = 0

        async def fake_fetch_window(
            storage_state,
            *,
            encrypted_id,
            external_id,
            start_date,
            end_date,
            user_agent,
            split_depth=0,
            window_results=None,
            failure_results=None,
            refresh_context=None,
        ):
            del storage_state, encrypted_id, external_id, start_date, end_date, user_agent
            del split_depth, window_results, failure_results, refresh_context
            nonlocal active_requests, max_active_requests
            active_requests += 1
            max_active_requests = max(max_active_requests, active_requests)
            await asyncio.sleep(0.01)
            active_requests -= 1
            return [], True

        with patch.object(rbc, "_fetch_rbc_cc_search_window_direct", side_effect=fake_fetch_window):
            transactions, succeeded = await rbc._collect_rbc_cc_backward_history_direct(
                {"cookies": []},
                account={
                    "encrypted_id": "encrypted",
                    "external_id": "rbc:V001",
                    "category": "creditCards",
                },
                encrypted_id="encrypted",
                external_id="rbc:V001",
                backfill_days=900,
                user_agent="ua",
            )

        self.assertTrue(succeeded)
        self.assertEqual([], transactions)
        self.assertEqual(1, max_active_requests)

    async def test_backfill_uses_search_only_and_keeps_partial_rows_retryable(self) -> None:
        calls: list[str] = []
        accounts = [
            {
                "encrypted_id": "encrypted",
                "external_id": "rbc:V001",
                "category": "creditCards",
            }
        ]

        async def fake_search(
            storage_state,
            *,
            account,
            encrypted_id,
            external_id,
            backfill_days,
            user_agent,
            windows=None,
            transaction_window_recorder=None,
        ):
            del storage_state, account, encrypted_id, external_id, backfill_days, user_agent, windows, transaction_window_recorder
            calls.append("search")
            return [
                {
                    "transactionId": "search-1",
                    "bookingDate": "2024-01-01",
                    "amount": 1,
                    "description": ["search"],
                    "creditDebitIndicator": "DEBIT",
                }
            ], False

        async def fake_warmup(storage_state, *, encrypted_id, external_id, user_agent):
            del storage_state, encrypted_id, external_id, user_agent
            calls.append("warmup")

        with (
            patch.object(rbc, "_warm_rbc_cc_account_context_direct", side_effect=fake_warmup),
            patch.object(rbc, "_collect_rbc_cc_backward_history_direct", side_effect=fake_search),
        ):
            (
                transactions,
                succeeded_accounts,
                failed_accounts,
                network_failed_accounts,
            ) = await rbc.fetch_transactions_direct(
                {"cookies": []},
                accounts,
                user_agent="ua",
            )

        self.assertEqual(["warmup", "search"], calls)
        self.assertEqual(["search-1"], [txn["transactionId"] for txn in transactions["rbc:V001"]])
        self.assertNotIn("rbc:V001", succeeded_accounts)
        self.assertEqual({"rbc:V001"}, failed_accounts)
        self.assertEqual(set(), network_failed_accounts)

    async def test_saved_artifact_sync_fails_when_all_transaction_accounts_fail(self) -> None:
        async def fake_fetch_json(storage_state, url, *, user_agent, label):
            del storage_state, url, user_agent, label
            return {
                "depositAccounts": {
                    "accounts": [
                        {
                            "accountId": "D001",
                            "accountNumber": "0001",
                            "accountCurrency": {"currencyCode": "CAD"},
                            "currentBalance": 100,
                            "encryptedAccountNumber": "encrypted",
                            "nickName": "Chequing",
                            "product": {
                                "productIdentifier": "CHEQUING",
                                "productName": "Day to Day Banking",
                                "productTypeName": "Chequing",
                            },
                        }
                    ],
                },
            }

        async def fake_fetch_transactions(
            storage_state,
            accounts,
            *,
            user_agent,
            mode_per_account=None,
            backfill_days=rbc.RBC_BACKFILL_DAYS,
            incremental_days=rbc.RBC_INCREMENTAL_DAYS,
            transaction_window_recorder=None,
        ):
            del storage_state, accounts, user_agent, mode_per_account
            del backfill_days, incremental_days, transaction_window_recorder
            return {}, set(), {"rbc:D001"}, set()

        with (
            patch.object(rbc, "_fetch_rbc_json_direct", side_effect=fake_fetch_json),
            patch.object(rbc, "fetch_transactions_direct", side_effect=fake_fetch_transactions),
            patch.object(rbc, "_rbc_log_event"),
        ):
            result = await rbc._sync_rbc_with_saved_artifacts(
                {"cookies": []},
                {},
                sync_scope="transactions",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "RBC transaction fetch failed for 1 account.",
            result["message"],
        )
        self.assertEqual(["rbc:D001"], result["transaction_fetch_failed_accounts"])

    async def test_saved_artifact_sync_fails_when_any_transaction_account_fails(self) -> None:
        async def fake_fetch_json(storage_state, url, *, user_agent, label):
            del storage_state, url, user_agent, label
            return {
                "depositAccounts": {
                    "accounts": [
                        {
                            "accountId": "D001",
                            "accountNumber": "0001",
                            "accountCurrency": {"currencyCode": "CAD"},
                            "currentBalance": 100,
                            "encryptedAccountNumber": "deposit-encrypted",
                            "nickName": "Chequing",
                            "product": {
                                "productIdentifier": "CHEQUING",
                                "productName": "Day to Day Banking",
                                "productTypeName": "Chequing",
                            },
                        }
                    ],
                },
                "linesLoans": {
                    "accounts": [
                        {
                            "accountId": "L001",
                            "accountNumber": "0002",
                            "accountCurrency": {"currencyCode": "CAD"},
                            "currentBalance": 50,
                            "encryptedAccountNumber": "loc-encrypted",
                            "nickName": "Line of Credit",
                            "product": {
                                "productIdentifier": "LOC",
                                "productName": "Line of Credit",
                                "productTypeName": "Line of Credit",
                            },
                        }
                    ],
                },
            }

        async def fake_fetch_transactions(
            storage_state,
            accounts,
            *,
            user_agent,
            mode_per_account=None,
            backfill_days=rbc.RBC_BACKFILL_DAYS,
            incremental_days=rbc.RBC_INCREMENTAL_DAYS,
            transaction_window_recorder=None,
        ):
            del storage_state, accounts, user_agent, mode_per_account
            del backfill_days, incremental_days, transaction_window_recorder
            return {}, {"rbc:D001"}, {"rbc:L001"}, set()

        with (
            patch.object(rbc, "_fetch_rbc_json_direct", side_effect=fake_fetch_json),
            patch.object(rbc, "fetch_transactions_direct", side_effect=fake_fetch_transactions),
            patch.object(rbc, "_rbc_log_event"),
        ):
            result = await rbc._sync_rbc_with_saved_artifacts(
                {"cookies": []},
                {},
                sync_scope="transactions",
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("RBC transaction fetch failed for 1 account.", result["message"])
        self.assertEqual(["rbc:D001"], result["transaction_fetch_succeeded_accounts"])
        self.assertEqual(["rbc:L001"], result["transaction_fetch_failed_accounts"])

    async def test_credit_card_warmup_uses_browser_sequence_and_encoded_account_id(self) -> None:
        urls: list[str] = []

        async def fake_fetch(storage_state, url, *, user_agent, label):
            del storage_state, user_agent, label
            urls.append(url)
            return {"hasError": False}

        with (
            patch.object(rbc, "_rbc_timestamp_ms", return_value=1778797841947),
            patch.object(rbc, "_fetch_rbc_json_direct", side_effect=fake_fetch),
        ):
            await rbc._warm_rbc_cc_account_context_direct(
                {"cookies": []},
                encrypted_id="abc+=/",
                external_id="rbc:V001",
                user_agent="ua",
            )

        self.assertEqual(5, len(urls))
        self.assertIn("/arrangements/credit-card/abc%2B%3D%2F?", urls[0])
        self.assertIn("/credit-card-activation?", urls[1])
        self.assertIn("/line-of-business?", urls[2])
        self.assertIn("/transactions/cc/posted/account/abc%2B%3D%2F?", urls[3])
        self.assertIn("/transactions/cc/authorized/account/abc%2B%3D%2F?", urls[4])
        self.assertTrue(all("timestamp=" in url for url in urls))

    async def test_failed_credit_card_window_retries_same_range_before_splitting(self) -> None:
        calls: list[tuple[str, str]] = []

        async def fake_fetch_chunk(
            storage_state,
            *,
            encrypted_id,
            external_id,
            from_date,
            to_date,
            user_agent,
            attempt=1,
            max_attempts=1,
        ):
            del storage_state, encrypted_id, external_id, user_agent, attempt, max_attempts
            calls.append((from_date, to_date))
            if len(calls) < 3:
                return None, False, rbc.RBC_CC_SEARCH_FAILURE_OTHER
            return [
                {
                    "transactionId": "retry-success-1",
                    "bookingDate": from_date,
                    "amount": 1,
                    "description": ["retry success"],
                    "creditDebitIndicator": "DEBIT",
                }
            ], True, None

        start = date.today() - timedelta(days=31)
        end = date.today()

        async def fake_sleep(_seconds):
            return None

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=start,
                end_date=end,
                user_agent="ua",
            )

        self.assertEqual(3, len(calls))
        self.assertEqual(1, len(set(calls)))
        self.assertTrue(succeeded)
        self.assertEqual(["retry-success-1"], [txn["transactionId"] for txn in transactions])

    async def test_credit_card_chunk_classifies_exact_http_500(self) -> None:
        response = {
            "__breaktwenty_rbc_debug": {
                "http_non_ok": True,
                "http_status": 500,
                "parse_failed": True,
            }
        }
        with patch.object(rbc, "_post_rbc_json_direct", new=AsyncMock(return_value=response)):
            transactions, succeeded, failure_kind = await rbc._fetch_rbc_cc_search_chunk_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                from_date="2022-09-08",
                to_date="2023-03-08",
                user_agent="ua",
            )

        self.assertIsNone(transactions)
        self.assertFalse(succeeded)
        self.assertEqual(rbc.RBC_CC_SEARCH_FAILURE_HTTP_500, failure_kind)

    async def test_three_exact_http_500_failures_get_one_delayed_final_attempt(self) -> None:
        calls: list[tuple[str, str]] = []
        sleeps: list[float] = []

        async def fake_fetch_chunk(
            storage_state,
            *,
            encrypted_id,
            external_id,
            from_date,
            to_date,
            user_agent,
            attempt=1,
            max_attempts=1,
        ):
            del storage_state, encrypted_id, external_id, user_agent, attempt, max_attempts
            calls.append((from_date, to_date))
            if len(calls) <= 3:
                return None, False, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500
            return [
                {
                    "transactionId": "delayed-success",
                    "bookingDate": from_date,
                    "amount": 1,
                    "description": ["delayed success"],
                    "creditDebitIndicator": "DEBIT",
                }
            ], True, None

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        start = date(2022, 9, 8)
        end = date(2023, 3, 8)
        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=start,
                end_date=end,
                user_agent="ua",
            )

        self.assertTrue(succeeded)
        self.assertEqual(["delayed-success"], [txn["transactionId"] for txn in transactions])
        self.assertEqual([(start.isoformat(), end.isoformat())] * 4, calls)
        self.assertEqual([2, 4, 15], sleeps)

    async def test_mixed_failures_do_not_get_http_500_final_attempt(self) -> None:
        failures = iter(
            (
                rbc.RBC_CC_SEARCH_FAILURE_HTTP_500,
                rbc.RBC_CC_SEARCH_FAILURE_OTHER,
                rbc.RBC_CC_SEARCH_FAILURE_HTTP_500,
            )
        )
        calls = 0

        async def fake_fetch_chunk(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return None, False, next(failures)

        sleep = AsyncMock()
        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", new=sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=date(2022, 9, 8),
                end_date=date(2023, 3, 8),
                user_agent="ua",
            )

        self.assertEqual(3, calls)
        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual([2, 4], [call.args[0] for call in sleep.await_args_list])

    async def test_http_500_final_attempt_respects_total_window_deadline(self) -> None:
        now = 0.0
        calls = 0
        sleeps: list[float] = []

        async def fake_fetch_chunk(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return None, False, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500

        async def fake_sleep(seconds):
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        with (
            patch.object(rbc, "RBC_CC_SEARCH_WINDOW_DEADLINE_SECONDS", 10),
            patch.object(rbc, "_rbc_monotonic_seconds", side_effect=lambda: now),
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=date(2022, 9, 8),
                end_date=date(2023, 3, 8),
                user_agent="ua",
            )

        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(3, calls)
        self.assertEqual([2, 4, 4], sleeps)

    async def test_four_exact_http_500_failures_leave_window_retryable(self) -> None:
        calls = 0
        window_results: list[rbc.RBCCreditCardSearchWindow] = []
        failure_results: list[rbc.RBCCreditCardSearchFailure] = []

        async def fake_fetch_chunk(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return None, False, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", new=AsyncMock()),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=date(2022, 9, 8),
                end_date=date(2023, 3, 8),
                user_agent="ua",
                window_results=window_results,
                failure_results=failure_results,
            )

        self.assertEqual(4, calls)
        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(
            [(date(2022, 9, 8), date(2023, 3, 8), [], False)],
            window_results,
        )
        self.assertEqual(
            [
                (
                    date(2022, 9, 8),
                    date(2023, 3, 8),
                    rbc.RBC_CC_SEARCH_FAILURE_HTTP_500,
                )
            ],
            failure_results,
        )

    async def test_deferred_exact_http_500_probe_is_one_request_without_inner_backoff(self) -> None:
        calls = 0
        failures: list[rbc.RBCCreditCardSearchFailure] = []

        async def fake_fetch_chunk(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return None, False, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500

        sleep = AsyncMock()
        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", new=sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=date(2022, 9, 8),
                end_date=date(2023, 3, 8),
                user_agent="ua",
                failure_results=failures,
                max_attempts_override=1,
            )

        self.assertEqual(1, calls)
        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(
            [(date(2022, 9, 8), date(2023, 3, 8), rbc.RBC_CC_SEARCH_FAILURE_HTTP_500)],
            failures,
        )
        sleep.assert_not_awaited()

    async def test_cancellation_during_http_500_delay_propagates(self) -> None:
        calls = 0
        window_results: list[rbc.RBCCreditCardSearchWindow] = []

        async def fake_fetch_chunk(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return None, False, rbc.RBC_CC_SEARCH_FAILURE_HTTP_500

        async def fake_sleep(seconds):
            if seconds == rbc.RBC_CC_SEARCH_HTTP_500_FINAL_WAIT_SECONDS:
                raise asyncio.CancelledError

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await rbc._fetch_rbc_cc_search_window_direct(
                    {"cookies": []},
                    encrypted_id="encrypted",
                    external_id="rbc:V001",
                    start_date=date(2022, 9, 8),
                    end_date=date(2023, 3, 8),
                    user_agent="ua",
                    window_results=window_results,
                )

        self.assertEqual(3, calls)
        self.assertEqual([], window_results)

    async def test_persistently_failed_credit_card_window_splits_after_retries(self) -> None:
        calls: list[tuple[str, str]] = []

        async def fake_fetch_chunk(
            storage_state,
            *,
            encrypted_id,
            external_id,
            from_date,
            to_date,
            user_agent,
            attempt=1,
            max_attempts=1,
        ):
            del storage_state, encrypted_id, external_id, user_agent, attempt, max_attempts
            calls.append((from_date, to_date))
            if len(calls) == 1:
                return None, False, rbc.RBC_CC_SEARCH_FAILURE_OTHER
            return [
                {
                    "transactionId": f"split-{from_date}",
                    "bookingDate": from_date,
                    "amount": 1,
                    "description": ["split child"],
                    "creditDebitIndicator": "DEBIT",
                }
            ], True, None

        start = date.today() - timedelta(days=31)
        end = date.today()

        with (
            patch.object(rbc, "RBC_CC_FAILED_SEARCH_MAX_FAIL_SPLIT_DEPTH", 1),
            patch.object(rbc, "RBC_CC_SEARCH_RETRY_ATTEMPTS", 1),
            patch.object(rbc, "RBC_CC_SEARCH_MIN_WINDOW_RETRY_ATTEMPTS", 1),
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=start,
                end_date=end,
                user_agent="ua",
            )

        self.assertEqual(3, len(calls))
        self.assertTrue(succeeded)
        self.assertEqual(2, len(transactions))

    async def test_failed_credit_card_min_window_is_not_split(self) -> None:
        calls: list[tuple[str, str]] = []

        async def fake_fetch_chunk(
            storage_state,
            *,
            encrypted_id,
            external_id,
            from_date,
            to_date,
            user_agent,
            attempt=1,
            max_attempts=1,
        ):
            del storage_state, encrypted_id, external_id, user_agent, attempt, max_attempts
            calls.append((from_date, to_date))
            return None, False, rbc.RBC_CC_SEARCH_FAILURE_OTHER

        start = date.today() - timedelta(days=rbc.RBC_CC_MIN_SEARCH_WINDOW_DAYS - 1)
        end = date.today()

        async def fake_sleep(_seconds):
            return None

        with (
            patch.object(rbc, "_fetch_rbc_cc_search_chunk_direct", side_effect=fake_fetch_chunk),
            patch.object(rbc.asyncio, "sleep", side_effect=fake_sleep),
        ):
            transactions, succeeded = await rbc._fetch_rbc_cc_search_window_direct(
                {"cookies": []},
                encrypted_id="encrypted",
                external_id="rbc:V001",
                start_date=start,
                end_date=end,
                user_agent="ua",
            )

        self.assertEqual([], transactions)
        self.assertFalse(succeeded)
        self.assertEqual(
            [(start.isoformat(), end.isoformat())] * rbc.RBC_CC_SEARCH_MIN_WINDOW_RETRY_ATTEMPTS,
            calls,
        )


if __name__ == "__main__":
    unittest.main()
