from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.persistence import persist_sync_result
from app.connectors.sync_context import get_connector_sync_context
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.database import async_session
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)

from app.scrapers.rbc import (
    RBC_BACKFILL_DAYS,
    RBC_CC_BACKFILL_CHUNK_DAYS,
    RBC_INCREMENTAL_DAYS,
    RBC_LOC_HISTORY_DAYS,
)

RBC_PAGE_TRANSITION_MESSAGE = (
    "RBC refreshed the page during sync. Please try syncing again."
)


def _parse_rbc_date(val: Any) -> datetime | None:
    if not val:
        return None
    if isinstance(val, str):
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


def _parse_rbc_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    if text.startswith("-"):
        negative = True

    text = text.lstrip("+-")
    for token in ("$", ",", "CAD", "USD", "cad", "usd"):
        text = text.replace(token, "")
    text = text.strip()
    if not text:
        return None

    try:
        amount = float(text)
    except (TypeError, ValueError):
        return None
    return -abs(amount) if negative else amount


def _is_rbc_page_transition_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(
        pattern in text
        for pattern in (
            "execution context was destroyed",
            "most likely because of a navigation",
            "cannot find context with specified id",
            "frame was detached",
            "rbc refreshed the page during sync",
        )
    )


def _build_rbc_page_transition_result() -> SyncResult:
    return SyncResult(
        status=SyncStatus.NETWORK_ERROR,
        message=RBC_PAGE_TRANSITION_MESSAGE,
    )


def _rbc_description_text(raw: dict) -> str | None:
    desc_raw = raw.get("description")
    if isinstance(desc_raw, list):
        parts = [str(part).strip() for part in desc_raw if str(part).strip()]
        desc_raw = " ".join(parts) if parts else None
    elif desc_raw is not None:
        desc_raw = str(desc_raw).strip() or None

    merchant_name = raw.get("merchantName")
    if merchant_name is not None:
        merchant_name = str(merchant_name).strip() or None

    narrative = raw.get("narrative")
    if narrative is not None:
        narrative = str(narrative).strip() or None

    return desc_raw or merchant_name or narrative


def _classify_rbc_transaction(raw: dict, category: str, amount: float) -> str:
    desc = (_rbc_description_text(raw) or "").lower()

    if (
        category in {"linesLoans", "mortgages"}
        and amount > 0
        and any(kw in desc for kw in ["interest payment", "interest paid", "intérêt"])
    ):
        return "interest_paid"
    if any(kw in desc for kw in ["interest", "intérêt"]):
        return "interest"
    if "dividend" in desc:
        return "dividend"
    if any(kw in desc for kw in ["fee", "frais", "service charge", "monthly plan"]):
        return "fee"
    # "Investment" in RBC = incoming transfer from another institution (e.g. Tangerine)
    if desc.strip() == "investment":
        return "transfer"
    if any(kw in desc for kw in ["e-transfer", "interac"]):
        return "transfer"
    if any(kw in desc for kw in ["transfer", "transfert"]):
        return "transfer"
    if any(kw in desc for kw in ["loan payment", "mortgage payment"]):
        return "withdrawal" if amount < 0 else "deposit"
    if any(kw in desc for kw in ["payroll", "direct deposit", "dépôt direct"]):
        return "deposit"
    if any(kw in desc for kw in ["payment", "paiement"]):
        return "deposit" if amount > 0 else "withdrawal"
    return "deposit" if amount >= 0 else "withdrawal"


def _rbc_fingerprint(date_str: str, amount: float, description: str) -> str:
    raw = f"{date_str}|{round(abs(amount), 2)}|{(description or '').strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_rbc_transaction(raw: dict, category: str) -> NormalizedTransaction | None:
    txn_id = (
        raw.get("transactionId")
        or raw.get("id")
        or raw.get("referenceNumber")
        or raw.get("transactionReferenceNumber")
    )
    if not txn_id:
        return None

    date_str = (
        raw.get("bookingDate")
        or raw.get("transactionDate")
        or raw.get("txnDate")
        or raw.get("postedDate")
        or raw.get("date")
        or raw.get("effectiveDate")
    )
    date = _parse_rbc_date(date_str)
    if date is None:
        return None

    amount_raw = (
        raw.get("amount")
        if raw.get("amount") is not None
        else raw.get("transactionAmount")
    )
    if isinstance(amount_raw, dict):
        amount_value = (
            amount_raw.get("value")
            if amount_raw.get("value") is not None
            else amount_raw.get("amount")
        )
        amount = _parse_rbc_amount(amount_value)
        if amount is None:
            return None
        currency = amount_raw.get("currencyCode") or raw.get("currency") or "CAD"
    else:
        amount = _parse_rbc_amount(amount_raw)
        if amount is None:
            return None
        currency = raw.get("currencyCode") or raw.get("currency") or "CAD"

    indicator = (
        raw.get("creditDebitIndicator")
        or raw.get("debitCreditIndicator")
        or raw.get("transactionType")
        or raw.get("txnType")
        or ""
    ).upper()
    if indicator in ("DEBIT", "D") and amount > 0:
        amount = -amount

    description = _rbc_description_text(raw)
    if not description:
        return None

    fingerprint = _rbc_fingerprint(date_str, amount, description or "")
    return NormalizedTransaction(
        external_id=f"rbc_{fingerprint}",
        date=date,
        type=_classify_rbc_transaction(raw, category, amount),
        amount=amount,
        currency=currency,
        description=description,
    )


class RBCConnector(DesktopVisibleAuthTransactionImportConnector):
    def __init__(self) -> None:
        super().__init__(
            provider="rbc",
            module_path="app.scrapers.rbc",
            display_name="RBC",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict]) -> dict[str, dict[str, Any]]:
            sync_state: dict[str, dict[str, Any]] = {}
            async with async_session() as db:
                for acct in accounts:
                    external_id = acct.get("external_id")
                    if not external_id:
                        continue
                    category = acct.get("category")
                    backfill_days = RBC_LOC_HISTORY_DAYS if category == "linesLoans" else RBC_BACKFILL_DAYS
                    backfill_start_date = (
                        date.today() - timedelta(days=RBC_LOC_HISTORY_DAYS)
                        if category == "linesLoans"
                        else None
                    )
                    chunk_days = (
                        RBC_CC_BACKFILL_CHUNK_DAYS
                        if category == "creditCards"
                        else backfill_days
                    )
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=external_id,
                        account_name=acct.get("name") or "Account",
                        account_type=acct.get("account_type"),
                        is_liability=bool(acct.get("is_liability")),
                        backfill_days=backfill_days,
                        backfill_chunk_days=chunk_days,
                        incremental_days=RBC_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                        backfill_start_date=backfill_start_date,
                    )
                    sync_state[external_id] = plan.as_sync_state()
                await db.commit()
            return sync_state
        return resolver

    async def _saved_artifact_sync_kwargs(self, user_id: int) -> dict[str, Any]:
        kwargs = await super()._saved_artifact_sync_kwargs(user_id)
        kwargs["account_sync_callback"] = self._make_account_sync_callback(user_id)
        return kwargs

    def _make_account_sync_callback(self, user_id: int):
        async def callback(accounts: list[dict]) -> None:
            context = get_connector_sync_context()
            if not (context and context.add_flow):
                return
            result = self._build_from_payload({"accounts": accounts, "transactions": {}})
            async with async_session() as db:
                async with sqlite_write_gate(), db.begin():
                    await persist_sync_result(
                        db,
                        user_id,
                        provider=self.provider,
                        provider_display_name=self.display_name,
                        provider_type="scraper",
                        result=result,
                        pending_add=True,
                        institution_id=context.institution_id,
                    )
        return callback

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict],
    ) -> list[NormalizedTransaction]:
        category = account_payload.get("category") or ""
        return self._normalize_transaction_rows(
            raw_transactions,
            lambda raw: _normalize_rbc_transaction(raw, category),
        )

    def _preprocess_saved_artifact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if _is_rbc_page_transition_error(payload.get("message")):
            payload["status"] = "network_error"
            payload["message"] = RBC_PAGE_TRANSITION_MESSAGE
        return payload

    def _sync_exception_result(self, user_id: int, exc: Exception) -> SyncResult | None:
        if not _is_rbc_page_transition_error(str(exc)):
            return None
        self._log_sync_exception(
            user_id,
            exc,
            stage="sync transient page transition",
            level="warning",
        )
        return _build_rbc_page_transition_result()

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
        category_by_external_id: dict[str, str] = {}

        for acct_data in accounts_data:
            name = acct_data["name"]
            acct_type = acct_data.get("account_type") or "savings"
            is_liability = bool(acct_data.get("is_liability", False))
            currency = acct_data.get("currency") or "CAD"
            external_id = acct_data.get("external_id")
            try:
                parsed_balance = float(acct_data.get("balance"))
            except (TypeError, ValueError):
                parsed_balance = None
            balance_authoritative = parsed_balance is not None and math.isfinite(parsed_balance)

            account = NormalizedAccount(
                name=name,
                account_type=acct_type,
                external_id=external_id,
                currency=currency,
                is_liability=is_liability,
                balance=abs(parsed_balance) if balance_authoritative else None,
                balance_authoritative=balance_authoritative,
                holdings_authoritative=True,
            )
            accounts.append(account)
            if external_id:
                account_by_external_id[external_id] = account
                category_by_external_id[external_id] = acct_data.get("category") or ""

        return self._build_result_from_accounts_and_transactions(
            accounts=accounts,
            account_by_external_id=account_by_external_id,
            transactions_data=transactions_data,
            transaction_fetch_succeeded_external_ids=transaction_fetch_succeeded_external_ids,
            transaction_normalizer=lambda raw, external_id, _account: _normalize_rbc_transaction(
                raw,
                category_by_external_id.get(external_id, ""),
            ),
        )
