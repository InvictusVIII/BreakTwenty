from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any
from urllib.parse import quote

import httpx


MOOMOO_CLOUD_ORIGIN = "https://webapi.moomoo.com"
MOOMOO_CLOUD_TIMEOUT = httpx.Timeout(30.0, connect=15.0, read=30.0, write=15.0, pool=5.0)
MOOMOO_CLOUD_MAX_PAGES = 500
MOOMOO_CLOUD_RATE_LIMIT_STATUS_CODES = frozenset({429, 439})
MOOMOO_CLOUD_RATE_LIMIT_ATTEMPTS = 5
MOOMOO_CLOUD_HISTORY_REQUEST_INTERVAL_SECONDS = 3.1
MOOMOO_CLOUD_MARKETS_BY_CODE = {
    1: "HK",
    2: "US",
    4: "HKCC",
    5: "FUTURES",
    6: "SG",
    12: "CA",
    15: "JP",
    18: "KR",
}


class MoomooCloudError(Exception):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "request",
        status_code: int | None = None,
        provider_code: str | int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.provider_code = provider_code


class MoomooCloudAuthRequired(MoomooCloudError):
    pass


class MoomooCloudTemporaryError(MoomooCloudError):
    pass


class MoomooCloudHistoryRateLimiter:
    """Pace each account/history endpoint below Moomoo's 10 calls/30s limit."""

    def __init__(self, interval_seconds: float = MOOMOO_CLOUD_HISTORY_REQUEST_INTERVAL_SECONDS) -> None:
        self.interval_seconds = max(float(interval_seconds), 0.0)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._next_request_at: dict[tuple[str, str], float] = {}

    async def wait(self, account_id: str, endpoint: str) -> None:
        key = (str(account_id), str(endpoint))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            delay = self._next_request_at.get(key, 0.0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at[key] = loop.time() + self.interval_seconds


@dataclass(frozen=True)
class MoomooCloudTokens:
    access_token: str
    refresh_token: str
    scope: str
    expires_in: int | None = None


def sanitize_scope(scope: str | None) -> list[str]:
    return [
        "accid:[authorized]" if item.startswith("accid:") else item
        for item in str(scope or "").split()
        if item
    ]


def granted_account_ids(scope: str | None) -> set[str]:
    return {
        item.removeprefix("accid:")
        for item in str(scope or "").split()
        if item.startswith("accid:") and item.removeprefix("accid:")
    }


def validate_read_scope(scope: str | None) -> None:
    entries = set(str(scope or "").split())
    if "trade:read" not in entries:
        raise MoomooCloudAuthRequired(
            "Moomoo authorization did not grant account and trading read access.",
            stage="token_scope",
            provider_code="missing_trade_read",
        )
    if not granted_account_ids(scope):
        raise MoomooCloudAuthRequired(
            "Moomoo authorization did not grant access to a trading account.",
            stage="token_scope",
            provider_code="missing_account_scope",
        )
    if "trade:write" in entries:
        raise MoomooCloudAuthRequired(
            "Moomoo authorization included trading write access. Reconnect and select account and trading read access only.",
            stage="token_scope",
            provider_code="trading_write_scope",
        )


def markets_for_account(account: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        MOOMOO_CLOUD_MARKETS_BY_CODE.get(int(value))
        for value in (account.get("enable_market") or [])
        if str(value).isdigit() and MOOMOO_CLOUD_MARKETS_BY_CODE.get(int(value))
    ))


def _provider_error(payload: Any, fallback: str) -> tuple[str | int | None, str]:
    if not isinstance(payload, dict):
        return None, fallback
    nested = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = payload.get("errcode", nested.get("code"))
    message = payload.get("errmsg") or payload.get("error_description") or nested.get("message")
    if not message and isinstance(payload.get("error"), str):
        message = payload["error"]
    return code, str(message or fallback)[:500]


def _unwrap(payload: Any, *, stage: str, status_code: int) -> Any:
    if isinstance(payload, dict) and payload.get("s") == "ok":
        return payload.get("d")
    if isinstance(payload, dict) and payload.get("ret_code") == 0:
        return payload.get("data")
    code, message = _provider_error(payload, "Moomoo returned an unsuccessful response.")
    error_type = MoomooCloudAuthRequired if status_code in {400, 401, 403} else MoomooCloudTemporaryError
    raise error_type(message, stage=stage, status_code=status_code, provider_code=code)


async def _request_json(
    method: str,
    path: str,
    *,
    stage: str,
    access_token: str | None = None,
    data: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Any, int]:
    if not path.startswith("/"):
        raise ValueError("Moomoo cloud request path must be absolute")
    owns_client = client is None
    request_client = client or httpx.AsyncClient(timeout=MOOMOO_CLOUD_TIMEOUT, trust_env=False)
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = None
        for attempt in range(MOOMOO_CLOUD_RATE_LIMIT_ATTEMPTS):
            try:
                response = await request_client.request(
                    method,
                    f"{MOOMOO_CLOUD_ORIGIN}{path}",
                    headers=headers,
                    data=data,
                    json=json_body,
                    params=params,
                    follow_redirects=False,
                )
            except httpx.TimeoutException as exc:
                raise MoomooCloudTemporaryError(
                    "Moomoo cloud API request timed out.", stage=stage, provider_code="timeout"
                ) from exc
            except httpx.TransportError as exc:
                raise MoomooCloudTemporaryError(
                    "Moomoo cloud API could not be reached.",
                    stage=stage,
                    provider_code=type(exc).__name__,
                ) from exc
            if (
                response.status_code not in MOOMOO_CLOUD_RATE_LIMIT_STATUS_CODES
                or attempt == MOOMOO_CLOUD_RATE_LIMIT_ATTEMPTS - 1
            ):
                break
            retry_after = response.headers.get("retry-after")
            try:
                delay = min(max(float(retry_after), 0.5), 31.0)
            except (TypeError, ValueError):
                delay = (
                    31.0
                    if response.status_code == 439
                    else min(float(2 ** attempt), 31.0)
                )
            await asyncio.sleep(delay)
        assert response is not None
        try:
            payload = response.json()
        except ValueError as exc:
            raise MoomooCloudTemporaryError(
                f"Moomoo returned a non-JSON response (HTTP {response.status_code}).",
                stage=stage,
                status_code=response.status_code,
                provider_code="invalid_json",
            ) from exc
        if response.status_code >= 400:
            code, message = _provider_error(payload, f"Moomoo returned HTTP {response.status_code}.")
            error_type = (
                MoomooCloudAuthRequired
                if response.status_code in {400, 401, 403}
                else MoomooCloudTemporaryError
            )
            raise error_type(
                message,
                stage=stage,
                status_code=response.status_code,
                provider_code=code,
            )
        return payload, response.status_code
    finally:
        if owns_client:
            await request_client.aclose()


async def register_public_client(redirect_uri: str) -> tuple[str, str]:
    payload, status_code = await _request_json(
        "POST",
        "/oauth2/register",
        stage="client_registration",
        json_body={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "BreakTwenty",
        },
    )
    client_id = payload.get("client_id") if isinstance(payload, dict) else None
    if not isinstance(client_id, str) or not client_id:
        code, message = _provider_error(payload, "Moomoo client registration did not return a client ID.")
        raise MoomooCloudTemporaryError(
            message,
            stage="client_registration",
            status_code=status_code,
            provider_code=code or "missing_client_id",
        )
    return client_id, str(payload.get("scope") or "")


async def exchange_authorization_code(
    *, client_id: str, code: str, redirect_uri: str, code_verifier: str
) -> MoomooCloudTokens:
    payload, status_code = await _request_json(
        "POST",
        "/oauth2/token",
        stage="token_exchange",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
        code_value, message = _provider_error(payload, "Moomoo token exchange did not return both tokens.")
        raise MoomooCloudAuthRequired(
            message,
            stage="token_exchange",
            status_code=status_code,
            provider_code=code_value or "missing_tokens",
        )
    tokens = MoomooCloudTokens(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        scope=str(payload.get("scope") or ""),
        expires_in=int(payload["expires_in"]) if str(payload.get("expires_in") or "").isdigit() else None,
    )
    validate_read_scope(tokens.scope)
    return tokens


async def refresh_access_token(
    *, client_id: str, refresh_token: str, granted_scope: str = ""
) -> MoomooCloudTokens:
    payload, status_code = await _request_json(
        "POST",
        "/oauth2/token",
        stage="token_refresh",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    if not isinstance(payload, dict) or not payload.get("access_token"):
        code, message = _provider_error(payload, "Moomoo token refresh did not return an access token.")
        raise MoomooCloudAuthRequired(
            message,
            stage="token_refresh",
            status_code=status_code,
            provider_code=code or "missing_access_token",
        )
    # OAuth token refresh responses may omit scope when the grant is unchanged.
    scope = str(payload.get("scope") or granted_scope)
    validate_read_scope(scope)
    return MoomooCloudTokens(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or refresh_token),
        scope=scope,
        expires_in=int(payload["expires_in"]) if str(payload.get("expires_in") or "").isdigit() else None,
    )


async def api_get(
    path: str,
    access_token: str,
    *,
    stage: str,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Any, int]:
    payload, status_code = await _request_json(
        "GET", path, stage=stage, access_token=access_token, params=params, client=client
    )
    return _unwrap(payload, stage=stage, status_code=status_code), status_code


def date_range_microseconds(start: date, end: date) -> tuple[int, int]:
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end, time.max, tzinfo=timezone.utc)
    return int(start_dt.timestamp() * 1_000_000), int(end_dt.timestamp() * 1_000_000)


async def fetch_history_pages(
    *,
    account_id: str,
    access_token: str,
    endpoint: str,
    rows_key: str,
    market: str,
    start: date,
    end: date,
    page_size: int,
    client: httpx.AsyncClient,
    rate_limiter: MoomooCloudHistoryRateLimiter | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_us, end_us = date_range_microseconds(start, end)
    page_flag = ""
    rows: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []
    encoded_account_id = quote(account_id, safe="")
    for page_number in range(1, MOOMOO_CLOUD_MAX_PAGES + 1):
        if rate_limiter is not None:
            await rate_limiter.wait(account_id, endpoint)
        data, status_code = await api_get(
            f"/api/v1.0/accounts/{encoded_account_id}/{endpoint}",
            access_token,
            stage=f"{endpoint}_{market}",
            params={
                "trd_market": market,
                "start": start_us,
                "end": end_us,
                "page_flag": page_flag,
                "page_size": page_size,
            },
            client=client,
        )
        page_rows = data.get(rows_key) if isinstance(data, dict) else None
        if not isinstance(page_rows, list):
            raise MoomooCloudTemporaryError(
                "Moomoo history response did not contain the documented row list.",
                stage=f"{endpoint}_{market}",
                status_code=status_code,
                provider_code="missing_rows",
            )
        rows.extend(dict(row) for row in page_rows if isinstance(row, dict))
        completed = bool(data.get("completed"))
        next_page_flag = str(data.get("page_flag") or "")
        page_summaries.append({
            "page": page_number,
            "status": status_code,
            "rows": len(page_rows),
            "completed": completed,
        })
        if completed:
            return rows, page_summaries
        if not next_page_flag or next_page_flag == page_flag:
            raise MoomooCloudTemporaryError(
                "Moomoo history pagination stopped before completion.",
                stage=f"{endpoint}_{market}",
                status_code=status_code,
                provider_code="invalid_page_flag",
            )
        page_flag = next_page_flag
    raise MoomooCloudTemporaryError(
        "Moomoo history exceeded the safe pagination limit.",
        stage=f"{endpoint}_{market}",
        provider_code="page_limit",
    )
