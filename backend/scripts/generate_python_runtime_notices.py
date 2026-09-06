#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from importlib import metadata
from pathlib import Path
from urllib.parse import quote


LICENSE_FILE_MARKERS = ("license", "copying", "notice", "copyright")
MAX_LICENSE_FILE_BYTES = 2 * 1024 * 1024
OPENSSL_VERSION = "4.0.1"
OPENSSL_LICENSE_EXPRESSION = "Apache-2.0"
OPENSSL_SOURCE_SHA256 = "2db3f3a0d6ea4b59e1f094ace2c8cd536dffb87cdc39084c5afa1e6f7f37dd09"
OPENSSL_SOURCE_URL = (
    "https://github.com/openssl/openssl/releases/download/"
    f"openssl-{OPENSSL_VERSION}/openssl-{OPENSSL_VERSION}.tar.gz"
)
OPENSSL_LICENSE_URL = (
    f"https://raw.githubusercontent.com/openssl/openssl/openssl-{OPENSSL_VERSION}/LICENSE.txt"
)


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._") or "package"


def _license_label(distribution: metadata.Distribution) -> str:
    expression = str(distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    classifiers = [
        value.removeprefix("License :: ").strip()
        for value in distribution.metadata.get_all("Classifier", [])
        if value.startswith("License ::")
    ]
    if classifiers:
        return " / ".join(classifiers)
    declared = " ".join(str(distribution.metadata.get("License") or "").split())
    if declared and len(declared) <= 160:
        return declared
    return ""


def _license_files(distribution: metadata.Distribution) -> list[Path]:
    selected: list[Path] = []
    for relative in distribution.files or ():
        name = Path(str(relative)).name.lower()
        if not any(marker in name for marker in LICENSE_FILE_MARKERS):
            continue
        path = Path(distribution.locate_file(relative))
        try:
            if not path.is_file() or path.stat().st_size > MAX_LICENSE_FILE_BYTES:
                continue
        except OSError:
            continue
        selected.append(path)
    return selected


def _project_url(distribution: metadata.Distribution) -> str:
    for value in distribution.metadata.get_all("Project-URL", []):
        _label, separator, url = str(value).partition(",")
        if separator and url.strip().startswith(("https://", "http://")):
            return url.strip()
    homepage = str(distribution.metadata.get("Home-page") or "").strip()
    return homepage if homepage.startswith(("https://", "http://")) else ""


def generate(
    output_dir: Path,
    *,
    fail_on_missing_license: bool,
    include_macos_x64_static_openssl: bool,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    licenses_dir = output_dir / "licenses"
    if licenses_dir.exists():
        shutil.rmtree(licenses_dir)
    licenses_dir.mkdir(mode=0o755)

    components: list[dict[str, object]] = []
    notice_rows: list[tuple[str, str, str, list[str]]] = []
    missing: list[str] = []
    distributions = sorted(
        metadata.distributions(),
        key=lambda item: str(item.metadata.get("Name") or "").lower(),
    )
    for distribution in distributions:
        name = str(distribution.metadata.get("Name") or "").strip()
        version = str(distribution.version or "").strip()
        if not name or not version:
            continue
        license_label = _license_label(distribution)
        copied_files: list[str] = []
        for index, source in enumerate(_license_files(distribution), start=1):
            destination_name = (
                f"{_safe_component(name)}-{_safe_component(version)}-"
                f"{index}-{_safe_component(source.name)}"
            )
            destination = licenses_dir / destination_name
            shutil.copyfile(source, destination)
            copied_files.append(f"licenses/{destination_name}")
        if not license_label and copied_files:
            license_label = "See bundled license text"
        if not license_label:
            missing.append(f"{name}=={version}")

        component: dict[str, object] = {
            "type": "library",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{quote(name.lower().replace('_', '-'), safe='-.')}@{quote(version, safe='-.+')}",
            "licenses": [{"license": {"name": license_label or "Undeclared"}}],
        }
        project_url = _project_url(distribution)
        if project_url:
            component["externalReferences"] = [{"type": "website", "url": project_url}]
        if copied_files:
            component["properties"] = [
                {"name": "breaktwenty:bundled-license-file", "value": path}
                for path in copied_files
            ]
        components.append(component)
        notice_rows.append((name, version, license_label or "Undeclared", copied_files))

    if include_macos_x64_static_openssl:
        source_license = Path(__file__).with_name("openssl-4.0.1-LICENSE.txt")
        if not source_license.is_file():
            raise RuntimeError(f"Bundled OpenSSL license text is missing: {source_license}")
        destination_name = f"OpenSSL-{OPENSSL_VERSION}-LICENSE.txt"
        shutil.copyfile(source_license, licenses_dir / destination_name)
        copied_files = [f"licenses/{destination_name}"]
        components.append(
            {
                "type": "library",
                "name": "OpenSSL",
                "version": OPENSSL_VERSION,
                "purl": f"pkg:generic/openssl@{OPENSSL_VERSION}",
                "licenses": [{"license": {"id": OPENSSL_LICENSE_EXPRESSION}}],
                "externalReferences": [
                    {"type": "distribution", "url": OPENSSL_SOURCE_URL},
                    {"type": "license", "url": OPENSSL_LICENSE_URL},
                ],
                "hashes": [{"alg": "SHA-256", "content": OPENSSL_SOURCE_SHA256}],
                "properties": [
                    {
                        "name": "breaktwenty:bundled-license-file",
                        "value": copied_files[0],
                    },
                    {
                        "name": "breaktwenty:linkage",
                        "value": "static macOS x64 cryptography dependency",
                    },
                ],
            }
        )
        notice_rows.append(
            ("OpenSSL", OPENSSL_VERSION, OPENSSL_LICENSE_EXPRESSION, copied_files)
        )

    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": components,
    }
    (output_dir / "python-runtime-sbom.cdx.json").write_text(
        json.dumps(bom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    notice_lines = [
        "# Packaged Python Runtime Notices",
        "",
        "This inventory is generated from the exact Python environment bundled with BreakTwenty.",
        "Review the accompanying license texts and CycloneDX SBOM before release.",
        "",
        "| Package | Version | Declared license | Bundled text |",
        "| --- | --- | --- | --- |",
    ]
    for name, version, license_label, copied_files in notice_rows:
        files = "<br>".join(f"`{path}`" for path in copied_files) or "—"
        escaped_name = name.replace("|", "\\|")
        escaped_version = version.replace("|", "\\|")
        escaped_license = license_label.replace("|", "\\|")
        notice_lines.append(
            f"| {escaped_name} | {escaped_version} | {escaped_license} | {files} |"
        )
    (output_dir / "PYTHON_RUNTIME_NOTICES.md").write_text(
        "\n".join(notice_lines) + "\n",
        encoding="utf-8",
    )

    if fail_on_missing_license and missing:
        raise RuntimeError(
            "Python distributions without declared or bundled license metadata: "
            + ", ".join(missing)
        )
    return {"components": len(components), "missing_licenses": missing}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate license notices and a CycloneDX SBOM for the active Python runtime."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--fail-on-missing-license", action="store_true")
    parser.add_argument("--include-macos-x64-static-openssl", action="store_true")
    args = parser.parse_args()
    result = generate(
        args.output_dir.resolve(),
        fail_on_missing_license=args.fail_on_missing_license,
        include_macos_x64_static_openssl=args.include_macos_x64_static_openssl,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
