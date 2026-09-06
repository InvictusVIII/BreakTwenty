from __future__ import annotations

import math
from typing import Any

from app.connectors.desktop_visible_auth import DesktopVisibleAuthSavedSessionConnector
from app.connectors.ibkr_helpers import map_ibkr_account_type
from app.connectors.types import (
    NormalizedAccount,
    NormalizedHolding,
    SyncResult,
    SyncStatus,
    account_result_key,
)


def _ibkr_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _ibkr_asset_class(pos: dict) -> str:
    return str(pos.get("assetClass") or pos.get("secType") or "").upper()


def _ibkr_stk_name(pos: dict) -> str | None:
    ticker = str(pos.get("ticker") or "").strip().upper()
    for key in ("fullName", "companyName", "name", "contractDesc"):
        val = str(pos.get(key) or "").strip()
        if val and val.upper() != ticker:
            return val
    return None


def _ibkr_format_expiry(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return f"{text[4:6]}/{text[6:8]}/{text[2:4]}"
    return text


def _ibkr_option_side_word(pos: dict) -> str | None:
    raw = str(pos.get("putOrCall") or pos.get("right") or "").strip().upper()
    if raw.startswith("C"):
        return "CALL"
    if raw.startswith("P"):
        return "PUT"
    return None


def _ibkr_format_strike(value: Any) -> str | None:
    try:
        return f"${float(value):g}"
    except (TypeError, ValueError):
        return None


def _ibkr_option_name(pos: dict, underlying_name: str | None) -> str | None:
    side = _ibkr_option_side_word(pos)
    strike = _ibkr_format_strike(pos.get("strike"))
    expiry = _ibkr_format_expiry(pos.get("expiry") or pos.get("expiryDate") or pos.get("lastTradingDay"))
    label = underlying_name or str(pos.get("undSym") or pos.get("underlyingSymbol") or "").strip() or None
    if not (side and strike and expiry and label):
        return None
    return f"{side} {label} {strike} EXP {expiry}"


class IBKRScraperConnector(DesktopVisibleAuthSavedSessionConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="ibkr",
            module_path="app.scrapers.ibkr",
            display_name="Interactive Brokers",
        )

    def _build_success_result(self, accounts_data: list[dict]) -> SyncResult:
        accounts: list[NormalizedAccount] = []
        holdings_by_account: dict[str, list[NormalizedHolding]] = {}
        all_holdings: list[NormalizedHolding] = []

        for acct_data in accounts_data:
            if not isinstance(acct_data, dict):
                continue
            acct_id = str(acct_data.get("acct_id") or "").strip()
            name = str(acct_data.get("name") or f"IBKR {acct_id or 'Account'}").strip()
            cust_type = acct_data.get("cust_type", "")
            acct_type = map_ibkr_account_type(cust_type, acct_data.get("type", ""))
            account_currency = acct_data.get("currency", "CAD")
            balance = _ibkr_float(acct_data.get("balance"), default=float("nan"))
            balance_authoritative = math.isfinite(balance)

            account = NormalizedAccount(
                name=name,
                account_type=acct_type,
                external_id=f"account:{acct_id}" if acct_id else None,
                currency=account_currency,
                is_liability=False,
                balance=balance if balance_authoritative else None,
                balance_authoritative=balance_authoritative,
                holdings_authoritative=False,
            )
            accounts.append(account)
            account_key = account_result_key(account)

            account_holdings: list[NormalizedHolding] = []

            raw_positions_value = acct_data.get("positions")
            raw_ledger_value = acct_data.get("ledger_by_currency")
            holdings_authoritative = isinstance(raw_positions_value, list) and isinstance(
                raw_ledger_value,
                dict,
            )
            raw_positions = [
                position for position in (raw_positions_value or []) if isinstance(position, dict)
            ]
            if isinstance(raw_positions_value, list) and len(raw_positions) != len(raw_positions_value):
                holdings_authoritative = False
            name_by_conid: dict[int, str] = {}
            for raw_conid, raw_name in (acct_data.get("name_by_conid") or {}).items():
                try:
                    name_by_conid[int(raw_conid)] = str(raw_name)
                except (TypeError, ValueError):
                    continue

            def _enriched_stk_name(pos: dict) -> str | None:
                try:
                    conid_int = int(pos.get("conid"))
                except (TypeError, ValueError):
                    return None
                return name_by_conid.get(conid_int)

            underlying_names: dict[str, str] = {}
            for pos in raw_positions:
                if _ibkr_asset_class(pos) != "STK":
                    continue
                ticker = str(pos.get("ticker") or "").strip().upper()
                stk_name = _ibkr_stk_name(pos)
                if ticker and stk_name:
                    underlying_names[ticker] = stk_name

            for pos in raw_positions:
                qty = _ibkr_float(pos.get("position"), default=float("nan"))
                if not math.isfinite(qty):
                    holdings_authoritative = False
                    continue
                if qty == 0:
                    continue
                symbol = (
                    pos.get("contractDesc")
                    or pos.get("description")
                    or pos.get("ticker")
                    or "???"
                )
                market_value = _ibkr_float(
                    pos.get("mktValue", pos.get("marketValue")),
                    default=float("nan"),
                )
                if not math.isfinite(market_value):
                    holdings_authoritative = False
                    continue
                avg_cost = _ibkr_float(pos.get("avgCost"))
                asset_class = _ibkr_asset_class(pos)

                if asset_class in ("OPT", "FOP"):
                    underlying_key = str(pos.get("undSym") or pos.get("underlyingSymbol") or "").strip().upper()
                    underlying_name = underlying_names.get(underlying_key)
                    display_name = _ibkr_option_name(pos, underlying_name) or str(symbol)
                else:
                    display_name = _enriched_stk_name(pos) or _ibkr_stk_name(pos) or str(symbol)

                account_holdings.append(
                    NormalizedHolding(
                        symbol=str(symbol),
                        name=display_name,
                        quantity=qty,
                        market_value=market_value,
                        average_cost=avg_cost * abs(qty) if avg_cost else None,
                        currency=pos.get("currency", "USD"),
                    )
                )

            ledger = raw_ledger_value or {}
            for curr_key, entry in ledger.items():
                if curr_key == "BASE":
                    continue
                if not isinstance(entry, dict):
                    holdings_authoritative = False
                    continue
                cash = _ibkr_float(entry.get("cashbalance"), default=float("nan"))
                if not math.isfinite(cash):
                    holdings_authoritative = False
                    continue
                if cash == 0:
                    continue
                account_holdings.append(
                    NormalizedHolding(
                        symbol=curr_key,
                        name=f"{curr_key} Cash",
                        quantity=1,
                        market_value=cash,
                        currency=curr_key,
                    )
                )

            account.holdings_authoritative = holdings_authoritative
            holdings_by_account[account_key] = account_holdings
            all_holdings.extend(account_holdings)

        return SyncResult(
            status=SyncStatus.OK,
            accounts=accounts,
            holdings=all_holdings,
            holdings_by_account=holdings_by_account,
        )
