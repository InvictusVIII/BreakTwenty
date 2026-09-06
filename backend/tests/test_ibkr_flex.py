from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
import tempfile
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import sync as sync_route
from app.connectors.ibkr_flex import IBKRFlexConnector
from app.connectors.sync_context import connector_sync_context
from app.connectors.types import SyncStatus
from app.database import Base
from app.models import Account, BalanceHistory, Institution, Transaction, User
from app.services.categories import seed_default_categories_for_user
from app.services.flex_query import (
    FLEX_FETCH_MAX_ATTEMPTS,
    IBKR_FLEX_NETWORK_BLOCKED_MESSAGE,
    IBKRFlexNetworkBlockedError,
    clear_flex_report_cache,
    fetch_flex_report,
    get_flex_report,
    get_xml_float,
    import_flex_transactions,
    normalize_flex_position_average_cost,
    parse_flex_report,
    request_flex_report,
)


class IBKRFlexParsingTests(unittest.TestCase):
    def test_malformed_cash_and_nav_numbers_do_not_abort_report(self) -> None:
        xml = """
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement accountId="U1234567" currency="CAD">
              <AccountInformation customerType="TRUST_C" acctAlias="TFSA" netLiquidation="not-a-number" />
              <OpenPosition
                accountId="U1234567"
                conid="1001"
                symbol="ABC"
                description="ABC Corp"
                assetCategory="STK"
                position="2"
                positionValue="100"
                fxRateToBase="1"
                costBasisMoney="80"
                multiplier="1"
                markPrice="50"
                currency="CAD"
              />
              <CashReportCurrency currency="BASE_SUMMARY" endingCash="still-not-a-number" />
              <CashReportCurrency currency="USD" endingCash="12.34" />
              <EquitySummaryInBase total="also-not-a-number" />
            </FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>
        """

        accounts = parse_flex_report(xml)

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["nav"], 0)
        self.assertEqual(accounts[0]["positions"][0]["market_value_base"], 100)
        self.assertEqual(
            accounts[0]["cash"],
            [
                {"currency": "BASE_SUMMARY", "amount": 0},
                {"currency": "USD", "amount": 12.34},
            ],
        )

    def test_option_average_cost_uses_quote_cost_basis_before_multiplier(self) -> None:
        average_cost = normalize_flex_position_average_cost(
            "OPT",
            cost_basis_money=-450,
            quantity=-3,
            multiplier=100,
        )

        self.assertEqual(average_cost, 4.5)

    def test_non_finite_numbers_are_ignored(self) -> None:
        self.assertEqual(get_xml_float("NaN", 12), 12)
        self.assertEqual(get_xml_float("Infinity", 12), 12)
        self.assertEqual(get_xml_float("-Infinity", 12), 12)

    def test_statement_date_range_is_exposed_for_transaction_import_target(self) -> None:
        xml = """
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement accountId="U1234567" currency="CAD" fromDate="20260112" toDate="20260515" period="LastNCalendarDays" whenGenerated="20260516;144934">
              <AccountInformation customerType="INDIVIDUAL" acctAlias="Margin" />
            </FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>
        """

        accounts = parse_flex_report(xml)

        self.assertEqual(accounts[0]["statement_from_date"], "2026-01-12")
        self.assertEqual(accounts[0]["statement_to_date"], "2026-05-15")
        self.assertEqual(accounts[0]["statement_period"], "LastNCalendarDays")
        self.assertEqual(accounts[0]["statement_when_generated"], "20260516;144934")

    def test_statement_backfill_target_uses_report_window_and_caps_to_one_year(self) -> None:
        connector = IBKRFlexConnector()

        self.assertEqual(
            connector._statement_backfill_days({
                "statement_from_date": "2026-04-16",
                "statement_to_date": "2026-05-16",
            }),
            30,
        )
        self.assertEqual(
            connector._statement_to_date({"statement_to_date": "2026-05-16"}),
            date(2026, 5, 16),
        )
        self.assertEqual(
            connector._statement_backfill_days({
                "statement_from_date": "2024-05-16",
                "statement_to_date": "2026-05-16",
            }),
            365,
        )
        self.assertEqual(connector._statement_backfill_days({}), 365)
        self.assertIsNone(connector._statement_to_date({}))


class IBKRFlexSyncScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_day_scraper_marker_skips_account_scope_only(self) -> None:
        connector = IBKRFlexConnector()
        connector._should_skip_same_day_ibkr_scraper_sync = AsyncMock(return_value=True)

        with (
            patch("app.connectors.ibkr_flex.get_flex_credentials", AsyncMock(return_value=("token", "query"))),
            patch(
                "app.connectors.ibkr_flex.get_flex_report",
                AsyncMock(side_effect=AssertionError("Flex report should not be requested")),
            ),
        ):
            result = await connector._sync_impl(1)

        self.assertEqual(result.status, SyncStatus.SKIPPED)

    async def test_transaction_scope_ignores_same_day_scraper_marker(self) -> None:
        connector = IBKRFlexConnector()
        connector._should_skip_same_day_ibkr_scraper_sync = AsyncMock(return_value=True)
        connector.fetch_and_persist_transaction_import_windows = AsyncMock(return_value=([], True, {}))
        accounts_data = [
            {
                "account_id": "U1234567",
                "acct_name": "Margin",
                "currency": "CAD",
                "customer_type": "INDIVIDUAL",
                "nav": 1000,
                "positions": [],
                "cash": [],
                "cash_transactions": [],
                "trades": [],
                "statement_from_date": "2025-05-17",
                "statement_to_date": "2026-05-17",
            }
        ]
        get_report = AsyncMock(return_value="<FlexQueryResponse />")

        with (
            connector_sync_context(user_id=1, provider="ibkr_flex", sync_scope="transactions"),
            patch("app.connectors.ibkr_flex.get_flex_credentials", AsyncMock(return_value=("token", "query"))),
            patch("app.connectors.ibkr_flex.get_flex_report", get_report),
            patch("app.connectors.ibkr_flex.parse_flex_report", return_value=accounts_data),
        ):
            result = await connector._sync_impl(1)

        self.assertEqual(result.status, SyncStatus.OK)
        get_report.assert_awaited_once()
        connector.fetch_and_persist_transaction_import_windows.assert_awaited_once()
        call_kwargs = connector.fetch_and_persist_transaction_import_windows.await_args.kwargs
        self.assertEqual(365, call_kwargs["overlap_days"])
        self.assertEqual(date(2026, 5, 17), call_kwargs["today"])

    async def test_transient_statement_generation_keeps_previous_data(self) -> None:
        connector = IBKRFlexConnector()
        connector._should_skip_same_day_ibkr_scraper_sync = AsyncMock(return_value=False)

        with (
            patch("app.connectors.ibkr_flex.get_flex_credentials", AsyncMock(return_value=("token", "query"))),
            patch(
                "app.connectors.ibkr_flex.get_flex_report",
                AsyncMock(
                    side_effect=Exception(
                        "Statement could not be generated at this time. Please try again shortly."
                    )
                ),
            ),
            patch("app.connectors.ibkr_flex.parse_flex_report") as parse_report,
        ):
            result = await connector._sync_impl(1)

        self.assertEqual(result.status, SyncStatus.SKIPPED)
        self.assertTrue(result.transaction_import_deferred)
        self.assertIn("Previous IBKR data was kept", result.message)
        parse_report.assert_not_called()

    async def test_deferred_statement_generation_does_not_enqueue_transaction_import(self) -> None:
        response = {
            "status": "skipped",
            "message": "IBKR Flex statement generation is temporarily unavailable.",
            "transaction_import_deferred": True,
        }

        with patch("app.api.routes.sync.enqueue_provider_transaction_import", AsyncMock()) as enqueue:
            result = await sync_route._enqueue_transaction_import_after_sync(
                1,
                "ibkr_flex",
                response,
                add_flow=False,
                reason="manual_sync",
                institution_id=1,
            )

        self.assertEqual(result, response)
        enqueue.assert_not_awaited()


class _FakeFlexResponse:
    def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeFlexClient:
    def __init__(self, responses: list[_FakeFlexResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def __aenter__(self) -> "_FakeFlexClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, url, params=None) -> _FakeFlexResponse:
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


def _flex_request_xml(status: str, *, reference: str = "", error: str = "") -> str:
    ref = f"<ReferenceCode>{reference}</ReferenceCode>" if reference else ""
    err = f"<ErrorMessage>{error}</ErrorMessage>" if error else ""
    return f"<FlexStatementResponse><Status>{status}</Status>{ref}{err}</FlexStatementResponse>"


class IBKRFlexRequestRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_retries_transient_busy_then_succeeds(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(200, _flex_request_xml(
                "Fail",
                error="Statement could not be generated at this time. Please try again shortly.",
            )),
            _FakeFlexResponse(200, _flex_request_xml(
                "Fail",
                error="Too many requests have been made from this token. Please try again shortly.",
            )),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="ABC123")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            reference = await request_flex_report("token", "query", user_id=1)

        self.assertEqual(reference, "ABC123")
        self.assertEqual(client.calls, 3)

    async def test_request_retries_http_503_then_succeeds(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(503, "Service Unavailable"),
            _FakeFlexResponse(503, "Service Unavailable"),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="ABC123")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()) as sleep,
        ):
            reference = await request_flex_report("token", "query", user_id=1)

        self.assertEqual(reference, "ABC123")
        self.assertEqual(client.calls, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [10, 10],
        )

    async def test_request_does_not_retry_http_auth_failure(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(401, "Unauthorized"),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="SHOULD_NOT_REACH")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(Exception):
                await request_flex_report("token", "query", user_id=1)

        self.assertEqual(client.calls, 1)

    async def test_request_html_access_denied_is_network_blocked(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(
                403,
                "<!doctype html><html><head><title>Access Denied</title></head>"
                "<body><h1>Access Denied</h1><p>Access denied for 203.0.113.10</p></body></html>",
                {"content-type": "text/html"},
            ),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="SHOULD_NOT_REACH")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(IBKRFlexNetworkBlockedError) as raised:
                await request_flex_report("token", "query", user_id=1)

        self.assertEqual(str(raised.exception), IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
        self.assertEqual(client.calls, 1)

    async def test_request_flex_ip_restriction_is_network_blocked(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(
                200,
                "<FlexStatementResponse><Status>Fail</Status>"
                "<ErrorCode>1013</ErrorCode><ErrorMessage>IP restriction.</ErrorMessage>"
                "</FlexStatementResponse>",
            ),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="SHOULD_NOT_REACH")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(IBKRFlexNetworkBlockedError) as raised:
                await request_flex_report("token", "query", user_id=1)

        self.assertEqual(str(raised.exception), IBKR_FLEX_NETWORK_BLOCKED_MESSAGE)
        self.assertEqual(client.calls, 1)

    async def test_request_does_not_retry_permanent_error(self) -> None:
        client = _FakeFlexClient([
            _FakeFlexResponse(200, _flex_request_xml("Fail", error="Token is invalid")),
            _FakeFlexResponse(200, _flex_request_xml("Success", reference="SHOULD_NOT_REACH")),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(Exception):
                await request_flex_report("token", "query", user_id=1)

        self.assertEqual(client.calls, 1)

    async def test_fetch_waits_past_one_minute_for_slow_statement(self) -> None:
        pending_xml = (
            "<FlexStatementResponse><Status>Warn</Status>"
            "<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>"
            "</FlexStatementResponse>"
        )
        ready_xml = "<FlexQueryResponse><FlexStatements /></FlexQueryResponse>"
        client = _FakeFlexClient([
            *[_FakeFlexResponse(200, pending_xml) for _ in range(10)],
            _FakeFlexResponse(200, ready_xml),
        ])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()) as sleep,
        ):
            xml = await fetch_flex_report("token", "ref", user_id=1)

        self.assertEqual(xml, ready_xml)
        self.assertEqual(client.calls, 11)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [7] * 11,
        )

    async def test_fetch_pending_exhaustion_times_out_after_configured_attempts(self) -> None:
        pending_xml = (
            "<FlexStatementResponse><Status>Warn</Status>"
            "<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>"
            "</FlexStatementResponse>"
        )
        client = _FakeFlexClient([_FakeFlexResponse(200, pending_xml)])

        with (
            patch("app.services.flex_query.httpx.AsyncClient", return_value=client),
            patch("app.services.flex_query.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(TimeoutError):
                await fetch_flex_report("token", "ref", user_id=1)

        self.assertEqual(client.calls, FLEX_FETCH_MAX_ATTEMPTS)


class IBKRFlexReportCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_flex_report_cache()

    def tearDown(self) -> None:
        clear_flex_report_cache()

    async def test_second_call_reuses_cached_statement(self) -> None:
        request_report = AsyncMock(return_value="ref")
        fetch_report = AsyncMock(return_value="<FlexQueryResponse />")

        with (
            patch("app.services.flex_query.request_flex_report", request_report),
            patch("app.services.flex_query.fetch_flex_report", fetch_report),
        ):
            first = await get_flex_report("token", "query", user_id=1)
            second = await get_flex_report("token", "query", user_id=1)

        self.assertEqual(first, "<FlexQueryResponse />")
        self.assertEqual(second, "<FlexQueryResponse />")
        request_report.assert_awaited_once()
        fetch_report.assert_awaited_once()

    async def test_distinct_credentials_are_not_shared(self) -> None:
        fetch_report = AsyncMock(return_value="<FlexQueryResponse />")

        with (
            patch("app.services.flex_query.request_flex_report", AsyncMock(return_value="ref")),
            patch("app.services.flex_query.fetch_flex_report", fetch_report),
        ):
            await get_flex_report("token", "query", user_id=1)
            await get_flex_report("token", "query", user_id=2)

        self.assertEqual(fetch_report.await_count, 2)


class IBKRFlexErrorClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connector = IBKRFlexConnector()

    def _status(self, message: str) -> SyncStatus:
        return self.connector.exception_result(1, Exception(message)).status

    def test_transient_poll_error_is_retryable_not_auth(self) -> None:
        self.assertEqual(
            self._status(
                "Flex report error: Statement could not be retrieved at this time. "
                "Please try again shortly."
            ),
            SyncStatus.NETWORK_ERROR,
        )

    def test_transient_request_error_is_retryable(self) -> None:
        self.assertEqual(
            self._status("Statement could not be generated at this time. Please try again shortly."),
            SyncStatus.NETWORK_ERROR,
        )

    def test_rate_limit_is_retryable(self) -> None:
        self.assertEqual(
            self._status("Too many requests have been made from this token. Please try again shortly."),
            SyncStatus.NETWORK_ERROR,
        )

    def test_request_http_503_is_retryable(self) -> None:
        self.assertEqual(
            self._status("IBKR Flex request failed with HTTP 503"),
            SyncStatus.NETWORK_ERROR,
        )

    def test_http_auth_failure_still_requires_auth(self) -> None:
        self.assertEqual(
            self._status("IBKR Flex fetch failed with HTTP 401"),
            SyncStatus.AUTH_REQUIRED,
        )

    def test_edge_access_denied_is_network_error(self) -> None:
        result = self.connector.exception_result(
            1,
            IBKRFlexNetworkBlockedError(IBKR_FLEX_NETWORK_BLOCKED_MESSAGE),
        )

        self.assertEqual(result.status, SyncStatus.NETWORK_ERROR)
        self.assertIn("VPN or proxy", result.message)

    def test_raw_access_denied_ip_message_is_network_error(self) -> None:
        self.assertEqual(
            self._status("Access denied for 203.0.113.10"),
            SyncStatus.NETWORK_ERROR,
        )

    def test_invalid_token_still_requires_auth(self) -> None:
        self.assertEqual(
            self._status("IBKR Flex request failed: invalid token"),
            SyncStatus.AUTH_REQUIRED,
        )

    def test_unknown_error_stays_error(self) -> None:
        self.assertEqual(
            self._status("No account data found in Flex report"),
            SyncStatus.ERROR,
        )


_DEFUNCT_FLEX_XML = """
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement accountId="U7777777" currency="CAD" fromDate="20240101" toDate="20240229" period="Custom" whenGenerated="20240301;090000">
      <AccountInformation accountId="U7777777" acctAlias="Defunct RRSP" currency="CAD" customerType="RRSP" />
      <EquitySummaryInBase>
        <EquitySummaryByReportDateInBase accountId="U7777777" reportDate="20240131" total="10000" />
        <EquitySummaryByReportDateInBase accountId="U7777777" reportDate="20240229" total="0" />
      </EquitySummaryInBase>
      <Trades>
        <Trade tradeID="900000001" accountId="U7777777" buySell="BUY" dateTime="20240115;103000" symbol="ABC" description="ABC CORP" assetCategory="STK" quantity="100" tradePrice="40" tradeMoney="4000" ibCommission="-1" currency="USD" />
      </Trades>
      <CashTransactions>
        <CashTransaction transactionID="800000002" accountId="U7777777" type="Dividends" dateTime="20240220;120000" amount="50" currency="USD" symbol="ABC" description="ABC(US0) CASH DIVIDEND USD 0.50 PER SHARE" />
      </CashTransactions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class IBKRFlexDefunctImportTests(unittest.IsolatedAsyncioTestCase):
    """File-import of a Flex Query whose account number matches no live/imported account
    creates an is_imported (defunct) account — mirroring the Questrade statement path — while
    a number that matches an existing account just extends it without flagging it imported."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{self._tmpdir.name}/test.db")
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._sessionmaker() as session:
            session.add(User(id=1))
            institution = Institution(
                user_id=1,
                name="Interactive Brokers",
                type="api",
                provider="ibkr",
            )
            session.add(institution)
            await session.flush()
            self.institution_id = institution.id
            await session.commit()
        # Seed the default category tree so the import's CategoryResolver can categorize
        # the imported transactions inline (Buy/Dividend/etc.), like a live sync.
        async with self._sessionmaker() as session:
            await seed_default_categories_for_user(session, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self._engine.dispose()
        self._tmpdir.cleanup()

    async def _import(self, xml: str) -> dict:
        with patch("app.services.flex_query.async_session", self._sessionmaker):
            return await import_flex_transactions(xml, 1, self.institution_id)

    async def test_unmatched_account_is_created_imported_with_history(self) -> None:
        result = await self._import(_DEFUNCT_FLEX_XML)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["accounts_created"], 1)
        self.assertEqual(result["accounts_updated"], 0)

        async with self._sessionmaker() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            self.assertEqual(len(accounts), 1)
            account = accounts[0]
            self.assertTrue(account.is_imported)
            self.assertEqual(account.account_type, "rrsp")
            self.assertEqual(account.external_id, "account:U7777777")
            self.assertEqual(account.name, "Defunct RRSP")
            balance_points = (
                await session.execute(
                    select(func.count()).select_from(BalanceHistory).where(
                        BalanceHistory.account_id == account.id
                    )
                )
            ).scalar_one()
            self.assertEqual(balance_points, 2)
            transactions = (
                await session.execute(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.account_id == account.id
                    )
                )
            ).scalar_one()
            self.assertEqual(transactions, 2)
            # Both rows (the BUY trade + the dividend) are categorized inline at import,
            # not left null for a later sweep — the defunct-account fix.
            categorized = (
                await session.execute(
                    select(func.count()).select_from(Transaction).where(
                        Transaction.account_id == account.id,
                        Transaction.category_id.is_not(None),
                    )
                )
            ).scalar_one()
            self.assertEqual(categorized, 2)

    async def test_reimport_reports_existing_transactions_as_skipped(self) -> None:
        first = await self._import(_DEFUNCT_FLEX_XML)
        second = await self._import(_DEFUNCT_FLEX_XML)

        self.assertEqual(first["imported"], 2)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 2)

        async with self._sessionmaker() as session:
            transaction_count = (
                await session.execute(select(func.count()).select_from(Transaction))
            ).scalar_one()
        self.assertEqual(transaction_count, 2)

    async def test_existing_account_is_extended_not_flagged_imported(self) -> None:
        async with self._sessionmaker() as session:
            session.add(Account(
                user_id=1,
                institution_id=self.institution_id,
                external_id="account:U7777777",
                name="Live RRSP",
                account_type="rrsp",
                currency="CAD",
                is_liability=False,
                is_imported=False,
            ))
            await session.commit()

        result = await self._import(_DEFUNCT_FLEX_XML)

        self.assertEqual(result["accounts_created"], 0)
        self.assertEqual(result["accounts_updated"], 1)

        async with self._sessionmaker() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            self.assertEqual(len(accounts), 1)
            self.assertFalse(accounts[0].is_imported)
            self.assertEqual(accounts[0].name, "Live RRSP")

    async def test_parse_finishes_before_entering_write_gate(self) -> None:
        gate_owned = ContextVar("flex_import_gate_owned", default=False)
        gate_entries = 0

        @asynccontextmanager
        async def checked_gate():
            nonlocal gate_entries
            gate_entries += 1
            token = gate_owned.set(True)
            try:
                yield
            finally:
                gate_owned.reset(token)

        def checked_parse(xml_text):
            self.assertFalse(gate_owned.get())
            return parse_flex_report(xml_text)

        with (
            patch("app.services.flex_query.sqlite_write_gate", new=checked_gate),
            patch("app.services.flex_query.parse_flex_report", side_effect=checked_parse),
        ):
            result = await self._import(_DEFUNCT_FLEX_XML)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(gate_entries, 1)

    async def test_missing_account_id_does_not_adopt_null_id_account(self) -> None:
        async with self._sessionmaker() as session:
            session.add_all([
                Account(
                    user_id=1,
                    institution_id=self.institution_id,
                    external_id=None,
                    name="Legacy RRSP A",
                    account_type="rrsp",
                    currency="CAD",
                    is_liability=False,
                ),
                Account(
                    user_id=1,
                    institution_id=self.institution_id,
                    external_id=None,
                    name="Legacy RRSP B",
                    account_type="rrsp",
                    currency="CAD",
                    is_liability=False,
                ),
            ])
            await session.commit()

        malformed_xml = """
        <FlexQueryResponse>
          <FlexStatements>
            <FlexStatement currency="CAD">
              <AccountInformation acctAlias="Unknown RRSP" customerType="RRSP" />
              <EquitySummaryByReportDateInBase reportDate="20240131" total="1000" />
            </FlexStatement>
          </FlexStatements>
        </FlexQueryResponse>
        """
        result = await self._import(malformed_xml)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["accounts_created"], 0)
        self.assertEqual(result["accounts_updated"], 0)
        async with self._sessionmaker() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            balance_count = (
                await session.execute(select(func.count()).select_from(BalanceHistory))
            ).scalar_one()
        self.assertEqual(len(accounts), 2)
        self.assertTrue(all(account.external_id is None for account in accounts))
        self.assertEqual(balance_count, 0)


if __name__ == "__main__":
    unittest.main()
