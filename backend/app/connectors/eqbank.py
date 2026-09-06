from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.connectors.payload_utils import first_present_value as _first_eqbank_value
from app.database import async_session
from app.scrapers.eqbank import EQBANK_BACKFILL_CHUNK_DAYS, EQBANK_MAX_BACKFILL_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _parse_eqbank_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, dict):
        nested = _first_eqbank_value(value, "Amount", "amount", "value", "current", "balance")
        return _parse_eqbank_amount(nested)

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    amount = float(match.group(0))
    amount = -amount if negative else amount
    return amount if math.isfinite(amount) else None


def _parse_eqbank_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _raw_eqbank_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _eqbank_stable_account_external_id(provider_account_id: str) -> str:
    return f"account:{provider_account_id}"


def _eqbank_account_external_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = (
        acct_data.get("provider_account_id")
        or acct_data.get("accountId")
        or acct_data.get("account_id")
        or _raw_eqbank_account_id(acct_data.get("external_id"))
    )
    raw_provider_account_id = _raw_eqbank_account_id(provider_account_id)
    if raw_provider_account_id:
        return _eqbank_stable_account_external_id(raw_provider_account_id)
    fallback = acct_data.get("external_id")
    if fallback is None:
        return None
    fallback_text = str(fallback).strip()
    return fallback_text or None


def _eqbank_alternate_external_ids(
    acct_data: dict[str, Any],
    external_id: str | None,
) -> tuple[str, ...]:
    values = [
        acct_data.get("alternate_external_id"),
        acct_data.get("account_number"),
        acct_data.get("accountNumber"),
        acct_data.get("provider_account_id"),
        acct_data.get("accountId"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _eqbank_account_type(raw: dict[str, Any]) -> tuple[str, bool]:
    text = " ".join(
        str(_first_eqbank_value(raw, key) or "")
        for key in (
            "account_type",
            "accountType",
            "product_type",
            "productType",
            "type",
            "category",
            "subcategory",
            "name",
            "accountName",
            "description",
        )
    ).lower()
    balance_type = str(_first_eqbank_value(raw, "balance_type", "balanceType") or "").upper()

    if "mortgage" in text:
        return "mortgage", True
    if "line of credit" in text or re.search(r"\bloc\b", text):
        return "loc", True
    if "credit card" in text or "card" in text:
        return "credit_card", True
    if "chequing" in text or "checking" in text:
        return "chequing", balance_type == "LIABILITY"
    if "tfsa" in text:
        return "tfsa", False
    if "fhsa" in text:
        return "fhsa", False
    if "rrsp" in text or re.search(r"\brsp\b", text):
        return "rrsp", False
    if "resp" in text:
        return "resp", False
    if "gic" in text or "notice" in text or "saving" in text or "hisa" in text:
        return "savings", False
    return "other", balance_type == "LIABILITY"


def _eqbank_transaction_description(raw: dict[str, Any]) -> str | None:
    value = (
        raw.get("TransactionInformation")
        or raw.get("transactionInformation")
        or raw.get("description")
        or raw.get("name")
        or raw.get("memo")
    )
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _eqbank_transaction_is_posted(raw: dict[str, Any]) -> bool:
    status = str(_first_eqbank_value(raw, "Status", "status") or "").strip().lower()
    if not status:
        return True
    if any(token in status for token in ("pending", "authorization", "authorized")):
        return False
    return any(token in status for token in ("booked", "posted", "settled", "cleared", "completed"))


def _eqbank_signed_amount(raw: dict[str, Any]) -> tuple[float, str]:
    amount_payload = _first_eqbank_value(raw, "Amount", "amount")
    amount = _parse_eqbank_amount(amount_payload) or 0.0
    currency = str(
        _first_eqbank_value(
            amount_payload if isinstance(amount_payload, dict) else {},
            "Currency",
            "currency",
        )
        or "CAD"
    )
    indicator = str(
        _first_eqbank_value(
            raw,
            "CreditDebitIndicator",
            "creditDebitIndicator",
            "debitCreditIndicator",
        )
        or ""
    ).upper()

    if indicator == "DEBIT" and amount > 0:
        amount = -amount
    if indicator == "CREDIT" and amount < 0:
        amount = abs(amount)

    return amount, currency


def _classify_eqbank_transaction(raw: dict[str, Any], amount: float) -> str:
    description = (_eqbank_transaction_description(raw) or "").lower()
    indicator = str(
        _first_eqbank_value(
            raw,
            "CreditDebitIndicator",
            "creditDebitIndicator",
            "debitCreditIndicator",
        )
        or ""
    ).lower()
    text = f"{indicator} {description}"

    if "interest" in text:
        return "interest" if amount >= 0 else "interest_paid"
    if any(token in text for token in ("transfer", "e-transfer", "interac")):
        return "transfer"
    if any(token in text for token in ("direct deposit", "deposit", "payroll")):
        return "deposit"
    if any(token in text for token in ("fee", "service charge", "nsf")):
        return "fee"
    if any(token in text for token in ("withdrawal", "withdraw", "payment", "purchase")):
        return "deposit" if amount > 0 else "withdrawal"
    return "deposit" if amount >= 0 else "withdrawal"


def _normalize_eqbank_transaction(raw: dict[str, Any]) -> NormalizedTransaction | None:
    if not _eqbank_transaction_is_posted(raw):
        return None

    transaction_id = _first_eqbank_value(raw, "TransactionId", "transactionId", "id")
    if not transaction_id:
        return None

    booked_at = _parse_eqbank_datetime(
        _first_eqbank_value(raw, "BookingDateTime", "bookingDateTime", "ValueDateTime", "valueDateTime")
    )
    if booked_at is None:
        return None

    amount, currency = _eqbank_signed_amount(raw)
    description = _eqbank_transaction_description(raw)

    return NormalizedTransaction(
        external_id=f"eqbank_{transaction_id}",
        date=booked_at,
        type=_classify_eqbank_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class EQBankConnector(DesktopVisibleAuthTransactionImportConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="eqbank",
            module_path="app.scrapers.eqbank",
            display_name="EQ Bank",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
            sync_windows: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for acct in accounts:
                    external_id = _eqbank_account_external_id(acct)
                    if not external_id:
                        continue
                    alternate_external_ids = _eqbank_alternate_external_ids(acct, external_id)
                    account_type, is_liability = _eqbank_account_type(acct)
                    account_name = (
                        _first_eqbank_value(acct, "name", "accountName", "description")
                        or "EQ Bank Account"
                    )
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=str(account_name).strip() or "EQ Bank Account",
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=EQBANK_MAX_BACKFILL_DAYS,
                        backfill_chunk_days=EQBANK_BACKFILL_CHUNK_DAYS,
                        incremental_days=DEFAULT_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                    )
                    sync_window = plan.as_sync_state()
                    sync_windows[external_id] = sync_window
                    for alternate_external_id in alternate_external_ids:
                        sync_windows[alternate_external_id] = sync_window
            return sync_windows

        return resolver

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict[str, Any]],
    ) -> list[NormalizedTransaction]:
        del account_payload
        return self._normalize_transaction_rows(raw_transactions, _normalize_eqbank_transaction)

    async def _saved_artifact_sync_kwargs(self, user_id: int) -> dict[str, Any]:
        kwargs = await super()._saved_artifact_sync_kwargs(user_id)
        credential = await self._get_saved_credentials(user_id)
        kwargs["username"] = credential.username if credential else None
        return kwargs

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
        accounts_data: list[dict[str, Any]] = payload.get("accounts") or []
        transactions_data: dict[str, list[dict[str, Any]]] = payload.get("transactions") or {}
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

            external_id = _eqbank_account_external_id(acct_data)
            alternate_external_ids = _eqbank_alternate_external_ids(acct_data, external_id)
            account_type, is_liability = _eqbank_account_type(acct_data)
            name = (
                _first_eqbank_value(acct_data, "name", "accountName", "description")
                or "EQ Bank Account"
            )
            currency = _first_eqbank_value(acct_data, "currency", "Currency") or "CAD"
            balance = _parse_eqbank_amount(
                _first_eqbank_value(
                    acct_data,
                    "balance",
                    "currentBalance",
                    "current_balance",
                    "availableBalance",
                    "available_balance",
                )
            )

            account = NormalizedAccount(
                name=str(name).strip() or "EQ Bank Account",
                account_type=account_type,
                external_id=external_id,
                currency=str(currency).strip() or "CAD",
                is_liability=is_liability,
                balance=float(abs(balance)) if balance is not None else None,
                balance_authoritative=balance is not None,
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
            transaction_normalizer=lambda raw, _external_id, _account: _normalize_eqbank_transaction(raw),
        )
