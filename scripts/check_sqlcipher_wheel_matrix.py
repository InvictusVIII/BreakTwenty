#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request

PACKAGE = "sqlcipher3"
VERSION = "0.6.2"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE}/{VERSION}/json"
REQUIRED_CP312_WHEEL_MARKERS = {
    "Linux x64": ("cp312-cp312-manylinux_2_28_x86_64.whl",),
    "Windows x64": ("cp312-cp312-win_amd64.whl",),
    "macOS ARM64": ("cp312-cp312-macosx_11_0_arm64.whl",),
    "macOS x64": ("cp312-cp312-macosx_10_13_x86_64.whl",),
}


def main() -> int:
    request = urllib.request.Request(
        PYPI_JSON_URL,
        headers={"User-Agent": "BreakTwenty SQLCipher release-health check"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    filenames = {
        str(item.get("filename") or "")
        for item in payload.get("urls", [])
        if item.get("packagetype") == "bdist_wheel"
    }
    missing = [
        platform
        for platform, markers in REQUIRED_CP312_WHEEL_MARKERS.items()
        if not any(
            filename.endswith(marker)
            for filename in filenames
            for marker in markers
        )
    ]
    if missing:
        raise SystemExit(
            "sqlcipher3 CPython 3.12 wheels are missing for: "
            + ", ".join(missing)
        )
    print(
        f"{PACKAGE} {VERSION} CPython 3.12 wheel matrix verified for "
        + ", ".join(REQUIRED_CP312_WHEEL_MARKERS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
