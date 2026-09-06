import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Any
from urllib.parse import urlparse

from app.brand import APP_BRAND_NAME
from app.connectors.connector_logging import get_connector_logger, log_connector_event
from app.connectors.sdk_process import SdkProcessWorker
from app.connectors.types import SyncResult, SyncStatus
from app.services.runtime_state import (
    get_provider_session_path,
    load_json_artifact_async,
    save_json_artifact,
    save_json_artifact_async,
)
from app.services.sqlite_write_gate import sqlite_write_gate
from app.services.sync_utils import is_network_error
from app.services.time_utils import utc_now as _utc_now
from app.services.user_secret_storage import (
    delete_user_secret_artifact,
    get_user_secret_artifact,
    upsert_user_secret_artifact,
)

PROVIDER = "wealthsimple"
logger = get_connector_logger(PROVIDER)
WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND = "wealthsimple_otp_challenge"
WEALTHSIMPLE_OTP_CHALLENGE_TTL = timedelta(minutes=10)
INVALID_LOGIN_MESSAGE = "Wrong attempt. Either credentials or verification code are invalid"
SESSION_SAVE_FAILED_MESSAGE = f"Login succeeded, but {APP_BRAND_NAME} could not save the Wealthsimple session. Please try again."

NETWORK_PROBE_TIMEOUT_SECONDS = 5
WEALTHSIMPLE_BLOCKING_CALL_TIMEOUT_SECONDS = 45.0
NETWORK_PROBE_URLS = (
    "https://my.wealthsimple.com/app/login",
    "https://api.production.wealthsimple.com/v1/oauth/v2/token",
)


async def load_session_async(user_id: int):
    session = await load_json_artifact_async(get_provider_session_path(user_id, PROVIDER))
    if session is None:
        return None
    return _session_payload(session)


def save_session(user_id: int, session):
    save_json_artifact(get_provider_session_path(user_id, PROVIDER), session)


def _session_payload(session: Any) -> dict[str, Any]:
    if isinstance(session, str):
        try:
            payload = json.loads(session)
        except JSONDecodeError as exc:
            raise ValueError("Wealthsimple session callback returned invalid JSON") from exc
    elif isinstance(session, dict):
        payload = dict(session)
    else:
        payload = {
            "client_id": getattr(session, "client_id", None),
            "access_token": getattr(session, "access_token", None),
            "refresh_token": getattr(session, "refresh_token", None),
            "session_id": getattr(session, "session_id", None),
            "wssdi": getattr(session, "wssdi", None),
            "token_info": getattr(session, "token_info", None),
        }

    return {
        "client_id": payload.get("client_id"),
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "session_id": payload.get("session_id"),
        "wssdi": payload.get("wssdi"),
        "token_info": payload.get("token_info"),
    }


def save_authenticated_session(user_id: int, session) -> None:
    save_session(user_id, _session_payload(session))


async def save_authenticated_session_async(user_id: int, session) -> None:
    await save_json_artifact_async(
        get_provider_session_path(user_id, PROVIDER),
        _session_payload(session),
    )


async def save_otp_challenge(user_id: int, email: str, password: str) -> None:
    import app.database as database

    now = _utc_now()
    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        await upsert_user_secret_artifact(
            db,
            int(user_id),
            WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
            {
                "email": str(email),
                "password": str(password),
                "created_at": now.isoformat(),
                "expires_at": (now + WEALTHSIMPLE_OTP_CHALLENGE_TTL).isoformat(),
            },
        )


async def consume_otp_challenge(user_id: int) -> dict[str, str]:
    import app.database as database

    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        payload = await get_user_secret_artifact(
            db,
            int(user_id),
            WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
        )
        await delete_user_secret_artifact(
            db,
            int(user_id),
            WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
        )
    if not isinstance(payload, dict):
        return {}
    try:
        expires_at = datetime.fromisoformat(str(payload.get("expires_at") or ""))
    except (TypeError, ValueError):
        return {}
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utc_now():
        return {}
    return {
        "email": str(payload.get("email") or ""),
        "password": str(payload.get("password") or ""),
    }


async def clear_otp_challenge(user_id: int) -> None:
    import app.database as database

    async with database.async_session() as db, sqlite_write_gate(), db.begin():
        await delete_user_secret_artifact(
            db,
            int(user_id),
            WEALTHSIMPLE_OTP_CHALLENGE_SECRET_KIND,
        )


def _provider_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        value = response.get("error") or response.get("code") or response.get("message")
        if not value:
            errors = response.get("errors")
            if isinstance(errors, list):
                for error in errors:
                    if not isinstance(error, dict):
                        continue
                    raw_extensions = error.get("extensions")
                    extensions = raw_extensions if isinstance(raw_extensions, dict) else {}
                    value = (
                        error.get("error")
                        or error.get("code")
                        or error.get("message")
                        or extensions.get("code")
                    )
                    if value:
                        break
        return str(value or "")[:120]
    return ""


def _safe_exception_message(exc: Exception) -> str:
    if isinstance(getattr(exc, "response", None), dict):
        return ""
    return str(exc)


def _log_login_exception(stage: str, user_id: int, exc: Exception) -> None:
    log_connector_event(
        logger,
        provider=PROVIDER,
        stage=stage,
        level="warning",
        debug=True,
        user_id=user_id,
        error=type(exc).__name__,
        provider_error=_provider_error_code(exc),
        message=_safe_exception_message(exc),
    )


async def _save_login_session(user_id: int, session, stage: str) -> SyncResult | None:
    try:
        await save_authenticated_session_async(user_id, session)
    except Exception as exc:
        _log_login_exception(stage, user_id, exc)
        return SyncResult(status=SyncStatus.ERROR, message=SESSION_SAVE_FAILED_MESSAGE)
    return None


def _response_is_unauthorized(response: Any) -> bool:
    if not isinstance(response, dict):
        return False

    message = str(response.get("message") or "").strip().lower().rstrip(".")
    code = str(response.get("code") or response.get("error") or "").strip().lower()
    if message in {"not authorized", "unauthorized"}:
        return True
    if code in {"unauthenticated", "unauthorized"}:
        return True

    extensions = response.get("extensions")
    if isinstance(extensions, dict) and _response_is_unauthorized(extensions):
        return True

    errors = response.get("errors")
    if isinstance(errors, list):
        return any(_response_is_unauthorized(error) for error in errors)

    return False


def _is_token_probe_unauthorized(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return _response_is_unauthorized(response)


def _refresh_ws_session(ws, session, persist_session_fct) -> None:
    from ws_api import ManualLoginRequired

    if not session.refresh_token:
        raise ManualLoginRequired("OAuth token invalid and cannot be refreshed.")

    response = ws.send_post(
        f"{ws.OAUTH_BASE_URL}/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": session.client_id,
        },
        {
            "x-wealthsimple-client": "@wealthsimple/wealthsimple",
            "x-ws-profile": "invest",
        },
    )
    if "access_token" not in response or "refresh_token" not in response:
        raise ManualLoginRequired(
            f"OAuth token invalid and cannot be refreshed: {response.get('error', 'Invalid response from API')}"
        )

    session.access_token = response["access_token"]
    session.refresh_token = response["refresh_token"]
    session.token_info = None
    ws.session.access_token = session.access_token
    ws.session.refresh_token = session.refresh_token
    ws.session.token_info = None
    persist_session_fct(session.to_json())


def _ensure_ws_session_valid(ws, session, persist_session_fct) -> None:
    if session.access_token:
        try:
            ws.search_security("XEQT")
        except Exception as exc:
            if not _is_token_probe_unauthorized(exc):
                raise
        else:
            return

    _refresh_ws_session(ws, session, persist_session_fct)


def get_ws_client(user_id: int, session_data, *, persist_session=None):
    from ws_api import WealthsimpleAPI, WSAPISession

    session_data = _session_payload(session_data)
    session = WSAPISession(
        client_id=session_data["client_id"],
        access_token=session_data["access_token"],
        refresh_token=session_data["refresh_token"],
        session_id=session_data["session_id"],
        wssdi=session_data["wssdi"],
        token_info=session_data.get("token_info"),
    )

    def persist(sess, username=None):
        save_authenticated_session(user_id, sess)
        log_connector_event(
            logger,
            provider=PROVIDER,
            stage="session refreshed",
            user_id=user_id,
            has_access_token=True,
            has_refresh_token=True,
        )

    ws = WealthsimpleAPI(session)
    ws.session.token_info = session.token_info
    # Callers running inside the async event loop pass persist_session to defer the
    # refreshed-token DB write so it can be committed through the async artifact
    # persistence path instead of this synchronous callback.
    _ensure_ws_session_valid(ws, session, persist_session or persist)
    return ws


class WealthsimpleSdkProcessHandler:
    def __init__(self, payload: dict) -> None:
        self._user_id = int(payload.get("user_id") or 0)
        self._refreshed_sessions: list[dict[str, Any]] = []
        self._ws = get_ws_client(
            self._user_id,
            payload.get("session") or {},
            persist_session=lambda session: self._refreshed_sessions.append(
                _session_payload(session)
            ),
        )

    def handle(self, operation: str, payload: dict):
        if operation == "accounts":
            return self._result(self._ws.get_accounts())
        if operation == "historical_financials":
            return self._result(
                self._ws.get_account_historical_financials(
                    str(payload.get("account_id") or ""),
                    currency=str(payload.get("currency") or "CAD"),
                    resolution=str(payload.get("resolution") or "DAILY"),
                )
            )
        if operation == "activities":
            return self._result(
                self._ws.get_activities(
                    account_id=str(payload.get("account_id") or ""),
                    start_date=payload.get("start_date"),
                    end_date=payload.get("end_date"),
                    load_all=True,
                )
            )
        if operation == "positions":
            token_info = self._ws.get_token_info() or {}
            identity_id = token_info.get("identity_canonical_id")
            if not identity_id:
                raise ValueError("Wealthsimple token metadata did not include an identity ID")
            edges = self._ws.do_graphql_query(
                "FetchIdentityPositions",
                {
                    "identityId": identity_id,
                    "currency": "CAD",
                    "filter": {"securityIds": None},
                    "includeAccountData": True,
                    "includeSecurity": True,
                    "aggregated": False,
                },
                "identity.financials.current.positions.edges",
                "array",
            )
            return self._result(edges)
        raise ValueError(f"Unsupported Wealthsimple SDK operation: {operation}")

    def close(self) -> None:
        close = getattr(self._ws, "close", None)
        if callable(close):
            close()

    def _result(self, value: Any) -> dict[str, Any]:
        result = {
            "value": value,
            "refreshed_sessions": list(self._refreshed_sessions),
        }
        self._refreshed_sessions.clear()
        return result


class WealthsimpleSdkWorker:
    def __init__(self, user_id: int, session: dict[str, Any]) -> None:
        self._worker = SdkProcessWorker(
            "app.services.wealthsimple:WealthsimpleSdkProcessHandler",
            {"user_id": user_id, "session": _session_payload(session)},
        )

    async def start(self) -> None:
        await self._worker.start()

    async def call(
        self,
        operation: str,
        payload: dict | None = None,
        *,
        timeout: float = WEALTHSIMPLE_BLOCKING_CALL_TIMEOUT_SECONDS,
    ):
        return await self._worker.call(operation, payload, timeout=timeout)

    async def close(self) -> None:
        await self._worker.close()


class WealthsimpleAuthProcessHandler:
    def __init__(self, payload: dict) -> None:
        pass

    def handle(self, operation: str, payload: dict):
        if operation != "login":
            raise ValueError(f"Unsupported Wealthsimple auth operation: {operation}")
        from ws_api import WealthsimpleAPI

        kwargs = {}
        otp_answer = payload.get("otp_answer")
        if otp_answer:
            kwargs["otp_answer"] = str(otp_answer)
        session = WealthsimpleAPI.login(
            str(payload.get("username") or ""),
            str(payload.get("password") or ""),
            **kwargs,
        )
        return _session_payload(session)

    def close(self) -> None:
        pass


async def run_wealthsimple_login(
    username: str,
    password: str,
    *,
    otp_answer: str | None = None,
) -> dict[str, Any]:
    worker = SdkProcessWorker(
        "app.services.wealthsimple:WealthsimpleAuthProcessHandler",
    )
    try:
        return await worker.call(
            "login",
            {
                "username": username,
                "password": password,
                "otp_answer": otp_answer,
            },
            timeout=WEALTHSIMPLE_BLOCKING_CALL_TIMEOUT_SECONDS,
        )
    finally:
        await worker.close()


def _probe_wealthsimple_connectivity_error(user_id: int | None = None):
    for url in NETWORK_PROBE_URLS:
        host = urlparse(url).hostname
        if not host:
            continue
        try:
            with socket.create_connection((host, 443), timeout=NETWORK_PROBE_TIMEOUT_SECONDS):
                pass
        except Exception as e:
            log_connector_event(
                logger,
                provider=PROVIDER,
                stage="connectivity probe failed",
                level="warning",
                user_id=user_id,
                host=host,
                error=type(e).__name__,
                message=str(e),
            )
            return e
    return None


async def _network_result_if_unreachable(user_id: int | None = None):
    probe_error = await asyncio.wait_for(
        asyncio.to_thread(_probe_wealthsimple_connectivity_error, user_id=user_id),
        timeout=(len(NETWORK_PROBE_URLS) * NETWORK_PROBE_TIMEOUT_SECONDS) + 2,
    )
    if probe_error and is_network_error(probe_error):
        return {"status": "network_error", "message": "Connection failed"}
    return None


class WealthsimpleApiSessionOtpAuth:
    async def login(self, user_id: int, username: str, password: str) -> SyncResult:
        await clear_otp_challenge(user_id)
        try:
            session = await run_wealthsimple_login(username, password)
            save_error = await _save_login_session(user_id, session, "login session save failed")
            if save_error:
                return save_error
            return SyncResult(status=SyncStatus.OK)
        except Exception as exc:
            remote_type = getattr(exc, "remote_type", "") or type(exc).__name__
            if remote_type == "OTPRequiredException":
                await save_otp_challenge(user_id, username, password)
                return SyncResult(status=SyncStatus.TWO_FA_REQUIRED, challenge_method="sms")
            if remote_type == "LoginFailedException":
                _log_login_exception("login rejected", user_id, exc)
                return SyncResult(status=SyncStatus.ERROR, message=INVALID_LOGIN_MESSAGE)
            if remote_type == "CurlException":
                _log_login_exception("login network failure", user_id, exc)
                return SyncResult(status=SyncStatus.NETWORK_ERROR, message="Connection failed")
            if is_network_error(exc):
                _log_login_exception("login network failure", user_id, exc)
                return SyncResult(status=SyncStatus.NETWORK_ERROR, message="Connection failed")
            _log_login_exception("login failed", user_id, exc)
            return SyncResult(status=SyncStatus.ERROR, message=INVALID_LOGIN_MESSAGE)

    async def complete_2fa(self, user_id: int, code: str | None = None) -> SyncResult:
        if not code:
            return SyncResult(status=SyncStatus.ERROR, message="OTP required")
        pending_creds = await consume_otp_challenge(user_id)
        email = pending_creds.get("email")
        password = pending_creds.get("password")

        if not email or not password:
            return SyncResult(
                status=SyncStatus.AUTH_REQUIRED,
                message="Session expired, please login again",
            )
        try:
            session = await run_wealthsimple_login(
                email,
                password,
                otp_answer=code,
            )
            save_error = await _save_login_session(user_id, session, "2fa session save failed")
            if save_error:
                return save_error
            return SyncResult(status=SyncStatus.OK)
        except Exception as exc:
            remote_type = getattr(exc, "remote_type", "") or type(exc).__name__
            if remote_type == "LoginFailedException":
                _log_login_exception("2fa rejected", user_id, exc)
                return SyncResult(status=SyncStatus.ERROR, message=INVALID_LOGIN_MESSAGE)
            if remote_type == "CurlException":
                _log_login_exception("2fa network failure", user_id, exc)
                await save_otp_challenge(user_id, email, password)
                return SyncResult(status=SyncStatus.NETWORK_ERROR, message="Connection failed")
            if is_network_error(exc):
                _log_login_exception("2fa network failure", user_id, exc)
                await save_otp_challenge(user_id, email, password)
                return SyncResult(status=SyncStatus.NETWORK_ERROR, message="Connection failed")
            _log_login_exception("2fa failed", user_id, exc)
            return SyncResult(status=SyncStatus.ERROR, message=INVALID_LOGIN_MESSAGE)
