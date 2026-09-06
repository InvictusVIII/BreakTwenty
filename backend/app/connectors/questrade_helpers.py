from __future__ import annotations


QUESTRADE_ACTIVITY_TYPE_MAP = {
    "Dividends": "dividend",
    "Buy": "buy",
    "Sell": "sell",
    "Deposits": "deposit",
    "Withdrawals": "withdrawal",
    "Fees and rebates": "fee",
    "Interest": "interest",
    "Transfers": "transfer",
    "FX conversion": "transfer",
    "Other": "other",
}


def map_questrade_account_type(qt_type: str) -> str:
    qt = qt_type.upper()
    if "TFSA" in qt:
        return "tfsa"
    if "RRSP" in qt or "LRSP" in qt:
        return "rrsp"
    if "FHSA" in qt:
        return "fhsa"
    if "RESP" in qt:
        return "resp"
    if "LIRA" in qt or "LIF" in qt:
        return "lira"
    if "MARGIN" in qt or "INDIVIDUAL" in qt:
        return "margin"
    return "other"


def map_questrade_activity_type(raw_type: str, action: str) -> str | None:
    action_up = (action or "").strip().upper()
    raw = (raw_type or "").strip()
    if raw == "Trades":
        if action_up == "BUY":
            return "buy"
        if action_up == "SELL":
            return "sell"
        return None
    if action_up == "BUY" and raw in ("", "Buy"):
        return "buy"
    if action_up == "SELL" and raw in ("", "Sell"):
        return "sell"
    return QUESTRADE_ACTIVITY_TYPE_MAP.get(raw)
