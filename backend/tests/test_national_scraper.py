from __future__ import annotations

import json
import unittest
from datetime import date
from unittest.mock import patch

from app.scrapers import national


class _FakeGraphQLResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self.status_code = status_code
        self.url = national.NATIONAL_GRAPHQL_URL
        self.text = json.dumps(payload)


class _FakeReplay:
    def __init__(self, payload: dict | list[tuple[dict, int]]):
        self.payloads = payload if isinstance(payload, list) else [(payload, 200)]
        self.requests: list[dict] = []
        self.request_count = 0

    async def post(self, url, *, headers=None, json=None, include_cookie_header=True, refresh_cookies=True):
        self.requests.append(json)
        payload, status_code = self.payloads[min(self.request_count, len(self.payloads) - 1)]
        self.request_count += 1
        return _FakeGraphQLResponse(payload, status_code=status_code)


class _FakeReplayContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def refreshed_session_artifact(self, *, extra=None):
        return dict(extra or {})


class NationalDirectReplayTests(unittest.IsolatedAsyncioTestCase):
    def test_redacted_authorization_does_not_override_captured_access_token(self) -> None:
        access_token = "opaque-national-token-value-abcdefghijklmnopqrstuvwxyz0123456789"
        session_artifact = {
            "access_token": access_token,
            "graphql_authorization": "Bearer <redacted>",
            "graphql_uses_authorization": True,
            "graphql_authorization_scheme": "bearer",
        }

        authorization = national._national_session_authorization_header(
            session_artifact,
            access_token=access_token,
        )

        self.assertEqual(f"Bearer {access_token}", authorization)

    def test_transaction_request_candidates_prefer_browser_shape_then_scoped_fallbacks(self) -> None:
        candidates = national._national_transaction_input_candidates(
            "account-key",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 6),
            include_undated=True,
        )

        self.assertTrue(candidates)
        self.assertNotIn("accountKey", candidates[0])
        self.assertEqual("2026-05-01", candidates[0]["fromDate"])
        self.assertEqual("2026-05-06", candidates[0]["toDate"])
        self.assertEqual(
            [{"fieldName": "effectiveDate", "ascending": False}],
            candidates[0]["queryParams"]["sorting"],
        )
        self.assertTrue(any(candidate.get("accountKey") == "account-key" for candidate in candidates[1:]))

    async def test_transaction_fetch_accepts_rows_from_browser_shape_when_row_account_key_matches(self) -> None:
        replay = _FakeReplay(
            {
                "data": {
                    "detailedTransactions": {
                        "items": [
                            {
                                "id": "txn-1",
                                "accountKey": "account-key",
                                "effectiveDate": "2026-05-05",
                                "amount": 12.34,
                            }
                        ]
                    }
                }
            }
        )

        rows, succeeded, undated = await national._national_fetch_transactions_window(
            replay,
            account={"key": "account-key", "external_id": "account:account-key", "currency": "CAD"},
            access_token="access-token",
            session_artifact={
                "session_id": "session-id",
                "graphql_uses_authorization": True,
                "graphql_authorization_scheme": "bearer",
            },
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 6),
            include_undated=False,
            user_id=None,
        )

        request_input = replay.requests[0]["variables"]["transactionsRequestInput"]
        self.assertNotIn("accountKey", request_input)
        self.assertTrue(succeeded)
        self.assertFalse(undated)
        self.assertEqual(1, len(rows))
        self.assertEqual("account:account-key", rows[0]["_account_external_id"])
        self.assertEqual("CAD", rows[0]["_account_currency"])

    async def test_transaction_fetch_retries_scoped_shape_after_http_400(self) -> None:
        replay = _FakeReplay(
            [
                ({"errors": [{"message": "bad request"}]}, 400),
                (
                    {
                        "data": {
                            "detailedTransactions": {
                                "items": [
                                    {
                                        "id": "txn-1",
                                        "effectiveDate": "2026-05-05",
                                        "amount": 12.34,
                                    }
                                ]
                            }
                        }
                    },
                    200,
                ),
            ]
        )

        rows, succeeded, undated = await national._national_fetch_transactions_window(
            replay,
            account={"key": "account-key", "external_id": "account:account-key", "currency": "CAD"},
            access_token="access-token",
            session_artifact={
                "session_id": "session-id",
                "graphql_uses_authorization": True,
                "graphql_authorization_scheme": "bearer",
            },
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 6),
            include_undated=False,
            user_id=None,
        )

        first_input = replay.requests[0]["variables"]["transactionsRequestInput"]
        second_input = replay.requests[1]["variables"]["transactionsRequestInput"]
        self.assertNotIn("accountKey", first_input)
        self.assertEqual("account-key", second_input["accountKey"])
        self.assertTrue(succeeded)
        self.assertFalse(undated)
        self.assertEqual(1, len(rows))

    async def test_backfill_partial_failure_does_not_mark_account_transaction_fetch_succeeded(self) -> None:
        calls = 0

        async def fake_fetch_window(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [{"id": "txn-1"}], True, False
            return [], False, False

        with patch.object(national, "_national_fetch_transactions_window", side_effect=fake_fetch_window):
            rows, succeeded = await national._national_fetch_account_transactions(
                object(),
                {
                    "key": "account-key",
                    "external_id": "account:account-key",
                    "sync_window": {
                        "mode": "backfill",
                        "end_date": date(2026, 5, 6),
                    },
                },
                access_token="access-token",
                session_artifact={},
                user_id=None,
            )

        self.assertEqual([{"id": "txn-1"}], rows)
        self.assertFalse(succeeded)

    async def test_saved_artifact_sync_errors_when_every_transaction_fetch_fails(self) -> None:
        async def fake_mode_resolver(accounts):
            return {}

        async def fake_fetch_accounts(*args, **kwargs):
            return [
                {
                    "key": "account-key",
                    "external_id": "account:account-key",
                    "currency": "CAD",
                }
            ]

        async def fake_fetch_account_transactions(*args, **kwargs):
            return [], False

        session_artifact = {
            "access_token": "opaque-national-token-value-abcdefghijklmnopqrstuvwxyz0123456789",
            "session_id": "session-id",
            "graphql_authorization": "Bearer opaque-national-token-value-abcdefghijklmnopqrstuvwxyz0123456789",
            "graphql_uses_authorization": True,
            "graphql_authorization_scheme": "bearer",
            "accounts_graphql_request_body": {"operationName": national.NATIONAL_ACCOUNTS_WITH_PROFILE_OPERATION},
        }

        with (
            patch.object(national, "SavedArtifactHttpClient", return_value=_FakeReplayContext()),
            patch.object(national, "_national_fetch_accounts", side_effect=fake_fetch_accounts),
            patch.object(
                national,
                "_national_fetch_account_transactions",
                side_effect=fake_fetch_account_transactions,
            ),
        ):
            result = await national._sync_national_with_saved_artifacts(
                {},
                session_artifact,
                user_id=None,
                mode_resolver=fake_mode_resolver,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "National Bank transaction fetch failed before any accounts could be synced.",
            result["message"],
        )
        self.assertEqual(["account:account-key"], result["transaction_fetch_failed_accounts"])


if __name__ == "__main__":
    unittest.main()
