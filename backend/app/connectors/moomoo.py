from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.sync_context import get_connector_sync_context
from app.connectors.sync_modes import AccountSyncWindow
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    NormalizedTransaction,
    SyncResult,
    account_result_key,
)
from app.services.connection_auth_storage import (
    load_connection_artifact_async,
    save_connection_artifact_async,
)
from app.services.currency import get_fx_rates
from app.services.moomoo_cloud import (
    MOOMOO_CLOUD_TIMEOUT,
    MoomooCloudHistoryRateLimiter,
    MoomooCloudAuthRequired,
    MoomooCloudTemporaryError,
    api_get as moomoo_cloud_get,
    fetch_history_pages as fetch_moomoo_cloud_history_pages,
    granted_account_ids as moomoo_cloud_granted_account_ids,
    markets_for_account as moomoo_cloud_markets_for_account,
    refresh_access_token as refresh_moomoo_cloud_access_token,
    sanitize_scope as sanitize_moomoo_cloud_scope,
)

MOOMOO_BACKFILL_DAYS = 3650
MOOMOO_CLOUD_HISTORY_CHUNK_DAYS = 360
MOOMOO_FUNDS_QUERY_CURRENCY = "USD"
MOOMOO_CASH_FIELDS = {
    "CAD": "ca_cash",
    "USD": "us_cash",
    "HKD": "hk_cash",
    "CNH": "cn_cash",
    "JPY": "jp_cash",
    "SGD": "sg_cash",
    "AUD": "au_cash",
    "MYR": "my_cash",
}
MOOMOO_MARKET_CURRENCY = {
    "CA": "CAD",
    "US": "USD",
    "HK": "HKD",
}
MOOMOO_SECURITY_FIRM_HOME_CURRENCY = {
    "FUTUSECURITIES": "HKD",
    "FUTUINC": "USD",
    "FUTUSG": "SGD",
    "FUTUAU": "AUD",
    "FUTUCA": "CAD",
    "FUTUMY": "MYR",
    "FUTUJP": "JPY",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    try:
        text = _clean_text(value)
        if not text or text.upper() in {"N/A", "NONE", "NAN"}:
            return None
        parsed = float(text)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _masked_account_ref(account_id: Any) -> str:
    text = _clean_text(account_id)
    return f"...{text[-4:]}" if len(text) > 4 else text or "unknown"


def _moomoo_code_symbol(code: Any) -> str:
    text = _clean_text(code)
    if "." in text:
        return text.split(".", 1)[1] or text
    return text


def _currency_from_code(code: Any, fallback: str = "CAD") -> str:
    text = _clean_text(code).upper()
    if "." in text:
        prefix = text.split(".", 1)[0]
        return MOOMOO_MARKET_CURRENCY.get(prefix, fallback)
    return fallback


def _account_home_currency(row: dict[str, Any], markets: list[str]) -> str:
    security_firm = _clean_text(row.get("security_firm")).upper()
    if security_firm in MOOMOO_SECURITY_FIRM_HOME_CURRENCY:
        return MOOMOO_SECURITY_FIRM_HOME_CURRENCY[security_firm]
    return MOOMOO_MARKET_CURRENCY.get(markets[0], "CAD") if markets else "CAD"


def _convert_balance_currency(
    balance: float,
    from_currency: str,
    to_currency: str,
    cad_rates: dict[str, float],
) -> float | None:
    source = _clean_text(from_currency).upper()
    target = _clean_text(to_currency).upper()
    if source == target:
        return balance
    source_rate = 1.0 if source == "CAD" else _safe_float(cad_rates.get(source))
    target_rate = 1.0 if target == "CAD" else _safe_float(cad_rates.get(target))
    if source_rate is None or source_rate <= 0 or target_rate is None or target_rate <= 0:
        return None
    return balance / source_rate * target_rate


def _parse_moomoo_datetime(value: Any) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.isdigit():
        try:
            numeric = int(text)
            divisor = 1_000_000 if numeric >= 100_000_000_000_000 else 1_000
            return datetime.fromtimestamp(numeric / divisor, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _map_moomoo_account_type(row: dict[str, Any]) -> str:
    combined = " ".join(_clean_text(value).upper() for value in row.values())
    if "TFSA" in combined or "TAX FREE" in combined:
        return "tfsa"
    if "RRSP" in combined or "RSP" in combined:
        return "rrsp"
    if "FHSA" in combined:
        return "fhsa"
    if "RESP" in combined:
        return "resp"
    if "LIRA" in combined or "LIF" in combined:
        return "lira"
    return "margin"


def _normalize_funds_row(row: dict[str, Any] | None, *, fallback_currency: str = "CAD") -> tuple[float | None, str]:
    row = row or {}
    currency = _clean_text(row.get("currency")) or fallback_currency
    balance = None
    for field in ("total_assets", "securities_assets", "cash"):
        parsed = _safe_float(row.get(field))
        if parsed is not None:
            balance = parsed
            break
    return balance, currency


def _normalize_cash_holdings(row: dict[str, Any] | None) -> list[NormalizedHolding]:
    row = row or {}
    holdings: list[NormalizedHolding] = []
    for currency, field in MOOMOO_CASH_FIELDS.items():
        amount = _safe_float(row.get(field))
        if amount is None or amount == 0:
            continue
        holdings.append(
            NormalizedHolding(
                symbol=currency,
                name=f"{currency} Cash",
                quantity=1,
                market_value=amount,
                currency=currency,
            )
        )
    return holdings


def _normalize_positions(rows: list[dict[str, Any]]) -> list[NormalizedHolding]:
    holdings: list[NormalizedHolding] = []
    for row in rows:
        quantity = _safe_float(row.get("qty"))
        if quantity is None or quantity == 0:
            continue
        code = _clean_text(row.get("code"))
        symbol = _moomoo_code_symbol(code)
        if not symbol:
            continue
        currency = _clean_text(row.get("currency")) or _currency_from_code(code)
        holdings.append(
            NormalizedHolding(
                symbol=symbol,
                name=_clean_text(row.get("stock_name")) or symbol,
                quantity=quantity,
                market_value=_safe_float(row.get("market_val")),
                average_cost=_safe_float(row.get("average_cost")) or _safe_float(row.get("cost_price")),
                last_price=_safe_float(row.get("nominal_price")),
                change_pct=_safe_float(row.get("pl_ratio")),
                daily_pnl=_safe_float(row.get("today_pl_val")),
                currency=currency,
            )
        )
    return holdings


def _map_deal_side(value: Any) -> str | None:
    side = _clean_text(value).upper()
    if side in {"BUY", "BUY_BACK"}:
        return "buy"
    if side in {"SELL", "SELL_SHORT"}:
        return "sell"
    return None


def _normalize_deals(
    rows: list[dict[str, Any]],
    *,
    account_id: int | str,
    order_currency_by_id: dict[str, str],
    fallback_currency: str = "CAD",
) -> list[NormalizedTransaction]:
    transactions: list[NormalizedTransaction] = []
    seen_external_ids: set[str] = set()
    for row in rows:
        status = _clean_text(row.get("status")).upper()
        if status and status not in {"OK", "FILLED_ALL", "FILLED_PART"}:
            continue
        mapped_type = _map_deal_side(row.get("trd_side"))
        if not mapped_type:
            continue
        tx_date = _parse_moomoo_datetime(row.get("create_time"))
        if tx_date is None:
            continue
        quantity = _safe_float(row.get("qty"))
        price = _safe_float(row.get("price"))
        if quantity is None or price is None:
            continue
        code = _clean_text(row.get("code"))
        symbol = _moomoo_code_symbol(code)
        order_id = _clean_text(row.get("order_id"))
        deal_id = _clean_text(row.get("deal_id"))
        external_id = f"moomoo_{deal_id}" if deal_id else (
            f"moomoo_{account_id}_{tx_date.isoformat()}_{code}_{mapped_type}_{quantity}_{price}"
        )
        if external_id in seen_external_ids:
            continue
        currency = order_currency_by_id.get(order_id) or _currency_from_code(code, fallback_currency)
        gross = abs(quantity * price)
        amount = -gross if mapped_type == "buy" else gross
        transactions.append(
            NormalizedTransaction(
                external_id=external_id,
                date=tx_date,
                type=mapped_type,
                amount=amount,
                currency=currency,
                symbol=symbol or None,
                description=_clean_text(row.get("stock_name")) or symbol or None,
                quantity=abs(quantity),
                price=price,
            )
        )
        seen_external_ids.add(external_id)
    return transactions




class MoomooConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="moomoo", display_name="Moomoo")

    async def _sync_impl(self, user_id: int) -> SyncResult:
        sync_context = get_connector_sync_context()
        institution_id = sync_context.institution_id if sync_context is not None else None
        cloud_session = await load_connection_artifact_async(
            user_id,
            self.provider,
            "session",
            institution_id=institution_id,
        )
        if (
            not isinstance(cloud_session, dict)
            or cloud_session.get("schema") != "breaktwenty.moomoo-cloud-oauth.v1"
            or cloud_session.get("auth_mode") != "cloud_oauth"
        ):
            return self.auth_required_result(
                user_id=user_id,
                message="Moomoo authorization is missing. Reconnect Moomoo.",
                stage="cloud auth required",
            )
        try:
            return await self._sync_cloud(
                user_id,
                cloud_session,
                institution_id=institution_id,
            )
        except MoomooCloudAuthRequired as exc:
            return self.auth_required_result(
                user_id=user_id,
                message="Moomoo authorization expired or no longer grants account read access. Reconnect Moomoo.",
                stage="cloud auth required",
                exception=exc,
                cloud_stage=exc.stage,
                status_code=exc.status_code,
                provider_code=exc.provider_code,
            )
        except MoomooCloudTemporaryError as exc:
            return self.network_error_result(
                user_id=user_id,
                message="Moomoo cloud account access did not finish. Try syncing again.",
                stage="cloud request failed",
                exception=exc,
                cloud_stage=exc.stage,
                status_code=exc.status_code,
                provider_code=exc.provider_code,
            )
    async def _sync_cloud(
        self,
        user_id: int,
        session: dict[str, Any],
        *,
        institution_id: int | None,
    ) -> SyncResult:
        client_id = _clean_text(session.get("client_id"))
        refresh_token = _clean_text(session.get("refresh_token"))
        if not client_id or not refresh_token:
            raise MoomooCloudAuthRequired(
                "Stored Moomoo cloud authorization is incomplete.",
                stage="stored_authorization",
                provider_code="missing_stored_token",
            )
        tokens = await refresh_moomoo_cloud_access_token(
            client_id=client_id,
            refresh_token=refresh_token,
            granted_scope=_clean_text(session.get("scope")),
        )
        self.log_event(
            "cloud token refreshed",
            user_id=user_id,
            scopes=sanitize_moomoo_cloud_scope(tokens.scope),
            expires_in=tokens.expires_in,
            debug=True,
        )
        updated_session = {
            **session,
            "refresh_token": tokens.refresh_token,
            "scope": tokens.scope,
        }
        if updated_session != session:
            saved = await save_connection_artifact_async(
                user_id,
                self.provider,
                "session",
                updated_session,
                institution_id=institution_id,
            )
            if not saved:
                raise MoomooCloudTemporaryError(
                    "Moomoo cloud authorization could not be updated.",
                    stage="token_storage",
                    provider_code="storage_failed",
                )

        async with httpx.AsyncClient(timeout=MOOMOO_CLOUD_TIMEOUT, trust_env=False) as client:
            history_rate_limiter = MoomooCloudHistoryRateLimiter()
            account_payload, account_status = await moomoo_cloud_get(
                "/api/v1.0/accounts/authorized_trd_accs",
                tokens.access_token,
                stage="authorized_accounts",
                client=client,
            )
            account_rows = account_payload.get("accounts") if isinstance(account_payload, dict) else None
            if not isinstance(account_rows, list) or not account_rows:
                raise MoomooCloudAuthRequired(
                    "Moomoo authorization returned no trading accounts.",
                    stage="authorized_accounts",
                    status_code=account_status,
                    provider_code="no_accounts",
                )
            self.record_diagnostics_fetch(
                stage="accounts.list",
                method="GET",
                transport="oauth_rest",
                url="https://webapi.moomoo.com/api/v1.0/accounts/authorized_trd_accs",
                status=account_status,
                payload=account_payload,
                records=account_rows,
                record_source="accounts",
            )
            granted_ids = moomoo_cloud_granted_account_ids(tokens.scope)
            accounts: list[NormalizedAccount] = []
            holdings_by_account: dict[str, list[NormalizedHolding]] = {}
            transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
            transaction_fetch_succeeded_accounts: set[str] = set()
            all_holdings: list[NormalizedHolding] = []
            all_transactions: list[NormalizedTransaction] = []
            persist_transaction_windows = self.should_persist_transaction_windows()
            snapshot_scope = not persist_transaction_windows
            cad_fx_rates: dict[str, float] | None = None

            for raw_account in account_rows:
                if not isinstance(raw_account, dict):
                    continue
                account_id = _clean_text(raw_account.get("account_id"))
                if not account_id:
                    raise MoomooCloudTemporaryError(
                        "Moomoo returned an account without an account ID.",
                        stage="authorized_accounts",
                        provider_code="missing_account_id",
                    )
                if "*" not in granted_ids and account_id not in granted_ids:
                    raise MoomooCloudAuthRequired(
                        "Moomoo returned an account outside the authorized account grant.",
                        stage="authorized_accounts",
                        provider_code="account_scope_mismatch",
                    )
                markets = moomoo_cloud_markets_for_account(raw_account)
                if not markets:
                    raise MoomooCloudTemporaryError(
                        "Moomoo returned no supported trading market for an authorized account.",
                        stage="authorized_accounts",
                        provider_code="no_queryable_market",
                    )
                account_ref = _masked_account_ref(
                    raw_account.get("account_card_number") or account_id
                )
                account_currency = _account_home_currency(raw_account, markets)
                funds_row: dict[str, Any] | None = None
                positions: list[dict[str, Any]] = []
                if snapshot_scope:
                    encoded_account_id = quote(account_id, safe="")
                    funds_payload, funds_status = await moomoo_cloud_get(
                        f"/api/v1.0/accounts/{encoded_account_id}/funds",
                        tokens.access_token,
                        stage="account_funds",
                        params={"currency": MOOMOO_FUNDS_QUERY_CURRENCY},
                        client=client,
                    )
                    if not isinstance(funds_payload, dict):
                        raise MoomooCloudTemporaryError(
                            "Moomoo funds response was incomplete.",
                            stage="account_funds",
                            status_code=funds_status,
                            provider_code="invalid_funds",
                        )
                    funds_row = funds_payload
                    self.record_diagnostics_fetch(
                        stage="balances.list",
                        method="GET",
                        transport="oauth_rest",
                        url="https://webapi.moomoo.com/api/v1.0/accounts/[account]/funds",
                        status=funds_status,
                        payload=funds_payload,
                        records=[funds_payload],
                        record_source="funds",
                        account_ref=account_id,
                    )
                    positions_payload, positions_status = await moomoo_cloud_get(
                        f"/api/v1.0/accounts/{encoded_account_id}/positions",
                        tokens.access_token,
                        stage="account_positions",
                        client=client,
                    )
                    positions = (
                        positions_payload
                        if isinstance(positions_payload, list)
                        else positions_payload.get("positions", [])
                        if isinstance(positions_payload, dict)
                        else []
                    )
                    if not isinstance(positions, list):
                        raise MoomooCloudTemporaryError(
                            "Moomoo positions response was incomplete.",
                            stage="account_positions",
                            status_code=positions_status,
                            provider_code="invalid_positions",
                        )
                    for position in positions:
                        if not isinstance(position, dict):
                            raise MoomooCloudTemporaryError(
                                "Moomoo positions response contained an invalid row.",
                                stage="account_positions",
                                status_code=positions_status,
                                provider_code="invalid_position_row",
                            )
                        quantity = _safe_float(position.get("qty"))
                        if quantity is None or (
                            quantity != 0
                            and (
                                not _clean_text(position.get("code"))
                                or _safe_float(position.get("market_val")) is None
                            )
                        ):
                            raise MoomooCloudTemporaryError(
                                "Moomoo positions response was incomplete.",
                                stage="account_positions",
                                status_code=positions_status,
                                provider_code="incomplete_position",
                            )
                    self.record_diagnostics_fetch(
                        stage="positions.list",
                        method="GET",
                        transport="oauth_rest",
                        url="https://webapi.moomoo.com/api/v1.0/accounts/[account]/positions",
                        status=positions_status,
                        payload=positions_payload,
                        records=positions,
                        record_source="positions",
                        account_ref=account_id,
                    )

                balance, funds_currency = _normalize_funds_row(
                    funds_row,
                    fallback_currency=MOOMOO_FUNDS_QUERY_CURRENCY,
                )
                if snapshot_scope and balance is None:
                    raise MoomooCloudTemporaryError(
                        "Moomoo funds response did not contain an account balance.",
                        stage="account_funds",
                        provider_code="missing_balance",
                    )
                if balance is not None and funds_currency != account_currency:
                    if cad_fx_rates is None:
                        cad_fx_rates = await get_fx_rates("CAD")
                    converted_balance = _convert_balance_currency(
                        balance,
                        funds_currency,
                        account_currency,
                        cad_fx_rates,
                    )
                    if converted_balance is None:
                        raise MoomooCloudTemporaryError(
                            "Moomoo account balance could not be converted to its home currency.",
                            stage="account_balance_conversion",
                            provider_code="missing_fx_rate",
                        )
                    balance = converted_balance
                account = NormalizedAccount(
                    name=f"Moomoo {account_ref}",
                    account_type=_map_moomoo_account_type(raw_account),
                    external_id=f"account:{account_id}",
                    currency=account_currency,
                    balance=balance,
                    balance_authoritative=snapshot_scope and balance is not None,
                    holdings_authoritative=snapshot_scope,
                )
                accounts.append(account)
                account_key = account_result_key(account)
                account_holdings = _normalize_cash_holdings(funds_row)
                account_holdings.extend(_normalize_positions(positions))
                account_transactions: list[NormalizedTransaction] = []
                window_log: dict[str, Any] | None = None

                async def fetch_window(sync_window: AccountSyncWindow):
                    start = sync_window.start_date or (sync_window.end_date - timedelta(days=MOOMOO_BACKFILL_DAYS))
                    transactions: list[NormalizedTransaction] = []
                    for market in markets:
                        orders, order_pages = await fetch_moomoo_cloud_history_pages(
                            account_id=account_id,
                            access_token=tokens.access_token,
                            endpoint="orders_history",
                            rows_key="orders",
                            market=market,
                            start=start,
                            end=sync_window.end_date,
                            page_size=100,
                            client=client,
                            rate_limiter=history_rate_limiter,
                        )
                        fills, fill_pages = await fetch_moomoo_cloud_history_pages(
                            account_id=account_id,
                            access_token=tokens.access_token,
                            endpoint="fills_history",
                            rows_key="order_fills",
                            market=market,
                            start=start,
                            end=sync_window.end_date,
                            page_size=50,
                            client=client,
                            rate_limiter=history_rate_limiter,
                        )
                        self.record_diagnostics_fetch(
                            stage="orders.list",
                            method="GET",
                            transport="oauth_rest",
                            url="https://webapi.moomoo.com/api/v1.0/accounts/[account]/orders_history",
                            payload={"pages": order_pages, "row_count": len(orders)},
                            records=orders,
                            record_source="orders",
                            account_ref=account_id,
                            window={"market": market, "start": start.isoformat(), "end": sync_window.end_date.isoformat()},
                        )
                        self.record_diagnostics_fetch(
                            stage="transactions.deals",
                            method="GET",
                            transport="oauth_rest",
                            url="https://webapi.moomoo.com/api/v1.0/accounts/[account]/fills_history",
                            payload={"pages": fill_pages, "row_count": len(fills)},
                            records=fills,
                            record_source="deals",
                            account_ref=account_id,
                            window={"market": market, "start": start.isoformat(), "end": sync_window.end_date.isoformat()},
                        )
                        order_currency_by_id = {
                            _clean_text(row.get("order_id")): _clean_text(row.get("currency"))
                            for row in orders
                            if _clean_text(row.get("order_id")) and _clean_text(row.get("currency"))
                        }
                        transactions.extend(_normalize_deals(
                            fills,
                            account_id=account_id,
                            order_currency_by_id=order_currency_by_id,
                            fallback_currency=MOOMOO_MARKET_CURRENCY.get(market, account_currency),
                        ))
                        self.log_event(
                            "cloud history result",
                            user_id=user_id,
                            account=account_ref,
                            market=market,
                            start=start.isoformat(),
                            end=sync_window.end_date.isoformat(),
                            order_pages=len(order_pages),
                            orders=len(orders),
                            fill_pages=len(fill_pages),
                            fills=len(fills),
                            debug=True,
                        )
                    return transactions, True

                if self.should_fetch_transactions():
                    if persist_transaction_windows:
                        account_transactions, transaction_succeeded, window_log = (
                            await self.fetch_and_persist_transaction_import_windows(
                                user_id=user_id,
                                account_external_id=account.external_id or "",
                                account_name=account.name,
                                account_type=account.account_type,
                                is_liability=account.is_liability,
                                fetch_window=fetch_window,
                                backfill_days=MOOMOO_BACKFILL_DAYS,
                                backfill_chunk_days=MOOMOO_CLOUD_HISTORY_CHUNK_DAYS,
                                failure_message="Moomoo cloud transaction history window did not finish.",
                            )
                        )
                    else:
                        sync_window = await self._transaction_sync_window(user_id, account.external_id)
                        window_log = sync_window.as_dict()
                        account_transactions, transaction_succeeded = await fetch_window(sync_window)
                    if transaction_succeeded:
                        transaction_fetch_succeeded_accounts.add(account_key)
                holdings_by_account[account_key] = account_holdings
                transactions_by_account[account_key] = account_transactions
                all_holdings.extend(account_holdings)
                all_transactions.extend(account_transactions)
                self.log_event(
                    "cloud account result",
                    user_id=user_id,
                    account=account_ref,
                    markets=markets,
                    holdings=len(account_holdings),
                    transactions=len(account_transactions),
                    window=window_log,
                    debug=True,
                )

        if not accounts:
            raise MoomooCloudAuthRequired(
                "Moomoo authorization returned no usable trading accounts.",
                stage="authorized_accounts",
                provider_code="no_usable_accounts",
            )
        return self.ok_result(
            accounts=accounts,
            holdings=all_holdings,
            transactions=all_transactions,
            holdings_by_account=holdings_by_account,
            transactions_by_account=transactions_by_account,
            transaction_fetch_succeeded_accounts=transaction_fetch_succeeded_accounts,
        )
