"""Connector registry — maps provider name to connector class + metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.connectors.base import Connector
from app.provider_catalog import get_provider_display_name, iter_connector_entries

if TYPE_CHECKING:
    from app.connectors.scraper import ScraperConnector


@dataclass(frozen=True)
class ConnectorSpec:
    provider: str
    module_path: str
    class_name: str
    provider_display_name: str
    provider_type: str = "api"
    persisted_provider: str | None = None
    sync_status_provider: str | None = None
    error_status: str = "error"

    @property
    def persistence_provider(self) -> str:
        return self.persisted_provider or self.provider

    @property
    def status_provider(self) -> str:
        return self.sync_status_provider or self.persistence_provider


_REGISTRY: dict[str, ConnectorSpec] = {
    provider: ConnectorSpec(
        provider=provider,
        module_path=str(config["modulePath"]),
        class_name=str(config["className"]),
        provider_display_name=str(config.get("providerDisplayName") or get_provider_display_name(provider)),
        provider_type=str(config.get("providerType") or "api"),
        persisted_provider=config.get("persistedProvider"),
        sync_status_provider=config.get("syncStatusProvider"),
        error_status=str(config.get("errorStatus") or "error"),
    )
    for provider, config in iter_connector_entries()
}


def get_connector_spec(provider: str) -> ConnectorSpec:
    if provider not in _REGISTRY:
        raise KeyError(f"No connector registered for provider {provider!r}")
    return _REGISTRY[provider]


def get_connector(provider: str) -> Connector:
    """Return a connector instance for *provider*, or raise ``KeyError``."""
    spec = get_connector_spec(provider)
    import importlib
    module = importlib.import_module(spec.module_path)
    cls = getattr(module, spec.class_name)
    return cls()


def get_scraper_connector(provider: str) -> "ScraperConnector":
    from app.connectors.scraper import ScraperConnector

    connector = get_connector(provider)
    if not isinstance(connector, ScraperConnector):
        raise TypeError(f"Provider {provider!r} is not a scraper connector")
    return connector
