from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from app.scrapers.results import (
    normalize_scraper_result,
    scraper_auth_required,
)
from app.services.runtime_state import (
    get_provider_artifact_path,
    get_provider_visible_auth_artifact_path,
    get_runtime_state,
    load_json_artifact_async,
    save_json_artifact,
    save_json_artifact_async,
)
from app.services.sync_utils import is_network_error

DEFAULT_SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)


def provider_runtime_artifact_path(
    user_id: int,
    provider: str,
    artifact_kind: str,
    *,
    visible_auth_attempt_namespace: str | None = None,
) -> str:
    attempt_id = ""
    if visible_auth_attempt_namespace:
        attempt_state = get_runtime_state(visible_auth_attempt_namespace, user_id)
        if isinstance(attempt_state, dict):
            attempt_id = str(attempt_state.get("attempt_id") or "").strip()
    if attempt_id:
        return get_provider_visible_auth_artifact_path(user_id, provider, attempt_id, artifact_kind)
    return get_provider_artifact_path(user_id, provider, artifact_kind)


def save_provider_runtime_artifact(
    user_id: int,
    provider: str,
    artifact_kind: str,
    artifact,
    *,
    visible_auth_attempt_namespace: str | None = None,
) -> None:
    save_json_artifact(
        provider_runtime_artifact_path(
            user_id,
            provider,
            artifact_kind,
            visible_auth_attempt_namespace=visible_auth_attempt_namespace,
        ),
        artifact,
    )


def visible_auth_attempt_namespace(provider: str) -> str:
    return f"visible_auth_attempt:{provider}"


async def save_visible_auth_session_artifact_async(
    user_id: int,
    provider: str,
    artifact,
) -> None:
    await save_provider_runtime_artifact_async(
        user_id,
        provider,
        "session",
        artifact,
        visible_auth_attempt_namespace=visible_auth_attempt_namespace(provider),
    )


async def save_provider_runtime_artifact_async(
    user_id: int,
    provider: str,
    artifact_kind: str,
    artifact,
    *,
    visible_auth_attempt_namespace: str | None = None,
) -> None:
    await save_json_artifact_async(
        provider_runtime_artifact_path(
            user_id,
            provider,
            artifact_kind,
            visible_auth_attempt_namespace=visible_auth_attempt_namespace,
        ),
        artifact,
    )


def parse_json_body(body_text: str | None):
    normalized = (body_text or "").lstrip("\ufeff").strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def storage_state_cookie_header(storage_state: dict[str, Any], url: str) -> str:
    parsed_url = urlsplit(url)
    host = parsed_url.hostname or ""
    path = parsed_url.path or "/"
    pairs: list[str] = []

    for cookie in storage_state.get("cookies", []) or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".")
        cookie_path = str(cookie.get("path") or "/")
        if not domain:
            continue
        if not (host == domain or host.endswith(f".{domain}")):
            continue
        if not path.startswith(cookie_path):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if name is None or value is None:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def default_user_agent(user_agent: str | None, fallback: str) -> str:
    normalized = str(user_agent or "").strip()
    return normalized or fallback


def session_user_agent(session_artifact: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(session_artifact, dict):
        return fallback
    return default_user_agent(session_artifact.get("user_agent"), fallback)


def default_scraper_user_agent(user_agent: str | None = None) -> str:
    return default_user_agent(user_agent, DEFAULT_SCRAPER_USER_AGENT)


def scraper_session_user_agent(
    session_artifact: dict[str, Any] | None,
    fallback: str = DEFAULT_SCRAPER_USER_AGENT,
) -> str:
    return session_user_agent(session_artifact, fallback)


def _headers_contain(headers: dict[str, str], name: str) -> bool:
    normalized = name.lower()
    return any(key.lower() == normalized for key in headers)


def seed_storage_state_cookies(client: httpx.AsyncClient, storage_state: dict[str, Any]) -> None:
    for cookie in storage_state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        domain = str(cookie.get("domain") or "").strip().lstrip(".")
        path = str(cookie.get("path") or "/").strip() or "/"
        if not name or not domain:
            continue
        client.cookies.set(name, value, domain=domain, path=path)


def client_cookie_value(client: httpx.AsyncClient, name: str, *, host: str) -> str:
    for cookie in client.cookies.jar:
        if cookie.name != name:
            continue
        domain = str(cookie.domain or "").lstrip(".")
        if host == domain or host.endswith(f".{domain}"):
            return str(cookie.value or "")
    return ""


def _cookie_same_site(cookie) -> str:
    rest = getattr(cookie, "_rest", {}) or {}
    for key in ("SameSite", "samesite", "sameSite"):
        value = rest.get(key)
        if value:
            normalized = str(value).strip().capitalize()
            if normalized in {"Strict", "Lax", "None"}:
                return normalized
    return "Lax"


def storage_state_with_client_cookies(
    storage_state: dict[str, Any],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    updated = copy.deepcopy(storage_state)
    existing_cookies = updated.get("cookies")
    if not isinstance(existing_cookies, list):
        existing_cookies = []

    previous_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in existing_cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "").lstrip(".")
        path = str(cookie.get("path") or "/") or "/"
        if not name or not domain:
            continue
        previous_by_key[(name, domain, path)] = cookie

    cookies_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in client.cookies.jar:
        name = str(cookie.name or "").strip()
        domain = str(cookie.domain or "").strip().lstrip(".")
        path = str(cookie.path or "/").strip() or "/"
        if not name or not domain:
            continue
        previous = previous_by_key.get((name, domain, path), {})
        cookies_by_key[(name, domain, path)] = {
            "name": name,
            "value": str(cookie.value or ""),
            "domain": previous.get("domain") or f".{domain}",
            "path": path,
            "expires": cookie.expires if cookie.expires is not None else previous.get("expires", -1),
            "httpOnly": bool(previous.get("httpOnly", False)),
            "secure": bool(cookie.secure or previous.get("secure", False)),
            "sameSite": previous.get("sameSite") or _cookie_same_site(cookie),
        }

    updated["cookies"] = list(cookies_by_key.values())
    return updated


def refresh_session_artifact(
    session_artifact: dict[str, Any] | None,
    *,
    user_agent: str | None = None,
    captured_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = dict(session_artifact) if isinstance(session_artifact, dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    artifact.setdefault("captured_at", captured_at or now)
    artifact["last_reused_at"] = now
    if user_agent:
        artifact["user_agent"] = user_agent
    if extra:
        artifact.update(extra)
    return artifact


class SavedArtifactHttpClient:
    def __init__(
        self,
        storage_state: dict[str, Any],
        session_artifact: dict[str, Any] | None = None,
        *,
        user_agent: str | None = None,
        timeout: float | httpx.Timeout = 30,
        follow_redirects: bool = True,
        trust_env: bool = False,
        seed_cookies: bool = False,
    ) -> None:
        self.storage_state = storage_state
        self.session_artifact = session_artifact if isinstance(session_artifact, dict) else {}
        self.user_agent = str(user_agent or "").strip()
        self.timeout = timeout
        self.follow_redirects = follow_redirects
        self.trust_env = trust_env
        self.seed_cookies = seed_cookies
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SavedArtifactHttpClient":
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=self.follow_redirects,
            trust_env=self.trust_env,
        )
        if self.seed_cookies:
            seed_storage_state_cookies(self.client, self.storage_state)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    def headers(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        include_cookie_header: bool = True,
    ) -> dict[str, str]:
        merged = dict(headers or {})
        if self.user_agent and not _headers_contain(merged, "User-Agent"):
            merged["User-Agent"] = self.user_agent
        if include_cookie_header and not _headers_contain(merged, "Cookie"):
            cookie_header = storage_state_cookie_header(self.storage_state, url)
            if cookie_header:
                merged["Cookie"] = cookie_header
        return merged

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        include_cookie_header: bool = True,
        refresh_cookies: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        if self.client is None:
            raise RuntimeError("SavedArtifactHttpClient must be used as an async context manager.")
        response = await self.client.request(
            method,
            url,
            headers=self.headers(url, headers, include_cookie_header=include_cookie_header),
            **kwargs,
        )
        if refresh_cookies and self.seed_cookies:
            self.refresh_storage_state_from_client()
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    def refresh_storage_state_from_client(self) -> dict[str, Any]:
        if self.client is None:
            return self.storage_state
        refreshed = storage_state_with_client_cookies(self.storage_state, self.client)
        self.storage_state.clear()
        self.storage_state.update(refreshed)
        return self.storage_state

    def refreshed_session_artifact(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return refresh_session_artifact(self.session_artifact, user_agent=self.user_agent, extra=extra)

    @staticmethod
    def response_payload(
        response: httpx.Response,
        *,
        parse_json=parse_json_body,
        include_headers: bool = False,
    ) -> dict[str, Any]:
        payload = parse_json(response.text) if parse_json else None
        result: dict[str, Any] = {
            "ok": response.is_success,
            "status": int(response.status_code),
            "url": str(response.url),
            "text": response.text,
            "payload": payload,
        }
        if include_headers:
            result["headers"] = dict(response.headers)
        return result

    @classmethod
    async def request_once(
        cls,
        storage_state: dict[str, Any],
        method: str,
        url: str,
        *,
        session_artifact: dict[str, Any] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | httpx.Timeout = 30,
        follow_redirects: bool = True,
        trust_env: bool = False,
        seed_cookies: bool = False,
        parse_json=parse_json_body,
        include_headers: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        async with cls(
            storage_state,
            session_artifact,
            user_agent=user_agent,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            seed_cookies=seed_cookies,
        ) as replay:
            response = await replay.request(method, url, headers=headers, **kwargs)
            return replay.response_payload(
                response,
                parse_json=parse_json,
                include_headers=include_headers,
            )


class SavedArtifactReplaySession:
    def __init__(
        self,
        provider: str,
        user_id: int,
        *,
        visible_auth_attempt_namespace: str | None = None,
        storage_artifact_kind: str = "storage_state",
        session_artifact_kind: str = "session",
        user_agent: str | None = None,
        user_agent_resolver: Callable[[dict[str, Any]], str] | None = None,
        fallback_user_agent: str = "",
        engine: str = "direct",
        log_event: Callable[..., None] | None = None,
    ) -> None:
        self.provider = provider
        self.user_id = user_id
        self.visible_auth_attempt_namespace = visible_auth_attempt_namespace
        self.storage_artifact_kind = storage_artifact_kind
        self.session_artifact_kind = session_artifact_kind
        self.user_agent = str(user_agent or "").strip()
        self.user_agent_resolver = user_agent_resolver
        self.fallback_user_agent = fallback_user_agent
        self.engine = engine
        self.log_event = log_event
        self.storage_state: dict[str, Any] | None = None
        self.session_artifact: dict[str, Any] = {}

    async def load_async(self) -> "SavedArtifactReplaySession":
        storage_state = await load_json_artifact_async(
            provider_runtime_artifact_path(
                self.user_id,
                self.provider,
                self.storage_artifact_kind,
                visible_auth_attempt_namespace=self.visible_auth_attempt_namespace,
            )
        )
        session_artifact = await load_json_artifact_async(
            provider_runtime_artifact_path(
                self.user_id,
                self.provider,
                self.session_artifact_kind,
                visible_auth_attempt_namespace=self.visible_auth_attempt_namespace,
            )
        )
        self.storage_state = storage_state if isinstance(storage_state, dict) else None
        self.session_artifact = dict(session_artifact) if isinstance(session_artifact, dict) else {}
        if not self.user_agent:
            if self.user_agent_resolver:
                self.user_agent = str(self.user_agent_resolver(self.session_artifact) or "").strip()
            elif self.fallback_user_agent:
                self.user_agent = session_user_agent(self.session_artifact, self.fallback_user_agent)
            else:
                self.user_agent = str(self.session_artifact.get("user_agent") or "").strip()
        return self

    def log(self, stage: str, **fields: Any) -> None:
        if not self.log_event:
            return
        fields.setdefault("user_id", self.user_id)
        fields.setdefault("engine", self.engine)
        self.log_event(stage, **fields)

    def missing_artifact_result(
        self,
        *,
        artifact_kind: str = "storage_state",
        stage: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        self.log(
            stage or f"headless sync missing {artifact_kind}",
            level="warning",
            has_session=bool(self.session_artifact),
        )
        return scraper_auth_required(message)

    def auth_required_result(
        self,
        *,
        stage: str = "headless sync auth_required",
        log_message: str | None = None,
        message: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        self.log(stage, message=log_message or message, **fields)
        return scraper_auth_required(message)

    def normalize_result(
        self,
        payload: Any,
        *,
        auth_message: str | None = None,
        unexpected_message: str = "Unexpected scraper response",
    ) -> dict[str, Any]:
        return normalize_scraper_result(
            payload,
            auth_message=auth_message,
            unexpected_message=unexpected_message,
        )

    def http_client(
        self,
        *,
        timeout: float | httpx.Timeout = 30,
        follow_redirects: bool = True,
        trust_env: bool = False,
        seed_cookies: bool = True,
    ) -> SavedArtifactHttpClient:
        if self.storage_state is None:
            raise RuntimeError("Saved artifact replay storage_state has not been loaded.")
        return SavedArtifactHttpClient(
            self.storage_state,
            self.session_artifact,
            user_agent=self.user_agent,
            timeout=timeout,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            seed_cookies=seed_cookies,
        )

    def refreshed_session_artifact(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return refresh_session_artifact(self.session_artifact, user_agent=self.user_agent, extra=extra)

    def save_session_artifact(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        artifact = self.refreshed_session_artifact(extra=extra)
        save_provider_runtime_artifact(
            self.user_id,
            self.provider,
            self.session_artifact_kind,
            artifact,
            visible_auth_attempt_namespace=self.visible_auth_attempt_namespace,
        )
        self.session_artifact.clear()
        self.session_artifact.update(artifact)
        return self.session_artifact

    async def save_storage_state_async(self, storage_state: dict[str, Any] | None = None) -> None:
        artifact = storage_state if isinstance(storage_state, dict) else self.storage_state
        if not isinstance(artifact, dict):
            return
        await save_provider_runtime_artifact_async(
            self.user_id,
            self.provider,
            self.storage_artifact_kind,
            artifact,
            visible_auth_attempt_namespace=self.visible_auth_attempt_namespace,
        )

    async def refresh_runtime_artifacts_async(
        self,
        storage_state: dict[str, Any] | None = None,
        *,
        session_artifact: dict[str, Any] | None = None,
        session_extra: dict[str, Any] | None = None,
    ) -> bool:
        storage_artifact = storage_state if isinstance(storage_state, dict) else self.storage_state
        if session_artifact is None:
            session_payload = self.refreshed_session_artifact(extra=session_extra)
        elif session_extra:
            session_payload = refresh_session_artifact(
                session_artifact,
                user_agent=self.user_agent,
                extra=session_extra,
            )
        else:
            session_payload = session_artifact
        try:
            await self.save_storage_state_async(storage_artifact)
            await save_provider_runtime_artifact_async(
                self.user_id,
                self.provider,
                self.session_artifact_kind,
                session_payload,
                visible_auth_attempt_namespace=self.visible_auth_attempt_namespace,
            )
        except Exception as exc:
            self.log("runtime state refresh failed", level="warning", message=str(exc))
            return False
        if isinstance(session_payload, dict):
            self.session_artifact.clear()
            self.session_artifact.update(session_payload)
        self.log(
            "runtime state refreshed",
            debug=True,
            storage_cookies=len(storage_artifact.get("cookies") or []) if isinstance(storage_artifact, dict) else 0,
            origins=len(storage_artifact.get("origins") or []) if isinstance(storage_artifact, dict) else 0,
        )
        return True

    @staticmethod
    def exception_is_network_error(exc: Exception) -> bool:
        return is_network_error(exc)


async def load_saved_artifact_replay_session_async(
    provider: str,
    user_id: int,
    *,
    user_agent_resolver: Callable[[dict[str, Any]], str] | None = None,
    log_event: Callable[..., None] | None = None,
    engine: str = "direct",
) -> SavedArtifactReplaySession:
    return await SavedArtifactReplaySession(
        provider,
        user_id,
        visible_auth_attempt_namespace=visible_auth_attempt_namespace(provider),
        user_agent_resolver=user_agent_resolver,
        engine=engine,
        log_event=log_event,
    ).load_async()
