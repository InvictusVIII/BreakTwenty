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
from app.connectors.payload_utils import first_present_value as _first_national_value
from app.database import async_session
from app.scrapers.national import NATIONAL_BACKFILL_CHUNK_DAYS, NATIONAL_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _localized_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("en", "fr"):
            nested = str(value.get(key) or "").strip()
            if nested:
                return nested
        return ""
    return str(value or "").strip()


def _parse_national_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, dict):
        nested = _first_national_value(value, "amount", "value", "current", "balance")
        return _parse_national_amount(nested)

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


def _parse_national_datetime(value: Any) -> datetime | None:
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


def _raw_national_provider_account_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _national_provider_account_id(acct_data: dict[str, Any]) -> str | None:
    for key in ("provider_account_id", "key", "guid", "accountNumber", "referenceId", "primaryCardReferenceId"):
        value = _raw_national_provider_account_id(acct_data.get(key))
        if value:
            return value
    return None


def _national_account_external_id(acct_data: dict[str, Any]) -> str | None:
    external_id = str(acct_data.get("external_id") or "").strip()
    if external_id:
        return external_id
    provider_account_id = _national_provider_account_id(acct_data)
    if provider_account_id:
        return f"account:{provider_account_id}"
    return None


def _national_alternate_external_ids(
    acct_data: dict[str, Any],
    external_id: str | None,
) -> tuple[str, ...]:
    values = [
        acct_data.get("key"),
        acct_data.get("guid"),
        acct_data.get("accountNumber"),
        acct_data.get("referenceId"),
        acct_data.get("primaryCardReferenceId"),
        acct_data.get("provider_account_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _national_account_type(raw: dict[str, Any]) -> tuple[str, bool]:
    provider_type = str(_first_national_value(raw, "type", "businessType") or "").upper()
    sub_type = str(_first_national_value(raw, "subType", "investmentAccountType", "bncInvestmentType") or "").upper()
    text = " ".join(
        str(_localized_text(_first_national_value(raw, key)) or "")
        for key in (
            "type",
            "subType",
            "businessType",
            "investmentAccountType",
            "bncInvestmentType",
            "productName",
            "nickname",
            "institutionName",
        )
    ).lower()

    if provider_type == "CREDIT_CARD" or "credit card" in text or "mastercard" in text or "visa" in text:
        return "credit_card", True
    if provider_type in {"LINE_OF_CREDIT", "ALL_IN_ONE"} or "line of credit" in text or re.search(r"\bloc\b", text):
        return "loc", True
    if provider_type == "MORTGAGE" or "mortgage" in text:
        return "mortgage", True
    if provider_type == "LOAN" or "loan" in text:
        return "loan", True
    if "fhsa" in text or "celiapp" in text or sub_type == "CELIAPP":
        return "fhsa", False
    if "tfsa" in text or sub_type == "TFSA":
        return "tfsa", False
    if "rrsp" in text or re.search(r"\brsp\b", text) or sub_type == "RRSP":
        return "rrsp", False
    if "resp" in text or sub_type == "RESP":
        return "resp", False
    if "rrif" in text or "ferr" in text or sub_type == "FERR":
        return "rrif", False
    if "lira" in text:
        return "lira", False
    if provider_type in {"CHECKING", "BANKING"} or "chequing" in text or "checking" in text:
        return "chequing", False
    if provider_type == "SAVINGS" or "saving" in text or "savings" in text:
        return "savings", False
    if provider_type == "INVESTMENT":
        return "investment", False
    return "other", provider_type in {"PROPERTY"}


def _national_account_name(raw: dict[str, Any]) -> str:
    for key in ("nickname", "productName", "institutionName", "accountNumber"):
        value = _localized_text(raw.get(key))
        if value:
            return value
    return "National Bank Account"


def _national_transaction_description(raw: dict[str, Any]) -> str | None:
    values = [
        _localized_text(_first_national_value(raw, "description", "descriptionOrig")),
        raw.get("memo"),
    ]
    parts = [re.sub(r"\s+", " ", str(value)).strip() for value in values if value]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return " | ".join(dict.fromkeys(parts))


def _national_transaction_is_posted(raw: dict[str, Any]) -> bool:
    if raw.get("isHidden") is True:
        return False
    status = str(_first_national_value(raw, "status", "transactionStatus") or "").strip().lower()
    if not status:
        return True
    if any(token in status for token in ("pending", "authorized", "authorization")):
        return False
    return True


def _national_signed_amount(raw: dict[str, Any]) -> tuple[float, str]:
    amount = _parse_national_amount(_first_national_value(raw, "realAmount", "amount", "value")) or 0.0
    indicator = str(_first_national_value(raw, "type", "creditDebitIndicator", "direction") or "").upper()
    if indicator == "DEBIT" and amount > 0:
        amount = -amount
    if indicator == "CREDIT" and amount < 0:
        amount = abs(amount)
    currency = str(_first_national_value(raw, "_account_currency", "currency") or "CAD")
    return amount, currency


def _classify_national_transaction(raw: dict[str, Any], amount: float) -> str:
    text = " ".join(
        [
            str(_first_national_value(raw, "type", "categoryId") or ""),
            str(_national_transaction_description(raw) or ""),
        ]
    ).lower()
    if "interest" in text or "intérêt" in text:
        return "interest" if amount >= 0 else "interest_paid"
    if any(token in text for token in ("fee", "service charge", "nsf", "frais")):
        return "fee"
    if any(token in text for token in ("transfer", "e-transfer", "interac", "virement")):
        return "transfer"
    if any(token in text for token in ("deposit", "payroll", "paie", "dépôt")):
        return "deposit"
    if any(token in text for token in ("payment", "purchase", "withdrawal", "debit", "paiement", "achat", "retrait")):
        return "deposit" if amount > 0 else "withdrawal"
    return "deposit" if amount >= 0 else "withdrawal"


def _national_transaction_external_id(
    raw: dict[str, Any],
    account_external_id: str,
    amount: float,
    description: str | None,
    booked_at: datetime,
) -> str:
    for key in ("guid", "id", "transactionKey", "sequenceNumber", "operationNumber", "checkNumber"):
        value = raw.get(key)
        if value:
            return f"national_{value}"
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
    return f"national_{digest}"


def _normalize_national_transaction(raw: dict[str, Any], account_external_id: str) -> NormalizedTransaction | None:
    if not _national_transaction_is_posted(raw):
        return None

    booked_at = _parse_national_datetime(_first_national_value(raw, "effectiveDate", "createdDate", "date"))
    if booked_at is None:
        return None

    amount, currency = _national_signed_amount(raw)
    description = _national_transaction_description(raw)
    return NormalizedTransaction(
        external_id=_national_transaction_external_id(raw, account_external_id, amount, description, booked_at),
        date=booked_at,
        type=_classify_national_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class NationalBankConnector(DesktopVisibleAuthTransactionImportConnector):
    unexpected_payload_message = "Unexpected National Bank scraper response"

    def __init__(self) -> None:
        super().__init__(
            provider="national",
            module_path="app.scrapers.national",
            display_name="National Bank",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
            sync_windows: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for acct in accounts:
                    external_id = _national_account_external_id(acct)
                    if not external_id:
                        continue
                    alternate_external_ids = _national_alternate_external_ids(acct, external_id)
                    account_type, is_liability = _national_account_type(acct)
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=_national_account_name(acct),
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=NATIONAL_MAX_HISTORY_DAYS,
                        backfill_chunk_days=NATIONAL_BACKFILL_CHUNK_DAYS,
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
        account_external_id = _national_account_external_id(account_payload) or str(
            account_payload.get("external_id") or ""
        )
        return self._normalize_transaction_rows(
            raw_transactions,
            lambda raw: _normalize_national_transaction(raw, account_external_id),
        )

    def _build_from_payload(self, payload: dict[str, Any]) -> SyncResult:
        accounts_data: list[dict[str, Any]] = payload.get("accounts") or []
        transactions_data: dict[str, list[dict[str, Any]]] = payload.get("transactions") or {}
        transaction_fetch_succeeded_external_ids = set(
            payload.get("transaction_fetch_succeeded_accounts") or []
        )

        if not accounts_data:
            return SyncResult(status=SyncStatus.ERROR, message="No National Bank accounts returned")

        accounts: list[NormalizedAccount] = []
        account_by_external_id: dict[str, NormalizedAccount] = {}

        for acct_data in accounts_data:
            if not isinstance(acct_data, dict):
                continue

            external_id = _national_account_external_id(acct_data)
            alternate_external_ids = _national_alternate_external_ids(acct_data, external_id)
            account_type, is_liability = _national_account_type(acct_data)
            currency = _first_national_value(acct_data, "currency", "primaryCurrency", "secondaryCurrency") or "CAD"
            balance = None
            if acct_data.get("balance_is_fresh", True):
                balance = _parse_national_amount(
                    _first_national_value(
                        acct_data,
                        "availableBalance",
                        "balance",
                        "primaryBalance",
                        "secondaryBalance",
                        "cashBalance",
                    )
                )

            account = NormalizedAccount(
                name=_national_account_name(acct_data),
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
            transaction_normalizer=lambda raw, external_id, _account: _normalize_national_transaction(raw, external_id),
        )
