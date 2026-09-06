"""Per-holding financial metrics (native/quote currency).

Ported 1:1 from the frontend Holdings table math (pages/Holdings.js) so CSV
exports show the same values as the on-screen positions table.

Important: BreakTwenty stores ``Holding.average_cost`` as the TOTAL quoted cost basis
(book value) for the position, NOT a per-unit price. Therefore:
- per-unit average cost = average_cost / |quantity|
- money cost basis     = average_cost (x contract multiplier for options),
                         signed by the position direction
- last price (when the provider doesn't supply one) = |market_value| /
                         (|quantity| x multiplier-for-options)
- unrealized P&L %      = (market_value - cost_basis) / |cost_basis| x 100
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Frontend CASH_SYMBOLS (Holdings.js)
CASH_SYMBOLS = frozenset({
    "CAD", "USD", "EUR", "JPY", "GBP", "CHF", "AUD",
    "HKD", "NZD", "SEK", "NOK", "DKK", "SGD", "CNH",
})

# Frontend option-detection patterns (Holdings.js). The text pattern catches the
# common CALL/PUT/FOP descriptors; the rest cover compact OCC / IBKR symbols.
_OPTION_TEXT = re.compile(r"\b(CALL|PUT|FOP)\b", re.IGNORECASE)
_OCC_OPTION = re.compile(r"^[A-Z.\-]{1,6}\d{6}[CP]\d{5,8}$", re.IGNORECASE)
_IBKR_OPTION_SYMBOL = re.compile(r"^[A-Z.\-]{1,10}\s+\d{6}[CP]\d{5,8}$", re.IGNORECASE)
_IBKR_FUTURES_OPTION_SYMBOL = re.compile(r"^[A-Z0-9]{2,6}\s+[CP]\d{3,6}(?:\.\d+)?$", re.IGNORECASE)
_IBKR_OPTION_NAME = re.compile(r"\b\d{1,2}[A-Z]{3}\d{2}\b.*\b[CP]\b", re.IGNORECASE)
_FOP = re.compile(r"\bFOP\b", re.IGNORECASE)
_WS = re.compile(r"\s+")

# Crypto-wallet detection (frontend Holdings.js: isCryptoWalletHolding).
_CRYPTO_WALLET_SYMBOLS = frozenset({"BTC", "ETH", "SOL", "USDC"})
_WALLET = re.compile(r"\bWALLET\b", re.IGNORECASE)


def is_crypto_wallet(symbol, name, account_type) -> bool:
    if (account_type or "").strip().lower() == "crypto":
        return True
    lookup = (symbol or "").strip().upper().split(" @", 1)[0].strip()
    descriptor = f"{lookup} {(name or '').upper()}"
    return lookup in _CRYPTO_WALLET_SYMBOLS or bool(_WALLET.search(descriptor))


def export_category(symbol, name, account_type) -> str:
    """Bucket a holding into its Investments sub-tab: cash | option | crypto | spot.

    Mirrors the frontend tab split: options by descriptor, crypto by wallet
    detection, everything else spot (the Holdings tab). Cash is its own bucket.
    """
    category = holding_category(symbol, name)
    if category == "spot" and is_crypto_wallet(symbol, name, account_type):
        return "crypto"
    return category


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _is_cash_symbol(symbol) -> bool:
    return (symbol or "").strip().upper() in CASH_SYMBOLS


def _is_option(symbol, name) -> bool:
    symbol = (symbol or "").strip()
    name = (name or "").strip()
    descriptor = _WS.sub(" ", f"{symbol} {name}").strip()
    collapsed = _WS.sub(" ", symbol).strip()
    return bool(
        _OPTION_TEXT.search(descriptor)
        or _OCC_OPTION.match(_WS.sub("", symbol))
        or _IBKR_OPTION_SYMBOL.match(collapsed)
        or _IBKR_FUTURES_OPTION_SYMBOL.match(collapsed)
        or _IBKR_OPTION_NAME.search(name)
    )


def holding_category(symbol, name) -> str:
    if _is_cash_symbol(symbol):
        return "cash"
    if _is_option(symbol, name):
        return "option"
    return "spot"


def _is_future(symbol, name, raw_multiplier) -> bool:
    # A futures contract: multiplier > 1 but not an option/cash. Its market_value AND
    # average_cost are SIGNED total notionals (mirrors isFutureHolding in pages/Holdings.js),
    # so the cost basis must be used as-is rather than re-signed by position direction.
    if _is_cash_symbol(symbol) or _is_option(symbol, name):
        return False
    value = _num(raw_multiplier)
    return value is not None and value > 1


def contract_multiplier(raw_multiplier, category, symbol, name) -> float:
    value = _num(raw_multiplier)
    if value is not None and value > 0:
        return value
    if category != "option":
        return 1.0
    descriptor = f"{symbol or ''} {name or ''}".upper()
    return 1000.0 if _FOP.search(descriptor) else 100.0


@dataclass(frozen=True)
class HoldingMetrics:
    category: str
    multiplier: float
    quantity: float
    market_value: float | None
    cost_basis: float | None            # money cost basis (multiplier-applied, signed)
    average_cost_per_unit: float | None
    last_price: float | None
    unrealized_pnl_pct: float | None


def compute_holding_metrics(
    *,
    symbol,
    name,
    quantity,
    market_value,
    average_cost,
    last_price,
    raw_multiplier,
) -> HoldingMetrics:
    category = holding_category(symbol, name)
    qty = _num(quantity) or 0.0
    abs_qty = abs(qty)
    market = _num(market_value)
    quoted_cost = _num(average_cost)  # total quoted cost basis (book value)
    multiplier = contract_multiplier(raw_multiplier, category, symbol, name)

    if category == "cash":
        # Cash positions carry value only — no cost basis / P&L / per-unit price.
        return HoldingMetrics(category, multiplier, qty, market, None, None, None, None)

    if quoted_cost is None:
        cost_basis = None
    elif _is_future(symbol, name, raw_multiplier):
        # Futures store a SIGNED total notional in average_cost — use it as-is so a short
        # reads negative; re-applying the position sign would double-flip it (see Holdings.js).
        cost_basis = quoted_cost
    else:
        base = quoted_cost * multiplier if category == "option" else quoted_cost
        cost_basis = base * (-1.0 if qty < 0 else 1.0)

    average_cost_per_unit = (quoted_cost / abs_qty) if (quoted_cost is not None and abs_qty) else None

    effective_last_price = _num(last_price)
    if effective_last_price is None and market is not None and abs_qty:
        denominator = abs_qty * multiplier if category == "option" else abs_qty
        effective_last_price = abs(market) / denominator if denominator > 0 else None

    if cost_basis not in (None, 0) and market is not None:
        unrealized_pnl_pct = (market - cost_basis) / abs(cost_basis) * 100
    else:
        unrealized_pnl_pct = None

    return HoldingMetrics(
        category=category,
        multiplier=multiplier,
        quantity=qty,
        market_value=market,
        cost_basis=cost_basis,
        average_cost_per_unit=average_cost_per_unit,
        last_price=effective_last_price,
        unrealized_pnl_pct=unrealized_pnl_pct,
    )
