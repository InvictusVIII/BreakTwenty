from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.connector_logging import log_connector_event
from app.connectors.questrade_helpers import (
    map_questrade_account_type,
    map_questrade_activity_type,
)
from app.connectors.sync_modes import (
    AccountSyncWindow,
    plan_account_sync_window,
    walk_transaction_backfill_windows,
)
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.database import async_session
from app.services.flex_query import derive_dividend_quantity_from_description
from app.services.questrade import (
    QUESTRADE_ACTIVITY_TIMEOUT,
    QuestradeAuthRequiredError,
    QuestradeTemporaryError,
    QuestradeTransportError,
    get_authenticated_client,
    qt_api_get,
)

logger = logging.getLogger("breaktwenty.connectors.questrade")
QUESTRADE_ACTIVITY_CHUNK_DAYS = 30
QUESTRADE_INCREMENTAL_ACTIVITY_CHUNK_DAYS = 3
QUESTRADE_INCREMENTAL_OVERLAP_DAYS = 7
QUESTRADE_EMPTY_HISTORY_STOP_CHUNKS = 2
QUESTRADE_MAX_HISTORY_DAYS = 3650
# Persist yearly backfill windows so progress reporting matches Moomoo / IBKR
# Flex / Wise. _fetch_activities owns Questrade's 30-day API chunks within each
# yearly window and walks every chunk back to the (bounded) window start, so each
# year in the 10-year target is fetched in full and no transaction history is
# skipped. QUESTRADE_EMPTY_HISTORY_STOP_CHUNKS only trims an UNBOUNDED backfill
# (no per-year window_start) once it has walked past the account's oldest activity.
QUESTRADE_DURABLE_BACKFILL_CHUNK_DAYS = 365
QUESTRADE_ACTIVITY_SERVER_RETRIES = 1
QUESTRADE_TIMEZONE = ZoneInfo("America/Toronto")


def _finite_snapshot_float(value, *, field: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Questrade {field} was not finite")
    return parsed


def _questrade_account_ref(account_number: str) -> str:
    digits = "".join(ch for ch in str(account_number or "") if ch.isdigit())
    if len(digits) >= 4:
        return f"...{digits[-4:]}"
    return "unknown"


def _parse_questrade_date(date_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            tx_date = datetime.strptime(date_str, fmt)
            if tx_date.tzinfo is None:
                tx_date = tx_date.replace(tzinfo=timezone.utc)
            return tx_date
        except (ValueError, TypeError):
            continue
    return None


def _questrade_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(QUESTRADE_TIMEZONE)


def _questrade_today() -> date:
    return _questrade_now().date()


def _questrade_sync_window_bounds(
    window: AccountSyncWindow,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime]:
    current = now or _questrade_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=QUESTRADE_TIMEZONE)
    else:
        current = current.astimezone(QUESTRADE_TIMEZONE)

    end_at = datetime.combine(window.end_date, time.max, tzinfo=QUESTRADE_TIMEZONE)
    if end_at > current:
        end_at = current

    start_at = (
        datetime.combine(window.start_date, time.min, tzinfo=QUESTRADE_TIMEZONE)
        if window.start_date
        else None
    )
    if start_at and start_at > end_at:
        start_at = end_at

    return start_at, end_at


def _format_questrade_activity_time(value: datetime) -> str:
    return value.astimezone(QUESTRADE_TIMEZONE).isoformat(timespec="seconds")


class QuestradeConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="questrade", display_name="Questrade")

    async def _transaction_sync_window(
        self,
        user_id: int,
        account_external_id: str | None,
    ) -> AccountSyncWindow:
        async with async_session() as db:
            return await plan_account_sync_window(
                db,
                user_id,
                account_external_id or "",
                provider=self.provider,
                overlap_days=QUESTRADE_INCREMENTAL_OVERLAP_DAYS,
                today=_questrade_today(),
            )

    async def _resolve_symbol_names(
        self,
        *,
        access_token: str,
        api_server: str,
        positions: list[dict],
        user_id: int,
        account_ref: str,
    ) -> dict[int, str]:
        from app.services.symbol_names import (
            get_cached_symbol_names,
            upsert_symbol_names,
        )

        ids: list[int] = []
        seen: set[int] = set()
        for pos in positions:
            sid = pos.get("symbolId")
            if isinstance(sid, int) and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        if not ids:
            return {}

        cached = await get_cached_symbol_names("questrade", [str(i) for i in ids])
        names: dict[int, str] = {}
        for sid in ids:
            cached_name = cached.get(str(sid))
            if cached_name:
                names[sid] = cached_name

        ids_to_fetch = [sid for sid in ids if sid not in names]
        if not ids_to_fetch:
            return names

        fresh_names: dict[int, str] = {}
        for chunk_start in range(0, len(ids_to_fetch), 100):
            chunk = ids_to_fetch[chunk_start : chunk_start + 100]
            try:
                resp = await qt_api_get(
                    access_token,
                    api_server,
                    f"symbols?ids={','.join(str(i) for i in chunk)}",
                )
            except Exception as e:
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="symbols lookup failed",
                    level="warning",
                    user_id=user_id,
                    account=account_ref,
                    error=type(e).__name__,
                    message=str(e),
                )
                continue
            for sym in resp.get("symbols", []) or []:
                sid = sym.get("symbolId")
                description = (sym.get("description") or "").strip()
                if isinstance(sid, int) and description:
                    fresh_names[sid] = description

        if fresh_names:
            await upsert_symbol_names(
                "questrade",
                {str(sid): name for sid, name in fresh_names.items()},
            )
            names.update(fresh_names)
        return names

    async def _sync_impl(self, user_id: int) -> SyncResult:
        try:
            access_token, api_server = await get_authenticated_client(user_id)
        except QuestradeAuthRequiredError as e:
            return self.auth_required_result(
                user_id=user_id,
                message=str(e),
                stage="auth required",
                exception=e,
            )
        except QuestradeTemporaryError as e:
            return self.network_error_result(
                user_id=user_id,
                message=str(e),
                stage="auth temporary failure",
                exception=e,
            )
        except Exception as e:
            return self.exception_result(user_id, e, stage="auth failed")

        try:
            accounts_data = await qt_api_get(access_token, api_server, "accounts")
            qt_accounts = accounts_data.get("accounts", [])
            self.record_diagnostics_fetch(
                stage="accounts.list",
                url=f"{api_server}v1/accounts",
                payload=accounts_data,
                records=qt_accounts,
                record_source="accounts",
            )
            if not qt_accounts:
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="accounts empty",
                    level="warning",
                    user_id=user_id,
                )
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Questrade returned no accounts during sync",
                )
            log_connector_event(
                logger,
                provider=self.provider,
                stage="accounts fetched",
                user_id=user_id,
                accounts=len(qt_accounts),
                debug=True,
            )

            accounts: list[NormalizedAccount] = []
            holdings_by_account: dict[str, list[NormalizedHolding]] = {}
            transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
            transaction_fetch_succeeded_accounts: set[str] = set()
            all_holdings: list[NormalizedHolding] = []
            all_transactions: list[NormalizedTransaction] = []
            transaction_fetch_failed = False
            fetch_transactions = self.should_fetch_transactions()
            persist_transaction_windows = self.should_persist_transaction_windows()
            snapshot_scope = not persist_transaction_windows
            snapshot_fetch_failed = False

            for qt_acct in qt_accounts:
                status = qt_acct.get("status", "").upper()
                if status in ("CLOSED", "SUSPENDED"):
                    continue

                qt_number = qt_acct["number"]
                account_ref = _questrade_account_ref(qt_number)
                account_name = f"QT-{qt_number}"
                acct_type = map_questrade_account_type(qt_acct.get("type", ""))
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="account sync start",
                    user_id=user_id,
                    account=account_ref,
                    account_type=acct_type,
                    debug=True,
                )

                bal_data = {}
                total_equity: float | None = None
                balance_succeeded = False
                if snapshot_scope:
                    try:
                        bal_data = await qt_api_get(access_token, api_server, f"accounts/{qt_number}/balances")
                        self.record_diagnostics_fetch(
                            stage="balances.list",
                            url=f"{api_server}v1/accounts/{qt_number}/balances",
                            payload=bal_data,
                            records=bal_data.get("perCurrencyBalances", []) or [],
                            record_source="perCurrencyBalances",
                            account_ref=qt_number,
                        )
                        combined_balances = bal_data.get("combinedBalances")
                        per_currency_balances = bal_data.get("perCurrencyBalances")
                        if not isinstance(combined_balances, list) or not isinstance(
                            per_currency_balances,
                            list,
                        ):
                            raise ValueError("Questrade balances response was incomplete")
                        total_equity = None
                        for bal in combined_balances:
                            if bal.get("currency") == "CAD":
                                total_equity = _finite_snapshot_float(
                                    bal.get("totalEquity"),
                                    field="total equity",
                                )
                                break
                        if total_equity is None and per_currency_balances:
                            total_equity = sum(
                                _finite_snapshot_float(
                                    bal.get("totalEquity"),
                                    field="per-currency total equity",
                                )
                                for bal in per_currency_balances
                            )
                        if total_equity is None:
                            raise ValueError("Questrade balances response did not include equity")
                        balance_succeeded = True
                    except Exception as e:
                        snapshot_fetch_failed = True
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="balances failed",
                            level="warning",
                            user_id=user_id,
                            account=account_ref,
                            error=type(e).__name__,
                            message=str(e),
                        )

                account = NormalizedAccount(
                    name=account_name,
                    account_type=acct_type,
                    external_id=f"account:{qt_number}",
                    currency="CAD",
                    balance=total_equity,
                    balance_authoritative=balance_succeeded,
                    holdings_authoritative=False,
                )
                accounts.append(account)
                account_key = account_result_key(account)

                account_holdings: list[NormalizedHolding] = []
                positions_succeeded = False
                if snapshot_scope:
                    try:
                        pos_data = await qt_api_get(access_token, api_server, f"accounts/{qt_number}/positions")
                        self.record_diagnostics_fetch(
                            stage="positions.list",
                            url=f"{api_server}v1/accounts/{qt_number}/positions",
                            payload=pos_data,
                            records=pos_data.get("positions", []) or [],
                            record_source="positions",
                            account_ref=qt_number,
                        )
                        positions = pos_data.get("positions")
                        if not isinstance(positions, list):
                            raise ValueError("Questrade positions response was incomplete")
                        symbol_names = await self._resolve_symbol_names(
                            access_token=access_token,
                            api_server=api_server,
                            positions=positions,
                            user_id=user_id,
                            account_ref=account_ref,
                        )
                        for pos in positions:
                            qty = _finite_snapshot_float(
                                pos.get("openQuantity", 0),
                                field="position quantity",
                            )
                            if qty == 0:
                                continue
                            symbol = pos.get("symbol", "???")
                            name = symbol_names.get(pos.get("symbolId")) or symbol
                            account_holdings.append(
                                NormalizedHolding(
                                    symbol=symbol,
                                    name=name,
                                    quantity=qty,
                                    market_value=_finite_snapshot_float(
                                        pos.get("currentMarketValue"),
                                        field="position market value",
                                    ),
                                    average_cost=(
                                        _finite_snapshot_float(
                                            pos.get("totalCost"),
                                            field="position total cost",
                                        ) or None
                                        if pos.get("totalCost") is not None
                                        else None
                                    ),
                                    currency=pos.get("currency", "CAD") or "CAD",
                                )
                            )
                        positions_succeeded = True
                    except Exception as e:
                        snapshot_fetch_failed = True
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="positions failed",
                            level="warning",
                            user_id=user_id,
                            account=account_ref,
                            error=type(e).__name__,
                            message=str(e),
                        )

                if balance_succeeded:
                    try:
                        for bal in bal_data.get("perCurrencyBalances", []) or []:
                            cash = _finite_snapshot_float(
                                bal.get("cash", 0),
                                field="cash balance",
                            )
                            if cash == 0:
                                continue
                            curr = bal.get("currency", "CAD")
                            account_holdings.append(
                                NormalizedHolding(
                                    symbol=curr,
                                    name=f"{curr} Cash",
                                    quantity=1,
                                    market_value=cash,
                                    currency=curr,
                                )
                            )
                    except Exception as e:
                        snapshot_fetch_failed = True
                        balance_succeeded = False
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="cash balances failed",
                            level="warning",
                            user_id=user_id,
                            account=account_ref,
                            error=type(e).__name__,
                            message=str(e),
                        )

                account.holdings_authoritative = balance_succeeded and positions_succeeded

                holdings_by_account[account_key] = account_holdings
                all_holdings.extend(account_holdings)

                account_transactions: list[NormalizedTransaction] = []
                transaction_fetch_succeeded = False
                window_log: dict | None = None
                if fetch_transactions:
                    if persist_transaction_windows:
                        async def fetch_window(sync_window: AccountSyncWindow):
                            return await self._fetch_activities(
                                access_token,
                                api_server,
                                qt_number,
                                sync_window=sync_window,
                                user_id=user_id,
                            )

                        account_transactions, transaction_fetch_succeeded, window_log = (
                            await self.fetch_and_persist_transaction_import_windows(
                                user_id=user_id,
                                account_external_id=account.external_id or "",
                                account_name=account.name,
                                account_type=account.account_type,
                                is_liability=account.is_liability,
                                fetch_window=fetch_window,
                                backfill_days=QUESTRADE_MAX_HISTORY_DAYS,
                                backfill_chunk_days=QUESTRADE_DURABLE_BACKFILL_CHUNK_DAYS,
                                overlap_days=QUESTRADE_INCREMENTAL_OVERLAP_DAYS,
                                failure_message="Questrade transaction history window did not finish.",
                            )
                        )
                    else:
                        sync_window = await self._transaction_sync_window(user_id, account.external_id)
                        window_log = sync_window.as_dict()
                        account_transactions, transaction_fetch_succeeded = await self._fetch_activities(
                            access_token,
                            api_server,
                            qt_number,
                            sync_window=sync_window,
                            user_id=user_id,
                        )
                transactions_by_account[account_key] = (
                    account_transactions if transaction_fetch_succeeded else []
                )
                if transaction_fetch_succeeded:
                    transaction_fetch_succeeded_accounts.add(account_key)
                    all_transactions.extend(account_transactions)
                elif fetch_transactions:
                    transaction_fetch_failed = True
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="account sync result",
                    user_id=user_id,
                    account=account_ref,
                    mode=window_log.get("mode") if isinstance(window_log, dict) else None,
                    window=window_log,
                    holdings=len(account_holdings),
                    transactions=len(account_transactions),
                    transactions_fetched=transaction_fetch_succeeded,
                    debug=True,
                )

            if snapshot_scope and snapshot_fetch_failed:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Questrade balances or positions did not finish. Try syncing again.",
                )

            if transaction_fetch_failed:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Questrade transaction history did not finish. Try syncing again.",
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
        except QuestradeAuthRequiredError as e:
            return self.auth_required_result(
                user_id=user_id,
                message=str(e),
                stage="sync auth required",
                exception=e,
            )
        except QuestradeTemporaryError as e:
            return self.network_error_result(
                user_id=user_id,
                message=str(e),
                stage="sync temporary failure",
                exception=e,
            )
        except Exception as e:
            return self.exception_result(user_id, e)

    async def _fetch_activities(
        self,
        access_token: str,
        api_server: str,
        account_number: str,
        *,
        sync_window: AccountSyncWindow,
        user_id: int | None = None,
    ) -> tuple[list[NormalizedTransaction], bool]:
        mode = sync_window.mode
        window_start, window_end = _questrade_sync_window_bounds(sync_window)
        fetch_succeeded = True
        account_ref = _questrade_account_ref(account_number)

        seen_external_ids: set[str] = set()
        transactions: list[NormalizedTransaction] = []
        activity_server_refreshes = 0

        async def refresh_activity_server(reason: str, exc: Exception | None = None) -> bool:
            nonlocal access_token, api_server, activity_server_refreshes
            if user_id is None or activity_server_refreshes >= QUESTRADE_ACTIVITY_SERVER_RETRIES:
                return False
            activity_server_refreshes += 1
            previous_api_server = api_server
            try:
                access_token, api_server = await get_authenticated_client(user_id)
            except Exception as refresh_exc:
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="activity server refresh failed",
                    level="warning",
                    user_id=user_id,
                    account=account_ref,
                    reason=reason,
                    attempt=activity_server_refreshes,
                    error=type(refresh_exc).__name__,
                    message=str(refresh_exc),
                )
                return False
            log_connector_event(
                logger,
                provider=self.provider,
                stage="activity server refresh",
                level="warning" if exc else "info",
                user_id=user_id,
                account=account_ref,
                reason=reason,
                attempt=activity_server_refreshes,
                previous_api_server=previous_api_server,
                api_server=api_server,
                error=type(exc).__name__ if exc else None,
                message=str(exc) if exc else None,
            )
            return True

        def append_activities(activities: list[dict]) -> bool:
            appended_any = False

            for act in activities:
                trade_date = (
                    act.get("tradeDate")
                    or act.get("transactionDate")
                    or act.get("settlementDate")
                    or ""
                ).strip()
                symbol = (act.get("symbol") or "").strip()
                amount = float(act.get("netAmount", 0) or 0)
                raw_type = (act.get("type") or "").strip()
                action = (act.get("action") or "").strip()
                description = (act.get("description") or "").strip() or None

                mapped_type = map_questrade_activity_type(raw_type, action)
                if not mapped_type:
                    continue

                external_id = f"questrade_{trade_date}_{symbol}_{amount}_{raw_type}_{action}"
                if external_id in seen_external_ids:
                    continue

                tx_date = _parse_questrade_date(trade_date)
                if tx_date is None:
                    continue

                quantity = float(act.get("quantity", 0) or 0) or None
                if quantity is None and mapped_type == "dividend":
                    quantity = derive_dividend_quantity_from_description(description, amount)

                transactions.append(
                    NormalizedTransaction(
                        external_id=external_id,
                        date=tx_date,
                        type=mapped_type,
                        amount=amount,
                        currency=act.get("currency", "CAD"),
                        symbol=symbol or None,
                        description=description,
                        quantity=quantity,
                        price=float(act.get("price", 0) or 0) or None,
                        commission=float(act.get("commission", 0) or 0) or None,
                    )
                )
                seen_external_ids.add(external_id)
                appended_any = True

            return appended_any

        async def fetch_activity_chunk(
            chunk_start: datetime,
            chunk_end: datetime,
        ) -> tuple[list[dict], bool]:
            start_time = _format_questrade_activity_time(chunk_start)
            end_time = _format_questrade_activity_time(chunk_end)
            query = urlencode({"startTime": start_time, "endTime": end_time})

            retryable_errors = (QuestradeTransportError, QuestradeTemporaryError)
            attempts = QUESTRADE_ACTIVITY_SERVER_RETRIES + 1 if user_id is not None else 1
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    data = await qt_api_get(
                        access_token,
                        api_server,
                        f"accounts/{account_number}/activities?{query}",
                        timeout=QUESTRADE_ACTIVITY_TIMEOUT,
                    )
                    activities = data.get("activities", []) or []
                    self.record_diagnostics_fetch(
                        stage="transactions.window",
                        url=f"{api_server}v1/accounts/{account_number}/activities",
                        params={"startTime": start_time, "endTime": end_time},
                        payload=data,
                        records=activities,
                        record_source="activities",
                        account_ref=account_number,
                        window={
                            "mode": mode,
                            "startTime": start_time,
                            "endTime": end_time,
                        },
                    )
                    return activities, True
                except QuestradeAuthRequiredError:
                    raise
                except retryable_errors as exc:
                    last_error = exc
                    should_refresh = isinstance(exc, QuestradeTransportError)
                    if (
                        should_refresh
                        and attempt < attempts
                        and await refresh_activity_server("activity_fetch_failed", exc)
                    ):
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    break

            if last_error is None:
                last_error = QuestradeTemporaryError("Connection failed - Questrade activity fetch did not complete")
            self.record_diagnostics_fetch(
                stage="transactions.window",
                url=f"{api_server}v1/accounts/{account_number}/activities",
                status=None,
                params={"startTime": start_time, "endTime": end_time},
                payload={
                    "error": type(last_error).__name__,
                    "message": str(last_error),
                },
                records=[],
                record_source="activities",
                account_ref=account_number,
                window={
                    "mode": mode,
                    "startTime": start_time,
                    "endTime": end_time,
                },
            )
            log_connector_event(
                logger,
                provider=self.provider,
                stage="activities failed",
                level="warning",
                user_id=user_id,
                account=account_ref,
                mode=mode,
                window=sync_window.as_dict(),
                chunk_start=start_time,
                chunk_end=end_time,
                error=type(last_error).__name__,
                message=str(last_error),
            )
            return [], False

        fetch_succeeded = await walk_transaction_backfill_windows(
            mode=mode,
            window_start=window_start,
            window_end=window_end,
            chunk_days=(
                QUESTRADE_INCREMENTAL_ACTIVITY_CHUNK_DAYS
                if mode == "incremental"
                else QUESTRADE_ACTIVITY_CHUNK_DAYS
            ),
            max_history_days=QUESTRADE_MAX_HISTORY_DAYS,
            empty_history_stop_chunks=QUESTRADE_EMPTY_HISTORY_STOP_CHUNKS,
            fetch_chunk=fetch_activity_chunk,
            append_rows=append_activities,
            stop_on_chunk_failure=True,
        )

        return transactions, fetch_succeeded
