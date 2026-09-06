from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.connectors.payload_utils import first_present_value as _first_tangerine_value
from app.database import async_session
from app.scrapers.tangerine import TANGERINE_BACKFILL_CHUNK_DAYS, TANGERINE_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _classify_tangerine_account(name: str, description: str = "") -> tuple[str, bool]:
    lower = f"{name} {description}".lower()
    if "chequing" in lower:
        return "chequing", False
    if "saving" in lower:
        return "savings", False
    if "mastercard" in lower or "visa" in lower or "credit card" in lower:
        return "credit_card", True
    if "line of credit" in lower:
        return "loc", True
    if "tfsa" in lower or "tax free" in lower:
        return "tfsa", False
    if "rrsp" in lower or "rsp" in lower:
        return "rrsp", False
    if "resp" in lower:
        return "resp", False
    if "gic" in lower:
        return "savings", False
    return "other", False


def _raw_tangerine_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    if text.startswith("suffix:"):
        return None
    text = unquote(text)
    return text or None


def _tangerine_stable_account_external_id(provider_account_id: str) -> str:
    return f"account:{provider_account_id}"


def _looks_like_tangerine_provider_account_id(value: Any) -> bool:
    text = _raw_tangerine_account_id(value)
    if not text or re.fullmatch(r"\d{4}", text):
        return False
    return text.lower() not in {"account", "accounts", "product", "products", "summary", "details", "overview"}


def _tangerine_account_external_id(acct_data: dict) -> str | None:
    provider_account_id = (
        acct_data.get("provider_account_id")
        or acct_data.get("api_account_id")
        or acct_data.get("account_id")
        or _raw_tangerine_account_id(acct_data.get("external_id"))
    )
    if provider_account_id:
        raw_provider_account_id = _raw_tangerine_account_id(provider_account_id)
        if _looks_like_tangerine_provider_account_id(raw_provider_account_id):
            return _tangerine_stable_account_external_id(raw_provider_account_id)
    return acct_data.get("external_id")


def _tangerine_alternate_external_ids(acct_data: dict, external_id: str | None) -> tuple[str, ...]:
    values = [
        acct_data.get("alternate_external_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _parse_tangerine_date(val: Any) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _parse_tangerine_amount(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match:
        text = match.group(0)
    try:
        amount = float(text or 0)
    except ValueError:
        return default
    amount = -amount if negative else amount
    return amount if math.isfinite(amount) else default


def _money_value(raw_value: Any) -> tuple[float, str]:
    if isinstance(raw_value, dict):
        value = _first_tangerine_value(raw_value, "amount", "value", "raw")
        currency = _first_tangerine_value(raw_value, "currency_code", "currencyCode", "currency") or "CAD"
        return _parse_tangerine_amount(value), currency
    return _parse_tangerine_amount(raw_value), "CAD"


def _is_tangerine_posted(raw: dict) -> bool:
    for key in ("pending", "is_pending", "isPending", "authorized", "is_authorized", "isAuthorized"):
        if raw.get(key) is True:
            return False

    status_parts = [
        raw.get("status"),
        raw.get("transaction_status"),
        raw.get("transactionStatus"),
        raw.get("authorization_status"),
        raw.get("authorizationStatus"),
        raw.get("authorized_status"),
        raw.get("authorizedStatus"),
        raw.get("booking_status"),
        raw.get("bookingStatus"),
    ]
    status = " ".join(str(part).lower() for part in status_parts if part)
    if not status:
        return True
    if any(token in status for token in ("pending", "authorized", "authorization")):
        return False
    return any(token in status for token in ("posted", "settled", "booked", "cleared", "completed"))


def _tangerine_description(raw: dict) -> str | None:
    value = (
        raw.get("description")
        or raw.get("display_description")
        or raw.get("displayDescription")
        or raw.get("merchant_name")
        or raw.get("merchantName")
        or raw.get("name")
        or raw.get("memo")
    )
    if isinstance(value, list):
        value = " ".join(str(part) for part in value if part)
    if isinstance(value, dict):
        value = value.get("text") or value.get("value") or value.get("description")
    return str(value).strip() if value else None


def _classify_tangerine_transaction(raw: dict, amount: float) -> str:
    desc = (_tangerine_description(raw) or "").lower()
    raw_type = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("type", "transaction_type", "transactionType", "category", "category_name", "categoryName")
    )
    text = f"{desc} {raw_type}"

    if "interest" in text:
        return "interest"
    if any(token in text for token in ("fee", "service charge", "monthly charge")):
        return "fee"
    if any(token in text for token in ("transfer", "e-transfer", "interac")):
        return "transfer"
    if "payment" in text:
        return "deposit" if amount > 0 else "withdrawal"
    if any(token in text for token in ("deposit", "payroll")):
        return "deposit"
    return "deposit" if amount >= 0 else "withdrawal"


def _normalize_tangerine_transaction(raw: dict) -> NormalizedTransaction | None:
    if not _is_tangerine_posted(raw):
        return None

    txn_id = raw.get("id")
    if not txn_id:
        return None

    date = _parse_tangerine_date(
        _first_tangerine_value(
            raw,
            "posted_date",
            "postedDate",
            "booking_date",
            "bookingDate",
            "transaction_date",
            "transactionDate",
            "date",
            "effective_date",
            "effectiveDate",
        )
    )
    if date is None:
        return None

    amount_source = _first_tangerine_value(raw, "amount", "transaction_amount", "transactionAmount")
    if amount_source is None:
        amount_source = _first_tangerine_value(raw, "debit_amount", "debitAmount", "credit_amount", "creditAmount")
    amount, amount_currency = _money_value(amount_source)
    currency = _first_tangerine_value(raw, "currency_code", "currencyCode", "currency", "amount_currency") or amount_currency

    indicator = (
        _first_tangerine_value(
            raw,
            "credit_debit_indicator",
            "creditDebitIndicator",
            "debit_credit_indicator",
            "debitCreditIndicator",
            "direction",
            "transaction_type",
            "transactionType",
        )
        or ""
    ).upper()
    if not indicator and _first_tangerine_value(raw, "debit_amount", "debitAmount") is not None and amount > 0:
        amount = -amount
    if indicator in ("DEBIT", "D", "WITHDRAWAL", "OUTFLOW") and amount > 0:
        amount = -amount
    if indicator in ("CREDIT", "C", "DEPOSIT", "INFLOW") and amount < 0:
        amount = abs(amount)

    description = _tangerine_description(raw)
    return NormalizedTransaction(
        external_id=f"tangerine_{txn_id}",
        date=date,
        type=_classify_tangerine_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class TangerineConnector(DesktopVisibleAuthTransactionImportConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="tangerine",
            module_path="app.scrapers.tangerine",
            display_name="Tangerine",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict]) -> dict[str, dict[str, str | int | None]]:
            modes: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for acct in accounts:
                    ext = _tangerine_account_external_id(acct)
                    if not ext:
                        continue
                    alternate_external_ids = _tangerine_alternate_external_ids(acct, ext)
                    account_type, is_liability = _classify_tangerine_account(
                        str(acct.get("name") or acct.get("description") or "Tangerine Account"),
                        str(acct.get("description") or ""),
                    )
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=ext,
                        account_name=str(
                            acct.get("name")
                            or acct.get("description")
                            or "Tangerine Account"
                        ),
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=TANGERINE_MAX_HISTORY_DAYS,
                        backfill_chunk_days=TANGERINE_BACKFILL_CHUNK_DAYS,
                        incremental_days=DEFAULT_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                    )
                    mode = plan.as_sync_state()
                    modes[ext] = mode
                    for alternate_external_id in alternate_external_ids:
                        modes[alternate_external_id] = mode
            return modes
        return resolver

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict[str, Any]],
    ) -> list[NormalizedTransaction]:
        del account_payload
        return self._normalize_transaction_rows(raw_transactions, _normalize_tangerine_transaction)

    def _build_from_payload(self, payload: dict) -> SyncResult:
        accounts_data: list[dict] = payload.get("accounts") or []
        transactions_data: dict[str, list[dict]] = payload.get("transactions") or {}
        transaction_fetch_succeeded_external_ids = set(
            payload.get("transaction_fetch_succeeded_accounts") or []
        )

        if not accounts_data:
            return SyncResult(status=SyncStatus.ERROR, message="No accounts returned")

        accounts: list[NormalizedAccount] = []
        account_by_external_id: dict[str, NormalizedAccount] = {}

        for acct_data in accounts_data:
            if not isinstance(acct_data, dict):
                continue
            name = str(acct_data.get("name") or acct_data.get("description") or "Tangerine Account")
            description = acct_data.get("description", "")
            acct_type, is_liability = _classify_tangerine_account(name, description)
            external_id = _tangerine_account_external_id(acct_data)
            alternate_external_ids = _tangerine_alternate_external_ids(acct_data, external_id)
            currency = acct_data.get("currency") or "CAD"
            raw_balance = _parse_tangerine_amount(
                acct_data.get("balance"),
                default=float("nan"),
            )
            balance_authoritative = (
                acct_data.get("balance") is not None and math.isfinite(raw_balance)
            )

            account = NormalizedAccount(
                name=name,
                account_type=acct_type,
                external_id=external_id,
                currency=currency,
                is_liability=is_liability,
                balance=float(abs(raw_balance)) if balance_authoritative else None,
                balance_authoritative=balance_authoritative,
                holdings_authoritative=True,
                alternate_external_ids=alternate_external_ids,
            )
            accounts.append(account)
            if external_id:
                account_by_external_id[external_id] = account
            for alternate_external_id in alternate_external_ids:
                account_by_external_id[alternate_external_id] = account

        if not accounts:
            return SyncResult(status=SyncStatus.ERROR, message="No accounts returned")

        return self._build_result_from_accounts_and_transactions(
            accounts=accounts,
            account_by_external_id=account_by_external_id,
            transactions_data=transactions_data,
            transaction_fetch_succeeded_external_ids=transaction_fetch_succeeded_external_ids,
            transaction_normalizer=lambda raw, _external_id, _account: _normalize_tangerine_transaction(raw),
        )
