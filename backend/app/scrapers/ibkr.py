import logging
import math
from typing import Any
from urllib.parse import urlsplit

from app.scrapers.browser_session import (
    SavedArtifactHttpClient,
    default_scraper_user_agent,
    load_saved_artifact_replay_session_async,
    scraper_session_user_agent,
)
from app.scrapers.results import scraper_auth_required, scraper_ok
from app.scrapers.scraper_logging import (
    log_scraper_event,
    sanitize_scraper_log_text,
    sanitize_scraper_log_url,
    scraper_debug_enabled,
)
from app.services.runtime_state import pop_runtime_state

PROVIDER = "ibkr"
VISIBLE_AUTH_ATTEMPT_NAMESPACE = f"visible_auth_attempt:{PROVIDER}"
logger = logging.getLogger("breaktwenty.scrapers.ibkr")

IBKR_PORTAL_URL = "https://portal.interactivebrokers.com/portal/#/dashboard"
API_BASE = "https://portal.interactivebrokers.com/portal.proxy/v1/portal"
IBKR_BROWSER_ENGINE = "httpx"
IBKR_DIRECT_TIMEOUT_SECONDS = 30
IBKR_DEBUG_ENV_VAR = "BREAKTWENTY_IBKR_DEBUG"
IBKR_SECDEF_BATCH_SIZE = 75


class IBKRAuthRequired(PermissionError):
    pass


def _ibkr_debug_logs_enabled() -> bool:
    return scraper_debug_enabled(logger=logger, env_names=(IBKR_DEBUG_ENV_VAR,))


def _ibkr_log_event(
    stage: str,
    *,
    level: str = "info",
    debug: bool = False,
    **fields: Any,
) -> None:
    log_scraper_event(
        logger,
        provider=PROVIDER,
        stage=stage,
        level=level,
        debug=debug,
        debug_enabled=_ibkr_debug_logs_enabled(),
        **fields,
    )


def _ibkr_sanitize_log_text(text: str | None, *, limit: int = 240) -> str:
    return sanitize_scraper_log_text(text, limit=limit)


def _ibkr_sanitize_url_for_log(url: str | None) -> str:
    return sanitize_scraper_log_url(url)


def _ibkr_mask_account_id(account_id: str | None) -> str:
    text = str(account_id or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return text
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def _ibkr_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _ibkr_user_agent(user_agent: str | None = None) -> str:
    return default_scraper_user_agent(user_agent)


def _ibkr_session_user_agent(session_artifact: dict[str, Any] | None) -> str:
    return scraper_session_user_agent(session_artifact)


def _ibkr_direct_headers(
    *,
    user_agent: str,
) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-CA,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": IBKR_PORTAL_URL,
        "User-Agent": _ibkr_user_agent(user_agent),
    }


def _ibkr_response_requires_auth(status: int, url: str, body_text: str | None = None) -> bool:
    if status in (401, 403):
        return True
    parsed = urlsplit(str(url or "").strip())
    path = parsed.path.lower()
    if "/sso/" in path or path.endswith("/sso/login"):
        return True
    body = str(body_text or "").lstrip()
    return bool(body.startswith("<") and ("sso/login" in body.lower() or "xyz-field-username" in body.lower()))


async def _ibkr_direct_get_json(
    replay: SavedArtifactHttpClient,
    url: str,
    *,
    user_id: int | None,
    endpoint: str,
    account_id: str | None = None,
):
    sanitized_url = _ibkr_sanitize_url_for_log(url)
    _ibkr_log_event(
        "api request",
        user_id=user_id,
        endpoint=endpoint,
        account=_ibkr_mask_account_id(account_id),
        url=sanitized_url,
        engine=IBKR_BROWSER_ENGINE,
        debug=True,
    )
    response = await replay.get(
        url,
        headers=_ibkr_direct_headers(user_agent=replay.user_agent),
    )
    body_text = response.text
    if _ibkr_response_requires_auth(int(response.status_code), str(response.url), body_text):
        _ibkr_log_event(
            "api auth required",
            user_id=user_id,
            endpoint=endpoint,
            account=_ibkr_mask_account_id(account_id),
            status=int(response.status_code),
            url=_ibkr_sanitize_url_for_log(str(response.url)),
            engine=IBKR_BROWSER_ENGINE,
        )
        raise IBKRAuthRequired("Interactive Brokers saved session is no longer authenticated.")
    if response.status_code >= 400:
        _ibkr_log_event(
            "api request failed",
            user_id=user_id,
            endpoint=endpoint,
            account=_ibkr_mask_account_id(account_id),
            status=int(response.status_code),
            url=sanitized_url,
            engine=IBKR_BROWSER_ENGINE,
            level="warning",
            message=_ibkr_sanitize_log_text(body_text, limit=180),
        )
        raise RuntimeError(f"Interactive Brokers {endpoint} request failed with status {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        if _ibkr_response_requires_auth(int(response.status_code), str(response.url), body_text):
            raise IBKRAuthRequired("Interactive Brokers saved session returned a login page.") from exc
        raise RuntimeError(f"Interactive Brokers {endpoint} response was not JSON.") from exc
    _ibkr_log_event(
        "api response",
        user_id=user_id,
        endpoint=endpoint,
        account=_ibkr_mask_account_id(account_id),
        status=int(response.status_code),
        url=sanitized_url,
        engine=IBKR_BROWSER_ENGINE,
        chars=len(body_text or ""),
        debug=True,
    )
    return payload


async def try_headless_sync(user_id: int):
    replay_session = await load_saved_artifact_replay_session_async(
        PROVIDER,
        user_id,
        user_agent_resolver=_ibkr_session_user_agent,
        engine=IBKR_BROWSER_ENGINE,
        log_event=_ibkr_log_event,
    )
    storage_state = replay_session.storage_state
    session_artifact_state = replay_session.session_artifact

    if not isinstance(storage_state, dict):
        _ibkr_log_event(
            "direct sync missing storage_state",
            user_id=user_id,
            engine=IBKR_BROWSER_ENGINE,
            has_session=bool(session_artifact_state),
            level="warning",
        )
        return scraper_auth_required()

    _ibkr_log_event(
        "direct sync start",
        user_id=user_id,
        engine=IBKR_BROWSER_ENGINE,
        has_storage_state=True,
        has_session=bool(session_artifact_state),
        storage_cookies=len(storage_state.get("cookies") or []),
        origins=len(storage_state.get("origins") or []),
    )
    try:
        async with replay_session.http_client(
            timeout=IBKR_DIRECT_TIMEOUT_SECONDS,
            follow_redirects=False,
            seed_cookies=True,
        ) as replay:
            data = await scrape_via_api(replay, user_id=user_id)
        accounts = data.get("accounts") if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get("status") == "ok":
            runtime_state_refreshed = await replay_session.refresh_runtime_artifacts_async(
                storage_state,
                session_extra={"browser_engine": IBKR_BROWSER_ENGINE},
            )
            _ibkr_log_event(
                "direct sync success",
                user_id=user_id,
                engine=IBKR_BROWSER_ENGINE,
                accounts=len(accounts or []),
                runtime_state_refreshed=runtime_state_refreshed,
            )
        else:
            _ibkr_log_event(
                "direct sync empty result",
                user_id=user_id,
                engine=IBKR_BROWSER_ENGINE,
                level="warning",
                status=data.get("status") if isinstance(data, dict) else None,
            )
        return data

    except IBKRAuthRequired:
        _ibkr_log_event(
            "direct sync auth_required",
            user_id=user_id,
            engine=IBKR_BROWSER_ENGINE,
            message="saved storage/session state did not reach authenticated Interactive Brokers APIs",
        )
        return scraper_auth_required()
    except Exception as e:
        _ibkr_log_event(
            "direct sync exception",
            user_id=user_id,
            engine=IBKR_BROWSER_ENGINE,
            level="warning",
            message=str(e),
        )
        raise


async def scrape_via_api(
    replay: SavedArtifactHttpClient,
    *,
    user_id: int | None = None,
):
    _ibkr_log_event(
        "api scrape start",
        user_id=user_id,
        engine=IBKR_BROWSER_ENGINE,
        debug=True,
    )
    try:
        acct_list = await _ibkr_direct_get_json(
            replay,
            f"{API_BASE}/portfolio2/accounts",
            user_id=user_id,
            endpoint="accounts",
        )

        if not isinstance(acct_list, list) or len(acct_list) == 0:
            _ibkr_log_event("api accounts empty", user_id=user_id, engine=IBKR_BROWSER_ENGINE, level="warning")
            return scraper_auth_required()
        _ibkr_log_event(
            "api accounts fetched",
            user_id=user_id,
            count=len(acct_list),
            engine=IBKR_BROWSER_ENGINE,
            debug=True,
        )

        accounts = []

        for acct in acct_list:
            if not isinstance(acct, dict):
                continue
            acct_id = str(acct.get("accountId") or "").strip()
            if not acct_id:
                continue

            ledger_entries = []
            try:
                ledger_entries = await _ibkr_direct_get_json(
                    replay,
                    f"{API_BASE}/portfolio2/{acct_id}/ledger",
                    user_id=user_id,
                    endpoint="ledger",
                    account_id=acct_id,
                )
                if not isinstance(ledger_entries, list):
                    ledger_entries = []
                ledger_entries = [entry for entry in ledger_entries if isinstance(entry, dict)]
            except IBKRAuthRequired:
                raise
            except Exception as e:
                _ibkr_log_event(
                    "ledger parse failed",
                    user_id=user_id,
                    account=_ibkr_mask_account_id(acct_id),
                    engine=IBKR_BROWSER_ENGINE,
                    level="warning",
                    message=str(e),
                )

            nav = 0
            ledger_by_currency = {}
            for entry in ledger_entries:
                key = entry.get("secondKey", "")
                ledger_by_currency[key] = entry
                if key == "BASE":
                    nav = _ibkr_float(entry.get("netLiquidationValue", entry.get("netliquidationvalue", 0)))

            positions = []
            try:
                positions = await _ibkr_direct_get_json(
                    replay,
                    f"{API_BASE}/portfolio2/{acct_id}/positions",
                    user_id=user_id,
                    endpoint="positions",
                    account_id=acct_id,
                )
                if not isinstance(positions, list):
                    positions = []
                positions = [position for position in positions if isinstance(position, dict)]
            except IBKRAuthRequired:
                raise
            except Exception as e:
                _ibkr_log_event(
                    "positions parse failed",
                    user_id=user_id,
                    account=_ibkr_mask_account_id(acct_id),
                    engine=IBKR_BROWSER_ENGINE,
                    level="warning",
                    message=str(e),
                )

            if not positions:
                try:
                    positions = await _ibkr_direct_get_json(
                        replay,
                        f"{API_BASE}/portfolio/{acct_id}/positions/0",
                        user_id=user_id,
                        endpoint="positions_fallback",
                        account_id=acct_id,
                    )
                    if not isinstance(positions, list):
                        positions = []
                    positions = [position for position in positions if isinstance(position, dict)]
                except IBKRAuthRequired:
                    raise
                except Exception as e:
                    _ibkr_log_event(
                        "positions fallback parse failed",
                        user_id=user_id,
                        account=_ibkr_mask_account_id(acct_id),
                        engine=IBKR_BROWSER_ENGINE,
                        level="warning",
                        message=str(e),
                    )

            alias = acct.get("accountAlias", "")
            display = acct.get("displayName", "")
            title = acct.get("accountTitle", "")
            name = str(alias or display or title or f"IBKR {acct_id}").strip()
            cust_type = acct.get("acctCustType", "")
            _ibkr_log_event(
                "account parsed",
                user_id=user_id,
                account=_ibkr_mask_account_id(acct_id),
                name=_ibkr_sanitize_log_text(name, limit=120),
                positions=len(positions),
                currencies=len(ledger_by_currency),
                balance=nav,
                engine=IBKR_BROWSER_ENGINE,
                debug=True,
            )

            accounts.append({
                "name": name,
                "balance": nav,
                "acct_id": acct_id,
                "cust_type": cust_type,
                "type": acct.get("type", ""),
                "currency": acct.get("currency", "CAD"),
                "positions": positions,
                "ledger_by_currency": ledger_by_currency,
            })

        if accounts:
            try:
                name_by_conid = await _enrich_ibkr_stk_names(replay, accounts, user_id=user_id)
            except IBKRAuthRequired:
                raise
            except Exception as e:
                _ibkr_log_event(
                    "stk name enrichment exception",
                    user_id=user_id,
                    engine=IBKR_BROWSER_ENGINE,
                    level="warning",
                    message=str(e),
                )
                name_by_conid = {}
            for acct in accounts:
                per_acct: dict[int, str] = {}
                for pos in acct.get("positions") or []:
                    if not isinstance(pos, dict):
                        continue
                    conid = _ibkr_position_conid(pos)
                    if conid is None:
                        continue
                    enriched = name_by_conid.get(conid)
                    if enriched:
                        per_acct[conid] = enriched
                if per_acct:
                    acct["name_by_conid"] = per_acct

        _ibkr_log_event("api scrape success", user_id=user_id, accounts=len(accounts), engine=IBKR_BROWSER_ENGINE)
        return scraper_ok(accounts) if accounts else scraper_auth_required()

    except IBKRAuthRequired:
        raise
    except Exception as e:
        _ibkr_log_event(
            "api scrape exception",
            user_id=user_id,
            engine=IBKR_BROWSER_ENGINE,
            level="warning",
            message=str(e),
        )
        raise


def _ibkr_position_conid(pos: dict) -> int | None:
    try:
        return int(pos.get("conid"))
    except (TypeError, ValueError):
        return None


def _ibkr_position_asset_class(pos: dict) -> str:
    return str(pos.get("assetClass") or pos.get("secType") or "").upper()


async def _fetch_ibkr_stk_secdef_names(
    replay: SavedArtifactHttpClient,
    conids: list[int],
    *,
    user_id: int | None,
) -> dict[int, str]:
    result: dict[int, str] = {}
    if not conids:
        return result
    unique = sorted({int(c) for c in conids if c})
    for offset in range(0, len(unique), IBKR_SECDEF_BATCH_SIZE):
        batch = unique[offset:offset + IBKR_SECDEF_BATCH_SIZE]
        url = f"{API_BASE}/trsrv/secdef?conids={','.join(str(c) for c in batch)}"
        try:
            payload = await _ibkr_direct_get_json(
                replay,
                url,
                user_id=user_id,
                endpoint="secdef",
            )
        except IBKRAuthRequired:
            raise
        except Exception as e:
            _ibkr_log_event(
                "secdef batch failed",
                user_id=user_id,
                engine=IBKR_BROWSER_ENGINE,
                level="warning",
                batch_size=len(batch),
                message=str(e),
            )
            continue
        secdefs = payload.get("secdef") if isinstance(payload, dict) else None
        if not isinstance(secdefs, list):
            continue
        for entry in secdefs:
            if not isinstance(entry, dict):
                continue
            try:
                conid_int = int(entry.get("conid"))
            except (TypeError, ValueError):
                continue
            for key in ("name", "description", "companyHeader", "longName"):
                name_val = str(entry.get(key) or "").strip()
                if name_val:
                    result[conid_int] = name_val
                    break
    return result


async def _enrich_ibkr_stk_names(
    replay: SavedArtifactHttpClient,
    accounts: list[dict],
    *,
    user_id: int | None,
) -> dict[int, str]:
    from app.services.symbol_names import get_cached_symbol_names, upsert_symbol_names

    all_conids: set[int] = set()
    for acct in accounts:
        for pos in acct.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            if _ibkr_position_asset_class(pos) != "STK":
                continue
            conid = _ibkr_position_conid(pos)
            if conid is not None:
                all_conids.add(conid)

    cached = await get_cached_symbol_names("ibkr", [str(c) for c in all_conids])
    name_by_conid: dict[int, str] = {}
    for conid in all_conids:
        cached_name = cached.get(str(conid))
        if cached_name:
            name_by_conid[conid] = cached_name

    conids_to_fetch = sorted(all_conids - set(name_by_conid.keys()))
    fetched_count = 0
    if conids_to_fetch:
        try:
            fetched = await _fetch_ibkr_stk_secdef_names(
                replay, conids_to_fetch, user_id=user_id
            )
        except IBKRAuthRequired:
            raise
        for conid, name in fetched.items():
            if conid in all_conids:
                name_by_conid[conid] = name
                fetched_count += 1
        if fetched:
            await upsert_symbol_names(
                "ibkr",
                {str(conid): name for conid, name in fetched.items()},
            )
    _ibkr_log_event(
        "stk name enrichment",
        user_id=user_id,
        engine=IBKR_BROWSER_ENGINE,
        cache_hits=len(name_by_conid) - fetched_count,
        fetched=fetched_count,
        requested=len(conids_to_fetch),
        debug=True,
    )
    return name_by_conid


async def cleanup_pending(user_id: int):
    pop_runtime_state(PROVIDER, user_id, None)
