from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.connectors.payload_utils import first_present_value as _first_td_value
from app.database import async_session
from app.scrapers.td import TD_BACKFILL_CHUNK_DAYS, TD_MAX_HISTORY_DAYS
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)


def _raw_td_provider_account_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("account:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _td_stable_account_external_id(provider_account_id: str) -> str:
    return f"account:{provider_account_id}"


def _td_provider_account_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = _raw_td_provider_account_id(
        acct_data.get("provider_account_id")
        or acct_data.get("accountIdentifier")
    )
    if provider_account_id:
        return provider_account_id

    bank = str(acct_data.get("bankNum") or "").strip()
    branch = str(acct_data.get("branchNum") or "").strip()
    account_num = str(acct_data.get("accountNum") or acct_data.get("accountNumber") or "").strip()
    if bank and branch and account_num:
        return f"{bank}:{branch}:{account_num}"
    return _raw_td_provider_account_id(acct_data.get("external_id"))


def _td_account_external_id(acct_data: dict[str, Any]) -> str | None:
    provider_account_id = _td_provider_account_id(acct_data)
    if provider_account_id:
        return _td_stable_account_external_id(provider_account_id)
    return acct_data.get("external_id")


def _td_alternate_external_ids(acct_data: dict[str, Any], external_id: str | None) -> tuple[str, ...]:
    values = [
        acct_data.get("accountKey"),
        acct_data.get("accountNum"),
        acct_data.get("accountNumber"),
        acct_data.get("accountIdentifier"),
        acct_data.get("summary_external_id"),
        acct_data.get("external_id") if acct_data.get("external_id") != external_id else None,
    ]
    return tuple(dict.fromkeys(str(value) for value in values if value and str(value) != external_id))


def _td_account_open_date(acct_data: dict[str, Any]) -> date | None:
    raw = _first_td_value(acct_data, "openDt", "accountOpeningDate")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _td_parse_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, dict):
        nested = _first_td_value(value, "amount", "amt", "value")
        return _td_parse_amount(nested)

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


def _td_parse_datetime(value: Any) -> datetime | None:
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

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _td_account_type(raw: dict[str, Any]) -> tuple[str, bool]:
    text = " ".join(
        str(_first_td_value(raw, key) or "")
        for key in (
            "accountName",
            "accountDesc",
            "accountDisplayName",
            "productCd",
            "planCd",
            "accountApplicationCd",
            "product_group_type",
        )
    ).lower()
    product_group_type = str(raw.get("product_group_type") or "").upper()

    if "mortgage" in text:
        return "mortgage", True
    if "line of credit" in text or re.search(r"\bloc\b", text) or "uloc" in text:
        return "loc", True
    if product_group_type == "CREDIT" or any(token in text for token in ("credit card", "visa", "mastercard")):
        return "credit_card", True
    if product_group_type == "LOAN_MORTGAGE":
        return "loan", True
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
    if "chequing" in text or "checking" in text:
        return "chequing", False
    if "saving" in text or product_group_type == "BANKING":
        return "savings", False
    return "other", False


def _td_transaction_description(raw: dict[str, Any]) -> str | None:
    value = (
        raw.get("description")
        or raw.get("transactionDescription")
        or raw.get("descriptionLine")
        or raw.get("merchantName")
        or raw.get("name")
    )
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _td_transaction_is_posted(raw: dict[str, Any]) -> bool:
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


def _td_signed_amount(raw: dict[str, Any]) -> tuple[float, str]:
    withdrawal_amount = _td_parse_amount(
        _first_td_value(raw, "withdrawalAmt", "withdrawalAmount", "debitedAmount")
    )
    if withdrawal_amount is not None and abs(withdrawal_amount) > 0:
        return -abs(withdrawal_amount), str(raw.get("_account_currency") or "CAD")

    deposit_amount = _td_parse_amount(
        _first_td_value(raw, "depositAmt", "depositAmount", "creditedAmount")
    )
    if deposit_amount is not None and abs(deposit_amount) > 0:
        return abs(deposit_amount), str(raw.get("_account_currency") or "CAD")

    amount = _td_parse_amount(_first_td_value(raw, "amount", "debitOrCredit")) or 0.0
    return amount, str(raw.get("_account_currency") or "CAD")


def _classify_td_transaction(raw: dict[str, Any], amount: float) -> str:
    description = (_td_transaction_description(raw) or "").lower()
    raw_type = str(_first_td_value(raw, "transactionType", "type") or "").lower()
    raw_subtype = str(_first_td_value(raw, "transactionSubType", "subType") or "").lower()
    text = f"{raw_type} {raw_subtype} {description}"

    if "open account" in text:
        return "other"
    if "interest" in text:
        return "interest" if amount >= 0 else "interest_paid"
    if any(token in text for token in ("fee", "service charge", "annual fee", "over limit")):
        return "fee"
    if any(token in text for token in ("transfer", "interac", "e-transfer")):
        return "transfer"
    if raw_type == "bpay" or "bill payment" in text:
        return "deposit" if amount > 0 else "withdrawal"
    if raw_type == "pos" or any(token in text for token in ("purchase", "debit purchase")):
        return "deposit" if amount > 0 else "withdrawal"
    if any(token in text for token in ("payroll", "direct deposit", "deposit", "open account")) or raw_type == "dep":
        return "deposit"
    if "payment" in text:
        return "deposit" if amount > 0 else "withdrawal"
    return "deposit" if amount >= 0 else "withdrawal"


def _normalize_td_transaction(raw: dict[str, Any]) -> NormalizedTransaction | None:
    if not _td_transaction_is_posted(raw):
        return None

    transaction_id = _first_td_value(raw, "transactionId", "eventId", "id")
    if not transaction_id:
        return None

    booked_at = _td_parse_datetime(
        _first_td_value(raw, "eventDttm", "postedOnDate", "date", "transactionDate")
    )
    if booked_at is None:
        return None

    amount, currency = _td_signed_amount(raw)
    description = _td_transaction_description(raw)
    account_key = raw.get("_account_key")
    external_id = (
        f"td_{account_key}_{transaction_id}"
        if account_key is not None
        else f"td_{transaction_id}"
    )

    return NormalizedTransaction(
        external_id=external_id,
        date=booked_at,
        type=_classify_td_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class TDConnector(DesktopVisibleAuthTransactionImportConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="td",
            module_path="app.scrapers.td",
            display_name="TD",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict[str, Any]]) -> dict[str, dict[str, str | int | None]]:
            sync_windows: dict[str, dict[str, str | int | None]] = {}
            async with async_session() as db:
                for account in accounts:
                    external_id = _td_account_external_id(account)
                    if not external_id:
                        continue
                    alternate_external_ids = _td_alternate_external_ids(account, external_id)
                    account_type, is_liability = _td_account_type(account)
                    account_name = (
                        _first_td_value(
                            account,
                            "accountName",
                            "accountDesc",
                            "accountDisplayName",
                        )
                        or "TD Account"
                    )
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=str(account_name).strip() or "TD Account",
                        account_type=account_type,
                        is_liability=is_liability,
                        backfill_days=TD_MAX_HISTORY_DAYS,
                        backfill_chunk_days=TD_BACKFILL_CHUNK_DAYS,
                        incremental_days=DEFAULT_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                        backfill_start_date=_td_account_open_date(account),
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
        return self._normalize_transaction_rows(raw_transactions, _normalize_td_transaction)

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

            external_id = _td_account_external_id(acct_data)
            alternate_external_ids = _td_alternate_external_ids(acct_data, external_id)
            account_type, is_liability = _td_account_type(acct_data)
            name = (
                _first_td_value(
                    acct_data,
                    "accountName",
                    "accountDesc",
                    "accountDisplayName",
                )
                or "TD Account"
            )
            balance_source = (
                acct_data.get("currentBalance")
                or acct_data.get("balanceAmt")
                or acct_data.get("accountBalance")
            )
            balance = _td_parse_amount(balance_source)
            currency = (
                _first_td_value(
                    balance_source if isinstance(balance_source, dict) else {},
                    "currencyCd",
                    "ccy",
                    "currency",
                )
                or "CAD"
            )

            account = NormalizedAccount(
                name=str(name).strip() or "TD Account",
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
            transaction_normalizer=lambda raw, _external_id, _account: _normalize_td_transaction(raw),
        )
