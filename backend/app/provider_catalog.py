from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

def _repo_catalog_path() -> Path:
    parents = list(Path(__file__).resolve().parents)
    repo_roots = [parent for parent in parents if (parent / "RULES.md").exists()]
    for parent in [*repo_roots, *parents]:
        candidate = parent / "config" / "provider_catalog.json"
        if candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError("Unable to locate config/provider_catalog.json")


def _configured_catalog_path(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    return candidate if candidate.exists() and candidate.is_file() else None


def _resolve_catalog_path() -> Path:
    configured_path = _configured_catalog_path(os.environ.get("PROVIDER_CATALOG_PATH"))
    if configured_path:
        return configured_path
    return _repo_catalog_path()


CATALOG_PATH = _resolve_catalog_path()
_CATALOG_CACHE: dict[str, Any] | None = None


def _expect_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object.")
    return value


def _expect_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string.")
    return value


def _expect_optional_mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    return _expect_mapping(value, path)


def _expect_optional_string_list(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{path} must be an array of non-empty strings.")
    return tuple(value)


def _expect_optional_bool(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean.")


def _validate_frontend_metadata(provider: str, metadata: dict[str, Any]) -> None:
    add_auth_modal = metadata.get("addAuthModal")
    if add_auth_modal is not None and add_auth_modal not in {"api", "scraper"}:
        raise ValueError(f"{provider}.frontend.addAuthModal must be 'api' or 'scraper'.")

    for key in ("backgroundSyncEndpoint", "userInitiatedSyncEndpoint"):
        value = metadata.get(key)
        if value is not None:
            _expect_string(value, f"{provider}.frontend.{key}")

    for key in ("scraperEndpoints", "scraperFieldLabels", "apiConfig", "scraperAuth", "dashboardAuth", "syncDisplay"):
        _expect_optional_mapping(metadata.get(key), f"{provider}.frontend.{key}")


def _validate_backend_metadata(provider: str, metadata: dict[str, Any]) -> None:
    connector = _expect_optional_mapping(metadata.get("connector"), f"{provider}.backend.connector")
    if connector:
        _expect_string(connector.get("modulePath"), f"{provider}.backend.connector.modulePath")
        _expect_string(connector.get("className"), f"{provider}.backend.connector.className")
        provider_type = connector.get("providerType")
        if provider_type is not None and provider_type not in {"api", "scraper"}:
            raise ValueError(f"{provider}.backend.connector.providerType must be 'api' or 'scraper'.")
        for key in ("persistedProvider", "syncStatusProvider", "errorStatus"):
            value = connector.get(key)
            if value is not None:
                _expect_string(value, f"{provider}.backend.connector.{key}")

    route = _expect_optional_mapping(metadata.get("route"), f"{provider}.backend.route")
    for key in (
        "syncPath",
        "loginPath",
        "twofaPath",
        "authHandler",
        "missingCredentialsMessage",
        "errorStatus",
        "statusProvider",
        "lockProvider",
    ):
        value = route.get(key)
        if value is not None:
            _expect_string(value, f"{provider}.backend.route.{key}")

    runtime_state = _expect_optional_mapping(metadata.get("runtimeState"), f"{provider}.backend.runtimeState")
    _expect_optional_string_list(runtime_state.get("artifactKinds"), f"{provider}.backend.runtimeState.artifactKinds")
    _expect_optional_string_list(
        runtime_state.get("pendingRuntimeNamespaces"),
        f"{provider}.backend.runtimeState.pendingRuntimeNamespaces",
    )
    _expect_optional_string_list(
        runtime_state.get("extraRuntimeProviders"),
        f"{provider}.backend.runtimeState.extraRuntimeProviders",
    )
    for key in ("browserEngine", "silentReuseEngine", "manualAuthEngine", "manualAuthMode", "authMode", "reuseMode"):
        value = runtime_state.get(key)
        if value is not None:
            _expect_string(value, f"{provider}.backend.runtimeState.{key}")
    for key in ("credentialChangeQuarantine", "desktopAuthRuntimeDir"):
        _expect_optional_bool(runtime_state.get(key), f"{provider}.backend.runtimeState.{key}")

    nightly = _expect_optional_mapping(metadata.get("nightly"), f"{provider}.backend.nightly")
    _expect_optional_bool(nightly.get("enabled"), f"{provider}.backend.nightly.enabled")
    if nightly.get("phase") is not None:
        _expect_string(nightly.get("phase"), f"{provider}.backend.nightly.phase")
    if nightly.get("order") is not None and not isinstance(nightly.get("order"), int):
        raise ValueError(f"{provider}.backend.nightly.order must be an integer.")

    _expect_optional_string_list(
        metadata.get("settingInvalidationKeys"),
        f"{provider}.backend.settingInvalidationKeys",
    )
    _expect_optional_string_list(
        metadata.get("configuredSettingsAllOf"),
        f"{provider}.backend.configuredSettingsAllOf",
    )
    _expect_optional_string_list(
        metadata.get("transientSettingKeys"),
        f"{provider}.backend.transientSettingKeys",
    )
    _expect_optional_mapping(metadata.get("apiSessionAuth"), f"{provider}.backend.apiSessionAuth")

    transaction_import = _expect_optional_mapping(
        metadata.get("transactionImport"),
        f"{provider}.backend.transactionImport",
    )
    _expect_optional_bool(transaction_import.get("enabled"), f"{provider}.backend.transactionImport.enabled")
    _expect_optional_bool(
        transaction_import.get("dailyDedupe"),
        f"{provider}.backend.transactionImport.dailyDedupe",
    )
    network_preflight = _expect_optional_mapping(
        metadata.get("networkPreflight"),
        f"{provider}.backend.networkPreflight",
    )
    if network_preflight.get("host") is not None:
        _expect_string(
            network_preflight.get("host"),
            f"{provider}.backend.networkPreflight.host",
        )


def _strip_schema_metadata(catalog: dict[str, Any]) -> dict[str, Any]:
    # JSON Schema metadata keys (e.g. "$schema") are not providers; drop them
    # so the cached catalog only contains provider entries.
    return {key: value for key, value in catalog.items() if not (isinstance(key, str) and key.startswith("$"))}


def _validate_provider_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    for provider, metadata in catalog.items():
        if not isinstance(provider, str) or provider != provider.strip().lower():
            raise ValueError(f"Provider keys must be lowercase provider ids. Got {provider!r}.")
        entry = _expect_mapping(metadata, f"{provider}")
        _expect_string(entry.get("displayName"), f"{provider}.displayName")
        institution_type = entry.get("institutionType")
        if institution_type is not None and institution_type not in {"api", "scraper"}:
            raise ValueError(f"{provider}.institutionType must be 'api' or 'scraper'.")
        for key in ("availableInAddList", "implemented"):
            _expect_optional_bool(entry.get(key), f"{provider}.{key}")
        _expect_optional_mapping(entry.get("logo"), f"{provider}.logo")
        _validate_frontend_metadata(provider, _expect_optional_mapping(entry.get("frontend"), f"{provider}.frontend"))
        _validate_backend_metadata(provider, _expect_optional_mapping(entry.get("backend"), f"{provider}.backend"))
    return catalog


def _load_provider_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    providers = _strip_schema_metadata(_expect_mapping(data, "provider catalog"))
    return _validate_provider_catalog(providers)


def _provider_runtime_artifact_kinds(runtime: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(kind) for kind in (runtime.get("artifactKinds") or []))


def get_provider_catalog() -> dict[str, Any]:
    global _CATALOG_CACHE

    if _CATALOG_CACHE is None:
        _CATALOG_CACHE = _load_provider_catalog()

    return _CATALOG_CACHE


def get_provider_metadata(provider: str) -> dict[str, Any]:
    metadata = get_provider_catalog().get(provider)
    if metadata is None:
        raise KeyError(f"Unknown provider {provider!r}")
    return metadata


def iter_provider_metadata() -> list[tuple[str, dict[str, Any]]]:
    return list(get_provider_catalog().items())


def get_provider_display_name(provider: str) -> str:
    return str(get_provider_metadata(provider).get("displayName") or provider)


def get_provider_institution_type(provider: str) -> str:
    return str(get_provider_metadata(provider).get("institutionType") or "api")


def get_provider_credential_storage_provider(provider: str) -> str:
    metadata = get_provider_metadata(provider)
    connector = ((metadata.get("backend") or {}).get("connector") or {})
    return str(connector.get("persistedProvider") or connector.get("syncStatusProvider") or provider)


def iter_available_institutions() -> list[dict[str, Any]]:
    institutions: list[dict[str, Any]] = []
    for provider, metadata in iter_provider_metadata():
        if not metadata.get("availableInAddList", False):
            continue
        if not metadata.get("implemented", False):
            continue
        institutions.append(
            {
                "name": get_provider_display_name(provider),
                "provider": provider,
                "type": get_provider_institution_type(provider),
                "implemented": bool(metadata.get("implemented", False)),
            }
        )
    return institutions


def iter_connector_entries() -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for provider, metadata in iter_provider_metadata():
        connector = (metadata.get("backend") or {}).get("connector")
        if connector:
            entries.append((provider, connector))
    return entries


def iter_nightly_providers(phase: str) -> list[str]:
    ordered: list[tuple[int, str]] = []
    for provider, metadata in iter_provider_metadata():
        nightly = (metadata.get("backend") or {}).get("nightly") or {}
        if not nightly.get("enabled"):
            continue
        if nightly.get("phase") != phase:
            continue
        ordered.append((int(nightly.get("order", 0)), provider))
    ordered.sort()
    return [provider for _, provider in ordered]


def get_status_provider_for_result(provider: str) -> str:
    connector = (get_provider_metadata(provider).get("backend") or {}).get("connector") or {}
    return str(connector.get("syncStatusProvider") or connector.get("persistedProvider") or provider)


def get_error_status_for_provider(provider: str) -> str:
    connector = (get_provider_metadata(provider).get("backend") or {}).get("connector") or {}
    return str(connector.get("errorStatus") or "error")


def get_lock_provider_for_route(provider: str) -> str:
    route = get_route_metadata(provider)
    return str(route.get("lockProvider") or provider)


def get_route_metadata(provider: str) -> dict[str, Any]:
    return dict((get_provider_metadata(provider).get("backend") or {}).get("route") or {})


def get_frontend_sync_endpoint(provider: str, mode: str) -> str | None:
    frontend = get_provider_metadata(provider).get("frontend") or {}
    if mode in {"manual", "sync_all", "user_initiated"}:
        return frontend.get("userInitiatedSyncEndpoint") or frontend.get("backgroundSyncEndpoint")
    return frontend.get("backgroundSyncEndpoint")


def get_sync_route_provider_for_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    for provider, metadata in iter_provider_metadata():
        route = ((metadata.get("backend") or {}).get("route") or {})
        if route.get("syncPath") == endpoint:
            return provider
    return None


def resolve_sync_route_provider(provider: str, mode: str) -> str | None:
    if mode == "nightly":
        return provider if get_route_metadata(provider).get("syncPath") else None
    endpoint = get_frontend_sync_endpoint(provider, mode)
    route_provider = get_sync_route_provider_for_endpoint(endpoint)
    if route_provider:
        return route_provider
    return provider if get_route_metadata(provider).get("syncPath") else None


def iter_sync_route_providers() -> list[str]:
    return [
        provider
        for provider, metadata in iter_provider_metadata()
        if ((metadata.get("backend") or {}).get("route") or {}).get("syncPath")
    ]


def iter_standard_scraper_auth_providers() -> list[str]:
    return [
        provider
        for provider, metadata in iter_provider_metadata()
        if ((metadata.get("backend") or {}).get("route") or {}).get("authHandler") == "standard_scraper"
    ]


def iter_api_session_otp_auth_providers() -> list[str]:
    return [
        provider
        for provider, metadata in iter_provider_metadata()
        if ((metadata.get("backend") or {}).get("route") or {}).get("authHandler") == "api_session_otp"
    ]


def iter_desktop_visible_auth_providers() -> list[str]:
    return [
        provider
        for provider, metadata in iter_provider_metadata()
        if ((metadata.get("frontend") or {}).get("scraperAuth") or {}).get("flow") == "desktop_visible_auth"
    ]


def get_setting_invalidation_provider(setting_key: str) -> str | None:
    for provider, metadata in iter_provider_metadata():
        keys = ((metadata.get("backend") or {}).get("settingInvalidationKeys") or [])
        if setting_key in keys:
            return provider
    return None


def iter_api_credential_setting_keys(*, include_transient: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    providers: list[tuple[str, tuple[str, ...]]] = []
    for provider, metadata in iter_provider_metadata():
        backend = metadata.get("backend") or {}
        keys = [
            str(key)
            for key in (
                backend.get("settingInvalidationKeys") or []
            )
        ]
        for key in backend.get("configuredSettingsAllOf") or []:
            key = str(key)
            if key not in keys:
                keys.append(key)
        if include_transient:
            for key in backend.get("transientSettingKeys") or []:
                key = str(key)
                if key not in keys:
                    keys.append(key)
        if keys:
            providers.append((provider, tuple(keys)))
    return providers


def get_related_state_providers(provider: str) -> tuple[str, ...]:
    related = [provider]
    for candidate, metadata in iter_provider_metadata():
        if candidate == provider:
            continue
        backend = metadata.get("backend") or {}
        connector = backend.get("connector") or {}
        route = backend.get("route") or {}
        related_keys = (
            connector.get("persistedProvider"),
            connector.get("syncStatusProvider"),
            route.get("statusProvider"),
            route.get("lockProvider"),
        )
        if provider in related_keys:
            related.append(candidate)
    return tuple(related)


def get_runtime_state_metadata(provider: str) -> dict[str, Any]:
    backend = get_provider_metadata(provider).get("backend") or {}
    runtime = dict(backend.get("runtimeState") or {})
    connector = backend.get("connector") or {}
    provider_type = str(connector.get("providerType") or "api")
    browser_engine = str(runtime.get("browserEngine") or "none")
    auth_mode = str(runtime.get("authMode") or provider_type)
    manual_auth_mode = str(runtime.get("manualAuthMode") or auth_mode)
    return {
        "artifactKinds": _provider_runtime_artifact_kinds(runtime),
        "browserEngine": browser_engine,
        "silentReuseEngine": str(runtime.get("silentReuseEngine") or browser_engine),
        "manualAuthEngine": str(runtime.get("manualAuthEngine") or browser_engine),
        "manualAuthMode": manual_auth_mode,
        "authMode": auth_mode,
        "reuseMode": str(runtime.get("reuseMode") or "none"),
        "pendingRuntimeNamespaces": tuple(
            str(namespace) for namespace in (runtime.get("pendingRuntimeNamespaces") or [])
        ),
        "extraRuntimeProviders": tuple(
            str(provider_name) for provider_name in (runtime.get("extraRuntimeProviders") or [])
        ),
        "credentialChangeQuarantine": bool(runtime.get("credentialChangeQuarantine")),
        "desktopAuthRuntimeDir": bool(runtime.get("desktopAuthRuntimeDir")),
    }


def get_desktop_visible_auth_metadata(provider: str) -> dict[str, Any]:
    frontend = get_provider_metadata(provider).get("frontend") or {}
    scraper_auth = frontend.get("scraperAuth") or {}
    if scraper_auth.get("flow") != "desktop_visible_auth":
        return {}
    return dict(scraper_auth.get("desktopVisibleAuth") or {})


def get_api_session_auth_metadata(provider: str) -> dict[str, Any]:
    return dict((get_provider_metadata(provider).get("backend") or {}).get("apiSessionAuth") or {})


def get_provider_network_preflight_host(provider: str) -> str | None:
    backend = get_provider_metadata(provider).get("backend") or {}
    preflight = backend.get("networkPreflight") or {}
    host = str(preflight.get("host") or "").strip().lower()
    return host or None


def get_transaction_import_metadata(provider: str) -> dict[str, Any]:
    return dict((get_provider_metadata(provider).get("backend") or {}).get("transactionImport") or {})


def provider_uses_background_transaction_import(provider: str) -> bool:
    return bool(get_transaction_import_metadata(provider).get("enabled"))


def provider_uses_daily_transaction_import_dedupe(provider: str) -> bool:
    return bool(get_transaction_import_metadata(provider).get("dailyDedupe"))


def get_transient_setting_keys(provider: str) -> tuple[str, ...]:
    keys = ((get_provider_metadata(provider).get("backend") or {}).get("transientSettingKeys") or [])
    return tuple(str(key) for key in keys)
