from __future__ import annotations

import httpx


def safe_market_data_error_message(exc: Exception) -> str:
    provider_code = getattr(exc, "breaktwenty_provider_code", None)
    if provider_code is not None:
        return f"provider code {provider_code}"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return exc.__class__.__name__


def raise_safe_market_data_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    raise httpx.HTTPStatusError(
        f"market data provider rejected request (HTTP {response.status_code})",
        request=response.request,
        response=response,
    )


def raise_safe_market_data_payload_error(response: httpx.Response, payload: dict) -> None:
    exc = httpx.HTTPStatusError(
        "market data provider rejected request",
        request=response.request,
        response=response,
    )
    exc.breaktwenty_provider_code = payload.get("code")
    raise exc
