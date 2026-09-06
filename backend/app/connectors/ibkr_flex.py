from __future__ import annotations

from datetime import date, datetime

from app.brand import APP_BRAND_NAME
from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.connector_logging import get_connector_logger, log_connector_event
from app.connectors.ibkr_helpers import map_ibkr_account_type
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.database import async_session
from app.services.flex_query import (
    IBKR_FLEX_NETWORK_BLOCKED_MESSAGE,
    IBKRFlexNetworkBlockedError,
    get_flex_credentials,
    get_flex_report,
    is_flex_network_block_message,
    is_retryable_flex_http_message,
    is_transient_flex_message,
    parse_flex_report,
)
from app.services.sync_utils import get_ibkr_scraper_success_marker
from app.services.user_utils import get_user_timezone_info

logger = get_connector_logger("ibkr_flex")

IBKR_FLEX_MAX_BACKFILL_DAYS = 365
IBKR_FLEX_BACKFILL_CHUNK_DAYS = 365
IBKR_FLEX_TEMPORARY_SKIP_MESSAGE = (
    "IBKR Flex statement generation is temporarily unavailable. "
    f"Previous IBKR data was kept; {APP_BRAND_NAME} will retry on the next sync."
)


def _is_ibkr_flex_auth_error(message: str) -> bool:
    lower = message.lower()
    return any(
        marker in lower
        for marker in (
            "invalid token",
            "query id",
            "not authorized",
            "authorization failed",
            "unauthorized",
            "access denied",
            "flex fetch failed with http 401",
            "flex fetch failed with http 403",
            "flex request failed with http 401",
            "flex request failed with http 403",
            "request report failed with http 401",
            "request report failed with http 403",
            "request failed with status 401",
            "request failed with status 403",
        )
    )


class IBKRFlexConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="ibkr_flex", display_name="IBKR Flex")

    @property
    def transaction_import_provider(self) -> str:
        return "ibkr"

    def is_auth_required_exception(self, exc: Exception) -> bool:
        return _is_ibkr_flex_auth_error(str(exc)) or super().is_auth_required_exception(exc)

    def exception_result(self, user_id: int, exc: Exception, *, stage: str = "sync failed") -> SyncResult:
        if isinstance(exc, IBKRFlexNetworkBlockedError) or is_flex_network_block_message(str(exc)):
            return self.network_error_result(
                user_id=user_id,
                message=IBKR_FLEX_NETWORK_BLOCKED_MESSAGE,
                stage="sync network blocked",
                exception=exc,
            )
        return super().exception_result(user_id, exc, stage=stage)

    def exception_is_network_error(self, exc: Exception) -> bool:
        # IBKR's transient statement-busy / rate-limit rejections are retryable,
        # not credential problems — surface them as network errors so the UI says
        # "try again" instead of forcing a pointless re-authentication.
        message = str(exc)
        return (
            is_transient_flex_message(message)
            or isinstance(exc, IBKRFlexNetworkBlockedError)
            or is_flex_network_block_message(message)
            or is_retryable_flex_http_message(message)
            or super().exception_is_network_error(exc)
        )

    async def _sync_impl(self, user_id: int) -> SyncResult:
        token, query_id = await get_flex_credentials(user_id)
        if not token or not query_id:
            return self.missing_credentials_result(
                user_id=user_id,
                message="Flex Query credentials not configured.",
            )

        if self.sync_scope() != "transactions" and await self._should_skip_same_day_ibkr_scraper_sync(user_id):
            log_connector_event(
                logger,
                provider=self.provider,
                stage="same-day scraper skip",
                user_id=user_id,
            )
            return SyncResult(
                status=SyncStatus.SKIPPED,
                message="IBKR Flex skipped because IBKR scraper already synced today",
            )

        try:
            try:
                xml_text = await get_flex_report(token, query_id, user_id=user_id)
            except Exception as exc:
                if is_transient_flex_message(str(exc)):
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="statement generation deferred",
                        level="warning",
                        user_id=user_id,
                        message=str(exc),
                        sync_scope=self.sync_scope(),
                    )
                    return SyncResult(
                        status=SyncStatus.SKIPPED,
                        message=IBKR_FLEX_TEMPORARY_SKIP_MESSAGE,
                        transaction_import_deferred=True,
                    )
                raise
            accounts_data = parse_flex_report(xml_text)
            if not accounts_data:
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="report empty",
                    level="warning",
                    user_id=user_id,
                )
                return SyncResult(
                    status=SyncStatus.ERROR,
                    message="No account data found in Flex report",
                )
            log_connector_event(
                logger,
                provider=self.provider,
                stage="report parsed",
                user_id=user_id,
                accounts=len(accounts_data),
                debug=True,
            )
            self.record_diagnostics_fetch(
                stage="accounts.list",
                method="FLEX",
                transport="flex",
                url="ibkr_flex://report/accounts",
                payload={"accounts": accounts_data},
                records=accounts_data,
                record_source="accounts",
            )

            accounts: list[NormalizedAccount] = []
            holdings_by_account: dict[str, list[NormalizedHolding]] = {}
            transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
            all_holdings: list[NormalizedHolding] = []
            all_transactions: list[NormalizedTransaction] = []
            transaction_fetch_succeeded_accounts: set[str] = set()
            fetch_transactions = self.should_fetch_transactions()
            persist_transaction_windows = self.should_persist_transaction_windows()
            transaction_fetch_failed = False

            for acct_data in accounts_data:
                account_id = (acct_data.get("account_id") or "").strip()
                self.record_diagnostics_fetch(
                    stage="positions.list",
                    method="FLEX",
                    transport="flex",
                    url="ibkr_flex://report/positions",
                    payload={"positions": acct_data.get("positions", []) or []},
                    records=acct_data.get("positions", []) or [],
                    record_source="positions",
                    account_ref=account_id,
                )
                self.record_diagnostics_fetch(
                    stage="transactions.cash",
                    method="FLEX",
                    transport="flex",
                    url="ibkr_flex://report/cash_transactions",
                    payload={"cash_transactions": acct_data.get("cash_transactions", []) or []},
                    records=acct_data.get("cash_transactions", []) or [],
                    record_source="cash_transactions",
                    account_ref=account_id,
                )
                self.record_diagnostics_fetch(
                    stage="transactions.trades",
                    method="FLEX",
                    transport="flex",
                    url="ibkr_flex://report/trades",
                    payload={"trades": acct_data.get("trades", []) or []},
                    records=acct_data.get("trades", []) or [],
                    record_source="trades",
                    account_ref=account_id,
                )
                account_name = self._account_name(acct_data)
                currency = acct_data.get("currency") or "CAD"
                acct_type = map_ibkr_account_type(
                    acct_data.get("customer_type", "")
                )
                nav = self._account_nav(acct_data)

                account = NormalizedAccount(
                    name=account_name,
                    account_type=acct_type,
                    external_id=f"account:{account_id}" if account_id else None,
                    currency=currency,
                    balance=nav,
                    balance_authoritative=nav is not None,
                    holdings_authoritative=True,
                    balance_history=tuple(acct_data.get("nav_history") or ()),
                )
                accounts.append(account)
                account_key = account_result_key(account)

                account_holdings = self._normalize_holdings(acct_data)
                holdings_by_account[account_key] = account_holdings
                all_holdings.extend(account_holdings)

                account_transactions: list[NormalizedTransaction] = []
                transaction_fetch_succeeded = False
                if fetch_transactions:
                    account_transactions = self._normalize_transactions(acct_data)
                    if persist_transaction_windows and account.external_id:
                        def fetch_window(sync_window):
                            start_date = sync_window.start_date
                            end_date = sync_window.end_date
                            window_transactions = [
                                transaction
                                for transaction in account_transactions
                                if (start_date is None or transaction.date.date() >= start_date)
                                and transaction.date.date() <= end_date
                            ]
                            return window_transactions, True

                        account_transactions, transaction_fetch_succeeded, _window_log = (
                            await self.fetch_and_persist_transaction_import_windows(
                                user_id=user_id,
                                account_external_id=account.external_id,
                                account_name=account.name,
                                account_type=account.account_type,
                                is_liability=account.is_liability,
                                fetch_window=fetch_window,
                                backfill_days=self._statement_backfill_days(acct_data),
                                backfill_chunk_days=IBKR_FLEX_BACKFILL_CHUNK_DAYS,
                                overlap_days=self._statement_backfill_days(acct_data),
                                today=self._statement_to_date(acct_data),
                                failure_message="IBKR Flex transaction history window did not finish.",
                            )
                        )
                    else:
                        transaction_fetch_succeeded = True
                    transactions_by_account[account_key] = account_transactions
                    all_transactions.extend(account_transactions)
                    if transaction_fetch_succeeded:
                        transaction_fetch_succeeded_accounts.add(account_key)
                    else:
                        transaction_fetch_failed = True

            if persist_transaction_windows and transaction_fetch_failed:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="IBKR Flex transaction history did not finish. Try syncing again.",
                )

            return SyncResult(
                status=SyncStatus.OK,
                accounts=accounts,
                holdings=all_holdings,
                transactions=all_transactions,
                holdings_by_account=holdings_by_account,
                transactions_by_account=transactions_by_account,
                transaction_fetch_succeeded_accounts=transaction_fetch_succeeded_accounts,
            )
        except Exception as e:
            return self.exception_result(user_id, e)

    async def _should_skip_same_day_ibkr_scraper_sync(self, user_id: int) -> bool:
        from app.connectors.sync_context import get_connector_sync_context

        context = get_connector_sync_context()
        if context is None or context.institution_id is None:
            return False
        async with async_session() as db:
            latest_sync = await get_ibkr_scraper_success_marker(
                db,
                user_id,
                context.institution_id,
            )

        if latest_sync is None:
            return False
        user_tz = await get_user_timezone_info(user_id)

        return latest_sync.astimezone(user_tz).date() == datetime.now(user_tz).date()

    def _account_name(self, acct_data: dict) -> str:
        acct_name = (acct_data.get("acct_name") or "").strip()
        if acct_name:
            return acct_name
        return f"IBKR {acct_data.get('account_id', 'Account')}"

    def _position_nav_base(self, pos: dict) -> float:
        # A position's contribution to account NAV, in base currency. Futures (assetCategory
        # FUT) report market_value as the signed NOTIONAL, but a future ties up no capital —
        # its only effect on account value is mark-to-market P&L (market − cost). So a future
        # contributes (market_value − average_cost) scaled to base, NOT its notional, otherwise
        # the notional drags the account balance (and thus net worth + Performance).
        market_value_base = float(pos.get("market_value_base", 0) or 0)
        if (pos.get("asset_category") or "").upper() != "FUT":
            return market_value_base
        market_value = float(pos.get("market_value", 0) or 0)
        average_cost = pos.get("average_cost")
        if average_cost is None or market_value == 0:
            return market_value_base
        fx_to_base = market_value_base / market_value
        return (market_value - float(average_cost)) * fx_to_base

    def _account_nav(self, acct_data: dict) -> float:
        nav = float(acct_data.get("nav", 0) or 0)
        if nav != 0:
            return nav

        positions_base = sum(
            self._position_nav_base(pos)
            for pos in acct_data.get("positions", [])
        )
        cash_entries = acct_data.get("cash", []) or []
        for cash in cash_entries:
            if cash.get("currency") == "BASE_SUMMARY":
                return float(cash.get("amount", 0) or 0) + positions_base
        return positions_base + sum(
            float(cash.get("amount", 0) or 0)
            for cash in cash_entries
            if cash.get("currency") != "BASE_SUMMARY"
        )

    def _statement_backfill_days(self, acct_data: dict) -> int:
        from_date = self._statement_from_date(acct_data)
        to_date = self._statement_to_date(acct_data)
        if from_date and to_date and from_date <= to_date:
            return min(max((to_date - from_date).days, 1), IBKR_FLEX_MAX_BACKFILL_DAYS)
        return IBKR_FLEX_MAX_BACKFILL_DAYS

    def _statement_from_date(self, acct_data: dict) -> date | None:
        return self._parse_statement_date(acct_data.get("statement_from_date"))

    def _statement_to_date(self, acct_data: dict) -> date | None:
        return self._parse_statement_date(acct_data.get("statement_to_date"))

    def _parse_statement_date(self, raw_value) -> date | None:
        raw = str(raw_value or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _normalize_holdings(self, acct_data: dict) -> list[NormalizedHolding]:
        holdings: list[NormalizedHolding] = []

        for pos in acct_data.get("positions", []) or []:
            if float(pos.get("quantity", 0) or 0) == 0:
                continue
            holdings.append(
                NormalizedHolding(
                    symbol=pos["symbol"],
                    name=pos.get("name"),
                    quantity=float(pos.get("quantity", 0) or 0),
                    market_value=pos.get("market_value"),
                    average_cost=pos.get("average_cost") or None,
                    last_price=pos.get("last_price"),
                    contract_multiplier=pos.get("contract_multiplier"),
                    change_pct=pos.get("change_pct"),
                    daily_pnl=pos.get("daily_pnl"),
                    currency=pos.get("currency", acct_data.get("currency", "CAD")),
                )
            )

        for cash in acct_data.get("cash", []) or []:
            if cash.get("currency") == "BASE_SUMMARY":
                continue
            if float(cash.get("amount", 0) or 0) == 0:
                continue
            curr = cash.get("currency")
            if not curr:
                continue
            holdings.append(
                NormalizedHolding(
                    symbol=curr,
                    name=f"{curr} Cash",
                    quantity=1,
                    market_value=float(cash.get("amount", 0) or 0),
                    currency=curr,
                )
            )

        return holdings

    def _normalize_transactions(self, acct_data: dict) -> list[NormalizedTransaction]:
        transactions: list[NormalizedTransaction] = []

        for tx in acct_data.get("cash_transactions", []) or []:
            transactions.append(
                NormalizedTransaction(
                    external_id=f"ibkr_{tx['transaction_id']}",
                    date=tx["date"],
                    type=tx["type"],
                    amount=tx["amount"],
                    currency=tx["currency"],
                    symbol=tx.get("symbol"),
                    description=tx.get("description"),
                    quantity=tx.get("quantity"),
                )
            )

        for trade in acct_data.get("trades", []) or []:
            transactions.append(
                NormalizedTransaction(
                    external_id=f"ibkr_trade_{trade['trade_id']}",
                    date=trade["date"],
                    type=trade["type"],
                    amount=trade["amount"],
                    currency=trade["currency"],
                    symbol=trade.get("symbol"),
                    description=trade.get("description"),
                    quantity=trade.get("quantity"),
                    price=trade.get("price"),
                    commission=trade.get("commission"),
                    asset_category=trade.get("asset_category"),
                    realized_pnl=trade.get("realized_pnl"),
                )
            )

        return transactions
