from __future__ import annotations

import os
import re
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
_checkout_root = BACKEND_ROOT.parent
REPO_ROOT = (
    _checkout_root
    if (_checkout_root / "scripts" / "desktop_visible_auth_requirements.lock").is_file()
    else None
)
_configured_scripts_root = Path(
    os.environ.get("BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR", "/repo/scripts")
)
SCRIPTS_ROOT = (
    REPO_ROOT / "scripts"
    if REPO_ROOT is not None
    else _configured_scripts_root
    if (_configured_scripts_root / "desktop_visible_auth_requirements.lock").is_file()
    else None
)
EXACT_PIN_RE = re.compile(r"^[A-Za-z0-9_.-]+==[^;\s]+(?:\s*;\s*.+)?$")
HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^;\s]+)(?P<marker>\s*;\s*.+)?$"
)
LOCK_FILES = (
    BACKEND_ROOT / "requirements-bootstrap.lock",
    BACKEND_ROOT / "requirements-macos-x64-build.lock",
    BACKEND_ROOT / "requirements.lock",
    BACKEND_ROOT / "requirements-dev.lock",
    BACKEND_ROOT / "requirements-audit.lock",
) + (
    (SCRIPTS_ROOT / "desktop_visible_auth_requirements.lock",)
    if SCRIPTS_ROOT is not None
    else ()
)
INPUT_LOCK_PAIRS = (
    (
        BACKEND_ROOT / "requirements-bootstrap.in",
        BACKEND_ROOT / "requirements-bootstrap.lock",
    ),
    (
        BACKEND_ROOT / "requirements-macos-x64-build.in",
        BACKEND_ROOT / "requirements-macos-x64-build.lock",
    ),
    (
        BACKEND_ROOT / "requirements-runtime.in",
        BACKEND_ROOT / "requirements.lock",
    ),
    (
        BACKEND_ROOT / "requirements-dev.in",
        BACKEND_ROOT / "requirements-dev.lock",
    ),
    (
        BACKEND_ROOT / "requirements-audit.in",
        BACKEND_ROOT / "requirements-audit.lock",
    ),
) + (
    (
        (
            SCRIPTS_ROOT / "desktop_visible_auth_requirements.txt",
            SCRIPTS_ROOT / "desktop_visible_auth_requirements.lock",
        ),
    )
    if SCRIPTS_ROOT is not None
    else ()
)
INSTALL_SITES = (
    ("scripts/prepare_portable_python_runtime.js", 3),
    ("scripts/build_desktop_packaged_runtime.js", 3),
    ("scripts/run_release_health.py", 2),
)


def _resolve_install_path(relative_path: str) -> Path:
    if relative_path.startswith("backend/"):
        return BACKEND_ROOT / relative_path.removeprefix("backend/")
    if relative_path.startswith("scripts/") and SCRIPTS_ROOT is not None:
        return SCRIPTS_ROOT / relative_path.removeprefix("scripts/")
    raise AssertionError(f"Install-site root is unavailable: {relative_path}")


def _path_label(path: Path) -> str:
    if path.is_relative_to(BACKEND_ROOT):
        return f"backend/{path.relative_to(BACKEND_ROOT).as_posix()}"
    if SCRIPTS_ROOT is not None and path.is_relative_to(SCRIPTS_ROOT):
        return f"scripts/{path.relative_to(SCRIPTS_ROOT).as_posix()}"
    return str(path)


def _logical_lock_entries(path: Path) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    requirement = ""
    hashes: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        if continued:
            line = line[:-1].rstrip()
        if line.startswith("--hash="):
            hashes.append(line)
        else:
            if requirement:
                entries.append((requirement, hashes))
            requirement = line
            hashes = []
        if requirement and not continued:
            entries.append((requirement, hashes))
            requirement = ""
            hashes = []
    if requirement:
        entries.append((requirement, hashes))
    return entries


def _source_entries(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _canonical_pin(requirement: str) -> tuple[str, str, str]:
    match = PIN_RE.fullmatch(requirement)
    if match is None:
        raise AssertionError(f"Requirement is not exact: {requirement}")
    name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
    marker = (match.group("marker") or "").strip()
    return name, match.group("version"), marker


def _pins(entries: list[str]) -> set[tuple[str, str, str]]:
    return {
        _canonical_pin(entry)
        for entry in entries
        if not entry.startswith(("-r ", "--requirement "))
    }


class PythonDependencyLockTests(unittest.TestCase):
    def test_all_locks_contain_only_exact_hashed_requirements(self) -> None:
        for lock_path in LOCK_FILES:
            with self.subTest(lock=_path_label(lock_path)):
                self.assertTrue(lock_path.is_file())
                requirements = 0
                for requirement, hashes in _logical_lock_entries(lock_path):
                    if requirement.startswith(("-r ", "--requirement ")):
                        included = requirement.split(maxsplit=1)[1]
                        self.assertTrue((lock_path.parent / included).is_file())
                        continue
                    requirements += 1
                    self.assertRegex(requirement, EXACT_PIN_RE)
                    self.assertTrue(hashes, requirement)
                    self.assertEqual(len(hashes), len(set(hashes)))
                    for digest in hashes:
                        self.assertRegex(digest, HASH_RE)
                self.assertGreater(requirements, 0)

    def test_committed_exact_inputs_match_every_lock_version(self) -> None:
        source_destinations = {
            source.resolve(): destination.resolve()
            for source, destination in INPUT_LOCK_PAIRS
        }
        for source, lock in INPUT_LOCK_PAIRS:
            with self.subTest(source=source.name, lock=lock.name):
                source_entries = _source_entries(source)
                lock_entries = [
                    requirement
                    for requirement, _hashes in _logical_lock_entries(lock)
                ]
                self.assertEqual(_pins(source_entries), _pins(lock_entries))
                expected_includes = set()
                for entry in source_entries:
                    if not entry.startswith(("-r ", "--requirement ")):
                        continue
                    included_source = (source.parent / entry.split(maxsplit=1)[1]).resolve()
                    included_lock = source_destinations.get(included_source)
                    self.assertIsNotNone(included_lock, entry)
                    assert included_lock is not None
                    expected_includes.add(
                        f"-r {included_lock.relative_to(lock.parent).as_posix()}"
                    )
                actual_includes = {
                    entry
                    for entry in lock_entries
                    if entry.startswith(("-r ", "--requirement "))
                }
                self.assertEqual(actual_includes, expected_includes)
                header = lock.read_text(encoding="utf-8").splitlines()[:6]
                self.assertIn(
                    "# Regenerate all locks: python3 "
                    "backend/scripts/generate_python_hash_lock.py",
                    header,
                )

    def test_declared_direct_pins_are_current_in_exact_inputs_and_locks(self) -> None:
        declarations = (
            (
                BACKEND_ROOT / "requirements.txt",
                BACKEND_ROOT / "requirements-runtime.in",
                BACKEND_ROOT / "requirements.lock",
            ),
            (
                BACKEND_ROOT / "requirements-dev.txt",
                BACKEND_ROOT / "requirements-dev.in",
                BACKEND_ROOT / "requirements-dev.lock",
            ),
        )
        for declaration, exact_input, lock in declarations:
            with self.subTest(declaration=declaration.name):
                declared = _pins(_source_entries(declaration))
                input_pins = _pins(_source_entries(exact_input))
                lock_pins = _pins(
                    [item for item, _hashes in _logical_lock_entries(lock)]
                )
                self.assertTrue(declared)
                self.assertLessEqual(declared, input_pins)
                self.assertLessEqual(declared, lock_pins)
        runtime_declaration = _source_entries(BACKEND_ROOT / "requirements.txt")
        self.assertFalse(
            any(
                entry.startswith(("-r ", "--requirement "))
                for entry in runtime_declaration
            ),
            "requirements.txt must not include its generated lock",
        )

    def test_lock_generator_has_one_canonical_non_circular_interface(self) -> None:
        generator = (
            BACKEND_ROOT / "scripts" / "generate_python_hash_lock.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--check"', generator)
        self.assertIn("LOCK_SPECS =", generator)
        self.assertNotIn('parser.add_argument("input"', generator)
        self.assertNotIn('parser.add_argument("output"', generator)
        if SCRIPTS_ROOT is not None:
            release_gate = (SCRIPTS_ROOT / "run_release_health.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("generate_python_hash_lock.py", release_gate)
            self.assertIn('"--check"', release_gate)
            self.assertIn("check_cryptography_distribution_matrix.py", release_gate)
            for variable in (
                "BREAKTWENTY_BROWSER_RUNTIME_DIR",
                "BREAKTWENTY_DESKTOP_AUTH_DIR",
                "BREAKTWENTY_DIAGNOSTIC_DIR",
                "BREAKTWENTY_VISIBLE_AUTH_SCRIPTS_DIR",
                "PROVIDER_CATALOG_PATH",
                "PYTHONPYCACHEPREFIX",
                "TMPDIR",
            ):
                self.assertIn(f'"{variable}"', release_gate)

    def test_intel_macos_cryptography_source_build_is_exact_and_pinned(self) -> None:
        runtime_pins = _pins(_source_entries(BACKEND_ROOT / "requirements-runtime.in"))
        build_pins = _pins(
            _source_entries(BACKEND_ROOT / "requirements-macos-x64-build.in")
        )
        self.assertIn(("cryptography", "50.0.0", ""), runtime_pins)
        self.assertIn(("maturin", "1.14.1", ""), build_pins)
        self.assertIn(("cffi", "2.1.0", ""), build_pins)
        self.assertIn(("pycparser", "3.0", ""), build_pins)

        if SCRIPTS_ROOT is not None:
            policy = (SCRIPTS_ROOT / "python_runtime_install_policy.js").read_text(
                encoding="utf-8"
            )
            self.assertIn("x86_64-apple-darwin", policy)
            self.assertIn("OPENSSL_STATIC", policy)
            self.assertIn("OPENSSL_SHA256", policy)
            self.assertIn("const OPENSSL_VERSION = '4.0.1'", policy)
            self.assertIn("MACOSX_DEPLOYMENT_TARGET", policy)
            self.assertIn("cryptography", policy)
            portable_builder = (
                SCRIPTS_ROOT / "prepare_portable_python_runtime.js"
            ).read_text(encoding="utf-8")
            packaged_builder = (
                SCRIPTS_ROOT / "build_desktop_packaged_runtime.js"
            ).read_text(encoding="utf-8")
            for builder in (portable_builder, packaged_builder):
                self.assertIn("'uninstall', '--yes', 'maturin'", builder)
            notices = (
                BACKEND_ROOT / "scripts" / "generate_python_runtime_notices.py"
            ).read_text(encoding="utf-8")
            self.assertIn("include_macos_x64_static_openssl", notices)
            self.assertIn("OPENSSL_SOURCE_SHA256", notices)
            self.assertTrue(
                (BACKEND_ROOT / "scripts" / "openssl-4.0.1-LICENSE.txt").is_file()
            )

    @unittest.skipIf(SCRIPTS_ROOT is None, "scripts checkout is not mounted")
    def test_visible_auth_lock_matches_declared_pins(self) -> None:
        assert SCRIPTS_ROOT is not None
        source = SCRIPTS_ROOT / "desktop_visible_auth_requirements.txt"
        lock = SCRIPTS_ROOT / "desktop_visible_auth_requirements.lock"
        source_pins = {
            line.strip()
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        lock_pins = {
            requirement
            for requirement, _hashes in _logical_lock_entries(lock)
            if not requirement.startswith(("-r ", "--requirement "))
        }
        self.assertEqual(lock_pins, source_pins)

    @unittest.skipIf(SCRIPTS_ROOT is None, "scripts checkout is not mounted")
    def test_visible_auth_runners_do_not_recommend_unverified_installs(self) -> None:
        assert SCRIPTS_ROOT is not None
        runner_paths = sorted(SCRIPTS_ROOT.glob("*_visible_auth.py"))
        self.assertGreater(len(runner_paths), 0)
        for runner_path in runner_paths:
            with self.subTest(runner=runner_path.name):
                content = runner_path.read_text(encoding="utf-8")
                self.assertNotRegex(content, r"(?i)pip(?:3)?\s+install")
                self.assertIn("PATCHRIGHT_SETUP_MESSAGE", content)
        common = (SCRIPTS_ROOT / "visible_auth_common.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("desktop_visible_auth_requirements.lock", common)
        self.assertNotRegex(common, r"(?i)pip(?:3)?\s+install")

    @unittest.skipIf(SCRIPTS_ROOT is None, "scripts checkout is not mounted")
    def test_supported_install_sites_enforce_hashes(self) -> None:
        for relative_path, command_count in INSTALL_SITES:
            install_path = _resolve_install_path(relative_path)
            with self.subTest(install_site=relative_path):
                content = install_path.read_text(encoding="utf-8")
                self.assertEqual(content.count("--require-hashes"), command_count)
                self.assertNotIn("pip==25.3", content)
                self.assertNotRegex(content, r"pip[^\n]*install[^\n]*requirements-dev\.txt")

    @unittest.skipIf(SCRIPTS_ROOT is None, "scripts checkout is not mounted")
    def test_runtime_build_uses_hashed_bootstrap_without_isolation(self) -> None:
        runtime_installers = (
            "scripts/prepare_portable_python_runtime.js",
            "scripts/build_desktop_packaged_runtime.js",
            "scripts/run_release_health.py",
        )
        for relative_path in runtime_installers:
            install_path = _resolve_install_path(relative_path)
            with self.subTest(installer=relative_path):
                content = install_path.read_text(encoding="utf-8")
                self.assertIn("requirements-bootstrap.lock", content)
                self.assertIn("--no-build-isolation", content)
                self.assertNotIn("moomoo" + "-api", content)


if __name__ == "__main__":
    unittest.main()
