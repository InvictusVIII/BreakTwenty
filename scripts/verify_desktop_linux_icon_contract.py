#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST_DIR = REPO_ROOT / "desktop" / "dist" / "electron"
BUILDER_CONFIG = REPO_ROOT / "desktop" / "electron-builder.yml"
MAIN_SOURCE = REPO_ROOT / "desktop" / "src" / "main.js"
CANONICAL_WINDOW_ICON = REPO_ROOT / "desktop" / "assets" / "icons" / "window-icon-64.png"
CANONICAL_WINDOW_ICON_SHA256 = "e999d5b8d3141c3b8259c6c033b5b7bcec7dbc4ae4bd50ae5ea7e5e0a627b8ca"
RUNTIME_DESKTOP_NAME_SNIPPET = b"app.setDesktopName(runtimeTaskbarAppId())"
LINUX_RUNTIME_ICON_SNIPPET = b"? ['window-icon-64.png']"
WINDOW_RUNTIME_ID_SNIPPET = b"function runtimeTaskbarAppId()"
FORBIDDEN_DESKTOP_BINDING = "StartupWMClass"


def fail(message: str) -> None:
    raise SystemExit(message)


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        fail(f"Missing required package file: {path}")


def sha256(path: Path) -> str:
    return hashlib.sha256(read_bytes(path)).hexdigest()


def verify_icon_bytes(path: Path) -> None:
    actual = sha256(path)
    if actual != CANONICAL_WINDOW_ICON_SHA256:
        fail(
            f"{path} has SHA-256 {actual}, expected the canonical transparent taskbar icon "
            f"{CANONICAL_WINDOW_ICON_SHA256}."
        )


def yaml_mapping_scalar(path: Path, keys: tuple[str, ...]) -> str | None:
    stack: list[tuple[int, str]] = []
    mapping_line = re.compile(r"^(\s*)([A-Za-z][A-Za-z0-9]*):(?:\s*(.*?))?\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "-")):
            continue
        match = mapping_line.match(line)
        if match is None:
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match.group(2)))
        if tuple(key for _indent, key in stack) == keys:
            return (match.group(3) or "").split("#", 1)[0].strip()
    return None


def verify_source_contract() -> None:
    main_source = read_bytes(MAIN_SOURCE)
    missing = [
        label
        for label, snippet in (
            ("Linux runtime desktop-name override", RUNTIME_DESKTOP_NAME_SNIPPET),
            ("runtime taskbar identity helper", WINDOW_RUNTIME_ID_SNIPPET),
            ("exact Linux 64px transparent runtime icon", LINUX_RUNTIME_ICON_SNIPPET),
        )
        if snippet not in main_source
    ]
    if missing:
        fail(f"{MAIN_SOURCE} is missing: {', '.join(missing)}")
    verify_icon_bytes(CANONICAL_WINDOW_ICON)

    binding = yaml_mapping_scalar(
        BUILDER_CONFIG,
        ("linux", "desktop", "entry", FORBIDDEN_DESKTOP_BINDING),
    )
    if binding not in {'""', "''"}:
        fail(
            f"{BUILDER_CONFIG} must explicitly set linux.desktop.entry."
            f"{FORBIDDEN_DESKTOP_BINDING} to an empty string; electron-builder otherwise "
            "regenerates a launcher binding that overrides the transparent runtime icon."
        )


def verify_app_asar(app_asar: Path) -> None:
    payload = read_bytes(app_asar)
    missing = [
        label
        for label, snippet in (
            ("Linux runtime desktop-name override", RUNTIME_DESKTOP_NAME_SNIPPET),
            ("runtime taskbar identity helper", WINDOW_RUNTIME_ID_SNIPPET),
            ("exact Linux 64px transparent runtime icon", LINUX_RUNTIME_ICON_SNIPPET),
        )
        if snippet not in payload
    ]
    if missing:
        fail(f"{app_asar} is missing: {', '.join(missing)}")


def verify_packaged_icon(package_root: Path) -> None:
    icon = package_root / "resources" / "desktop" / "assets" / "icons" / "window-icon-64.png"
    verify_icon_bytes(icon)


def verify_desktop_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == FORBIDDEN_DESKTOP_BINDING and value.strip():
            fail(
                f"{path} has a non-empty {FORBIDDEN_DESKTOP_BINDING}; "
                "Linux taskbars can then resolve the running window through the bordered launcher icon."
            )


def verify_extracted_deb(deb_path: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="breaktwenty-linux-icon-contract-") as tmp:
        extract_root = Path(tmp)
        subprocess.run(
            ["dpkg-deb", "-x", str(deb_path), str(extract_root)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        app_asars = list(extract_root.glob("opt/*/resources/app.asar"))
        if not app_asars:
            fail(f"{deb_path} does not contain opt/*/resources/app.asar")
        for app_asar in app_asars:
            verify_app_asar(app_asar)
            verify_packaged_icon(app_asar.parent.parent)
        desktop_files = list(extract_root.glob("usr/share/applications/*.desktop"))
        if not desktop_files:
            fail(f"{deb_path} does not contain a usr/share/applications desktop file")
        for desktop_file in desktop_files:
            verify_desktop_file(desktop_file)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, default=DEFAULT_DIST_DIR)
    parser.add_argument("--artifact-basename", default="BreakTwenty")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()

    verify_source_contract()
    if args.source_only:
        print("Linux desktop icon source contract verified.")
        return 0

    dist_dir = args.dist_dir.resolve()
    unpacked_asar = dist_dir / "linux-unpacked" / "resources" / "app.asar"
    verify_app_asar(unpacked_asar)
    verify_packaged_icon(unpacked_asar.parent.parent)

    for desktop_file in dist_dir.glob("**/*.desktop"):
        verify_desktop_file(desktop_file)

    debs = sorted(dist_dir.glob(f"{args.artifact_basename}-*.deb"))
    if not debs:
        fail(f"No Linux deb artifacts found for {args.artifact_basename} under {dist_dir}")
    for deb in debs:
        verify_extracted_deb(deb)

    print("Linux desktop icon contract verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
