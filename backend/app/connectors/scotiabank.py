from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.connectors.desktop_visible_auth import DesktopVisibleAuthTransactionImportConnector
from app.connectors.sync_modes import DEFAULT_BACKFILL_MONTHS
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
)
from app.database import async_session
from app.services.transaction_import import (
    DEFAULT_INCREMENTAL_DAYS,
    DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    plan_account_transaction_import,
)
from app.services.user_utils import get_user_local_today_from_db


def _scotia_month_floor(months: int, *, today: date | None = None) -> date:
    months = max(int(months or 1), 1)
    today = today or date.today()
    start_month = today.month - (months - 1)
    start_year = today.year
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return date(start_year, start_month, 1)


def _scotia_month_windows(months: int, *, today: date | None = None) -> tuple[tuple[date, date], ...]:
    today = today or date.today()
    start_date = _scotia_month_floor(months, today=today)
    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= today:
        month_end = date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
        window_end = min(month_end, today)
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return tuple(windows)


def _scotia_range_from_sync_state(value: Any) -> tuple[date, date] | None:
    if not isinstance(value, dict):
        return None
    try:
        start = date.fromisoformat(str(value.get("start") or "")[:10])
        end = date.fromisoformat(str(value.get("end") or "")[:10])
    except ValueError:
        return None
    if start > end:
        return None
    return start, end


def _scotia_range_covers(outer: tuple[date, date], inner: tuple[date, date]) -> bool:
    return outer[0] <= inner[0] and outer[1] >= inner[1]


def _scotia_month_window_bounded(window: tuple[date, date], *, today: date) -> bool:
    start, end = window
    backfill_start = _scotia_month_floor(DEFAULT_BACKFILL_MONTHS, today=today)
    month_end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return start in {backfill_start, date(start.year, start.month, 1)} and end == min(month_end, today)


def _scotia_pending_month_windows(sync_state: dict[str, Any], *, today: date | None = None) -> list[dict[str, str]]:
    today = today or date.today()
    pending = [
        parsed
        for parsed in (
            _scotia_range_from_sync_state(value)
            for value in (sync_state.get("pending_backfill_windows") or [])
        )
        if parsed is not None
    ]
    if pending and all(_scotia_month_window_bounded(window, today=today) for window in pending):
        return [{"start": start.isoformat(), "end": end.isoformat()} for start, end in pending]

    completed = [
        parsed
        for parsed in (
            _scotia_range_from_sync_state(value)
            for value in (sync_state.get("completed_backfill_windows") or [])
        )
        if parsed is not None
    ]
    return [
        {"start": start.isoformat(), "end": end.isoformat()}
        for start, end in _scotia_month_windows(DEFAULT_BACKFILL_MONTHS, today=today)
        if not any(_scotia_range_covers(completed_range, (start, end)) for completed_range in completed)
    ]


def _parse_scotia_date(val: Any) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_scotia_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _classify_scotia_transaction(raw: dict, amount: float) -> str:
    txn_type = str(raw.get("type") or "").upper()
    indicator = str(raw.get("transactionType") or "").upper()

    if txn_type == "PURCHASE":
        return "deposit" if amount > 0 else "withdrawal"
    if txn_type == "PAYMENT":
        # Payment to a credit card / LoC comes in as a CREDIT that reduces
        # the liability balance. Mirror RBC's payment handling: positive
        # amounts are deposits (inflow against the account), negative are
        # withdrawals.
        return "deposit" if amount > 0 else "withdrawal"
    if txn_type == "REDEMPTIONS":
        return "deposit"
    if txn_type == "CREDIT ADJUSTMENT":
        return "deposit"
    if indicator == "CREDIT":
        return "deposit"
    if indicator == "DEBIT":
        return "withdrawal"
    return "withdrawal"


def _normalize_scotia_transaction(raw: dict) -> NormalizedTransaction | None:
    key = raw.get("transactionKey")
    if not key:
        return None

    date = _parse_scotia_date(raw.get("transactionDate"))
    if date is None:
        return None

    amount_obj = raw.get("transactionAmount") if isinstance(raw.get("transactionAmount"), dict) else {}
    amount = _parse_scotia_float(amount_obj.get("amount"))
    currency = amount_obj.get("currencyCode") or "CAD"

    indicator = str(raw.get("transactionType") or "").upper()
    if indicator == "DEBIT" and amount > 0:
        amount = -amount

    description_parts = [
        str(raw.get("description") or "").strip(),
        str(raw.get("subDescription") or "").strip(),
    ]
    description = " ".join(p for p in description_parts if p) or None

    merchant = raw.get("merchant") if isinstance(raw.get("merchant"), dict) else {}
    mcc = str(merchant.get("categoryCode") or "").strip() or None
    provider_recurring = (
        True
        if str(raw.get("recurringPaymentIndicator") or "").strip().upper() == "RECURRING"
        else None
    )

    return NormalizedTransaction(
        external_id=f"scotiabank_{key}",
        date=date,
        type=_classify_scotia_transaction(raw, amount),
        amount=amount,
        currency=currency,
        description=description,
        mcc=mcc,
        provider_recurring=provider_recurring,
    )


class ScotiabankConnector(DesktopVisibleAuthTransactionImportConnector):
    transaction_window_account_name = "Scotiabank Account"
    transaction_window_failed_message = "Scotiabank transaction window failed."

    def __init__(self) -> None:
        super().__init__(
            provider="scotiabank",
            module_path="app.scrapers.scotiabank",
            display_name="Scotiabank",
        )

    def _make_mode_resolver(self, user_id: int):
        async def resolver(accounts: list[dict]) -> dict[str, dict[str, Any]]:
            sync_windows: dict[str, dict[str, Any]] = {}
            async with async_session() as db:
                today = await get_user_local_today_from_db(db, user_id)
                backfill_start = _scotia_month_floor(DEFAULT_BACKFILL_MONTHS, today=today)
                backfill_days = max((today - backfill_start).days, 1)
                for acct in accounts:
                    ext = acct.get("external_id")
                    if not ext:
                        continue
                    plan = await plan_account_transaction_import(
                        db,
                        user_id=user_id,
                        provider=self.provider,
                        account_external_id=ext,
                        account_name=acct.get("name") or "Scotiabank Account",
                        account_type=acct.get("account_type"),
                        is_liability=bool(acct.get("is_liability")),
                        backfill_days=backfill_days,
                        backfill_chunk_days=31,
                        incremental_days=DEFAULT_INCREMENTAL_DAYS,
                        overlap_days=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
                        today=today,
                    )
                    state = plan.as_sync_state()
                    if state.get("mode") == "backfill":
                        state["pending_backfill_windows"] = _scotia_pending_month_windows(state, today=today)
                    sync_windows[ext] = state
                await db.commit()
            return sync_windows
        return resolver

    def _normalize_window_transactions(
        self,
        *,
        account_payload: dict[str, Any],
        raw_transactions: list[dict],
    ) -> list[NormalizedTransaction]:
        del account_payload
        return self._normalize_transaction_rows(raw_transactions, _normalize_scotia_transaction)

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
            name = str(acct_data.get("name") or "Scotiabank Account")
            acct_type = acct_data.get("account_type") or "chequing"
            is_liability = bool(acct_data.get("is_liability", False))
            currency = acct_data.get("currency") or "CAD"
            external_id = acct_data.get("external_id")
            if not external_id:
                continue

            balance = _parse_scotia_float(acct_data.get("balance"), default=float("nan"))
            balance_authoritative = math.isfinite(balance)
            account = NormalizedAccount(
                name=name,
                account_type=acct_type,
                external_id=external_id,
                currency=currency,
                is_liability=is_liability,
                balance=abs(balance) if balance_authoritative else None,
                balance_authoritative=balance_authoritative,
                holdings_authoritative=True,
            )
            accounts.append(account)
            if external_id:
                account_by_external_id[external_id] = account

        if not accounts:
            return SyncResult(status=SyncStatus.ERROR, message="No valid accounts returned")

        return self._build_result_from_accounts_and_transactions(
            accounts=accounts,
            account_by_external_id=account_by_external_id,
            transactions_data=transactions_data,
            transaction_fetch_succeeded_external_ids=transaction_fetch_succeeded_external_ids,
            transaction_normalizer=lambda raw, _external_id, _account: _normalize_scotia_transaction(raw),
        )
