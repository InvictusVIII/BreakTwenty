"""Coinbase connector — reference implementation of the Connector interface.

Wraps the Coinbase Advanced API to fetch accounts, holdings (with cost
basis), and full paginated transaction history.  All provider-specific
quirks (USD/CAD triangulation, cost basis fallback, fiat wallet filtering,
pagination) are encapsulated here.

DB persistence is handled externally by ``connectors.persistence``.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, urlparse

from app.connectors.api_base import ApiProviderConnectorBase
from app.connectors.connector_logging import get_connector_logger, log_connector_event
from app.connectors.sdk_process import SdkProcessCrashed, SdkProcessError, SdkProcessWorker
from app.connectors.sync_modes import (
    AccountSyncWindow,
    sync_window_utc_bounds,
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
from app.services.connection_auth_storage import get_api_credentials

logger = get_connector_logger("coinbase")

FIAT_CURRENCIES = {"CAD", "USD", "EUR", "GBP", "AUD", "CHF", "JPY"}
COINBASE_MAX_HISTORY_DAYS = 3650
COINBASE_TRANSACTION_CHUNK_DAYS = 365
COINBASE_BLOCKING_CALL_TIMEOUT_SECONDS = 60.0
COINBASE_TRANSACTION_CALL_TIMEOUT_SECONDS = 120.0
COINBASE_FX_MAX_AGE_SECONDS = 24 * 60 * 60

COINBASE_TX_TYPE_MAP = {
    "buy": "buy",
    "sell": "sell",
    "send": None,
    "receive": "deposit",
    "staking_reward": "dividend",
    "inflation_reward": "dividend",
    "interest": "interest",
    "fiat_deposit": "deposit",
    "fiat_withdrawal": "withdrawal",
    "exchange_deposit": "deposit",
    "exchange_withdrawal": "withdrawal",
}


# ---------------------------------------------------------------------------
# Helpers (pure functions, no DB access)
# ---------------------------------------------------------------------------

def _get_attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _coinbase_ref(value: object) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 6:
        return f"...{raw[-6:]}"
    return raw or "unknown"


def _is_coinbase_auth_error(error: Exception) -> bool:
    remote_type = getattr(error, "remote_type", "")
    if remote_type in {"AuthenticationError", "UnauthorizedException"}:
        return True
    message = str(error).lower()
    return any(
        keyword in message
        for keyword in (
            "unauthorized",
            "invalid api key",
            "invalid api credentials",
            "invalid api key name",
            "permission denied",
            "status code: 401",
            "401",
            "403",
        )
    )


def _amount_to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            return None
    raw = _get_attr(value, "value")
    if raw is None:
        raw = _get_attr(value, "amount")
    if raw is not None:
        try:
            parsed = float(raw)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            return None
    return None


def _get_credentials_from_settings(settings: dict[str, str]):
    return settings.get("coinbase_api_key"), settings.get("coinbase_api_secret")


def _make_client(api_key: str, api_secret: str):
    from coinbase.rest import RESTClient
    return RESTClient(api_key=api_key, api_secret=api_secret)


def _plain_sdk_value(value):
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _plain_sdk_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain_sdk_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _plain_sdk_value(model_dump())
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _plain_sdk_value(to_dict())
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {
            str(key): _plain_sdk_value(item)
            for key, item in raw.items()
            if not str(key).startswith("_")
        }
    return str(value)


class CoinbaseSdkProcessHandler:
    def __init__(self, payload: dict) -> None:
        self._user_id = int(payload.get("user_id") or 0) or None
        self._client = _make_client(
            str(payload.get("api_key") or ""),
            str(payload.get("api_secret") or ""),
        )

    def handle(self, operation: str, payload: dict):
        if operation == "accounts":
            response = self._client.get_accounts(limit=250)
            accounts = _get_attr(response, "accounts")
            if isinstance(accounts, tuple):
                accounts = list(accounts)
            normalized_accounts = []
            for account in accounts or []:
                raw_currency = _get_attr(account, "currency", "")
                currency = _get_attr(raw_currency, "code") or raw_currency
                normalized_accounts.append(
                    {
                        "name": _get_attr(account, "name", str(currency or "")),
                        "currency": str(currency or ""),
                        "available_balance": _plain_sdk_value(
                            _get_attr(account, "available_balance")
                        ),
                    }
                )
            return {
                "accounts": normalized_accounts,
                "payload": _plain_sdk_value(response),
            }
        if operation == "snapshot":
            currencies = [str(value) for value in payload.get("currencies") or []]
            usd_cad_rate = float(payload.get("usd_cad_rate") or 0)
            return {
                "spot_prices": {
                    currency: _get_spot_price_cad(
                        self._client,
                        currency,
                        usd_cad_rate,
                        user_id=self._user_id,
                    )
                    for currency in currencies
                },
                "cost_basis": _get_cost_basis_by_asset(
                    self._client,
                    user_id=self._user_id,
                ),
                "asset_names": {
                    currency: resolved
                    for currency in currencies
                    if (
                        resolved := _get_coinbase_asset_name(
                            self._client,
                            currency,
                            user_id=self._user_id,
                        )
                    )
                },
            }
        if operation == "transactions":
            diagnostics: list[dict] = []
            transactions, succeeded = _parse_transactions(
                self._client,
                sync_window=payload.get("sync_window"),
                user_id=self._user_id,
                record_fetch=lambda **entry: diagnostics.append(_plain_sdk_value(entry)),
            )
            return {
                "transactions": transactions,
                "succeeded": succeeded,
                "diagnostics": diagnostics,
            }
        raise ValueError(f"Unsupported Coinbase SDK operation: {operation}")

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


class CoinbaseSdkWorker:
    def __init__(self, user_id: int, api_key: str, api_secret: str) -> None:
        self._worker = SdkProcessWorker(
            "app.connectors.coinbase:CoinbaseSdkProcessHandler",
            {
                "user_id": user_id,
                "api_key": api_key,
                "api_secret": api_secret,
            },
        )

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def start(self) -> None:
        await self._worker.start()

    async def close(self) -> None:
        await self._worker.close()

    async def call(self, operation: str, payload: dict | None = None, *, timeout: float):
        return await self._worker.call(operation, payload, timeout=timeout)


async def _get_usd_cad_rate(*, user_id: int | None = None) -> float | None:
    from app.services.currency import get_cached_rates_entry, get_fx_rates

    try:
        rates = await get_fx_rates("CAD")
        cached = get_cached_rates_entry("CAD")
        usd_per_cad = float(rates.get("USD") or 0)
        if not cached or time.time() - cached[0] > COINBASE_FX_MAX_AGE_SECONDS:
            raise TimeoutError("CAD FX cache is stale")
        if not math.isfinite(usd_per_cad) or usd_per_cad <= 0:
            raise ValueError("CAD FX response did not include USD")
        return 1.0 / usd_per_cad
    except Exception as e:
        log_connector_event(
            logger,
            provider="coinbase",
            stage="fx rate unavailable",
            level="warning",
            user_id=user_id,
            error=type(e).__name__,
            message=str(e),
        )
        return None


_CRYPTO_STATIC_NAMES: dict[str, str] = {
    "USDC": "USD Coin",
    "USDT": "Tether",
    "DAI": "Dai",
    "CAD": "Canadian Dollar",
    "USD": "US Dollar",
}


def _get_coinbase_asset_name(
    client,
    currency: str,
    *,
    user_id: int | None = None,
) -> str | None:
    static = _CRYPTO_STATIC_NAMES.get(currency)
    if static:
        return static
    for quote in ("USD", "USDC"):
        try:
            product = client.get_product(f"{currency}-{quote}")
            name = (_get_attr(product, "base_name") or "").strip()
            if name:
                return name
        except Exception as e:
            log_connector_event(
                logger,
                provider="coinbase",
                stage="asset name lookup miss",
                user_id=user_id,
                asset=currency,
                quote=quote,
                error=type(e).__name__,
                message=str(e),
                debug=True,
            )
    return None


def _get_spot_price_cad(
    client,
    currency: str,
    usd_cad_rate: float,
    *,
    user_id: int | None = None,
) -> float | None:
    if currency == "CAD":
        return 1.0
    if currency in ("USD", "USDC", "USDT"):
        return usd_cad_rate
    try:
        product = client.get_product(f"{currency}-USD")
        price = _amount_to_float(_get_attr(product, "price"))
        if price is None:
            raise ValueError("Coinbase product response did not include a numeric price")
        return price * usd_cad_rate
    except Exception as e:
        log_connector_event(
            logger,
            provider="coinbase",
            stage="spot price unavailable",
            user_id=user_id,
            asset=currency,
            error=type(e).__name__,
            message=str(e),
            debug=True,
        )
        return None


def _get_cost_basis_by_asset(client, *, user_id: int | None = None) -> dict[str, float]:
    try:
        portfolios_resp = client.get_portfolios()
        portfolios = _get_attr(portfolios_resp, "portfolios", [])
    except Exception as e:
        log_connector_event(
            logger,
            provider="coinbase",
            stage="portfolios failed",
            level="warning",
            user_id=user_id,
            error=type(e).__name__,
            message=str(e),
        )
        return {}

    result: dict[str, float] = {}
    position_count = 0
    for portfolio in portfolios or []:
        uuid = _get_attr(portfolio, "uuid")
        if not uuid:
            continue
        try:
            breakdown_resp = client.get_portfolio_breakdown(uuid, currency="CAD")
            positions = _get_attr(_get_attr(breakdown_resp, "breakdown"), "spot_positions", [])
        except Exception as e:
            log_connector_event(
                logger,
                provider="coinbase",
                stage="portfolio breakdown failed",
                level="warning",
                user_id=user_id,
                portfolio=_coinbase_ref(uuid),
                error=type(e).__name__,
                message=str(e),
            )
            continue

        for pos in positions or []:
            asset = _get_attr(pos, "asset")
            if not asset:
                continue
            position_count += 1
            cost = _amount_to_float(_get_attr(pos, "cost_basis"))
            if cost is None:
                avg = _amount_to_float(_get_attr(pos, "average_entry_price"))
                qty = _amount_to_float(_get_attr(pos, "total_balance_crypto"))
                if avg is not None and qty is not None:
                    cost = avg * qty
            if cost is not None:
                result[asset] = result.get(asset, 0.0) + cost

    log_connector_event(
        logger,
        provider="coinbase",
        stage="cost basis fetched",
        user_id=user_id,
        portfolios=len(portfolios or []),
        positions=position_count,
        assets=len(result),
        debug=True,
    )
    return result


# ---------------------------------------------------------------------------
# Transaction parsing (pure, no DB)
# ---------------------------------------------------------------------------

def _parse_coinbase_datetime(date_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            tx_date = datetime.strptime(date_str, fmt)
            if tx_date.tzinfo is None:
                tx_date = tx_date.replace(tzinfo=timezone.utc)
            else:
                tx_date = tx_date.astimezone(timezone.utc)
            return tx_date
        except (ValueError, TypeError):
            continue
    try:
        tx_date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if tx_date.tzinfo is None:
            return tx_date.replace(tzinfo=timezone.utc)
        return tx_date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_transactions(
    client,
    *,
    sync_window: AccountSyncWindow | None = None,
    user_id: int | None = None,
    record_fetch: Callable[..., None] | None = None,
) -> tuple[list[NormalizedTransaction], bool]:
    """Fetch and parse paginated transaction history for the planned window."""
    transactions: list[NormalizedTransaction] = []
    fetch_succeeded = True
    now = datetime.now(timezone.utc)
    window_start, window_end = (
        sync_window_utc_bounds(sync_window, now=now)
        if sync_window
        else (None, now)
    )

    account_path = "/v2/accounts"
    account_params: dict[str, str] = {"limit": "100"}

    while True:
        try:
            resp = client.get(account_path, params=account_params)
            cb_accounts = resp.get("data", []) if isinstance(resp, dict) else []
            if record_fetch:
                record_fetch(
                    stage="transaction_accounts.page",
                    method="SDK",
                    transport="sdk",
                    url=f"coinbase://{account_path}",
                    params=account_params,
                    payload=resp,
                    records=cb_accounts,
                    record_source="accounts",
                )
        except Exception as e:
            log_connector_event(
                logger,
                provider="coinbase",
                stage="transaction accounts failed",
                level="warning",
                user_id=user_id,
                error=type(e).__name__,
                message=str(e),
            )
            return transactions, False

        for cb_acct in cb_accounts:
            cb_acct_id = cb_acct.get("id")
            if not cb_acct_id:
                continue

            currency_info = cb_acct.get("currency", {})
            wallet_currency = (
                currency_info.get("code", "")
                if isinstance(currency_info, dict)
                else str(currency_info)
            )
            symbol = wallet_currency

            base_path = f"/v2/accounts/{cb_acct_id}/transactions"
            params: dict[str, str] = {"limit": "100", "order": "desc"}
            while True:
                try:
                    tx_resp = client.get(base_path, params=params)
                    raw_transactions = tx_resp.get("data", []) if isinstance(tx_resp, dict) else []
                    if record_fetch:
                        record_fetch(
                            stage="transactions.window",
                            method="SDK",
                            transport="sdk",
                            url=f"coinbase://{base_path}",
                            params=params,
                            payload=tx_resp,
                            records=raw_transactions,
                            record_source="transactions",
                            account_ref=cb_acct_id,
                            window=sync_window.as_dict() if sync_window else {},
                        )
                except Exception as e:
                    fetch_succeeded = False
                    log_connector_event(
                        logger,
                        provider="coinbase",
                        stage="transactions failed",
                        level="warning",
                        user_id=user_id,
                        wallet=symbol,
                        account=_coinbase_ref(cb_acct_id),
                        error=type(e).__name__,
                        message=str(e),
                    )
                    break

                page_dates: list[datetime] = []
                for tx in raw_transactions:
                    tx_date = _parse_coinbase_datetime(_get_attr(tx, "created_at", ""))
                    if tx_date is not None:
                        page_dates.append(tx_date)

                    parsed = _parse_single_transaction(tx, wallet_currency, symbol)
                    if parsed is None:
                        continue
                    if window_start and parsed.date < window_start:
                        continue
                    if parsed.date > window_end:
                        continue
                    transactions.append(parsed)

                if window_start and page_dates and min(page_dates) < window_start:
                    break

                next_uri = tx_resp.get("pagination", {}).get("next_uri")
                if not next_uri:
                    break
                parsed_url = urlparse(next_uri)
                base_path = parsed_url.path
                params = {k: v[0] for k, v in parse_qs(parsed_url.query).items()}

        next_accounts_uri = resp.get("pagination", {}).get("next_uri")
        if not next_accounts_uri:
            break
        parsed_url = urlparse(next_accounts_uri)
        account_path = parsed_url.path
        account_params = {k: v[0] for k, v in parse_qs(parsed_url.query).items()}

    return transactions, fetch_succeeded


def _parse_single_transaction(
    tx: dict,
    wallet_currency: str,
    symbol: str,
) -> NormalizedTransaction | None:
    tx_id = _get_attr(tx, "id", "")
    if not tx_id or _get_attr(tx, "status") != "completed":
        return None

    raw_type = _get_attr(tx, "type", "")
    mapped_type = COINBASE_TX_TYPE_MAP.get(raw_type)
    if mapped_type is None:
        native_amt = _amount_to_float(_get_attr(tx, "native_amount"))
        if native_amt is None:
            native_amt = _amount_to_float(_get_attr(tx, "amount")) or 0.0
        if raw_type == "trade":
            mapped_type = "sell" if native_amt < 0 else "buy"
        elif raw_type == "send":
            mapped_type = "deposit" if native_amt >= 0 else "withdrawal"
        else:
            return None

    # Skip fiat buy/sell legs
    if wallet_currency in FIAT_CURRENCIES and mapped_type in ("buy", "sell"):
        return None

    # Amount
    native_amount = _get_attr(tx, "native_amount", {})
    crypto_amount = _get_attr(tx, "amount", {})
    native_value = _amount_to_float(native_amount)
    crypto_value = _amount_to_float(crypto_amount)
    if native_value is not None:
        amount = native_value
        currency = _get_attr(native_amount, "currency", "CAD")
    elif crypto_value is not None:
        amount = crypto_value
        currency = _get_attr(crypto_amount, "currency", symbol)
    else:
        return None

    # Quantity
    quantity = None
    if crypto_value is not None:
        quantity = abs(crypto_value)

    # Description
    details = _get_attr(tx, "details", {}) or {}
    description = _get_attr(details, "title") or _get_attr(tx, "description") or None
    if mapped_type in ("buy", "sell") and quantity is not None and symbol:
        verb = "Bought" if mapped_type == "buy" else "Sold"
        description = f"{verb} {quantity:g} {symbol}"

    # Date
    date_str = _get_attr(tx, "created_at", "")
    tx_date = _parse_coinbase_datetime(date_str)
    if tx_date is None:
        return None

    # Sign normalization: buys = negative, sells = positive
    if mapped_type == "buy" and amount > 0:
        amount = -amount
    elif mapped_type == "sell" and amount < 0:
        amount = -amount

    return NormalizedTransaction(
        external_id=f"coinbase_{tx_id}",
        date=tx_date,
        type=mapped_type,
        symbol=symbol or None,
        description=description,
        amount=round(amount, 2),
        currency=currency or "CAD",
        quantity=quantity,
    )


# ---------------------------------------------------------------------------
# Connector implementation
# ---------------------------------------------------------------------------

class CoinbaseConnector(ApiProviderConnectorBase):

    def __init__(self) -> None:
        super().__init__(provider="coinbase", display_name="Coinbase")

    def is_auth_required_exception(self, exc: Exception) -> bool:
        return _is_coinbase_auth_error(exc) or super().is_auth_required_exception(exc)

    def _create_sdk_worker(
        self,
        user_id: int,
        api_key: str,
        api_secret: str,
    ) -> CoinbaseSdkWorker:
        return CoinbaseSdkWorker(user_id, api_key, api_secret)

    async def _sync_impl(self, user_id: int) -> SyncResult:
        # 1. Read credentials
        async with async_session() as db:
            settings = await get_api_credentials(
                db,
                user_id,
                "coinbase",
                setting_keys=("coinbase_api_key", "coinbase_api_secret"),
            )

        api_key, api_secret = _get_credentials_from_settings(settings)
        if not api_key or not api_secret:
            return self.missing_credentials_result(
                user_id=user_id,
                message="Coinbase API credentials not configured.",
            )

        worker = self._create_sdk_worker(user_id, api_key, api_secret)
        try:
            return await self._sync_with_sdk_worker(user_id, worker)
        finally:
            await worker.close()

    async def _sync_with_sdk_worker(
        self,
        user_id: int,
        worker: CoinbaseSdkWorker,
    ) -> SyncResult:

        # 2. Fetch accounts from Coinbase API
        try:
            account_result = await worker.call(
                "accounts",
                timeout=COINBASE_BLOCKING_CALL_TIMEOUT_SECONDS,
            )
            accounts_resp = account_result.get("payload")
            accounts_list = account_result.get("accounts")
            if not isinstance(accounts_list, (list, tuple)):
                return self.network_error_result(
                    user_id=user_id,
                    message="Coinbase account data was incomplete. Try syncing again.",
                    stage="accounts response incomplete",
                )
            accounts_list = list(accounts_list)
            self.record_diagnostics_fetch(
                stage="accounts.list",
                method="SDK",
                transport="sdk",
                url="coinbase://get_accounts",
                params={"limit": 250},
                payload=accounts_resp,
                records=accounts_list,
                record_source="accounts",
            )
        except Exception as e:
            if self.exception_is_network_error(e):
                return self.network_error_result(
                    user_id=user_id,
                    message=f"Connection failed - {e}",
                    stage="accounts network failure",
                    exception=e,
                )
            if _is_coinbase_auth_error(e):
                return self.auth_required_result(
                    user_id=user_id,
                    message="Wrong API credentials, please insert correct API Key Name and Private Key",
                    stage="accounts auth failed",
                    exception=e,
                )
            if isinstance(e, SdkProcessCrashed):
                return self.network_error_result(
                    user_id=user_id,
                    message="Coinbase worker stopped unexpectedly. Try syncing again.",
                    stage="accounts worker failure",
                    exception=e,
                )
            return self.error_result(
                user_id=user_id,
                message="Wrong API credentials, please insert correct API Key Name and Private Key",
                stage="accounts auth failed",
                exception=e,
            )
        log_connector_event(
            logger,
            provider=self.provider,
            stage="accounts fetched",
            user_id=user_id,
            accounts=len(accounts_list or []),
            debug=True,
        )

        # 3. Filter active accounts
        active: list[dict] = []
        account_balance_parse_failed = False
        for acct in accounts_list:
            bal = _amount_to_float(_get_attr(acct, "available_balance"))
            if bal is None:
                account_balance_parse_failed = True
                continue
            if bal > 0:
                raw_currency = _get_attr(acct, "currency", "???")
                currency = (
                    raw_currency
                    if isinstance(raw_currency, str)
                    else str(raw_currency)
                )
                active.append({
                    "name": _get_attr(acct, "name", currency),
                    "currency": currency,
                    "balance": bal,
                })
        log_connector_event(
            logger,
            provider=self.provider,
            stage="active accounts filtered",
            user_id=user_id,
            active_accounts=len(active),
            debug=True,
        )

        snapshot_scope = not self.should_persist_transaction_windows()
        holdings: list[NormalizedHolding] = []
        total_cad: float | None = None
        snapshot_authoritative = False
        if snapshot_scope:
            if account_balance_parse_failed:
                return self.network_error_result(
                    user_id=user_id,
                    message="Coinbase account balances were incomplete. Try syncing again.",
                    stage="account balance parse failed",
                )

            spot_prices: dict[str, float] = {}
            cost_basis: dict[str, float] = {}
            asset_names: dict[str, str] = {}
            if active:
                usd_cad = 1.0
                if any(asset["currency"] != "CAD" for asset in active):
                    usd_cad = await _get_usd_cad_rate(user_id=user_id)
                    if usd_cad is None:
                        return self.network_error_result(
                            user_id=user_id,
                            message="Coinbase valuation rates were unavailable. Try syncing again.",
                            stage="fx rate unavailable",
                        )
                currencies = sorted({asset["currency"] for asset in active})
                snapshot = await worker.call(
                    "snapshot",
                    {"currencies": currencies, "usd_cad_rate": usd_cad},
                    timeout=COINBASE_BLOCKING_CALL_TIMEOUT_SECONDS,
                )
                spot_prices = dict(snapshot.get("spot_prices") or {})
                cost_basis = dict(snapshot.get("cost_basis") or {})
                resolved_names = dict(snapshot.get("asset_names") or {})
                for currency in currencies:
                    price = spot_prices.get(currency)
                    if price is None or price <= 0:
                        return self.network_error_result(
                            user_id=user_id,
                            message=f"Coinbase could not value {currency}. Try syncing again.",
                            stage="spot price unavailable",
                            asset=currency,
                        )
                from app.services.symbol_names import (
                    get_cached_symbol_names,
                    upsert_symbol_names,
                )

                cached_names = await get_cached_symbol_names("coinbase", currencies)
                asset_names = dict(cached_names)
                fresh_names = {
                    currency: resolved
                    for currency, resolved in resolved_names.items()
                    if currency not in asset_names and resolved
                }
                asset_names.update(fresh_names)
                if fresh_names:
                    await upsert_symbol_names("coinbase", fresh_names)

            total_cad = 0.0
            for asset in active:
                currency = asset["currency"]
                quantity = asset["balance"]
                market_value = quantity * spot_prices[currency]
                holdings.append(NormalizedHolding(
                    symbol=currency,
                    name=asset_names.get(currency) or asset["name"],
                    quantity=quantity,
                    market_value=round(market_value, 2),
                    average_cost=round(cost_basis[currency], 2) if currency in cost_basis else None,
                    currency="CAD",
                ))
                total_cad += market_value
            snapshot_authoritative = True

        account_name = "Coinbase"
        norm_account = NormalizedAccount(
            name=account_name,
            account_type="crypto",
            external_id="portfolio:primary",
            currency="CAD",
            balance=round(total_cad, 2) if total_cad is not None else None,
            balance_authoritative=snapshot_authoritative,
            holdings_authoritative=snapshot_authoritative,
        )
        account_key = account_result_key(norm_account)

        # 6. Transactions (best-effort)
        transactions: list[NormalizedTransaction] = []
        transaction_fetch_succeeded = False
        window_log: dict | None = None
        if self.should_fetch_transactions():
            try:
                if self.should_persist_transaction_windows():
                    async def fetch_window(sync_window: AccountSyncWindow):
                        result = await worker.call(
                            "transactions",
                            {"sync_window": sync_window},
                            timeout=COINBASE_TRANSACTION_CALL_TIMEOUT_SECONDS,
                        )
                        for entry in result.get("diagnostics") or []:
                            self.record_diagnostics_fetch(**entry)
                        return (
                            list(result.get("transactions") or []),
                            bool(result.get("succeeded")),
                        )

                    transactions, transaction_fetch_succeeded, window_log = (
                        await self.fetch_and_persist_transaction_import_windows(
                            user_id=user_id,
                            account_external_id=norm_account.external_id or "",
                            account_name=norm_account.name,
                            account_type=norm_account.account_type,
                            is_liability=norm_account.is_liability,
                            fetch_window=fetch_window,
                            backfill_days=COINBASE_MAX_HISTORY_DAYS,
                            backfill_chunk_days=COINBASE_TRANSACTION_CHUNK_DAYS,
                            failure_message="Coinbase transaction history window did not finish.",
                        )
                    )
                else:
                    sync_window = await self._transaction_sync_window(user_id, norm_account.external_id)
                    window_log = sync_window.as_dict()
                    result = await worker.call(
                        "transactions",
                        {"sync_window": sync_window},
                        timeout=COINBASE_TRANSACTION_CALL_TIMEOUT_SECONDS,
                    )
                    for entry in result.get("diagnostics") or []:
                        self.record_diagnostics_fetch(**entry)
                    transactions = list(result.get("transactions") or [])
                    transaction_fetch_succeeded = bool(result.get("succeeded"))
            except (SdkProcessError, TimeoutError):
                raise
            except Exception as e:
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="transactions parse failed",
                    level="warning",
                    user_id=user_id,
                    error=type(e).__name__,
                    message=str(e),
                )
            if self.should_persist_transaction_windows() and not transaction_fetch_succeeded:
                return SyncResult(
                    status=SyncStatus.NETWORK_ERROR,
                    message="Coinbase transaction history did not finish. Try syncing again.",
                )

        log_connector_event(
            logger,
            provider=self.provider,
            stage="sync result",
            user_id=user_id,
            accounts=1,
            mode=window_log.get("mode") if isinstance(window_log, dict) else None,
            window=window_log,
            holdings=len(holdings),
            transactions=len(transactions),
            transactions_fetched=transaction_fetch_succeeded,
            debug=True,
        )

        return SyncResult(
            status=SyncStatus.OK,
            accounts=[norm_account],
            holdings=holdings,
            holdings_by_account={account_key: holdings},
            transactions=transactions,
            transactions_by_account={account_key: transactions},
            transaction_fetch_succeeded_accounts=(
                {account_key} if transaction_fetch_succeeded else set()
            ),
        )
