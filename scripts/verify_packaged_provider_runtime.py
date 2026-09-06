from __future__ import annotations

import argparse
import importlib
import json
import sys
import unittest
from pathlib import Path


PROVIDER_TEST_MODULES = (
    "test_amex_scraper",
    "test_bmo_connector",
    "test_cibc_connector",
    "test_coinbase_connector",
    "test_ibkr_flex",
    "test_national_connector",
    "test_questrade_connector",
    "test_rbc_scraper",
    "test_scotiabank_scraper",
    "test_tangerine_scraper",
    "test_wealthsimple_connector",
    "test_wise_connector",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-root", required=True, type=Path)
    parser.add_argument("--source-backend", required=True, type=Path)
    return parser.parse_args()


def require_packaged_app(resource_root: Path) -> None:
    packaged_backend = (resource_root / "backend").resolve()
    app = importlib.import_module("app")
    app_path = Path(app.__file__ or "").resolve()
    if not app_path.is_relative_to(packaged_backend):
        raise RuntimeError(
            f"Provider verification imported app from {app_path}, not {packaged_backend}"
        )


def import_catalog_connectors(resource_root: Path) -> int:
    catalog_path = resource_root / "config" / "provider_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    imported = 0
    for provider, metadata in catalog.items():
        if provider.startswith("$") or not isinstance(metadata, dict):
            continue
        connector = ((metadata.get("backend") or {}).get("connector") or {})
        module_path = connector.get("modulePath")
        class_name = connector.get("className")
        if not module_path or not class_name:
            continue
        module = importlib.import_module(module_path)
        if not hasattr(module, class_name):
            raise RuntimeError(f"{provider} connector is missing {module_path}.{class_name}")
        imported += 1
    return imported


def run_provider_tests(source_backend: Path) -> unittest.result.TestResult:
    tests_dir = (source_backend / "tests").resolve()
    sys.path.append(str(tests_dir))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module_name in PROVIDER_TEST_MODULES:
        suite.addTests(loader.loadTestsFromName(module_name))
    return unittest.TextTestRunner(verbosity=1).run(suite)


def main() -> int:
    args = parse_args()
    resource_root = args.resource_root.resolve()
    require_packaged_app(resource_root)
    connector_count = import_catalog_connectors(resource_root)
    result = run_provider_tests(args.source_backend)
    if not result.wasSuccessful():
        return 1
    print(
        f"Packaged provider runtime verified: {connector_count} catalog connectors, "
        f"{result.testsRun} provider contract tests"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
