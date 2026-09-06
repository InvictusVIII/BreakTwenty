from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.connector_logging import log_connector_event
from app.connectors.sdk_process import SdkProcessError
from app.connectors.sync_modes import (
    AccountSyncWindow,
    sync_window_utc_bounds,
)
from app.connectors.wealthsimple_helpers import (
    WS_ACTIVITY_TYPE_MAP,
    classify_wealthsimple_account,
)
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.services.sync_utils import is_network_error
from app.services.wealthsimple import (
    WEALTHSIMPLE_BLOCKING_CALL_TIMEOUT_SECONDS,
    WealthsimpleSdkWorker,
    _network_result_if_unreachable,
    load_session_async,
    save_authenticated_session_async,
)

logger = logging.getLogger("breaktwenty.connectors.wealthsimple")

WEALTHSIMPLE_BACKFILL_DAYS = 3650
WEALTHSIMPLE_ACTIVITY_CHUNK_DAYS = 365


def _wealthsimple_account_ref(account_id: object) -> str:
    raw = str(account_id or "")
    return f"...{raw[-4:]}" if len(raw) >= 4 else "unknown"


def _is_wealthsimple_auth_error(exc: Exception) -> bool:
    if getattr(exc, "remote_type", "") in {
        "ManualLoginRequired",
        "LoginFailedException",
        "OTPRequiredException",
    }:
        return True
    err = str(exc).lower()
    return any(
        marker in err
        for marker in (
            "401",
            "unauthorized",
            "not authorized",
            "manual login required",
            "token",
            "client_id",
            "expired",
        )
    )


def _parse_wealthsimple_date(date_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _ws_amount(value) -> float | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("amount")
    if raw is None:
        return None
    try:
        parsed = float(raw)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _wealthsimple_balance_history(edges) -> tuple:
    """Map ``FetchAccountHistoricalFinancials`` edges → ``((datetime, balance), ...)`` for
    ``NormalizedAccount.balance_history``. Each node carries a ``date`` and a
    ``netLiquidationValueV2`` Money value; balances are stored positive (abs), matching the
    current-balance treatment so liabilities read as amount-owed."""
    if not isinstance(edges, list):
        return ()
    points: list[tuple[datetime, float]] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        point_date = _parse_wealthsimple_date(node.get("date"))
        amount = _ws_amount(node.get("netLiquidationValueV2"))
        if point_date is None or amount is None:
            continue
        points.append((point_date, abs(amount)))
    return tuple(points)


def _normalize_wealthsimple_position(
    edge: dict,
    *,
    account_key_by_ws_id: dict[str, str],
) -> tuple[str, NormalizedHolding, str | None] | None:
    """Map a single Wealthsimple PositionV2 edge to (account_key, NormalizedHolding, skip_reason).

    Returns None when the edge is fully unrecognizable. When the position cannot be normalized
    (option-typed, multi-leg strategy, multi-account aggregation, missing fields), returns
    (skip_reason,) packaged so the caller can log a structured diagnostic. Returns a real
    NormalizedHolding only for single-account, non-option, non-leg equity/ETF positions.
    """
    if not isinstance(edge, dict):
        return None
    node = edge.get("node") if "node" in edge else edge
    if not isinstance(node, dict):
        return None

    legs = node.get("legs")
    if isinstance(legs, list) and legs:
        return ("", None, "multi-leg strategy")

    security = node.get("security") or {}
    if not isinstance(security, dict):
        return ("", None, "missing security")

    option_details = security.get("optionDetails")
    if option_details:
        return ("", None, "option position")

    stock = security.get("stock") or {}
    if not isinstance(stock, dict):
        return ("", None, "non-stock security")

    symbol = (stock.get("symbol") or "").strip()
    name = (stock.get("name") or "").strip()
    if not symbol:
        return ("", None, "missing symbol")

    qty_raw = node.get("quantity")
    try:
        quantity = float(qty_raw) if qty_raw is not None else 0.0
    except (TypeError, ValueError):
        return ("", None, "non-numeric quantity")
    if not math.isfinite(quantity):
        return ("", None, "non-numeric quantity")
    if quantity == 0:
        return ("", None, "zero quantity")

    accounts_field = node.get("accounts") or []
    if not isinstance(accounts_field, list) or len(accounts_field) == 0:
        return ("", None, "no account attribution")
    if len(accounts_field) > 1:
        return ("", None, "aggregated across multiple accounts")

    ws_id = (accounts_field[0] or {}).get("id")
    if not ws_id:
        return ("", None, "blank account id")
    account_key = account_key_by_ws_id.get(ws_id)
    if not account_key:
        return ("", None, "account id not in current sync")

    total_value = _ws_amount(node.get("totalValue"))
    if total_value is None:
        return ("", None, "missing market value")
    book_value = _ws_amount(node.get("bookValue"))
    avg_price = _ws_amount(node.get("averagePrice"))
    average_cost = book_value if book_value is not None else (
        avg_price * quantity if avg_price is not None else None
    )

    quote = security.get("quoteV2") or {}
    last_price = None
    if isinstance(quote, dict):
        raw_price = quote.get("price")
        try:
            last_price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            last_price = None
        if last_price is not None and not math.isfinite(last_price):
            last_price = None

    currency = str(security.get("currency") or "CAD").upper()

    holding = NormalizedHolding(
        symbol=symbol,
        name=name or symbol,
        quantity=quantity,
        market_value=total_value,
        average_cost=average_cost,
        last_price=last_price,
        currency=currency,
    )
    return (account_key, holding, None)


def _normalize_wealthsimple_activity(
    act: dict,
    *,
    requested_account_key: str,
    account_key_by_ws_id: dict[str, str],
    seen_external_ids: set[str],
) -> tuple[str, NormalizedTransaction] | None:
    act_id = act.get("id") or act.get("canonical_id") or ""
    if not act_id:
        return None

    external_id = f"wealthsimple_{act_id}"
    if external_id in seen_external_ids:
        return None

    account_key = account_key_by_ws_id.get(act.get("account_id", "")) or requested_account_key
    if not account_key:
        return None

    raw_type = (act.get("type") or act.get("object") or "").lower().replace("-", "_")
    mapped_type = WS_ACTIVITY_TYPE_MAP.get(raw_type)
    if not mapped_type:
        return None

    net_cash = act.get("net_cash") or act.get("amount") or {}
    if isinstance(net_cash, dict):
        amount = float(net_cash.get("amount", 0) or 0)
        currency = net_cash.get("currency", "CAD")
    else:
        amount = float(net_cash or 0)
        currency = "CAD"

    tx_date = _parse_wealthsimple_date(
        act.get("completed_at")
        or act.get("created_at")
        or act.get("effective_date")
        or ""
    )
    if tx_date is None:
        return None

    price_raw = act.get("market_value") or act.get("price")
    if isinstance(price_raw, dict):
        price_raw = price_raw.get("amount")

    symbol = act.get("symbol")
    security = act.get("security")
    if not symbol and isinstance(security, dict):
        symbol = security.get("stock", {}).get("symbol")

    seen_external_ids.add(external_id)
    return account_key, NormalizedTransaction(
        external_id=external_id,
        date=tx_date,
        type=mapped_type,
        amount=amount,
        currency=currency,
        symbol=symbol or None,
        description=act.get("description") or None,
        quantity=float(act.get("quantity")) if act.get("quantity") else None,
        price=float(price_raw) if price_raw else None,
    )


class WealthsimpleConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="wealthsimple", display_name="Wealthsimple")

    def is_auth_required_exception(self, exc: Exception) -> bool:
        return _is_wealthsimple_auth_error(exc) or super().is_auth_required_exception(exc)

    def _create_sdk_worker(
        self,
        user_id: int,
        session_data: dict,
    ) -> WealthsimpleSdkWorker:
        return WealthsimpleSdkWorker(user_id, session_data)

    async def _call_sdk(
        self,
        worker: WealthsimpleSdkWorker,
        user_id: int,
        operation: str,
        payload: dict | None = None,
    ):
        result = await worker.call(
            operation,
            payload,
            timeout=WEALTHSIMPLE_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
        refreshed_sessions = result.get("refreshed_sessions") or []
        if refreshed_sessions:
            await save_authenticated_session_async(user_id, refreshed_sessions[-1])
        return result.get("value")

    async def _sync_impl(self, user_id: int) -> SyncResult:
        session_data = await load_session_async(user_id)
        if not isinstance(session_data, dict) or "client_id" not in session_data:
            network_result = await _network_result_if_unreachable(user_id=user_id)
            if network_result:
                return self.network_error_result(
                    user_id=user_id,
                    message=network_result.get("message") or "Connection failed",
                    stage="session network failure",
                )
            return self.auth_required_result(user_id=user_id)

        try:
            worker = self._create_sdk_worker(user_id, session_data)
            await worker.start()
            accounts_data = await self._call_sdk(worker, user_id, "accounts")
            self.record_diagnostics_fetch(
                stage="accounts.list",
                method="SDK",
                transport="sdk",
                url="wealthsimple://get_accounts",
                payload={"accounts": accounts_data},
                records=accounts_data,
                record_source="accounts",
            )

            accounts: list[NormalizedAccount] = []
            transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
            all_transactions: list[NormalizedTransaction] = []
            account_key_by_ws_id: dict[str, str] = {}
            activities_by_account: dict[str, list[dict]] = {}
            holdings_by_account: dict[str, list[NormalizedHolding]] = {}
            all_holdings: list[NormalizedHolding] = []
            transaction_fetch_succeeded_accounts: set[str] = set()
            fetch_transactions = self.should_fetch_transactions()
            persist_transaction_windows = self.should_persist_transaction_windows()
            snapshot_scope = not persist_transaction_windows
            transaction_fetch_failed = False
            snapshot_fetch_failed = False
            account_identity_missing = False

            for acct in accounts_data:
                if acct.get("status") != "open":
                    continue

                ws_account_id = acct.get("id", "")
                name = (
                    acct.get("nickname")
                    or acct.get("description")
                    or acct.get("unifiedAccountType", "Account")
                )
                balance = _ws_amount(
                    acct.get("financials", {})
                    .get("currentCombined", {})
                    .get("netLiquidationValue", {})
                )
                balance_authoritative = snapshot_scope and balance is not None
                if snapshot_scope and not balance_authoritative:
                    snapshot_fetch_failed = True
                currency = acct.get("currency", "CAD")
                acct_type, is_liability = classify_wealthsimple_account(acct.get("unifiedAccountType", ""))

                # Backfill daily account value: Wealthsimple exposes a per-account daily
                # net-liquidation series (the data behind its own net-worth chart), so the
                # graph can extend back before the first sync. Non-fatal — a failure here
                # leaves the account on its current-balance snapshot.
                balance_history_points: tuple = ()
                if snapshot_scope and ws_account_id:
                    try:
                        history_edges = await self._call_sdk(
                            worker,
                            user_id,
                            "historical_financials",
                            {
                                "account_id": ws_account_id,
                                "currency": currency,
                                "resolution": "DAILY",
                            },
                        )
                        balance_history_points = _wealthsimple_balance_history(history_edges)
                    except TimeoutError:
                        raise
                    except Exception as exc:
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="historical financials failed",
                            level="warning",
                            user_id=user_id,
                            error=type(exc).__name__,
                        )

                account = NormalizedAccount(
                    name=name,
                    account_type=acct_type,
                    external_id=f"account:{ws_account_id}" if ws_account_id else None,
                    currency=currency,
                    is_liability=is_liability,
                    balance=float(abs(balance)) if balance is not None else None,
                    balance_authoritative=balance_authoritative,
                    holdings_authoritative=False,
                    balance_history=balance_history_points,
                )
                accounts.append(account)
                account_key = account_result_key(account)

                if ws_account_id:
                    account_key_by_ws_id[ws_account_id] = account_key
                elif snapshot_scope:
                    account_identity_missing = True
                transactions_by_account.setdefault(account_key, [])

                if not ws_account_id:
                    continue

                account_ref = _wealthsimple_account_ref(ws_account_id)
                if fetch_transactions:
                    window_log: dict | None = None
                    try:
                        async def fetch_window(sync_window: AccountSyncWindow):
                            window_start, window_end = sync_window_utc_bounds(sync_window)
                            activities = await self._call_sdk(
                                worker,
                                user_id,
                                "activities",
                                {
                                    "account_id": ws_account_id,
                                    "start_date": window_start,
                                    "end_date": window_end,
                                },
                            )
                            self.record_diagnostics_fetch(
                                stage="transactions.window",
                                method="SDK",
                                transport="sdk",
                                url="wealthsimple://get_activities",
                                payload={"activities": activities},
                                records=activities,
                                record_source="activities",
                                account_ref=ws_account_id,
                                window=sync_window.as_dict(),
                            )
                            seen_window_ids: set[str] = set()
                            normalized = []
                            for activity in activities or []:
                                item = _normalize_wealthsimple_activity(
                                    activity,
                                    requested_account_key=account_key,
                                    account_key_by_ws_id=account_key_by_ws_id,
                                    seen_external_ids=seen_window_ids,
                                )
                                if item and item[0] == account_key:
                                    normalized.append(item[1])
                            return normalized, True

                        if persist_transaction_windows:
                            account_transactions, transaction_fetch_succeeded, window_log = (
                                await self.fetch_and_persist_transaction_import_windows(
                                    user_id=user_id,
                                    account_external_id=account.external_id or "",
                                    account_name=account.name,
                                    account_type=account.account_type,
                                    is_liability=account.is_liability,
                                    fetch_window=fetch_window,
                                    backfill_days=WEALTHSIMPLE_BACKFILL_DAYS,
                                    backfill_chunk_days=WEALTHSIMPLE_ACTIVITY_CHUNK_DAYS,
                                    failure_message="Wealthsimple transaction history window did not finish.",
                                )
                            )
                            transactions_by_account[account_key] = (
                                account_transactions if transaction_fetch_succeeded else []
                            )
                            if transaction_fetch_succeeded:
                                transaction_fetch_succeeded_accounts.add(account_key)
                                all_transactions.extend(account_transactions)
                            else:
                                transaction_fetch_failed = True
                        else:
                            sync_window = await self._transaction_sync_window(user_id, account.external_id)
                            window_log = sync_window.as_dict()
                            log_connector_event(
                                logger,
                                provider=self.provider,
                                stage="activities sync start",
                                user_id=user_id,
                                account=account_ref,
                                mode=sync_window.mode,
                                window=window_log,
                                debug=True,
                            )
                            window_start, window_end = sync_window_utc_bounds(sync_window)
                            activities = await self._call_sdk(
                                worker,
                                user_id,
                                "activities",
                                {
                                    "account_id": ws_account_id,
                                    "start_date": window_start,
                                    "end_date": window_end,
                                },
                            )
                            activities_by_account[account_key] = activities
                            self.record_diagnostics_fetch(
                                stage="transactions.window",
                                method="SDK",
                                transport="sdk",
                                url="wealthsimple://get_activities",
                                payload={"activities": activities},
                                records=activities,
                                record_source="activities",
                                account_ref=ws_account_id,
                                window=window_log,
                            )
                            transaction_fetch_succeeded_accounts.add(account_key)
                            transaction_fetch_succeeded = True
                            account_transactions = []
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="activities sync result",
                            user_id=user_id,
                            account=account_ref,
                            mode=window_log.get("mode") if isinstance(window_log, dict) else None,
                            window=window_log,
                            activities=(
                                len(account_transactions)
                                if persist_transaction_windows
                                else len(activities_by_account.get(account_key, []))
                            ),
                            debug=True,
                        )
                    except Exception as e:
                        if is_network_error(e):
                            return self.network_error_result(
                                user_id=user_id,
                                stage="activities network failure",
                                exception=e,
                                account=account_ref,
                            )
                        if _is_wealthsimple_auth_error(e):
                            network_result = await _network_result_if_unreachable(user_id=user_id)
                            if network_result:
                                return self.network_error_result(
                                    user_id=user_id,
                                    message=network_result.get("message") or "Connection failed",
                                    stage="activities network failure",
                                    account=account_ref,
                                )
                            return self.auth_required_result(
                                user_id=user_id,
                                stage="activities auth required",
                                exception=e,
                                account=account_ref,
                            )
                        log_connector_event(
                            logger,
                            provider=self.provider,
                            stage="activities failed",
                            level="warning",
                            user_id=user_id,
                            account=account_ref,
                            window=window_log,
                            error=type(e).__name__,
                            message=str(e),
                        )
                        if persist_transaction_windows:
                            transaction_fetch_failed = True

            positions_fetch_succeeded = snapshot_scope and not account_identity_missing
            if snapshot_scope and account_key_by_ws_id:
                try:
                    position_edges = await self._call_sdk(
                        worker,
                        user_id,
                        "positions",
                    )
                    if not isinstance(position_edges, list):
                        raise ValueError("Wealthsimple positions response was not a list")
                    self.record_diagnostics_fetch(
                        stage="positions.list",
                        method="GraphQL",
                        transport="sdk",
                        url="wealthsimple://FetchIdentityPositions",
                        payload={"positions": position_edges},
                        records=position_edges,
                        record_source="positions",
                    )
                    for edge in position_edges:
                        result = _normalize_wealthsimple_position(
                            edge,
                            account_key_by_ws_id=account_key_by_ws_id,
                        )
                        if result is None:
                            continue
                        account_key, holding, skip_reason = result
                        if skip_reason:
                            node = (edge or {}).get("node") if isinstance(edge, dict) else None
                            sec = (node or {}).get("security") or {} if isinstance(node, dict) else {}
                            log_connector_event(
                                logger,
                                provider=self.provider,
                                stage="position skipped",
                                level="info",
                                user_id=user_id,
                                reason=skip_reason,
                                security_id=sec.get("id") if isinstance(sec, dict) else None,
                                security_type=sec.get("securityType") if isinstance(sec, dict) else None,
                                debug=True,
                            )
                            if skip_reason in {
                                "missing security",
                                "missing symbol",
                                "non-numeric quantity",
                                "no account attribution",
                                "aggregated across multiple accounts",
                                "blank account id",
                                "missing market value",
                            }:
                                positions_fetch_succeeded = False
                                snapshot_fetch_failed = True
                            continue
                        holdings_by_account.setdefault(account_key, []).append(holding)
                        all_holdings.append(holding)
                except (SdkProcessError, TimeoutError):
                    raise
                except Exception as e:
                    positions_fetch_succeeded = False
                    snapshot_fetch_failed = True
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="positions failed",
                        level="warning",
                        user_id=user_id,
                        error=type(e).__name__,
                        message=str(e),
                    )

            if snapshot_scope:
                if account_identity_missing:
                    snapshot_fetch_failed = True
                for account in accounts:
                    account.holdings_authoritative = positions_fetch_succeeded

            seen_external_ids: set[str] = set()
            for requested_account_key, activities in activities_by_account.items():
                for act in activities or []:
                    item = _normalize_wealthsimple_activity(
                        act,
                        requested_account_key=requested_account_key,
                        account_key_by_ws_id=account_key_by_ws_id,
                        seen_external_ids=seen_external_ids,
                    )
                    if not item:
                        continue
                    account_key, tx = item
                    transactions_by_account.setdefault(account_key, []).append(tx)
                    all_transactions.append(tx)

            if persist_transaction_windows and transaction_fetch_failed:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Wealthsimple transaction history did not finish. Try syncing again.",
                )

            if snapshot_scope and snapshot_fetch_failed:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Wealthsimple balances or positions did not finish. Try syncing again.",
                )

            return SyncResult(
                status=SyncStatus.OK,
                accounts=accounts,
                holdings=all_holdings,
                holdings_by_account=holdings_by_account,
                transactions=all_transactions,
                transactions_by_account=transactions_by_account,
                transaction_fetch_succeeded_accounts=transaction_fetch_succeeded_accounts,
            )
        except Exception as e:
            if is_network_error(e):
                return self.network_error_result(
                    user_id=user_id,
                    stage="network failure",
                    exception=e,
                )

            if _is_wealthsimple_auth_error(e):
                network_result = await _network_result_if_unreachable(user_id=user_id)
                if network_result:
                    return self.network_error_result(
                        user_id=user_id,
                        message=network_result.get("message") or "Connection failed",
                        stage="network failure",
                    )
                return self.auth_required_result(
                    user_id=user_id,
                    stage="auth required",
                    exception=e,
                )
            return self.exception_result(user_id, e)
        finally:
            if "worker" in locals():
                await worker.close()
