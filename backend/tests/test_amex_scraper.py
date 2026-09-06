import socket
import unittest
from unittest.mock import AsyncMock, patch

from app.scrapers import amex
from app.connectors.amex import _normalize_amex_transaction
from app.scrapers.results import normalize_scraper_result


class _FakeActivityResponse:
    status_code = 200
    is_success = True

    def __init__(self, payload):
        self.payload = payload
        self.url = amex.AMEX_READ_ACCOUNT_ACTIVITY_URL
        self.text = "{}"

    def json(self):
        return self.payload


class _FakeActivityClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeActivityResponse(self.payloads.pop(0))


class _FakeReplayHttpClient:
    def __init__(self):
        self.client = object()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None


_UNSET = object()


class _FakeReplaySession:
    def __init__(self, *, storage_state=_UNSET, session_artifact=None):
        self.user_id = None
        self.storage_state = {"cookies": [], "origins": []} if storage_state is _UNSET else storage_state
        self.session_artifact = session_artifact if session_artifact is not None else {"user_agent": "ua"}
        self.user_agent = self.session_artifact.get("user_agent") or "ua"
        self.refreshed_storage_state = None

    def load(self):
        return self

    def missing_artifact_result(self):
        amex._amex_log_event("headless sync missing storage_state", user_id=self.user_id, level="warning")
        return amex.scraper_auth_required()

    def http_client(self, **kwargs):
        return _FakeReplayHttpClient()

    async def refresh_runtime_artifacts_async(self, storage_state, **kwargs):
        self.refreshed_storage_state = storage_state
        return True

    def normalize_result(self, payload):
        return normalize_scraper_result(payload)


def _fake_replay_factory(fake_replay):
    async def factory(provider, user_id, **kwargs):
        fake_replay.user_id = user_id
        return fake_replay

    return factory


class AmexDirectSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_activity_uses_year_view_and_offset_pagination(self):
        def payload(count):
            return {
                "member": {"years": [2026, 2025, 2024]},
                "activityData": {
                    "totalTransactionCount": 252,
                    "data": [{"transactions": [{"identifier": f"txn-{index}"} for index in range(count)]}],
                },
            }

        client = _FakeActivityClient([payload(100), payload(100), payload(52)])

        response, rows = await amex._fetch_account_activity_view(
            client,
            {},
            user_agent="ua",
            account={"provider_account_id": "account-token"},
            view=amex.AMEX_ACTIVITY_VIEW_BY_YEAR,
            year=2026,
        )

        self.assertEqual(252, len(rows))
        self.assertEqual(
            [1, 101, 201],
            [call["json"]["transactionFilters"]["offset"] for call in client.calls],
        )
        self.assertTrue(all(call["json"]["view"] == "VIEW_BY_YEAR" for call in client.calls))
        self.assertTrue(all(call["json"]["year"] == 2026 for call in client.calls))
        self.assertTrue(all(call["headers"]["CE-Source"] == "WEB" for call in client.calls))
        self.assertEqual([2026, 2025, 2024], amex._amex_activity_years(response))

    async def test_account_activity_rejects_incomplete_reported_total(self):
        client = _FakeActivityClient(
            [
                {
                    "activityData": {
                        "totalTransactionCount": 1,
                        "data": [{"transactions": []}],
                    },
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "stopped before the reported total"):
            await amex._fetch_account_activity_view(
                client,
                {},
                user_agent="ua",
                account={"provider_account_id": "account-token"},
                view=amex.AMEX_ACTIVITY_VIEW_BY_YEAR,
                year=2026,
            )

    async def test_account_activity_rejects_missing_transaction_structure(self):
        client = _FakeActivityClient(
            [{"activityData": {"totalTransactionCount": 0}}]
        )

        with self.assertRaisesRegex(RuntimeError, "invalid activityData.data"):
            await amex._fetch_account_activity_view(
                client,
                {},
                user_agent="ua",
                account={"provider_account_id": "account-token"},
                view=amex.AMEX_ACTIVITY_VIEW_BY_YEAR,
                year=2026,
            )

    async def test_transaction_backfill_records_full_provider_date_range(self):
        def payload(*, identifier, start_date=None):
            return {
                "member": {
                    "years": [2026, 2025, 2024],
                    "startDateForSearch": start_date,
                },
                "activityData": {
                    "totalTransactionCount": 1 if identifier else 0,
                    "data": [
                        {"transactions": [{"identifier": identifier}] if identifier else []}
                    ],
                },
            }

        client = _FakeActivityClient(
            [
                payload(identifier=None, start_date="2024-06-04"),
                payload(identifier="current"),
                payload(identifier="middle"),
                payload(identifier="historic"),
            ]
        )
        recorded = []

        async def recorder(event):
            recorded.append(event)

        results, succeeded = await amex.fetch_transactions(
            client,
            {},
            [{"name": "Card", "external_id": "account:account-token"}],
            user_agent="ua",
            user_id=1,
            mode_per_account={
                "account:account-token": {
                    "mode": "backfill",
                    "start_date": None,
                    "backfill_start_date": "2025-01-01",
                    "end_date": "2026-07-10",
                }
            },
            transaction_window_recorder=recorder,
        )

        self.assertEqual({"account:account-token"}, succeeded)
        self.assertEqual(
            ["current", "middle", "historic"],
            [row["identifier"] for row in results["account:account-token"]],
        )
        self.assertEqual(
            [("2024-06-04", "2026-07-10")],
            [
                (event["start_date"], event["end_date"])
                for event in recorded
                if event["event"] == "completed"
            ],
        )
        self.assertEqual(
            ["RECENT", "VIEW_BY_YEAR", "VIEW_BY_YEAR", "VIEW_BY_YEAR"],
            [call["json"]["view"] for call in client.calls],
        )
        self.assertEqual([2026, 2025, 2024], [call["json"].get("year") for call in client.calls[1:]])

    async def test_transaction_backfill_rejects_empty_year_when_recent_has_rows(self):
        def payload(identifier, *, start_date=None):
            return {
                "member": {"years": [2026], "startDateForSearch": start_date},
                "activityData": {
                    "totalTransactionCount": 1 if identifier else 0,
                    "data": [{"transactions": [{"identifier": identifier}] if identifier else []}],
                },
            }

        client = _FakeActivityClient(
            [
                payload("recent", start_date="2023-12-11"),
                payload(None),
            ]
        )
        recorded = []

        async def recorder(event):
            recorded.append(event)

        results, succeeded = await amex.fetch_transactions(
            client,
            {},
            [{"name": "Card", "external_id": "account:account-token"}],
            user_agent="ua",
            user_id=1,
            mode_per_account={
                "account:account-token": {
                    "mode": "backfill",
                    "end_date": "2026-07-10",
                }
            },
            transaction_window_recorder=recorder,
        )

        self.assertEqual({}, results)
        self.assertEqual(set(), succeeded)
        self.assertEqual(["started", "failed"], [event["event"] for event in recorded])
        self.assertIn("empty despite recent activity", recorded[-1]["error"])

    async def test_transaction_incremental_keeps_year_activity_fetch(self):
        def payload(identifier):
            return {
                "member": {"years": [2026, 2025, 2024], "startDateForSearch": "2016-06-04"},
                "activityData": {
                    "totalTransactionCount": 1 if identifier else 0,
                    "data": [{"transactions": [{"identifier": identifier}] if identifier else []}],
                },
            }

        client = _FakeActivityClient([payload(None), payload("recent")])
        recorded = []

        async def recorder(event):
            recorded.append(event)

        results, succeeded = await amex.fetch_transactions(
            client,
            {},
            [{"name": "Card", "external_id": "account:account-token"}],
            user_agent="ua",
            user_id=1,
            mode_per_account={
                "account:account-token": {
                    "mode": "incremental",
                    "start_date": "2026-05-23",
                    "end_date": "2026-07-10",
                }
            },
            transaction_window_recorder=recorder,
        )

        self.assertEqual({"account:account-token"}, succeeded)
        self.assertEqual(["recent"], [row["identifier"] for row in results["account:account-token"]])
        self.assertEqual(["RECENT", "VIEW_BY_YEAR"], [call["json"]["view"] for call in client.calls])
        self.assertEqual(("2026-05-23", "2026-07-10"), (
            recorded[-1]["start_date"], recorded[-1]["end_date"]
        ))

    def test_new_activity_transaction_uses_display_description_and_identifier(self):
        transaction = _normalize_amex_transaction(
            {
                "identifier": "activity-id",
                "postDate": "2026-07-10",
                "displayDescription": "Merchant",
                "transactionAmount": {"amount": 12.34, "currency": "CAD"},
                "status": "posted",
                "type": "DEBIT",
            }
        )

        self.assertIsNotNone(transaction)
        self.assertEqual("amex_activity-id", transaction.external_id)
        self.assertEqual("Merchant", transaction.description)
        self.assertEqual(-12.34, transaction.amount)

    def test_new_activity_transaction_skips_pending_rows(self):
        self.assertIsNone(
            _normalize_amex_transaction(
                {
                    "identifier": "pending-id",
                    "postDate": "2026-07-10",
                    "transactionAmount": {"amount": 12.34, "currency": "CAD"},
                    "status": "pending",
                    "type": "DEBIT",
                }
            )
        )

    def test_transit_decoder_handles_nested_list_tags(self):
        payload = [["not-a-tag"], {"nested": ["~#iL", ["value"]]}]

        self.assertEqual([["not-a-tag"], {"nested": ["value"]}], amex._amex_decode_transit(payload))

    def test_dashboard_parser_handles_list_product_order_tokens(self):
        raw_state = {
            "modules": {
                "axp-myca-root": {
                    "products": {
                        "details": {
                            "types": {
                                "CARD_PRODUCT": {
                                    "productsList": {
                                        "['nested-token']": {
                                            "account_token": "acct-token",
                                            "account_key": "acct-key",
                                            "sorted_index": 0,
                                            "account": {"display_account_number": "XXXXX12345"},
                                            "product": {"name": "American Express Cobalt Card"},
                                        }
                                    },
                                    "productsOrder": [["nested-token"]],
                                }
                            }
                        }
                    }
                }
            }
        }

        accounts = amex._amex_accounts_from_raw_accounts(
            amex._amex_raw_accounts_from_dashboard_state(raw_state)
        )

        self.assertEqual(1, len(accounts))
        self.assertEqual("American Express Cobalt Card", accounts[0]["name"])
        self.assertEqual("account:acct-token", accounts[0]["external_id"])
        self.assertEqual("acct-key", accounts[0]["account_key"])

    def test_statement_page_bootstrap_builds_account(self):
        raw_html = """
        <script>
        var statementData = {"data":{"cardMemberInfo":{"CARD_MEMBER":{"cards":[{"sortedIndex":0,"accountToken":"acct-token","cardKeyData":"acct-key","lastFiveDigits":"12345","obfuscatedAccountNumber":"XXX-12345","productId":{"cardProductDesc":"American Express Cobalt Card"}}]}},"defaultBalanceInfo":{"totalBalance":123.45,"currencyCode":"CAD"},"statement":{"statementCycle":{"cycleType":"UNBILLED"},"totalNumOfTrans":1,"transactionsList":[{"uniqueReferenceNumber":"txn-1","dateProcessed":"2026-05-03","transactionAmount":9.99,"descriptionLine":"Coffee"}]}}};
        </script>
        """

        accounts = amex._amex_accounts_from_dashboard_html(raw_html)

        self.assertEqual(1, len(accounts))
        self.assertEqual("account:acct-token", accounts[0]["external_id"])
        self.assertEqual("acct-key", accounts[0]["account_key"])
        self.assertEqual(123.45, accounts[0]["balance"])

    def test_dashboard_parser_merges_statement_and_initial_state_accounts(self):
        raw_html = """
        <script>
        var statementData = {"data":{"cardMemberInfo":{"CARD_MEMBER":{"cards":[{"sortedIndex":0,"accountToken":"statement-token","cardKeyData":"statement-key","lastFiveDigits":"12345","obfuscatedAccountNumber":"XXX-12345","productId":{"cardProductDesc":"American Express Cobalt Card"}}]}},"defaultBalanceInfo":{"totalBalance":123.45,"currencyCode":"CAD"}}};
        </script>
        <script id="initial-state">{"modules":{"axp-myca-root":{"products":{"details":{"types":{"CARD_PRODUCT":{"productsList":{"dashboard-token":{"account_token":"dashboard-token","account_key":"dashboard-key","sorted_index":1,"account":{"display_account_number":"XXXXX98765"},"product":{"name":"American Express Green Card"}}},"productsOrder":["dashboard-token"]}}}}}}}</script>
        """

        accounts = amex._amex_accounts_from_dashboard_html(raw_html)

        self.assertEqual(
            ["American Express Cobalt Card", "American Express Green Card"],
            [account["name"] for account in accounts],
        )

    async def test_missing_storage_state_returns_auth_required_payload(self):
        fake_replay = _FakeReplaySession(storage_state=None)
        with (
            patch.object(amex, "load_saved_artifact_replay_session_async", side_effect=_fake_replay_factory(fake_replay)),
            patch.object(amex, "_amex_log_event") as log_event,
        ):
            result = await amex.try_headless_sync(1)

        self.assertEqual("auth_required", result["status"])
        self.assertEqual([], result["accounts"])
        log_event.assert_any_call(
            "headless sync missing storage_state",
            user_id=1,
            level="warning",
        )

    async def test_direct_auth_required_returns_payload(self):
        fake_replay = _FakeReplaySession()
        with (
            patch.object(amex, "load_saved_artifact_replay_session_async", side_effect=_fake_replay_factory(fake_replay)),
            patch.object(
                amex,
                "_build_authenticated_payload_direct",
                new=AsyncMock(side_effect=amex.AmexAuthRequired("signin")),
            ),
            patch.object(amex, "_amex_log_event") as log_event,
        ):
            result = await amex.try_headless_sync(1)

        self.assertEqual("auth_required", result["status"])
        self.assertEqual({}, result["transactions"])
        log_event.assert_any_call(
            "headless sync auth_required",
            user_id=1,
            engine="direct",
            message="saved-session direct reuse did not reach authenticated American Express state",
            auth_stage=None,
            auth_signal=None,
            auth_status=None,
            auth_url=None,
            storage_cookies=0,
        )

    async def test_direct_network_exception_returns_network_error(self):
        fake_replay = _FakeReplaySession()
        with (
            patch.object(amex, "load_saved_artifact_replay_session_async", side_effect=_fake_replay_factory(fake_replay)),
            patch.object(
                amex,
                "_build_authenticated_payload_direct",
                new=AsyncMock(side_effect=socket.gaierror(-3, "Temporary failure in name resolution")),
            ),
            patch.object(amex, "_amex_log_event") as log_event,
        ):
            result = await amex.try_headless_sync(1)

        self.assertEqual("network_error", result["status"])
        self.assertEqual("Connection failed", result["message"])
        log_event.assert_any_call(
            "headless sync network_error",
            user_id=1,
            engine=amex.AMEX_DIRECT_ENGINE,
            level="warning",
            exception_type="gaierror",
            message="[Errno -3] Temporary failure in name resolution",
        )

    async def test_direct_success_saves_session_artifact(self):
        fake_replay = _FakeReplaySession()
        payload = {
            "status": "ok",
            "accounts": [{"name": "Card", "external_id": "account:abc"}],
            "transactions": {},
            "transaction_fetch_succeeded_accounts": [],
        }

        with (
            patch.object(amex, "load_saved_artifact_replay_session_async", side_effect=_fake_replay_factory(fake_replay)),
            patch.object(
                amex,
                "_build_authenticated_payload_direct",
                new=AsyncMock(return_value=payload),
            ),
            patch.object(amex, "_amex_log_event"),
        ):
            result = await amex.try_headless_sync(1)

        self.assertEqual("ok", result["status"])
        self.assertEqual(payload["accounts"], result["accounts"])
        self.assertEqual({}, result["holdings"])
        self.assertIsNone(result["message"])
        self.assertEqual(fake_replay.storage_state, fake_replay.refreshed_storage_state)

    async def test_direct_payload_exception_still_propagates(self):
        fake_replay = _FakeReplaySession()
        with (
            patch.object(amex, "load_saved_artifact_replay_session_async", side_effect=_fake_replay_factory(fake_replay)),
            patch.object(
                amex,
                "_build_authenticated_payload_direct",
                new=AsyncMock(side_effect=ValueError("boom")),
            ),
            patch.object(amex, "_amex_log_event"),
        ):
            with self.assertRaises(ValueError):
                await amex.try_headless_sync(1)


if __name__ == "__main__":
    unittest.main()
