"""Normalized data types returned by all connectors.

These dataclasses are decoupled from SQLAlchemy models.  The persistence
layer converts them into ORM objects so that every connector — whether a
scraper, a direct API, or a future Flinks/Plaid integration — shares the
same DB-write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SyncStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    AUTH_REQUIRED = "auth_required"
    TWO_FA_REQUIRED = "2fa_required"
    WAITING = "waiting"
    NETWORK_ERROR = "network_error"
    SKIPPED = "skipped"
    ALREADY_SYNCING = "already_syncing"


@dataclass
class NormalizedAccount:
    name: str
    account_type: str  # "crypto", "tfsa", "rrsp", "chequing", "credit_card", etc.
    external_id: Optional[str] = None
    currency: str = "CAD"
    is_liability: bool = False
    balance: Optional[float] = None
    # Snapshot replacement is opt-in. A failed/omitted provider field must leave
    # the previously persisted value intact; an authoritative zero/empty result
    # explicitly replaces it.
    balance_authoritative: bool = False
    holdings_authoritative: bool = False
    alternate_external_ids: tuple[str, ...] = ()
    # Optional pre-sync daily balance series — ``(datetime, balance)`` points upserted one
    # per local day into balance_history, backfilling history the single ``balance`` snapshot
    # can't (e.g. IBKR Flex daily NAV). Empty for connectors that only report a current balance.
    balance_history: tuple = ()


def account_result_key(account: NormalizedAccount) -> str:
    return account.external_id or account.name


@dataclass
class NormalizedHolding:
    symbol: str
    name: Optional[str] = None
    quantity: float = 0.0
    market_value: Optional[float] = None
    average_cost: Optional[float] = None
    last_price: Optional[float] = None
    contract_multiplier: Optional[float] = None
    change_pct: Optional[float] = None
    daily_pnl: Optional[float] = None
    currency: str = "CAD"


@dataclass
class NormalizedTransaction:
    external_id: str
    date: datetime
    type: str  # "buy", "sell", "deposit", "withdrawal", "dividend", "interest", etc.
    amount: float
    currency: str = "CAD"
    symbol: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    commission: Optional[float] = None
    mcc: Optional[str] = None
    asset_category: Optional[str] = None  # provider asset class (e.g. "FUT") when known
    realized_pnl: Optional[float] = None  # broker realized P&L on the trade (e.g. IBKR fifoPnlRealized)
    provider_recurring: Optional[bool] = None


@dataclass
class SyncResult:
    """Standardised result shape returned by every connector's sync()."""
    status: SyncStatus
    message: Optional[str] = None
    accounts: list[NormalizedAccount] = field(default_factory=list)
    holdings: list[NormalizedHolding] = field(default_factory=list)
    transactions: list[NormalizedTransaction] = field(default_factory=list)

    # Maps NormalizedAccount → list of holdings/transactions for that account.
    # Keyed by account_result_key(account): external_id when available,
    # otherwise the display name as a fallback.
    holdings_by_account: dict[str, list[NormalizedHolding]] = field(default_factory=dict)
    transactions_by_account: dict[str, list[NormalizedTransaction]] = field(default_factory=dict)
    transaction_fetch_succeeded_accounts: set[str] = field(default_factory=set)
    challenge_method: Optional[str] = None
    transaction_import_deferred: bool = False

    def to_route_response(self) -> dict:
        """Convert to the dict shape routes currently return to the frontend."""
        d: dict = {"status": self.status.value}
        if self.message:
            from app.connectors.connector_logging import safe_connector_public_message

            d["message"] = safe_connector_public_message(self.message)
        if self.transaction_import_deferred:
            d["transaction_import_deferred"] = True
        if self.status == SyncStatus.TWO_FA_REQUIRED and self.challenge_method:
            d["method"] = self.challenge_method
        if self.status == SyncStatus.OK and self.accounts:
            d["synced"] = [
                {
                    "account": a.name,
                    "type": a.account_type,
                    "balance": a.balance,
                    "holdings": len(
                        self.holdings_by_account.get(account_result_key(a), self.holdings_by_account.get(a.name, []))
                    ),
                }
                for a in self.accounts
            ]
        return d
