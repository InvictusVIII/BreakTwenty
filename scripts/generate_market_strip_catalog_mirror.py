#!/usr/bin/env python3
"""Generate the frontend mirror of the canonical market-strip catalog."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = SOURCE_ROOT / "backend" / "app" / "services" / "market_strip.py"
TARGET_PATH = SOURCE_ROOT / "frontend" / "src" / "constants" / "marketStripCatalog.generated.json"
PROVIDER_PRIORITY = (
    "MARKET_DATA_PROVIDER_FMP",
    "MARKET_DATA_PROVIDER_TWELVE_DATA",
    "MARKET_DATA_PROVIDER_POLYGON",
)


def _assignment(tree: ast.Module, name: str) -> ast.AST:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value
    raise ValueError(f"{name} assignment was not found")


def _call_argument(call: ast.Call, position: int, name: str, default=None):
    if len(call.args) > position:
        return ast.literal_eval(call.args[position])
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.literal_eval(keyword.value)
    return default


def _provider_symbols(call: ast.Call) -> dict[str, str]:
    node = call.args[2] if len(call.args) > 2 else next(
        (keyword.value for keyword in call.keywords if keyword.arg == "provider_symbols"),
        None,
    )
    if not isinstance(node, ast.Dict):
        raise ValueError("Market strip tile has no literal provider_symbols mapping")
    symbols = {}
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if not isinstance(key_node, ast.Name):
            raise ValueError("Market strip provider key is not a named constant")
        symbols[key_node.id] = ast.literal_eval(value_node)
    return symbols


def build_catalog(source_path: Path = SOURCE_PATH) -> dict:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    tiles_node = _assignment(tree, "MARKET_STRIP_TILES")
    if not isinstance(tiles_node, (ast.Tuple, ast.List)):
        raise ValueError("MARKET_STRIP_TILES is not a literal collection")

    tiles = []
    for tile_node in tiles_node.elts:
        if not isinstance(tile_node, ast.Call):
            raise ValueError("MARKET_STRIP_TILES contains a non-call entry")
        symbols = _provider_symbols(tile_node)
        fred_symbol = _call_argument(tile_node, 3, "fred_symbol")
        primary_symbol = next(
            (symbols[provider] for provider in PROVIDER_PRIORITY if provider in symbols),
            fred_symbol,
        )
        tiles.append({
            "id": _call_argument(tile_node, 0, "id"),
            "label": _call_argument(tile_node, 1, "label"),
            "symbol": primary_symbol,
            "fred_symbol": fred_symbol,
            "custom": False,
            "precision": _call_argument(tile_node, 5, "precision", 2),
        })

    default_watchlist = list(ast.literal_eval(_assignment(tree, "DEFAULT_MARKET_STRIP_WATCHLIST")))
    max_tiles = int(ast.literal_eval(_assignment(tree, "MARKET_STRIP_MAX_TILES")))
    tile_ids = {tile["id"] for tile in tiles}
    missing = [tile_id for tile_id in default_watchlist if tile_id not in tile_ids]
    if missing:
        raise ValueError(f"Default market-strip tiles are missing from the catalog: {missing}")
    if len(default_watchlist) > max_tiles:
        raise ValueError("Default market-strip watchlist exceeds MARKET_STRIP_MAX_TILES")
    return {
        "max_tiles": max_tiles,
        "default_watchlist": default_watchlist,
        "tiles": tiles,
    }


def rendered_catalog() -> str:
    return f"{json.dumps(build_catalog(), ensure_ascii=False, indent=2)}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered_catalog()
    current = TARGET_PATH.read_text(encoding="utf-8") if TARGET_PATH.exists() else None
    if args.check:
        if current != expected:
            raise SystemExit(
                "Market strip catalog mirror is stale. Run "
                "python3 scripts/generate_market_strip_catalog_mirror.py."
            )
        return 0
    if current != expected:
        TARGET_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
