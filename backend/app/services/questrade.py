import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urlparse

import httpx

from app.connectors.connector_logging import log_connector_event
from app.database import async_session
from app.services.connection_auth_storage import get_api_credentials, upsert_api_credentials
from app.services.sqlite_write_gate import sqlite_write_gate


QUESTRADE_AUTH_URL = "https://login.questrade.com/oauth2/token"
QUESTRADE_TIMEOUT = httpx.Timeout(30.0, connect=15.0, read=30.0, write=15.0, pool=5.0)
QUESTRADE_ACTIVITY_TIMEOUT = httpx.Timeout(75.0, connect=15.0, read=75.0, write=15.0, pool=5.0)
QUESTRADE_HEALTH_TIMEOUT = httpx.Timeout(5.0, connect=5.0, read=5.0, write=5.0, pool=5.0)
QUESTRADE_MAX_SERVER_RETRIES = 10
QUESTRADE_AUTH_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
QUESTRADE_PROVIDER = "questrade"
logger = logging.getLogger("breaktwenty.connectors.questrade")
QUESTRADE_PROBE_BODY_SAMPLE_LIMIT = 220


class QuestradeAuthRequiredError(Exception):
    pass


class QuestradeTemporaryError(Exception):
    pass


class QuestradeTransportError(QuestradeTemporaryError):
    pass


@dataclass(frozen=True)
class QuestradeApiServerHealthResult:
    ok: bool
    duration_ms: int
    status_code: int | None = None
    content_type: str | None = None
    body_sample: str | None = None
    error: str | None = None
    message: str | None = None


def normalize_api_server(api_server: str) -> str:
    base = (api_server or "").strip()
    if not base:
        raise QuestradeTemporaryError("Connection failed - Questrade auth did not return an api_server")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return base.rstrip("/") + "/"


def build_api_url(api_server: str, endpoint: str) -> str:
    base = normalize_api_server(api_server)
    path = endpoint.lstrip("/")
    if not path.startswith("v1/"):
        path = f"v1/{path}"
    return f"{base}{path}"


def _api_server_host(api_server: str) -> str:
    try:
        return urlparse(normalize_api_server(api_server)).netloc or "unknown"
    except QuestradeTemporaryError:
        return "unknown"


def _elapsed_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _sanitize_probe_text(value: str | None, *, limit: int = QUESTRADE_PROBE_BODY_SAMPLE_LIMIT) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


async def get_questrade_credentials(user_id: int):
    async with async_session() as db:
        settings = await get_api_credentials(
            db,
            user_id,
            QUESTRADE_PROVIDER,
            setting_keys=(
                "questrade_refresh_token",
                "questrade_access_token",
                "questrade_api_server",
            ),
        )
        return (
            settings.get("questrade_refresh_token"),
            settings.get("questrade_access_token"),
            settings.get("questrade_api_server"),
        )


async def save_questrade_credentials(
    user_id: int,
    access_token: str,
    refresh_token: str,
    api_server: str,
) -> None:
    async with async_session() as db, sqlite_write_gate(), db.begin():
        await upsert_api_credentials(
            db,
            user_id,
            QUESTRADE_PROVIDER,
            {
                "questrade_access_token": access_token,
                "questrade_refresh_token": refresh_token,
                "questrade_api_server": api_server,
            },
            ensure_institution=True,
        )


async def refresh_access_token(user_id: int, refresh_token: str):
    async with httpx.AsyncClient(timeout=QUESTRADE_TIMEOUT, trust_env=False) as client:
        resp = await client.get(
            QUESTRADE_AUTH_URL,
            params={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        if resp.status_code != 200:
            detail = (resp.text or "").lower()
            if resp.status_code in (400, 401, 403) or any(
                marker in detail for marker in ("invalid_grant", "invalid token", "unauthorized")
            ):
                raise QuestradeAuthRequiredError("Invalid token")
            raise QuestradeTemporaryError(
                f"Connection failed - Questrade auth returned {resp.status_code}"
            )

        data = resp.json()
        new_access = data["access_token"]
        new_refresh = data["refresh_token"]
        api_server = data["api_server"]

        await save_questrade_credentials(user_id, new_access, new_refresh, api_server)

        return new_access, new_refresh, api_server


async def _health_check_api_server(access_token: str, api_server: str) -> QuestradeApiServerHealthResult:
    """Quick probe of /v1/time to verify the assigned api_server is alive."""
    base = normalize_api_server(api_server)
    url = f"{base}v1/time"
    started_at = perf_counter()
    try:
        async with httpx.AsyncClient(timeout=QUESTRADE_HEALTH_TIMEOUT, trust_env=False) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        return QuestradeApiServerHealthResult(
            ok=resp.status_code == 200,
            duration_ms=_elapsed_ms(started_at),
            status_code=resp.status_code,
            content_type=_sanitize_probe_text(resp.headers.get("content-type")),
            body_sample=None if resp.status_code == 200 else _sanitize_probe_text(resp.text),
        )
    except Exception as exc:
        return QuestradeApiServerHealthResult(
            ok=False,
            duration_ms=_elapsed_ms(started_at),
            error=type(exc).__name__,
            message=_sanitize_probe_text(str(exc) or type(exc).__name__),
        )


async def _exchange_token_once(user_id: int, refresh_token: str):
    """Wrap refresh_access_token with consistent error classification."""
    try:
        return await refresh_access_token(user_id, refresh_token)
    except QuestradeAuthRequiredError:
        raise
    except QuestradeTemporaryError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if any(kw in msg for kw in ("timeout", "connect", "dns", "network", "unreachable",
               "resolve", "name resolution", "name or service not known",
               "max retries exceeded", "connectionpool", "sslerror", "connectionrefused")):
            raise QuestradeTemporaryError(f"Connection failed - {e}")
        raise QuestradeAuthRequiredError("Invalid token")


async def _exchange_token_with_retries(user_id: int, refresh_token: str):
    last_error: QuestradeTemporaryError | None = None
    attempts = len(QUESTRADE_AUTH_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await _exchange_token_once(user_id, refresh_token)
        except QuestradeAuthRequiredError:
            raise
        except QuestradeTemporaryError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            log_connector_event(
                logger,
                provider=QUESTRADE_PROVIDER,
                stage="auth exchange retry",
                level="warning",
                user_id=user_id,
                attempt=attempt,
                max_attempts=attempts,
                error=type(exc).__name__,
                message=str(exc),
            )
            await asyncio.sleep(QUESTRADE_AUTH_RETRY_DELAYS_SECONDS[attempt - 1])

    if last_error is not None:
        raise last_error
    raise QuestradeTemporaryError("Connection failed - Questrade auth did not complete")


async def get_authenticated_client(user_id: int):
    """Exchange the refresh token and return a freshly-minted access token paired
    with a reachable api_server.

    Questrade's refresh token is single-use: every exchange burns the stored token,
    issues a new one, and assigns one api_server. We exchange once, health-check the
    assigned server, and re-exchange only when that server is genuinely unreachable
    (Questrade rotates api01-api07.iq.questrade.com and occasionally hands out a dead
    host). The token and server returned always come from the same, most recent
    exchange, so a token can never be invalidated by a later rotation before it is
    used. A merely slow but working server is accepted as-is — slowness is neither an
    auth failure nor a server-health failure, and must not trigger a re-exchange."""
    current_refresh, _, _ = await get_questrade_credentials(user_id)
    if not current_refresh:
        log_connector_event(
            logger,
            provider=QUESTRADE_PROVIDER,
            stage="auth missing refresh token",
            user_id=user_id,
        )
        raise QuestradeAuthRequiredError("Questrade refresh token not configured. Go to Settings to add it.")

    for attempt in range(1, QUESTRADE_MAX_SERVER_RETRIES + 1):
        access_token, new_refresh, api_server = await _exchange_token_with_retries(user_id, current_refresh)
        current_refresh = new_refresh  # every exchange burns the previous refresh token
        host = _api_server_host(api_server)
        log_connector_event(
            logger,
            provider=QUESTRADE_PROVIDER,
            stage="api server probe start",
            user_id=user_id,
            attempt=attempt,
            max_attempts=QUESTRADE_MAX_SERVER_RETRIES,
            api_server=host,
        )
        health = await _health_check_api_server(access_token, api_server)
        if health.ok:
            log_connector_event(
                logger,
                provider=QUESTRADE_PROVIDER,
                stage="api server probe ok",
                user_id=user_id,
                attempt=attempt,
                api_server=host,
                duration_ms=health.duration_ms,
                status_code=health.status_code,
                content_type=health.content_type,
            )
            return access_token, api_server
        log_connector_event(
            logger,
            provider=QUESTRADE_PROVIDER,
            stage="api server probe failed",
            level="warning",
            user_id=user_id,
            attempt=attempt,
            max_attempts=QUESTRADE_MAX_SERVER_RETRIES,
            api_server=host,
            duration_ms=health.duration_ms,
            status_code=health.status_code,
            content_type=health.content_type,
            body_sample=health.body_sample,
            error=health.error,
            message=health.message,
        )

    raise QuestradeTemporaryError(
        "Questrade API servers are unreachable. This is a Questrade infrastructure issue — please try again later."
    )


async def qt_api_get(
    access_token: str,
    api_server: str,
    endpoint: str,
    *,
    timeout: httpx.Timeout | None = None,
):
    api_server = normalize_api_server(api_server)
    url = build_api_url(api_server, endpoint)
    async with httpx.AsyncClient(timeout=timeout or QUESTRADE_TIMEOUT, trust_env=False) as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
            raise QuestradeTransportError(
                f"Connection failed - unable to reach Questrade API ({type(e).__name__})"
            ) from e
        except (httpx.ConnectError, httpx.NetworkError, httpx.TransportError) as e:
            detail = str(e).strip() or type(e).__name__
            raise QuestradeTransportError(
                f"Connection failed - unable to reach Questrade API ({detail})"
            ) from e

        if resp.status_code != 200:
            detail = (resp.text or "").lower()
            if resp.status_code in (401, 403) or "unauthorized" in detail:
                raise QuestradeAuthRequiredError("Unauthorized")
            provider_message = ""
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    provider_message = _sanitize_probe_text(parsed.get("message")) or ""
            except Exception:
                provider_message = _sanitize_probe_text(resp.text) or ""
            suffix = f" ({provider_message})" if provider_message else ""
            raise QuestradeTemporaryError(
                f"Connection failed - Questrade API returned {resp.status_code}{suffix}"
            )
        return resp.json()
