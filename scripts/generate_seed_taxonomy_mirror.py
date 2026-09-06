#!/usr/bin/env python3
"""Generate the frontend mirror of the canonical seeded category taxonomy."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = SOURCE_ROOT / "backend" / "app" / "services" / "categories.py"
TARGET_PATH = SOURCE_ROOT / "frontend" / "src" / "constants" / "seedCategoryTaxonomy.generated.json"
EMOJI_ASSET_ROOT = SOURCE_ROOT / "frontend" / "public" / "emoji"


def _seed_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("&", "and")).strip("_")


def _call_argument(call: ast.Call, position: int, name: str, default=None):
    if len(call.args) > position:
        return ast.literal_eval(call.args[position])
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.literal_eval(keyword.value)
    return default


def _taxonomy_expression(tree: ast.Module) -> ast.List:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "SEED_TAXONOMY" and isinstance(node.value, ast.List):
                return node.value
    raise ValueError("SEED_TAXONOMY list was not found")


def build_taxonomy(source_path: Path = SOURCE_PATH) -> dict:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    groups = []
    leaves = []

    for group_index, group_node in enumerate(_taxonomy_expression(tree).elts):
        if not isinstance(group_node, ast.Call):
            raise ValueError("SEED_TAXONOMY contains a non-call group entry")
        group_name = _call_argument(group_node, 0, "name")
        group_icon = _call_argument(group_node, 1, "icon")
        group_color_dark = _call_argument(group_node, 2, "color_dark")
        group_color_light = _call_argument(group_node, 3, "color_light")
        group_classification = _call_argument(group_node, 4, "classification")
        group_icon_set = _call_argument(group_node, 6, "icon_set")
        group_seed_key = _call_argument(group_node, 7, "seed_key") or _seed_key(group_name)
        group_id = 9000 + group_index * 100
        groups.append({
            "id": group_id,
            "parent_id": None,
            "name": group_name,
            "icon": group_icon,
            "icon_set": group_icon_set,
            "color_dark": group_color_dark,
            "color_light": group_color_light,
            "classification": group_classification,
            "is_system": True,
            "sort_order": group_index,
            "seed_key": group_seed_key,
        })

        leaves_node = next(
            (
                keyword.value
                for keyword in group_node.keywords
                if keyword.arg == "leaves"
            ),
            group_node.args[5] if len(group_node.args) > 5 else None,
        )
        if not isinstance(leaves_node, (ast.Tuple, ast.List)):
            raise ValueError(f"Seed group {group_name!r} has no literal leaves collection")
        for leaf_index, leaf_node in enumerate(leaves_node.elts):
            if not isinstance(leaf_node, ast.Call):
                raise ValueError(f"Seed group {group_name!r} contains a non-call leaf entry")
            leaf_name = _call_argument(leaf_node, 0, "name")
            leaf_icon = _call_argument(leaf_node, 1, "icon")
            leaf_icon_set = _call_argument(leaf_node, 3, "icon_set")
            leaf_seed_key = _call_argument(leaf_node, 4, "seed_key") or _seed_key(leaf_name)
            leaf_color_dark = _call_argument(leaf_node, 5, "color_dark") or group_color_dark
            leaf_color_light = _call_argument(leaf_node, 6, "color_light") or group_color_light
            leaf_classification = _call_argument(leaf_node, 7, "classification") or group_classification
            leaves.append({
                "id": group_id + leaf_index + 1,
                "parent_id": group_id,
                "name": leaf_name,
                "icon": leaf_icon,
                "icon_set": leaf_icon_set,
                "color_dark": leaf_color_dark,
                "color_light": leaf_color_light,
                "classification": leaf_classification,
                "is_system": True,
                "sort_order": leaf_index,
                "seed_key": leaf_seed_key,
            })

    return {
        "categories": [*groups, *leaves],
        "classifications": ["expense", "income", "investment", "transfer"],
    }


def _icon_asset_path(category: dict) -> Path:
    icon_set = category["icon_set"] or "twemoji"
    icon = category["icon"]
    if icon_set == "finance":
        filename = icon
    else:
        filename = "-".join(
            f"{ord(character):x}"
            for character in icon
            if ord(character) != 0xFE0F
        )
    return EMOJI_ASSET_ROOT / icon_set / f"{filename}.svg"


def validate_icon_assets(taxonomy: dict) -> None:
    missing = [
        (category["name"], _icon_asset_path(category))
        for category in taxonomy["categories"]
        if not _icon_asset_path(category).is_file()
    ]
    if missing:
        details = ", ".join(
            f"{name}: {path.relative_to(SOURCE_ROOT)}"
            for name, path in missing
        )
        raise ValueError(f"Seed category icons are missing bundled assets: {details}")


def rendered_taxonomy() -> str:
    taxonomy = build_taxonomy()
    validate_icon_assets(taxonomy)
    return f"{json.dumps(taxonomy, ensure_ascii=False, indent=2)}\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = rendered_taxonomy()
    current = TARGET_PATH.read_text(encoding="utf-8") if TARGET_PATH.exists() else None
    if args.check:
        if current != expected:
            raise SystemExit(
                "Seed category taxonomy mirror is stale. Run "
                "python3 scripts/generate_seed_taxonomy_mirror.py."
            )
        return 0
    if current != expected:
        TARGET_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
