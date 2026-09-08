from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.runtime_paths import DIAGNOSTIC_DIR, runtime_display_path
from app.services.runtime_state import get_runtime_state
from app.services.support_logging import (
    DEFAULT_SUPPORT_CAPTURE_LEVEL,
    get_support_capture_level,
    normalize_capture_level,
    support_capture_is_developer_local,
)
from app.services.sync_tracking import get_provider_sync_id
from app.services.time_utils import utc_now as _utc_now

logger = logging.getLogger("breaktwenty.diagnostics.replay")

REPLAY_DIAGNOSTICS_KEEP = 3
REPLAY_DIAGNOSTICS_SCHEMA_VERSION = 1
REDACTED_VALUE = "<redacted>"
# Sample-row caps are level-aware: the shippable `redacted_rich` baseline stays
# minimal (privacy + bounded support bundles), while local-only `developer_local`
# captures the full row spread so first-party work (e.g. mapping a provider's
# category taxonomy) sees every value, not just a structural handful.
MAX_SAMPLE_RECORDS = 3
MAX_SAMPLE_RECORDS_DEVELOPER_LOCAL = 1000
MAX_SAMPLE_LIST_ITEMS = 5
MAX_SAMPLE_DEPTH = 5
MAX_STRING_LENGTH = 900

SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "authtoken",
    "auth_token",
    "bearer",
    "cardnumber",
    "card_number",
    "cookie",
    "credential",
    "deviceid",
    "device_id",
    "mac_addr",
    "mac_address",
    "macaddr",
    "macaddress",
    "password",
    "secret",
    "session",
    "token",
    "x-auth-token",
    "x-device-id",
    "xsrf",
)
SENSITIVE_KEYS = frozenset(
    {
        "ploan",
    }
)
ACCOUNT_IDENTIFIER_KEYS = frozenset(
    {
        "acc_id",
        "account_display",
        "accountid",
        "account_id",
        "accountkey",
        "account_key",
        "accountnumber",
        "account_number",
        "acct_num",
        "card_num",
        "card_actv_id",
        "card_activation_id",
        "cardnumber",
        "card_number",
        "encoded_account_number",
        "external_id",
        "externalid",
        "number",
        "provider_account_id",
        "uni_card_num",
    }
)
STRICT_IDENTIFIER_KEYS = frozenset(
    {
        "acquirerreferencenumber",
        "encryptedid",
        "encrypted_id",
        "key",
        "transactionid",
        "transaction_id",
        "transactionkey",
        "transaction_key",
    }
)
# Free-text narrative fields that carry financial PII (what was bought, from
# whom, and any user/merchant message). Redacted at the redacted_rich baseline
# and only retained at developer_local. Structural siblings such as mcc /
# categoryCode / amount / currency / type / status are intentionally NOT here.
NARRATIVE_PII_KEY_FRAGMENTS = (
    "counterparty",
    "description",
    "memo",
    "merchant",
    "narrative",
    "payee",
    "remittance",
)
NARRATIVE_PII_KEYS = frozenset(
    {
        "comment",
        "comments",
        "message",
        "name",
        "note",
        "notes",
        "recipientname",
        "sendername",
    }
)
COMPACT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{12,}$")
PROVIDER_PREFIXED_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_:-]{1,32}:[A-Za-z0-9._:=/-]{8,}$", re.IGNORECASE)
DEVELOPER_LOCAL_SAFE_KEY_PREFIXES = ("has", "is")
DEVELOPER_LOCAL_SAFE_KEY_SUFFIXES = (
    "available",
    "count",
    "counts",
    "cookies",
    "enabled",
    "expired",
    "fresh",
    "origins",
    "present",
    "removed",
    "slot",
    "succeeded",
    "type",
    "used",
    "valid",
)
DEVELOPER_LOCAL_SAFE_STRING_VALUES = frozenset(
    {
        "active",
        "bearer",
        "false",
        "missing",
        "no",
        "none",
        "null",
        "present",
        "quarantine",
        "true",
        "yes",
    }
)
DEVELOPER_LOCAL_SAFE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
VISIBLE_AUTH_ATTEMPT_NAMESPACE_PREFIX = "visible_auth_attempt:"
PROVIDER_DIAGNOSTIC_DIRS = {
    "amex": "AMEX",
    "bmo": "BMO",
    "cibc": "CIBC",
    "coinbase": "Coinbase",
    "eqbank": "EQBank",
    "ibkr": "IBKR",
    "ibkr_flex": "IBKR",
    "moomoo": "Moomoo",
    "national": "National",
    "questrade": "Questrade",
    "rbc": "RBC",
    "scotiabank": "Scotiabank",
    "tangerine": "Tangerine",
    "td": "TD",
    "wealthsimple": "Wealthsimple",
    "wise": "Wise",
}
# Known route labels are structural diagnostics, not customer identifiers. Keep
# them readable even when their length would otherwise match the conservative
# compact-identifier heuristic.
SAFE_URL_PATH_SEGMENTS = frozenset(
    {
        "authorized_trd_accs",
        "fills_history",
        "history_deal",
        "history_order",
        "orders_history",
    }
)


def _timestamp_slug(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_file_component(value: Any, *, max_length: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return normalized[:max_length]


def _provider_dir(provider: str, provider_dir: str | None = None) -> str:
    configured = str(provider_dir or "").strip()
    if configured:
        return configured
    mapped = PROVIDER_DIAGNOSTIC_DIRS.get(str(provider or "").strip().lower())
    if mapped:
        return mapped
    provider_slug = _safe_file_component(provider, max_length=40)
    return provider_slug.upper() if provider_slug else "Provider"


def _diagnostic_dir(provider: str, provider_dir: str | None = None) -> Path:
    return DIAGNOSTIC_DIR / _provider_dir(provider, provider_dir)


def _phase2_prefix(provider: str) -> str:
    provider_slug = _safe_file_component(provider, max_length=40) or "provider"
    return f"{provider_slug}_phase2_replay"


def _run_suffix(sync_id: str | None, attempt_id: str | None) -> str:
    parts: list[str] = []
    sync_component = _safe_file_component(sync_id)
    attempt_component = _safe_file_component(attempt_id)
    if sync_component:
        parts.append(f"sync-{sync_component}")
    if attempt_component:
        parts.append(f"attempt-{attempt_component}")
    return "_".join(parts)


def phase2_replay_path(
    provider: str,
    user_id: int,
    *,
    timestamp_slug: str,
    result: str,
    sync_id: str | None = None,
    attempt_id: str | None = None,
    provider_dir: str | None = None,
) -> Path:
    result_component = _safe_file_component(result, max_length=40) or "finished"
    run_suffix = _run_suffix(sync_id, attempt_id)
    filename_parts = [
        _phase2_prefix(provider),
        f"user{int(user_id)}",
        timestamp_slug,
    ]
    if run_suffix:
        filename_parts.append(run_suffix)
    filename_parts.append(result_component)
    return _diagnostic_dir(provider, provider_dir) / f"{'_'.join(filename_parts)}.jsonl"


def _phase2_glob(provider: str, user_id: int) -> str:
    return f"{_phase2_prefix(provider)}_user{int(user_id)}_*.jsonl"


def prune_phase2_replay_diagnostics(
    provider: str,
    user_id: int,
    *,
    provider_dir: str | None = None,
    keep: int = REPLAY_DIAGNOSTICS_KEEP,
) -> None:
    directory = _diagnostic_dir(provider, provider_dir)
    if not directory.exists():
        return
    candidates = []
    for path in directory.glob(_phase2_glob(provider, user_id)):
        try:
            candidates.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    for _mtime, _name, path in candidates[max(0, keep) :]:
        try:
            path.unlink()
        except OSError:
            continue


def find_phase1_har_path(
    provider: str,
    user_id: int,
    *,
    sync_id: str | None = None,
    attempt_id: str | None = None,
    provider_dir: str | None = None,
) -> Path | None:
    directory = _diagnostic_dir(provider, provider_dir)
    if not directory.exists():
        return None
    sync_component = _safe_file_component(sync_id)
    attempt_component = _safe_file_component(attempt_id)
    candidates: list[tuple[float, str, Path]] = []
    provider_slug = _safe_file_component(provider, max_length=40) or "provider"
    patterns = (
        f"{provider_slug}_visible_auth_user{int(user_id)}_*.har",
        f"{provider_slug}_visible_auth_user-{int(user_id)}_*.har",
    )
    for pattern in patterns:
        for path in directory.glob(pattern):
            name = path.name
            if sync_component and f"sync-{sync_component}" not in name:
                continue
            if attempt_component and f"attempt-{attempt_component}" not in name:
                continue
            try:
                candidates.append((path.stat().st_mtime, name, path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key or "").strip().lower())


def _is_sensitive_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in {_normalized_key(item) for item in SENSITIVE_KEYS} or any(
        _normalized_key(fragment) in normalized for fragment in SENSITIVE_KEY_FRAGMENTS
    )


def _developer_local_safe_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized.startswith(DEVELOPER_LOCAL_SAFE_KEY_PREFIXES) or any(
        normalized.endswith(suffix) for suffix in DEVELOPER_LOCAL_SAFE_KEY_SUFFIXES
    )


def _developer_local_safe_sensitive_value(key: Any, value: Any, *, capture_level: str) -> bool:
    if not support_capture_is_developer_local(capture_level):
        return False
    if value is None:
        return True
    if isinstance(value, bool):
        return _developer_local_safe_key(key)
    if isinstance(value, (int, float)):
        return _developer_local_safe_key(key)
    if not isinstance(value, str) or not _developer_local_safe_key(key):
        return False
    normalized_value = value.strip().strip("\"'").lower()
    return bool(
        normalized_value in DEVELOPER_LOCAL_SAFE_STRING_VALUES
        or DEVELOPER_LOCAL_SAFE_NUMBER_RE.fullmatch(normalized_value)
    )


def _is_account_identifier_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in {re.sub(r"[^a-z0-9]+", "", item) for item in ACCOUNT_IDENTIFIER_KEYS}


def _looks_like_identifier(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(COMPACT_IDENTIFIER_RE.fullmatch(text)) and (
        any(char.isalpha() for char in text) or len(re.sub(r"\D+", "", text)) >= 8
    )


def _fingerprint_value(value: Any, *, include_suffix: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:10]
    if not include_suffix:
        return f"<redacted:{digest}>"
    suffix = text[-4:] if len(text) >= 4 else text
    return f"<redacted:{digest}:last4:{suffix}>"


def _is_strict_identifier_key(key: Any) -> bool:
    return _normalized_key(key) in {re.sub(r"[^a-z0-9]+", "", item) for item in STRICT_IDENTIFIER_KEYS}


def _is_narrative_pii_key(key: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in {re.sub(r"[^a-z0-9]+", "", item) for item in NARRATIVE_PII_KEYS}:
        return True
    return any(fragment in normalized for fragment in NARRATIVE_PII_KEY_FRAGMENTS)


def _looks_like_provider_identifier(value: Any) -> bool:
    return bool(PROVIDER_PREFIXED_IDENTIFIER_RE.fullmatch(str(value or "").strip()))


def _sanitize_scalar(key: str, value: Any, *, capture_level: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if _is_sensitive_key(key):
        if _developer_local_safe_sensitive_value(key, value, capture_level=capture_level):
            return str(value)
        return REDACTED_VALUE
    developer_local = support_capture_is_developer_local(capture_level)
    if not developer_local and _is_narrative_pii_key(key):
        return REDACTED_VALUE
    include_suffix = developer_local
    if _is_account_identifier_key(key) or (_normalized_key(key) == "id" and _looks_like_identifier(value)):
        return _fingerprint_value(value, include_suffix=include_suffix)
    if not developer_local and (_is_strict_identifier_key(key) or _looks_like_provider_identifier(value)):
        return _fingerprint_value(value, include_suffix=False)
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if len(text) > MAX_STRING_LENGTH:
        return f"{text[:MAX_STRING_LENGTH].rstrip()}..."
    return text


def _object_to_payload(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        for args in (("records",), ()):
            try:
                converted = to_dict(*args)
            except Exception:
                continue
            if converted is not value:
                return converted
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def sanitize_payload(
    value: Any,
    *,
    key: str = "",
    depth: int = 0,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> Any:
    if depth > MAX_SAMPLE_DEPTH:
        return "<max-depth>"
    value = _object_to_payload(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for item_key, item_value in value.items():
            item_key_text = str(item_key)
            include_suffix = support_capture_is_developer_local(capture_level)
            if _is_sensitive_key(item_key_text):
                if _developer_local_safe_sensitive_value(item_key_text, item_value, capture_level=capture_level):
                    sanitized[item_key_text] = item_value
                else:
                    sanitized[item_key_text] = REDACTED_VALUE
            elif _is_account_identifier_key(item_key_text) or (_normalized_key(item_key_text) == "id" and _looks_like_identifier(item_value)):
                sanitized[item_key_text] = _fingerprint_value(item_value, include_suffix=include_suffix)
            elif not include_suffix and (_is_strict_identifier_key(item_key_text) or _looks_like_provider_identifier(item_value)):
                sanitized[item_key_text] = _fingerprint_value(item_value, include_suffix=False)
            else:
                sanitized[item_key_text] = sanitize_payload(
                    item_value,
                    key=item_key_text,
                    depth=depth + 1,
                    capture_level=capture_level,
                )
        return sanitized
    if isinstance(value, list):
        return [
            sanitize_payload(item, key=key, depth=depth + 1, capture_level=capture_level)
            for item in value[:MAX_SAMPLE_LIST_ITEMS]
        ]
    if isinstance(value, tuple):
        return [
            sanitize_payload(item, key=key, depth=depth + 1, capture_level=capture_level)
            for item in value[:MAX_SAMPLE_LIST_ITEMS]
        ]
    return _sanitize_scalar(key, value, capture_level=capture_level)


def _sanitize_url_path(path: str) -> str:
    segments = []
    for segment in str(path or "").split("/"):
        segments.append(
            _fingerprint_value(segment)
            if segment.lower() not in SAFE_URL_PATH_SEGMENTS and _looks_like_identifier(segment)
            else segment
        )
    return "/".join(segments)


def _url_summary(
    url: str,
    params: dict[str, Any] | None,
    *,
    capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
) -> dict[str, Any]:
    parsed = urlsplit(str(url or ""))
    query: dict[str, Any] = {}
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        query[name] = value
    if isinstance(params, dict):
        query.update(params)
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path": _sanitize_url_path(parsed.path),
        "query": sanitize_payload(query, capture_level=capture_level),
    }


def _payload_shape(payload: Any) -> dict[str, Any]:
    payload = _object_to_payload(payload)
    if isinstance(payload, dict):
        return {
            "type": "object",
            "keys": sorted(str(key) for key in payload.keys())[:80],
        }
    if isinstance(payload, list):
        return {
            "type": "array",
            "items": len(payload),
        }
    return {"type": type(payload).__name__}


def _record_keys(records: list[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for record in records:
        if isinstance(record, dict):
            keys.update(str(key) for key in record.keys())
    return sorted(keys)[:120]


def _coerce_record_list(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    coerced: list[dict[str, Any]] = []
    for record in records:
        converted = _object_to_payload(record)
        if isinstance(converted, dict):
            coerced.append(converted)
    return coerced


def current_visible_auth_attempt_id(provider: str, user_id: int | None) -> str:
    if user_id is None:
        return ""
    state = get_runtime_state(f"{VISIBLE_AUTH_ATTEMPT_NAMESPACE_PREFIX}{provider}", int(user_id), {})
    if isinstance(state, dict):
        return str(state.get("attempt_id") or "").strip()
    return ""


class ReplayDiagnosticsCapture:
    def __init__(
        self,
        provider: str,
        user_id: int,
        *,
        sync_id: str | None = None,
        attempt_id: str | None = None,
        provider_dir: str | None = None,
        phase1_har_path: Path | None = None,
        capture_level: str = DEFAULT_SUPPORT_CAPTURE_LEVEL,
    ) -> None:
        self.provider = str(provider or "").strip().lower()
        self.user_id = int(user_id)
        self.sync_id = str(sync_id or "").strip()
        self.attempt_id = str(attempt_id or "").strip()
        self.provider_dir = provider_dir
        self.timestamp_slug = _timestamp_slug()
        self.phase1_har_path = phase1_har_path
        self.capture_level = normalize_capture_level(capture_level)
        self._finished = False
        directory = _diagnostic_dir(self.provider, provider_dir)
        directory.mkdir(parents=True, exist_ok=True)
        run_suffix = _run_suffix(self.sync_id, self.attempt_id)
        temp_parts = [
            _phase2_prefix(self.provider),
            f"user{self.user_id}",
            self.timestamp_slug,
        ]
        if run_suffix:
            temp_parts.append(run_suffix)
        temp_parts.append("in_progress")
        self.path = directory / f"{'_'.join(temp_parts)}.jsonl"
        if not self._write(
            {
                "event": "start",
                "phase": "phase2_backend_replay",
                "schema_version": REPLAY_DIAGNOSTICS_SCHEMA_VERSION,
                "provider": self.provider,
                "user_id": self.user_id,
                "sync_id": self.sync_id,
                "attempt_id": self.attempt_id,
                "capture_level": self.capture_level,
                "phase1_har_path": runtime_display_path(phase1_har_path) if phase1_har_path else "",
                "captured_at": _utc_now().isoformat(),
            }
        ):
            raise OSError(f"Unable to write replay diagnostics to {self.path}")

    def _write(self, payload: dict[str, Any]) -> bool:
        if self._finished:
            return False
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
                handle.write("\n")
            return True
        except OSError as exc:
            self._finished = True
            logger.warning(
                "phase2 replay diagnostics disabled after write failure",
                extra={
                    "provider": self.provider,
                    "user_id": self.user_id,
                    "path": runtime_display_path(self.path),
                    "error": type(exc).__name__,
                },
            )
            return False

    def record_http_fetch(
        self,
        *,
        stage: str,
        method: str,
        url: str,
        status: int | None,
        params: dict[str, Any] | None = None,
        request_json: Any | None = None,
        payload: Any | None = None,
        records: Any | None = None,
        record_source: str = "",
        account_ref: Any | None = None,
        window: dict[str, Any] | None = None,
        transport: str = "http",
    ) -> None:
        if self._finished:
            return
        record_list = _coerce_record_list(records)
        developer_local = support_capture_is_developer_local(self.capture_level)
        sample_limit = MAX_SAMPLE_RECORDS_DEVELOPER_LOCAL if developer_local else MAX_SAMPLE_RECORDS
        include_payload_sample = developer_local and not record_list
        include_suffix = developer_local
        self._write(
            {
                "event": "http_fetch",
                "phase": "phase2_backend_replay",
                "schema_version": REPLAY_DIAGNOSTICS_SCHEMA_VERSION,
                "provider": self.provider,
                "user_id": self.user_id,
                "sync_id": self.sync_id,
                "attempt_id": self.attempt_id,
                "capture_level": self.capture_level,
                "captured_at": _utc_now().isoformat(),
                "stage": str(stage or "").strip(),
                "transport": str(transport or "http").strip(),
                "method": str(method or "").strip().upper(),
                "url": _url_summary(url, params, capture_level=self.capture_level),
                "request_json": sanitize_payload(request_json, capture_level=self.capture_level) if request_json is not None else None,
                "status": int(status or 0),
                "account_ref": _fingerprint_value(account_ref, include_suffix=include_suffix) if account_ref else "",
                "window": sanitize_payload(window or {}, capture_level=self.capture_level),
                "response": {
                    "payload_shape": _payload_shape(payload),
                    "record_source": str(record_source or "").strip(),
                    "record_count": len(record_list),
                    "record_keys": _record_keys(record_list),
                    "sample_records": [
                        sanitize_payload(record, capture_level=self.capture_level)
                        for record in record_list[:sample_limit]
                    ],
                    "payload_sample": sanitize_payload(payload, capture_level=self.capture_level) if include_payload_sample else None,
                },
            }
        )

    def record_data_fetch(
        self,
        *,
        stage: str,
        payload: Any | None = None,
        records: Any | None = None,
        record_source: str = "",
        account_ref: Any | None = None,
        window: dict[str, Any] | None = None,
        transport: str = "provider",
        url: str = "",
    ) -> None:
        self.record_http_fetch(
            stage=stage,
            method=str(transport or "provider").upper(),
            url=url or f"{self.provider}://{stage}",
            status=200,
            payload=payload,
            records=records,
            record_source=record_source,
            account_ref=account_ref,
            window=window,
            transport=transport,
        )

    def finish(self, result: str) -> Path:
        if self._finished:
            return self.path
        if not self._write(
            {
                "event": "finish",
                "phase": "phase2_backend_replay",
                "schema_version": REPLAY_DIAGNOSTICS_SCHEMA_VERSION,
                "provider": self.provider,
                "user_id": self.user_id,
                "sync_id": self.sync_id,
                "attempt_id": self.attempt_id,
                "capture_level": self.capture_level,
                "result": str(result or "finished").strip() or "finished",
                "captured_at": _utc_now().isoformat(),
            }
        ):
            return self.path
        final_path = phase2_replay_path(
            self.provider,
            self.user_id,
            timestamp_slug=self.timestamp_slug,
            result=result,
            sync_id=self.sync_id,
            attempt_id=self.attempt_id,
            provider_dir=self.provider_dir,
        )
        try:
            self.path.replace(final_path)
        except OSError:
            final_path = self.path
        self.path = final_path
        self._finished = True
        prune_phase2_replay_diagnostics(
            self.provider,
            self.user_id,
            provider_dir=self.provider_dir,
            keep=REPLAY_DIAGNOSTICS_KEEP,
        )
        return self.path


def begin_replay_diagnostics(
    provider: str,
    user_id: int | None,
    *,
    sync_id: str | None = None,
    attempt_id: str | None = None,
    provider_dir: str | None = None,
) -> ReplayDiagnosticsCapture | None:
    if user_id is None:
        return None
    capture_level = get_support_capture_level(provider, user_id=int(user_id))
    resolved_sync_id = str(sync_id or get_provider_sync_id(int(user_id), provider) or "").strip()
    resolved_attempt_id = str(attempt_id or current_visible_auth_attempt_id(provider, int(user_id)) or "").strip()
    phase1_har_path = find_phase1_har_path(
        provider,
        int(user_id),
        sync_id=resolved_sync_id,
        attempt_id=resolved_attempt_id,
        provider_dir=provider_dir,
    )
    try:
        return ReplayDiagnosticsCapture(
            provider,
            int(user_id),
            sync_id=resolved_sync_id,
            attempt_id=resolved_attempt_id,
            provider_dir=provider_dir,
            phase1_har_path=phase1_har_path,
            capture_level=capture_level,
        )
    except OSError as exc:
        logger.warning(
            "phase2 replay diagnostics unavailable",
            extra={
                "provider": str(provider or "").strip().lower(),
                "user_id": int(user_id),
                "error": type(exc).__name__,
            },
        )
        return None


def record_provider_payload_sections(
    capture: ReplayDiagnosticsCapture | None,
    payload: dict[str, Any],
    *,
    source: str,
    transport: str = "provider",
) -> None:
    if not capture or not isinstance(payload, dict):
        return
    capture.record_data_fetch(
        stage=f"{source}.payload",
        payload=payload,
        record_source="payload",
        transport=transport,
    )
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        capture.record_data_fetch(
            stage="accounts.list",
            payload={"accounts": accounts},
            records=accounts,
            record_source="accounts",
            transport=transport,
        )
    holdings = payload.get("holdings")
    if isinstance(holdings, list):
        capture.record_data_fetch(
            stage="holdings.list",
            payload={"holdings": holdings},
            records=holdings,
            record_source="holdings",
            transport=transport,
        )
    transactions = payload.get("transactions")
    if isinstance(transactions, dict):
        for account_ref, rows in transactions.items():
            if not isinstance(rows, list):
                continue
            capture.record_data_fetch(
                stage="transactions.window",
                payload={"transactions": rows},
                records=rows,
                record_source="transactions",
                account_ref=account_ref,
                transport=transport,
            )
    elif isinstance(transactions, list):
        capture.record_data_fetch(
            stage="transactions.list",
            payload={"transactions": transactions},
            records=transactions,
            record_source="transactions",
            transport=transport,
        )
