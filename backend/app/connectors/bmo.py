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
from app.connectors.payload_utils import first_present_value as _first_bmo_value
from app.database import async_session
from app.scrapers.bmo import BMO_BACKFILL_CHUNK_DAYS, BMO_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _bmo_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _raw_bmo_provider_account_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _bmo_provider_account_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = _raw_bmo_provider_account_id(acct_data.get("provider_account_id"))
    if provider_account_id:
        return provider_account_id

    category_name = str(_first_bmo_value(acct_data, "category_name", "categoryName") or "account").lower()
    digits = _bmo_digits(_first_bmo_value(acct_data, "accountNumber", "masked_account_number"))
    if digits:
        return f"{category_name}:{digits}"

    account_index = _first_bmo_value(acct_data, "accountIndex", "account_index")
    if account_index is not None:
        return f"{category_name}:index:{account_index}"

    name = str(_first_bmo_value(acct_data, "ocifAccountName", "productName", "name") or "").strip()
    if name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if slug:
            return f"{category_name}:{slug}"
    return None


def _bmo_account_external_id(acct_data: dict[str, Any]) -> str | None:
    external_id = acct_data.get("external_id")
    if external_id:
        return str(external_id)
    provider_account_id = _bmo_provider_account_id(acct_data)
    if provider_account_id:
        return f"account:{provider_account_id}"
    return None


def _bmo_alternate_external_ids(
    acct_data: dict[str, Any],
    external_id: str | None,
) -> tuple[str, ...]:
    values = [
        acct_data.get("accountNumber"),
        _bmo_digits(acct_data.get("accountNumber")) or None,
        acct_data.get("provider_account_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _parse_bmo_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, dict):
        nested = _first_bmo_value(value, "amount", "Amount", "value", "current", "balance")
        return _parse_bmo_amount(nested)

    text = str(value).strip()
    if not text:
        return None
    negative = (text.startswith("(") and text.endswith(")")) or text.endswith("-")
    text = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "").rstrip("-")
    text = text.replace("CAD", "").replace("USD", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    amount = float(match.group(0))
    if negative:
        amount = -abs(amount)
    return amount if math.isfinite(amount) else None


def _parse_bmo_datetime(value: Any) -> datetime | None:
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

    for fmt in (
        "%Y-%m-%d",
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


def _bmo_account_type(raw: dict[str, Any]) -> tuple[str, bool]:
    category_name = str(_first_bmo_value(raw, "category_name", "categoryName") or "").upper()
    text = " ".join(
        str(_first_bmo_value(raw, key) or "")
        for key in (
            "accountType",
            "productName",
            "ocifAccountName",
            "group_head_title",
            "groupHeadTitle",
            "name",
        )
    ).lower()

    if category_name == "CC" or "credit card" in text or "mastercard" in text or "visa" in text:
        return "credit_card", True
    if category_name == "LM":
        if "mortgage" in text:
            return "mortgage", True
        if "line of credit" in text or re.search(r"\bloc\b", text):
            return "loc", True
        return "loan", True
    if "mortgage" in text:
        return "mortgage", True
    if "line of credit" in text or re.search(r"\bloc\b", text):
        return "loc", True
    if "chequing" in text or "checking" in text:
        return "chequing", False
    if "credit" in text:
        return "credit_card", True
    if "tfsa" in text:
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
    if "saving" in text or "savings" in text or "amplifier" in text:
        return "savings", False
    if category_name == "IN":
        return "other", False
    return "other", False


def _bmo_transaction_description(raw: dict[str, Any]) -> str | None:
    values = [
        _first_bmo_value(
            raw,
            "description",
            "transactionDescription",
            "descr",
            "memo",
            "details",
            "name",
        ),
        _first_bmo_value(raw, "descriptionLine1", "descriptionLine2"),
    ]
    parts = [re.sub(r"\s+", " ", str(value)).strip() for value in values if value]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return " | ".join(dict.fromkeys(parts))


def _bmo_description_text(raw: dict[str, Any]) -> str:
    return (_bmo_transaction_description(raw) or "").lower()


def _bmo_transaction_is_posted(raw: dict[str, Any]) -> bool:
    for key in ("pending", "isPending", "authorized", "isAuthorized"):
        if raw.get(key) is True:
            return False

    status = " ".join(
        str(raw.get(key) or "").lower()
        for key in ("status", "transactionStatus", "postingStatus")
    )
    if not status:
        return True
    if any(token in status for token in ("pending", "authorized")):
        return False
    return True


def _bmo_signed_amount(raw: dict[str, Any]) -> tuple[float, str]:
    withdrawal_amount = _parse_bmo_amount(
        _first_bmo_value(raw, "withdrawalAmt", "withdrawalAmount", "debitAmount", "debitedAmount")
    )
    if withdrawal_amount is not None and abs(withdrawal_amount) > 0:
        return -abs(withdrawal_amount), str(raw.get("_account_currency") or raw.get("currency") or "CAD")

    deposit_amount = _parse_bmo_amount(
        _first_bmo_value(raw, "depositAmt", "depositAmount", "creditAmount", "creditedAmount")
    )
    if deposit_amount is not None and abs(deposit_amount) > 0:
        return abs(deposit_amount), str(raw.get("_account_currency") or raw.get("currency") or "CAD")

    amount = _parse_bmo_amount(
        _first_bmo_value(
            raw,
            "amount",
            "transactionAmount",
            "txnAmount",
            "transactionAmt",
            "value",
        )
    ) or 0.0
    indicator = str(
        _first_bmo_value(raw, "creditDebitIndicator", "debitCreditIndicator", "direction")
        or ""
    ).upper()
    if not indicator:
        description = _bmo_description_text(raw)
        if any(token in description for token in ("recvd", "received", "deposit", "payroll", "credit")):
            indicator = "CREDIT"
        elif any(token in description for token in ("sent", "withdrawal", "bill payment", "purchase", "debit")):
            indicator = "DEBIT"
    if indicator == "DEBIT" and amount > 0:
        amount = -amount
    if indicator == "CREDIT" and amount < 0:
        amount = abs(amount)
    currency = str(
        _first_bmo_value(raw, "_account_currency", "currency", "Currency") or "CAD"
    )
    return amount, currency


def _classify_bmo_transaction(raw: dict[str, Any], amount: float) -> str:
    text = " ".join(
        [
            str(_first_bmo_value(raw, "transactionType", "type") or ""),
            str(_bmo_transaction_description(raw) or ""),
        ]
    ).lower()
    if "interest" in text:
        return "interest" if amount >= 0 else "interest_paid"
    if "service charge" in text or re.search(r"\b(?:fee|nsf)\b", text):
        return "fee"
    if any(token in text for token in ("interac", "e-transfer", "etrnsfr")):
        if any(token in text for token in ("recvd", "received", "deposit", "credit")):
            return "deposit"
        if any(token in text for token in ("sent", "withdrawal", "debit")):
            return "withdrawal" if amount < 0 else "deposit"
        return "transfer"
    if any(token in text for token in ("transfer", "e-transfer", "interac")):
        return "transfer"
    if any(token in text for token in ("deposit", "payroll")):
        return "deposit"
    if any(token in text for token in ("bill payment", "withdrawal", "purchase", "debit")):
        return "withdrawal" if amount < 0 else "deposit"
    return "deposit" if amount >= 0 else "withdrawal"


def _bmo_transaction_external_id(
    raw: dict[str, Any],
    account_external_id: str,
    amount: float,
    description: str | None,
    booked_at: datetime,
) -> str:
    for key in (
        "transactionId",
        "txnId",
        "id",
        "referenceNumber",
        "referenceId",
        "sequenceNumber",
        "cimgRef",
    ):
        value = raw.get(key)
        if value:
            return f"bmo_{value}"

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
    return f"bmo_{digest}"


def _normalize_bmo_transaction(raw: dict[str, Any], account_external_id: str) -> NormalizedTransaction | None:
    if not _bmo_transaction_is_posted(raw):
        return None

    booked_at = _parse_bmo_datetime(
        _first_bmo_value(
            raw,
            "date",
            "transactionDate",
            "postedDate",
            "txnDate",
            "transactionDt",
            "postedDt",
            "effectiveDate",
            "bookingDate",
            "bookingDateTime",
        )
    )
    if booked_at is None:
        return None

    amount, currency = _bmo_signed_amount(raw)
    description = _bmo_transaction_description(raw)
    return NormalizedTransaction(
        external_id=_bmo_transaction_external_id(
            raw,
            account_external_id,
            amount,
            description,
            booked_at,
        ),
        date=booked_at,
        type=_classify_bmo_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class BMOConnector(DesktopVisibleAuthTransactionImportConnector):
    unexpected_payload_message = "Unexpected BMO scraper response"

    def __init__(self) -> None:
        super().__init__(
            provider="bmo",
            module_path="app.scrapers.bmo",
            display_name="BMO",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
            sync_windows: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for acct in accounts:
                    external_id = _bmo_account_external_id(acct)
                    if not external_id:
                        continue
                    alternate_external_ids = _bmo_alternate_external_ids(acct, external_id)
                    account_type, is_liability = _bmo_account_type(acct)
                    account_name = (
                        _first_bmo_value(acct, "ocifAccountName", "productName", "name")
                        or "BMO Account"
                    )
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=str(account_name).strip() or "BMO Account",
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=BMO_MAX_HISTORY_DAYS,
                        backfill_chunk_days=BMO_BACKFILL_CHUNK_DAYS,
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
        account_external_id = _bmo_account_external_id(account_payload) or str(
            account_payload.get("external_id") or ""
        )
        return self._normalize_transaction_rows(
            raw_transactions,
            lambda raw: _normalize_bmo_transaction(raw, account_external_id),
        )

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
        accounts_data: list[dict[str, Any]] = payload.get("accounts") or []
        transactions_data: dict[str, list[dict[str, Any]]] = payload.get("transactions") or {}
        transaction_fetch_succeeded_external_ids = set(
            payload.get("transaction_fetch_succeeded_accounts") or []
        )

        if not accounts_data:
            return SyncResult(status=SyncStatus.ERROR, message="No BMO accounts returned")

        accounts: list[NormalizedAccount] = []
        account_by_external_id: dict[str, NormalizedAccount] = {}

        for acct_data in accounts_data:
            if not isinstance(acct_data, dict):
                continue

            external_id = _bmo_account_external_id(acct_data)
            alternate_external_ids = _bmo_alternate_external_ids(acct_data, external_id)
            account_type, is_liability = _bmo_account_type(acct_data)
            name = (
                _first_bmo_value(acct_data, "ocifAccountName", "productName", "name")
                or "BMO Account"
            )
            currency = _first_bmo_value(acct_data, "currency", "Currency") or "CAD"
            balance = None
            if acct_data.get("balance_is_fresh", True):
                balance = _parse_bmo_amount(
                    _first_bmo_value(
                        acct_data,
                        "accountBalance",
                        "currentBalance",
                        "balance",
                        "availableAmount",
                    )
                )

            account = NormalizedAccount(
                name=str(name).strip() or "BMO Account",
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
            transaction_normalizer=lambda raw, external_id, _account: _normalize_bmo_transaction(raw, external_id),
        )
