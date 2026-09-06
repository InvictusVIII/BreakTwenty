from __future__ import annotations

import importlib
import json
import unittest

from jsonschema import Draft202012Validator

from app.connectors.base import Connector
from app.connectors.registry import _REGISTRY
from app.provider_catalog import (
    CATALOG_PATH,
    _load_provider_catalog,
    get_desktop_visible_auth_metadata,
    get_provider_metadata,
    iter_available_institutions,
    iter_provider_metadata,
    provider_uses_background_transaction_import,
    resolve_sync_route_provider,
)

SCHEMA_PATH = CATALOG_PATH.parent / "provider_catalog.schema.json"


class ProviderCatalogSchemaTests(unittest.TestCase):
    def test_schema_file_is_valid_json_schema(self) -> None:
        with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        # Raises jsonschema.exceptions.SchemaError if the schema itself is malformed.
        Draft202012Validator.check_schema(schema)

    def test_live_catalog_matches_schema(self) -> None:
        with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        with CATALOG_PATH.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(catalog), key=lambda err: list(err.absolute_path))
        if errors:
            messages = "\n".join(
                f"  - {'/'.join(str(part) for part in err.absolute_path) or '<root>'}: {err.message}"
                for err in errors
            )
            self.fail(f"provider_catalog.json does not match schema:\n{messages}")

    def test_python_validator_still_loads_catalog(self) -> None:
        # Sanity: the existing runtime validator must continue to accept the catalog
        # even with $schema metadata present. Returns the loaded catalog or raises.
        loaded = _load_provider_catalog()
        self.assertIsInstance(loaded, dict)
        self.assertNotIn("$schema", loaded)
        self.assertGreater(len(loaded), 0)

    def test_moomoo_managed_oauth_is_enabled_for_add_and_manual_flows(self) -> None:
        visible_auth = get_desktop_visible_auth_metadata("moomoo")
        metadata = get_provider_metadata("moomoo")
        api_config = metadata["frontend"]["apiConfig"]
        runtime_state = metadata["backend"]["runtimeState"]
        self.assertTrue(visible_auth.get("enabled"))
        self.assertTrue(visible_auth.get("addFlow"))
        self.assertTrue(visible_auth.get("manualFlow"))
        self.assertEqual(
            visible_auth.get("runnerCommand"),
            ["breaktwenty-visible-auth-python", "scripts/moomoo_oauth_visible_auth.py"],
        )
        self.assertEqual(api_config.get("fields"), [])
        self.assertNotIn("privacyText", api_config)
        self.assertEqual(runtime_state.get("authMode"), "cloud_oauth")
        self.assertEqual(runtime_state.get("reuseMode"), "encrypted_oauth")
        self.assertEqual(metadata["backend"].get("settingInvalidationKeys"), [])
        self.assertEqual(metadata["backend"].get("configuredSettingsAllOf"), [])

    def test_implemented_catalog_entries_resolve_to_connectors_and_sync_routes(self) -> None:
        implemented_providers = {
            provider
            for provider, metadata in iter_provider_metadata()
            if metadata.get("implemented", False)
        }
        self.assertEqual(set(_REGISTRY), implemented_providers)
        from app.main import app

        route_paths = set(app.openapi()["paths"])

        for provider, metadata in iter_provider_metadata():
            with self.subTest(provider=provider):
                backend = metadata.get("backend") or {}
                connector = backend.get("connector") or {}
                route = backend.get("route") or {}
                if provider not in implemented_providers:
                    self.assertFalse(connector)
                    self.assertFalse(route.get("syncPath"))
                    continue

                spec = _REGISTRY[provider]
                module = importlib.import_module(spec.module_path)
                connector_class = getattr(module, spec.class_name)
                self.assertTrue(issubclass(connector_class, Connector))
                self.assertEqual(spec.module_path, connector["modulePath"])
                self.assertEqual(spec.class_name, connector["className"])
                self.assertIn(f'/api{route["syncPath"]}', route_paths)

    def test_every_add_provider_uses_accounts_first_then_background_transactions(self) -> None:
        from app.api.routes.sync import _provider_sync_scope

        for institution in iter_available_institutions():
            provider = institution["provider"]
            with self.subTest(provider=provider):
                add_modal = (get_provider_metadata(provider).get("frontend") or {}).get("addAuthModal")
                self.assertIn(add_modal, {"api", "scraper"})
                route_provider = resolve_sync_route_provider(provider, "background")
                self.assertIsNotNone(route_provider)
                self.assertTrue(provider_uses_background_transaction_import(route_provider))
                self.assertEqual("accounts", _provider_sync_scope(route_provider))


if __name__ == "__main__":
    unittest.main()
