from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import httpx

from app.connectors.api_base import ApiProviderAuthRequired, ApiProviderConnectorBase
from app.connectors.connector_logging import log_connector_event
from app.connectors.sync_modes import (
    AccountSyncWindow,
    sync_window_utc_bounds,
    walk_transaction_backfill_windows,
)
from app.connectors.types import (
    NormalizedAccount,
    NormalizedTransaction,
    SyncResult,
    SyncStatus,
    account_result_key,
)
from app.connectors.wise_helpers import CURRENCY_NAMES, map_wise_type
from app.database import async_session
from app.services.connection_auth_storage import get_api_credentials

API_BASE = "https://api.wise.com"
logger = logging.getLogger("breaktwenty.connectors.wise")
WISE_STATEMENT_CHUNK_DAYS = 460
WISE_EMPTY_HISTORY_STOP_CHUNKS = 2
WISE_MAX_HISTORY_DAYS = 3650


async def get_api_token(user_id: int) -> str | None:
    async with async_session() as db:
        credentials = await get_api_credentials(
            db,
            user_id,
            "wise",
            setting_keys=("wise_api_token",),
        )
        if credentials.get("wise_api_token"):
            return credentials["wise_api_token"]
    return None


def _parse_wise_date(date_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    try:
        parsed = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wise_balance_ref(balance_id: object) -> str:
    raw = str(balance_id or "")
    return f"...{raw[-4:]}" if len(raw) >= 4 else "unknown"


class WiseConnector(ApiProviderConnectorBase):
    def __init__(self) -> None:
        super().__init__(provider="wise", display_name="Wise")

    async def _sync_impl(self, user_id: int) -> SyncResult:
        token = await get_api_token(user_id)
        if not token:
            return self.missing_credentials_result(
                user_id=user_id,
                message="Wise API token not configured.",
            )

        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await self.request(
                    client,
                    "GET",
                    f"{API_BASE}/v2/profiles",
                    headers=headers,
                    timeout=20,
                    user_id=user_id,
                    stage="profiles list",
                    retry_attempts=2,
                )
                profiles = resp.json()
                self.record_diagnostics_fetch(
                    stage="profiles.list",
                    url=f"{API_BASE}/v2/profiles",
                    status=resp.status_code,
                    payload=profiles,
                    records=profiles,
                    record_source="profiles",
                )
                if not profiles:
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="profiles empty",
                        level="warning",
                        user_id=user_id,
                    )
                    return SyncResult(status=SyncStatus.ERROR, message="No Wise profiles found")

                profile = next((p for p in profiles if p.get("type") == "PERSONAL"), profiles[0])
                profile_id = profile.get("id")
                if not profile_id:
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="profile id missing",
                        level="warning",
                        user_id=user_id,
                    )
                    return SyncResult(status=SyncStatus.ERROR, message="No valid Wise profile found")

                resp = await self.request(
                    client,
                    "GET",
                    f"{API_BASE}/v4/profiles/{profile_id}/balances?types=STANDARD",
                    headers=headers,
                    timeout=15,
                    user_id=user_id,
                    stage="balances list",
                    retry_attempts=2,
                )
                balances = resp.json()
                self.record_diagnostics_fetch(
                    stage="accounts.list",
                    url=f"{API_BASE}/v4/profiles/{profile_id}/balances",
                    status=resp.status_code,
                    params={"types": "STANDARD"},
                    payload={"balances": balances},
                    records=balances,
                    record_source="balances",
                )
                if not balances:
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="balances empty",
                        level="warning",
                        user_id=user_id,
                    )
                    return SyncResult(status=SyncStatus.ERROR, message="No balances found")
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="balances fetched",
                    user_id=user_id,
                    balances=len(balances),
                    debug=True,
                )

                accounts: list[NormalizedAccount] = []
                transactions_by_account: dict[str, list[NormalizedTransaction]] = {}
                transaction_fetch_succeeded_accounts: set[str] = set()
                all_transactions: list[NormalizedTransaction] = []
                fetch_transactions = self.should_fetch_transactions()
                persist_transaction_windows = self.should_persist_transaction_windows()
                transaction_fetch_failed = False

                for bal in balances:
                    balance_id = bal.get("id")
                    currency = bal.get("currency")
                    amount_data = bal.get("amount", {}) or {}
                    if not balance_id or not currency:
                        continue

                    balance_value = _to_float(amount_data.get("value"), default=float("nan"))
                    balance_authoritative = math.isfinite(balance_value)
                    account_name = CURRENCY_NAMES.get(currency, currency)
                    account = NormalizedAccount(
                        name=account_name,
                        account_type="chequing",
                        external_id=f"balance:{balance_id}",
                        currency=currency,
                        balance=balance_value if balance_authoritative else None,
                        balance_authoritative=balance_authoritative,
                        holdings_authoritative=True,
                    )
                    accounts.append(account)

                    balance_ref = _wise_balance_ref(balance_id)
                    account_transactions: list[NormalizedTransaction] = []
                    transaction_fetch_succeeded = False
                    window_log: dict | None = None
                    if fetch_transactions:
                        if persist_transaction_windows:
                            async def fetch_window(sync_window: AccountSyncWindow):
                                return await self._fetch_balance_transactions(
                                    client,
                                    headers,
                                    profile_id,
                                    bal,
                                    sync_window=sync_window,
                                    user_id=user_id,
                                )

                            account_transactions, transaction_fetch_succeeded, window_log = (
                                await self.fetch_and_persist_transaction_import_windows(
                                    user_id=user_id,
                                    account_external_id=account.external_id or "",
                                    account_name=account.name,
                                    account_type=account.account_type,
                                    is_liability=account.is_liability,
                                    fetch_window=fetch_window,
                                    backfill_days=WISE_MAX_HISTORY_DAYS,
                                    backfill_chunk_days=WISE_STATEMENT_CHUNK_DAYS,
                                    failure_message="Wise transaction history window did not finish.",
                                )
                            )
                        else:
                            sync_window = await self._transaction_sync_window(user_id, account.external_id)
                            window_log = sync_window.as_dict()
                            log_connector_event(
                                logger,
                                provider=self.provider,
                                stage="balance sync start",
                                user_id=user_id,
                                balance=balance_ref,
                                currency=currency,
                                mode=sync_window.mode,
                                window=window_log,
                                debug=True,
                            )
                            account_transactions, transaction_fetch_succeeded = await self._fetch_balance_transactions(
                                client,
                                headers,
                                profile_id,
                                bal,
                                sync_window=sync_window,
                                user_id=user_id,
                            )
                    account_key = account_result_key(account)
                    transactions_by_account[account_key] = account_transactions
                    if transaction_fetch_succeeded:
                        transaction_fetch_succeeded_accounts.add(account_key)
                    elif fetch_transactions:
                        transaction_fetch_failed = True
                    all_transactions.extend(account_transactions)
                    log_connector_event(
                        logger,
                        provider=self.provider,
                        stage="balance sync result",
                        user_id=user_id,
                        balance=balance_ref,
                        currency=currency,
                        mode=window_log.get("mode") if isinstance(window_log, dict) else None,
                        window=window_log,
                        transactions=len(account_transactions),
                        transactions_fetched=transaction_fetch_succeeded,
                        debug=True,
                    )

                if persist_transaction_windows and transaction_fetch_failed:
                    return SyncResult(
                        status=SyncStatus.NETWORK_ERROR,
                        message="Wise transaction history did not finish. Try syncing again.",
                    )

                return SyncResult(
                    status=SyncStatus.OK,
                    accounts=accounts,
                    transactions=all_transactions,
                    transactions_by_account=transactions_by_account,
                    transaction_fetch_succeeded_accounts=transaction_fetch_succeeded_accounts,
                )
        except Exception as e:
            return self.exception_result(user_id, e)

    async def _fetch_balance_transactions(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        profile_id: int,
        balance: dict,
        *,
        sync_window: AccountSyncWindow,
        user_id: int | None = None,
    ) -> tuple[list[NormalizedTransaction], bool]:
        balance_id = balance.get("id")
        currency = balance.get("currency")
        if not balance_id or not currency:
            return [], False

        now = datetime.now(timezone.utc)
        mode = sync_window.mode
        window_start, window_end = sync_window_utc_bounds(sync_window, now=now)
        seen_external_ids: set[str] = set()
        transactions: list[NormalizedTransaction] = []
        fetch_succeeded = True
        balance_ref = _wise_balance_ref(balance_id)

        async def fetch_statement_chunk(
            chunk_start: datetime,
            chunk_end: datetime,
        ) -> tuple[list[dict], bool]:
            interval_start = chunk_start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            interval_end = chunk_end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            try:
                statement_url = (
                    f"{API_BASE}/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json"
                )
                statement_params = {
                    "currency": currency,
                    "intervalStart": interval_start,
                    "intervalEnd": interval_end,
                    "type": "COMPACT",
                }
                if hasattr(client, "request"):
                    resp = await self.request(
                        client,
                        "GET",
                        statement_url,
                        headers=headers,
                        params=statement_params,
                        timeout=30,
                        user_id=user_id,
                        stage="statement",
                        retry_attempts=2,
                    )
                else:
                    resp = await client.get(
                        statement_url,
                        headers=headers,
                        params=statement_params,
                        timeout=30,
                    )
                    status_code = getattr(resp, "status_code", 200)
                    if status_code in {401, 403}:
                        raise ApiProviderAuthRequired(f"statement returned HTTP {status_code}")
                    resp.raise_for_status()
                data = resp.json()
                raw_transactions = data.get("transactions", []) or []
                self.record_diagnostics_fetch(
                    stage="transactions.window",
                    url=f"{API_BASE}/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
                    status=getattr(resp, "status_code", 200),
                    params={
                        "currency": currency,
                        "intervalStart": interval_start,
                        "intervalEnd": interval_end,
                        "type": "COMPACT",
                    },
                    payload=data,
                    records=raw_transactions,
                    record_source="transactions",
                    account_ref=balance_id,
                    window={
                        "mode": mode,
                        "intervalStart": interval_start,
                        "intervalEnd": interval_end,
                    },
                )
            except ApiProviderAuthRequired:
                raise
            except Exception as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                log_connector_event(
                    logger,
                    provider=self.provider,
                    stage="statement failed",
                    level="warning",
                    user_id=user_id,
                    balance=balance_ref,
                    currency=currency,
                    mode=mode,
                    window=sync_window.as_dict(),
                    status_code=status_code,
                    error=type(e).__name__,
                    message=str(e),
                )
                return [], False

            return raw_transactions, True

        def append_transactions(raw_transactions: list[dict]) -> bool:
            appended_any = False

            for tx in raw_transactions:
                ref = tx.get("referenceNumber", "")
                if not ref:
                    continue

                external_id = f"wise_{ref}"
                if external_id in seen_external_ids:
                    continue

                details = tx.get("details", {}) or {}
                details_type = details.get("type", "")
                tx_type = tx.get("type", "")
                amount_info = tx.get("amount", {}) or {}
                amount_value = _to_float(amount_info.get("value", 0) or 0)

                mapped_type = map_wise_type(details_type, tx_type, amount_value)
                if not mapped_type:
                    continue

                tx_date = _parse_wise_date(tx.get("date", ""))
                if tx_date is None:
                    continue

                fee_info = tx.get("totalFees", {}) or {}
                commission = abs(_to_float(fee_info.get("value", 0) or 0)) or None

                transactions.append(
                    NormalizedTransaction(
                        external_id=external_id,
                        date=tx_date,
                        type=mapped_type,
                        amount=amount_value,
                        currency=currency,
                        symbol=currency,
                        description=details.get("description") or details.get("paymentReference") or None,
                        commission=commission,
                    )
                )
                seen_external_ids.add(external_id)
                appended_any = True

            return appended_any

        fetch_succeeded = await walk_transaction_backfill_windows(
            mode=mode,
            window_start=window_start,
            window_end=window_end,
            chunk_days=WISE_STATEMENT_CHUNK_DAYS,
            max_history_days=WISE_MAX_HISTORY_DAYS,
            empty_history_stop_chunks=WISE_EMPTY_HISTORY_STOP_CHUNKS,
            fetch_chunk=fetch_statement_chunk,
            append_rows=append_transactions,
        )

        return transactions, fetch_succeeded
