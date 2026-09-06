from __future__ import annotations

import hashlib
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
from app.connectors.payload_utils import first_present_value as _first_cibc_value
from app.database import async_session
from app.scrapers.cibc import CIBC_BACKFILL_CHUNK_DAYS, CIBC_FALLBACK_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _cibc_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _raw_cibc_provider_account_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _cibc_provider_account_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = _raw_cibc_provider_account_id(
        _first_cibc_value(acct_data, "provider_account_id", "id", "accountId", "account_id")
    )
    if provider_account_id:
        return provider_account_id

    transit = str(acct_data.get("transit") or "").strip()
    account_number = _cibc_digits(acct_data.get("number"))
    if transit and account_number:
        return f"{transit}:{account_number}"
    if account_number:
        return account_number
    return _raw_cibc_provider_account_id(acct_data.get("external_id"))


def _cibc_account_external_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = _cibc_provider_account_id(acct_data)
    if provider_account_id:
        return f"account:{provider_account_id}"
    return acct_data.get("external_id")


def _cibc_alternate_external_ids(acct_data: dict[str, Any], external_id: str | None) -> tuple[str, ...]:
    values = [
        acct_data.get("id"),
        acct_data.get("accountId"),
        acct_data.get("number"),
        _cibc_digits(acct_data.get("number")) or None,
        acct_data.get("provider_account_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _parse_cibc_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, dict):
        return _parse_cibc_amount(
            _first_cibc_value(
                value,
                "amount",
                "value",
                "sortValue",
                "cadAmount",
                "current",
                "displayValue",
                "formattedAmount",
                "formatted",
            )
        )

    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    text = text.replace("CAD", "").replace("USD", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    amount = float(match.group(0))
    if negative:
        amount = -abs(amount)
    return amount if math.isfinite(amount) else None


def _first_cibc_amount(raw: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in raw:
            continue
        amount = _parse_cibc_amount(raw.get(key))
        if amount is not None:
            return amount
    return None


def _parse_cibc_datetime(value: Any) -> datetime | None:
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

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _cibc_nested_dict(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _cibc_account_name(raw: dict[str, Any]) -> str:
    details = _cibc_nested_dict(raw, "details")
    display = _cibc_nested_dict(raw, "displayAttributes")
    categorization = _cibc_nested_dict(raw, "categorization")

    nickname = str(raw.get("nickname") or "").strip()
    if nickname:
        return nickname

    text_values = [
        raw.get("name"),
        raw.get("productName"),
        details.get("productName"),
        details.get("productNameKey"),
        display.get("displayName"),
        display.get("fullName"),
        display.get("name"),
        categorization.get("subCategory"),
        categorization.get("holding"),
    ]
    normalized = " ".join(str(value or "") for value in text_values).lower()
    if "chequ" in normalized:
        return "Chequing"
    if "saving" in normalized or "savings" in normalized:
        return "Savings"
    if "visa" in normalized:
        return "Visa"
    if "mastercard" in normalized:
        return "Mastercard"
    if "line" in normalized and "credit" in normalized:
        return "Line of Credit"
    if "mortgage" in normalized:
        return "Mortgage"
    if "tfsa" in normalized or "tax advantage" in normalized:
        return "TFSA"
    if "rrsp" in normalized or re.search(r"\brsp\b", normalized):
        return "RRSP"

    subcategory = str(categorization.get("subCategory") or "").replace("_", " ").strip()
    if subcategory:
        return subcategory.title()
    category = str(categorization.get("category") or "").replace("_", " ").strip()
    if category:
        return f"CIBC {category.title()}"
    return "CIBC Account"


def _cibc_account_type(raw: dict[str, Any]) -> tuple[str, bool]:
    categorization = _cibc_nested_dict(raw, "categorization")
    details = _cibc_nested_dict(raw, "details")
    text = " ".join(
        str(value or "")
        for value in (
            raw.get("_type"),
            raw.get("name"),
            raw.get("nickname"),
            raw.get("productName"),
            details.get("productName"),
            details.get("productNameKey"),
            categorization.get("category"),
            categorization.get("subCategory"),
            categorization.get("holding"),
            categorization.get("taxPlan"),
        )
    ).lower()

    if "mortgage" in text:
        return "mortgage", True
    if "line_of_credit" in text or "line of credit" in text or re.search(r"\bloc\b", text):
        return "loc", True
    if "credit" in text or "visa" in text or "mastercard" in text:
        return "credit_card", True
    if "loan" in text:
        return "loan", True
    if "tfsa" in text or "tax advantage" in text:
        return "tfsa", False
    if "fhsa" in text:
        return "fhsa", False
    if "rrsp" in text or re.search(r"\brsp\b", text):
        return "rrsp", False
    if "resp" in text:
        return "resp", False
    if "rrif" in text:
        return "rrif", False
    if "lira" in text:
        return "lira", False
    if "registered_investment" in text or "non_registered_investment" in text:
        return "margin", False
    if "chequ" in text:
        return "chequing", False
    if "saving" in text or "savings" in text or "deposit" in text:
        return "savings", False
    return "other", False


def _cibc_transaction_description(raw: dict[str, Any]) -> str | None:
    parts: list[str] = []

    def is_compact_provider_code(value: Any) -> bool:
        part = re.sub(r"\s+", "", str(value or "")).strip()
        return bool(re.fullmatch(r"[A-Z0-9._-]{3,12}", part)) and any(char.isdigit() for char in part)

    def append(value: Any) -> None:
        part = re.sub(r"\s+", " ", str(value)).strip() if value else ""
        if not part:
            return
        part_key = part.casefold()
        for existing in list(parts):
            existing_key = existing.casefold()
            if part_key == existing_key or part_key in existing_key:
                return
            if existing_key in part_key:
                parts.remove(existing)
        parts.append(part)

    metadata_values = [
        raw.get("transactionLocation"),
        raw.get("location"),
        raw.get("transactionCategory"),
        raw.get("categoryDescription"),
        raw.get("transactionTypeDescription"),
    ]
    for value in metadata_values:
        if not is_compact_provider_code(value):
            append(value)
    for value in (
        _first_cibc_value(raw, "description", "transactionDescription", "memo", "details", "name"),
        raw.get("descriptionLine1"),
        raw.get("descriptionLine2"),
        raw.get("merchantName"),
    ):
        append(value)
    if not parts:
        return None
    return " ".join(parts)


def _cibc_transaction_is_posted(raw: dict[str, Any]) -> bool:
    for key in ("pending", "isPending", "authorized", "isAuthorized"):
        if raw.get(key) is True:
            return False
    status = " ".join(str(raw.get(key) or "").lower() for key in ("status", "transactionStatus", "postingStatus"))
    if not status:
        return True
    return not any(token in status for token in ("pending", "authorized"))


def _cibc_signed_amount(raw: dict[str, Any]) -> tuple[float, str]:
    withdrawal_amount = _first_cibc_amount(
        raw,
        "withdrawalAmt",
        "withdrawalAmount",
        "debitAmount",
        "debitedAmount",
        "debit",
        "debitSortValue",
        "debitValue",
    )
    if withdrawal_amount is not None and abs(withdrawal_amount) > 0:
        return -abs(withdrawal_amount), str(raw.get("_account_currency") or raw.get("currency") or "CAD")

    deposit_amount = _first_cibc_amount(
        raw,
        "depositAmt",
        "depositAmount",
        "creditAmount",
        "creditedAmount",
        "credit",
        "creditSortValue",
        "creditValue",
    )
    if deposit_amount is not None and abs(deposit_amount) > 0:
        return abs(deposit_amount), str(raw.get("_account_currency") or raw.get("currency") or "CAD")

    amount = _first_cibc_amount(raw, "amount", "transactionAmount", "value") or 0.0
    direction = str(_first_cibc_value(raw, "creditDebitIndicator", "debitCreditIndicator", "direction", "actionType") or "").upper()
    if (direction in {"D", "DR"} or any(token in direction for token in ("DEBIT", "WITHDRAWAL"))) and amount > 0:
        amount = -amount
    if (direction in {"C", "CR"} or any(token in direction for token in ("CREDIT", "DEPOSIT"))) and amount < 0:
        amount = abs(amount)
    return amount, str(raw.get("_account_currency") or raw.get("currency") or "CAD")


def _classify_cibc_transaction(raw: dict[str, Any], amount: float) -> str:
    text = " ".join(
        str(value or "")
        for value in (
            _first_cibc_value(raw, "transactionType", "type", "actionType"),
            _cibc_transaction_description(raw),
        )
    ).lower()
    if "interest" in text:
        return "interest" if amount >= 0 else "interest_paid"
    if any(token in text for token in ("fee", "service charge", "nsf")):
        return "fee"
    if any(token in text for token in ("transfer", "e-transfer", "interac")):
        return "transfer"
    if "deposit" in text:
        return "deposit"
    if any(token in text for token in ("withdrawal", "purchase", "debit", "payment")):
        return "withdrawal" if amount < 0 else "deposit"
    return "deposit" if amount >= 0 else "withdrawal"


def _cibc_transaction_external_id(raw: dict[str, Any], account_external_id: str, amount: float, description: str | None, booked_at: datetime) -> str:
    for key in ("transactionId", "txnId", "id", "referenceNumber", "referenceId", "sequenceNumber"):
        value = raw.get(key)
        if value:
            return f"cibc_{value}"

    digest = hashlib.sha1(
        "|".join(
            [
                account_external_id,
                booked_at.isoformat(),
                f"{amount:.2f}",
                description or "",
            ]
        ).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"cibc_{digest}"


def _normalize_cibc_transaction(raw: dict[str, Any], account_external_id: str) -> NormalizedTransaction | None:
    if not _cibc_transaction_is_posted(raw):
        return None

    booked_at = _parse_cibc_datetime(
        _first_cibc_value(
            raw,
            "date",
            "transactionDate",
            "postedDate",
            "effectiveDate",
            "bookingDate",
            "bookingDateTime",
        )
    )
    if booked_at is None:
        return None

    amount, currency = _cibc_signed_amount(raw)
    description = _cibc_transaction_description(raw)
    return NormalizedTransaction(
        external_id=_cibc_transaction_external_id(raw, account_external_id, amount, description, booked_at),
        date=booked_at,
        type=_classify_cibc_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class CIBCConnector(DesktopVisibleAuthTransactionImportConnector):
    unexpected_payload_message = "CIBC scraper returned an invalid response"
    generic_phase2_diagnostics_enabled = False

    def __init__(self) -> None:
        super().__init__(
            provider="cibc",
            module_path="app.scrapers.cibc",
            display_name="CIBC",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
            sync_windows: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for acct in accounts:
                    external_id = _cibc_account_external_id(acct)
                    if not external_id:
                        continue
                    alternate_external_ids = _cibc_alternate_external_ids(acct, external_id)
                    account_type, is_liability = _cibc_account_type(acct)
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=_cibc_account_name(acct),
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=CIBC_FALLBACK_MAX_HISTORY_DAYS,
                        backfill_chunk_days=CIBC_BACKFILL_CHUNK_DAYS,
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
        account_external_id = _cibc_account_external_id(account_payload) or str(
            account_payload.get("external_id") or ""
        )
        return self._normalize_transaction_rows(
            raw_transactions,
            lambda raw: _normalize_cibc_transaction(raw, account_external_id),
        )

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
        accounts_data: list[dict[str, Any]] = payload.get("accounts") or []
        transactions_data: dict[str, list[dict[str, Any]]] = payload.get("transactions") or {}
        transaction_fetch_succeeded_external_ids = set(payload.get("transaction_fetch_succeeded_accounts") or [])

        if not accounts_data:
            return SyncResult(status=SyncStatus.ERROR, message="No CIBC accounts returned")

        accounts: list[NormalizedAccount] = []
        account_by_external_id: dict[str, NormalizedAccount] = {}

        for acct_data in accounts_data:
            if not isinstance(acct_data, dict):
                continue

            external_id = _cibc_account_external_id(acct_data)
            alternate_external_ids = _cibc_alternate_external_ids(acct_data, external_id)
            account_type, is_liability = _cibc_account_type(acct_data)
            balance = _parse_cibc_amount(_first_cibc_value(acct_data, "balance", "currentBalance", "availableFunds"))
            currency = _first_cibc_value(acct_data, "currency", "Currency") or "CAD"

            account = NormalizedAccount(
                name=_cibc_account_name(acct_data),
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
            transaction_normalizer=lambda raw, external_id, _account: _normalize_cibc_transaction(raw, external_id),
        )
