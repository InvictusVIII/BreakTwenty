from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.sync_context import get_connector_sync_context
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.database import async_session
from app.models import TransactionImportAccountState
from app.scrapers.amex import AMEX_BACKFILL_CHUNK_DAYS, AMEX_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _nested_value(raw: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = raw
        matched = True
        for key in path:
            if not isinstance(value, dict) or value.get(key) is None:
                matched = False
                break
            value = value.get(key)
        if matched:
            return value
    return None


def _raw_amex_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    if text.startswith("suffix:"):
        return None
    return text or None


def _amex_stable_account_external_id(provider_account_id: str) -> str:
    return f"account:{provider_account_id}"


def _amex_account_external_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = (
        acct_data.get("provider_account_id")
        or acct_data.get("acc_token")
        or _nested_value(acct_data, ("extendedTransactionDetails", "accToken"))
        or _raw_amex_account_id(acct_data.get("external_id"))
    )
    raw_provider_account_id = _raw_amex_account_id(provider_account_id)
    if raw_provider_account_id:
        return _amex_stable_account_external_id(raw_provider_account_id)
    return acct_data.get("external_id")


def _amex_alternate_external_ids(acct_data: dict[str, Any], external_id: str | None) -> tuple[str, ...]:
    last5 = str(acct_data.get("last5") or "").strip()
    alternate_suffixes = []
    if acct_data.get("last4"):
        alternate_suffixes.append(f"suffix:{acct_data.get('last4')}")
    if len(last5) >= 4:
        alternate_suffixes.append(f"suffix:{last5[-4:]}")
        alternate_suffixes.append(f"suffix:{last5[:4]}")
    values = [
        acct_data.get("alternate_external_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
        *alternate_suffixes,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _amex_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y", "pending"}


def _parse_amex_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if abs(timestamp) >= 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        timestamp = float(text)
        if len(text) >= 13 or abs(timestamp) >= 1_000_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d %Y",
        "%B %d %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_amex_amount(value: Any) -> tuple[float, str]:
    currency = "CAD"
    raw_value = value
    if isinstance(value, dict):
        raw_value = (
            value.get("amount")
            if value.get("amount") is not None
            else value.get("value")
        )
        currency = (
            value.get("currencyCode")
            or value.get("currency")
            or value.get("isoCurrencyCode")
            or currency
        )
    if isinstance(raw_value, (int, float)):
        return float(raw_value), currency

    text = str(raw_value or "").strip()
    if not text:
        return 0.0, currency

    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    text = text.replace("CAD", "").replace("USD", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0, currency
    amount = float(match.group(0))
    if negative:
        amount = -abs(amount)
    return amount, currency


def _amex_description(raw: dict[str, Any]) -> str | None:
    candidates = [
        raw.get("displayDescription"),
        raw.get("descriptionLine"),
        raw.get("description"),
        raw.get("merchantName"),
        _nested_value(raw, ("merchant", "merchantName")),
        _nested_value(raw, ("merchant", "name")),
        _nested_value(raw, ("merchantDetails", "merchantName")),
        _nested_value(raw, ("merchantDetails", "name")),
        _nested_value(raw, ("extendedTransactionDetails", "merchantName")),
        _nested_value(raw, ("extendedTransactionDetails", "merchantDetails", "merchantName")),
        _nested_value(raw, ("extendedTransactionDetails", "merchantDetails", "name")),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            candidate = " ".join(str(part).strip() for part in candidate if str(part).strip())
        elif isinstance(candidate, dict):
            candidate = (
                candidate.get("name")
                or candidate.get("merchantName")
                or candidate.get("description")
            )
        if candidate:
            text = str(candidate).strip()
            if text:
                return text
    return None


def _amex_signed_amount(raw: dict[str, Any], amount: float, description: str | None) -> float:
    indicator = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("paymentType", "transactionType", "debitCreditIndicator", "type")
    )
    desc = (description or "").lower()
    combined = f"{indicator} {desc}"

    credit_tokens = (
        "credit",
        "payment",
        "refund",
        "adjustment",
        "reversal",
        "redeem",
        "return",
    )
    debit_tokens = (
        "debit",
        "charge",
        "purchase",
        "cash advance",
        "fee",
        "interest",
    )

    if any(token in combined for token in credit_tokens) and amount < 0:
        amount = abs(amount)
    if any(token in combined for token in debit_tokens) and amount > 0:
        amount = -abs(amount)
    return amount


def _amex_contains_phrase(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.lower()).replace(r"\ ", r"\s+")
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None


def _classify_amex_transaction(raw: dict[str, Any], amount: float, description: str | None) -> str:
    indicator = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("paymentType", "transactionType", "debitCreditIndicator", "type")
    )
    desc = (description or "").lower()
    combined = f"{indicator} {desc}"

    if _amex_contains_phrase(combined, "interest"):
        return "interest_paid" if amount < 0 else "interest"
    if any(
        _amex_contains_phrase(combined, phrase)
        for phrase in ("fee", "annual membership", "late payment", "foreign transaction")
    ):
        return "fee"
    if _amex_contains_phrase(combined, "transfer"):
        return "transfer"
    if any(
        _amex_contains_phrase(combined, phrase)
        for phrase in ("payment", "refund", "credit", "adjustment", "reversal")
    ):
        return "deposit" if amount > 0 else "withdrawal"
    return "deposit" if amount >= 0 else "withdrawal"


def _amex_transaction_fingerprint(date: datetime, amount: float, description: str | None) -> str:
    raw = f"{date.isoformat()}|{round(amount, 2)}|{(description or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _normalize_amex_transaction(raw: dict[str, Any]) -> NormalizedTransaction | None:
    if (
        _amex_truthy(raw.get("pendingTransactionIndicator"))
        or str(raw.get("status") or "").strip().lower() == "pending"
    ):
        return None

    date = _parse_amex_date(
        raw.get("dateProcessed")
        or raw.get("postDate")
        or raw.get("chargeDate")
    )
    if date is None:
        return None

    amount, currency = _parse_amex_amount(raw.get("transactionAmount"))
    currency = raw.get("currencyCode") or currency
    description = _amex_description(raw)
    amount = _amex_signed_amount(raw, amount, description)

    txn_key = (
        raw.get("identifier")
        or raw.get("uniqueReferenceNumber")
        or raw.get("transactionId")
        or raw.get("referenceNumber")
    )
    if txn_key:
        external_id = f"amex_{txn_key}"
    else:
        external_id = f"amex_{_amex_transaction_fingerprint(date, amount, description)}"

    return NormalizedTransaction(
        external_id=external_id,
        date=date,
        type=_classify_amex_transaction(raw, amount, description),
        amount=amount,
        currency=currency or "CAD",
        description=description,
    )


class AmexConnector(DesktopVisibleAuthTransactionImportConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="amex",
            module_path="app.scrapers.amex",
            display_name="American Express",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict]) -> dict[str, dict[str, str | int | None]]:
            sync_state: dict[str, dict[str, str | int | None]] = {}
            context = get_connector_sync_context()
            if context is None or context.institution_id is None:
                raise RuntimeError("Connection identity is required for Amex sync planning")
            async with async_session() as db:
                for acct in accounts:
                    external_id = _amex_account_external_id(acct)
                    if not external_id:
                        continue
                    alternate_external_ids = _amex_alternate_external_ids(acct, external_id)
                    existing_state = (
                        await db.execute(
                            select(TransactionImportAccountState.id).where(
                                TransactionImportAccountState.user_id == user_id,
                                TransactionImportAccountState.institution_id == context.institution_id,
                                TransactionImportAccountState.account_external_id == external_id,
                            )
                        )
                    ).scalar_one_or_none()
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=str(acct.get("name") or "American Express Card"),
                        account_type="credit_card",
                        is_liability=True,
                        backfill_days=AMEX_MAX_HISTORY_DAYS if existing_state is None else 1,
                        backfill_chunk_days=AMEX_BACKFILL_CHUNK_DAYS,
                        incremental_days=DEFAULT_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                    )
                    sync_window = plan.as_sync_state()
                    sync_state[external_id] = sync_window
                    for alternate_external_id in alternate_external_ids:
                        sync_state[alternate_external_id] = sync_window
            return sync_state

        return resolver

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict[str, Any]],
    ) -> list[NormalizedTransaction]:
        del account_payload
        return self._normalize_transaction_rows(raw_transactions, _normalize_amex_transaction)

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
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
            external_id = _amex_account_external_id(acct_data)
            alternate_external_ids = _amex_alternate_external_ids(acct_data, external_id)
            try:
                parsed_balance = float(acct_data.get("balance"))
            except (TypeError, ValueError):
                parsed_balance = None
            balance_authoritative = parsed_balance is not None and math.isfinite(parsed_balance)

            account = NormalizedAccount(
                name=acct_data["name"],
                account_type="credit_card",
                external_id=external_id,
                currency=acct_data.get("currency") or "CAD",
                is_liability=True,
                balance=abs(parsed_balance) if balance_authoritative else None,
                balance_authoritative=balance_authoritative,
                holdings_authoritative=True,
                alternate_external_ids=alternate_external_ids,
            )
            accounts.append(account)
            if external_id:
                account_by_external_id[external_id] = account
            for alternate_external_id in alternate_external_ids:
                account_by_external_id[alternate_external_id] = account

        return self._build_result_from_accounts_and_transactions(
            accounts=accounts,
            account_by_external_id=account_by_external_id,
            transactions_data=transactions_data,
            transaction_fetch_succeeded_external_ids=transaction_fetch_succeeded_external_ids,
            transaction_normalizer=lambda raw, _external_id, _account: _normalize_amex_transaction(raw),
        )
