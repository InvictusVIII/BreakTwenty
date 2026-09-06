from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import platform
import socket
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.brand import APP_BRAND_NAME

DEFAULT_PREFLIGHT_PORT = 443
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 2.0
DEFAULT_DNS_PROBE_TIMEOUT_SECONDS = 2.0
SYNC_DNS_GATE_PROVIDER_LIMIT = 3
SYNC_DNS_GATE_MIN_TARGETS = 2
SYNC_DNS_GATE_RETRY_DELAYS_SECONDS = (3.0, 7.0, 10.0, 10.0)
SYNC_DNS_CANARY_HOST = "example.com"
SYNC_DNS_CANARY_PROVIDER = "internet_canary"
SYNC_NETWORK_SUPPORT_PROVIDER = "network"
SYNC_NETWORK_SUPPORT_TRIGGER = "sync_dns_gate_blocked"
SYNC_NETWORK_BLOCKED_CODE = "dns_resolution_temporarily_unavailable"
SYNC_NETWORK_BLOCKED_MESSAGE = (
    f"{APP_BRAND_NAME} couldn't reach your financial providers because of a temporary "
    "connection problem. Check your internet connection and try again."
)

logger = logging.getLogger("breaktwenty.network_preflight")


@dataclass(frozen=True)
class NetworkPreflightResult:
    provider: str
    host: str | None
    port: int
    status: str
    diagnosis: str
    duration_ms: int
    resolved_addresses: tuple[str, ...] = ()
    connected_address: str | None = None
    error: str | None = None
    error_code: int | None = None
    error_name: str | None = None

    def log_fields(self) -> dict[str, Any]:
        ipv4_count, ipv6_count = _address_family_counts(self.resolved_addresses)
        return {
            "preflight_status": self.status,
            "diagnosis": self.diagnosis,
            "host": self.host,
            "port": self.port,
            "resolved_count": len(self.resolved_addresses),
            "resolved_ipv4_count": ipv4_count,
            "resolved_ipv6_count": ipv6_count,
            "connected_address_family": _address_family(self.connected_address),
            "error": self.error,
            "error_code": self.error_code,
            "error_name": self.error_name,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class DnsResolutionResult:
    provider: str
    host: str | None
    status: str
    diagnosis: str
    duration_ms: int
    resolved_addresses: tuple[str, ...] = ()
    error: str | None = None
    error_code: int | None = None
    error_name: str | None = None

    def log_fields(self) -> dict[str, Any]:
        ipv4_count, ipv6_count = _address_family_counts(self.resolved_addresses)
        return {
            "provider": self.provider,
            "host": self.host,
            "dns_status": self.status,
            "diagnosis": self.diagnosis,
            "duration_ms": self.duration_ms,
            "resolved_count": len(self.resolved_addresses),
            "resolved_ipv4_count": ipv4_count,
            "resolved_ipv6_count": ipv6_count,
            "error": self.error,
            "error_code": self.error_code,
            "error_name": self.error_name,
        }


@dataclass(frozen=True)
class SyncNetworkGateResult:
    status: str
    attempts: int
    probes: tuple[DnsResolutionResult, ...]
    probe_attempts: tuple[tuple[DnsResolutionResult, ...], ...] = ()

    def blocker_payload(self) -> dict[str, Any]:
        return {
            "code": SYNC_NETWORK_BLOCKED_CODE,
            "message": SYNC_NETWORK_BLOCKED_MESSAGE,
            "technical_detail": "Temporary DNS lookup failure",
            "retryable": True,
        }

    def route_response(self) -> dict[str, Any]:
        return {
            "status": "network_blocked",
            **self.blocker_payload(),
        }


def provider_preflight_host(provider: str) -> str | None:
    normalized_provider = str(provider or "").strip().lower()
    try:
        from app.provider_catalog import get_provider_network_preflight_host

        return get_provider_network_preflight_host(normalized_provider)
    except (KeyError, ValueError):
        return None


async def run_provider_dns_resolution(
    provider: str,
    *,
    timeout_seconds: float = DEFAULT_DNS_PROBE_TIMEOUT_SECONDS,
) -> DnsResolutionResult:
    normalized_provider = str(provider or "").strip().lower()
    host = provider_preflight_host(normalized_provider)
    if not host:
        return DnsResolutionResult(
            provider=normalized_provider,
            host=None,
            status="not_configured",
            diagnosis="no_provider_preflight_host_configured",
            duration_ms=0,
        )
    return await _run_dns_resolution(
        normalized_provider,
        host,
        timeout_seconds=timeout_seconds,
    )


async def run_sync_network_gate(
    providers: list[Any] | tuple[Any, ...],
    *,
    user_id: int | None,
    flow: str,
    mode: str,
    batch_id: str | None = None,
    retry_delays_seconds: tuple[float, ...] = SYNC_DNS_GATE_RETRY_DELAYS_SECONDS,
) -> SyncNetworkGateResult:
    targets = _sync_dns_gate_targets(providers)
    if len(targets) < SYNC_DNS_GATE_MIN_TARGETS:
        result = SyncNetworkGateResult(status="inconclusive", attempts=0, probes=())
        _log_sync_gate_result(
            result,
            flow=flow,
            mode=mode,
            batch_id=batch_id,
        )
        return result

    max_attempts = len(retry_delays_seconds) + 1
    probe_attempts: list[tuple[DnsResolutionResult, ...]] = []
    for attempt in range(1, max_attempts + 1):
        raw_results = await asyncio.gather(
            *(
                _run_dns_resolution(
                    provider,
                    host,
                    timeout_seconds=DEFAULT_DNS_PROBE_TIMEOUT_SECONDS,
                )
                for provider, host in targets
            ),
            return_exceptions=True,
        )
        probes = tuple(
            result
            for result in raw_results
            if isinstance(result, DnsResolutionResult)
        )
        probe_attempts.append(probes)
        status = _sync_dns_gate_status(probes, expected_count=len(targets))
        result = SyncNetworkGateResult(
            status=status,
            attempts=attempt,
            probes=probes,
            probe_attempts=tuple(probe_attempts),
        )
        _log_sync_gate_result(
            result,
            flow=flow,
            mode=mode,
            batch_id=batch_id,
        )
        if status != "temporary_failure":
            return result
        if attempt >= max_attempts:
            blocked = SyncNetworkGateResult(
                status="blocked",
                attempts=attempt,
                probes=probes,
                probe_attempts=tuple(probe_attempts),
            )
            resolver_facts: dict[str, Any] = {}
            try:
                resolver_facts = collect_sanitized_resolver_facts()
                _log_sanitized_resolver_context(
                    blocked,
                    facts=resolver_facts,
                    flow=flow,
                    mode=mode,
                    batch_id=batch_id,
                )
            except Exception as exc:
                logger.warning(
                    "sync DNS gate resolver context unavailable error_type=%s",
                    type(exc).__name__,
                )
            if user_id is not None:
                await _archive_sync_network_block(
                    blocked,
                    user_id=user_id,
                    resolver_facts=resolver_facts,
                    flow=flow,
                    mode=mode,
                    batch_id=batch_id,
                )
            return blocked
        await asyncio.sleep(max(float(retry_delays_seconds[attempt - 1]), 0.0))

    return SyncNetworkGateResult(status="inconclusive", attempts=0, probes=())


async def _run_dns_resolution(
    provider: str,
    host: str,
    *,
    timeout_seconds: float,
) -> DnsResolutionResult:
    started_at = perf_counter()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _run_dns_resolution_sync,
                provider,
                host,
                DEFAULT_PREFLIGHT_PORT,
            ),
            timeout=max(float(timeout_seconds), 0.1),
        )
    except TimeoutError:
        return DnsResolutionResult(
            provider=provider,
            host=host,
            status="temporary_failure",
            diagnosis="dns_lookup_timed_out_from_backend_runtime",
            duration_ms=_elapsed_ms(started_at),
            error="DNS lookup timed out",
            error_name="TIMEOUT",
        )


async def run_provider_network_preflight(
    provider: str,
    *,
    timeout_seconds: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
) -> NetworkPreflightResult:
    normalized_provider = str(provider or "").strip().lower()
    host = provider_preflight_host(normalized_provider)
    if not host:
        return NetworkPreflightResult(
            provider=normalized_provider,
            host=None,
            port=DEFAULT_PREFLIGHT_PORT,
            status="not_configured",
            diagnosis="no_provider_preflight_host_configured",
            duration_ms=0,
        )
    return await asyncio.to_thread(
        _run_network_preflight_sync,
        normalized_provider,
        host,
        DEFAULT_PREFLIGHT_PORT,
        timeout_seconds,
    )


def _run_dns_resolution_sync(
    provider: str,
    host: str,
    port: int,
) -> DnsResolutionResult:
    started_at = perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        is_temporary = exc.errno == socket.EAI_AGAIN
        error_code, error_name = _socket_error_identity(exc)
        return DnsResolutionResult(
            provider=provider,
            host=host,
            status="temporary_failure" if is_temporary else "failed",
            diagnosis=(
                "temporary_dns_lookup_failure_from_backend_runtime"
                if is_temporary
                else "dns_lookup_failed_from_backend_runtime"
            ),
            duration_ms=_elapsed_ms(started_at),
            error=_sanitize_preflight_error(exc),
            error_code=error_code,
            error_name=error_name,
        )
    except OSError as exc:
        error_code, error_name = _socket_error_identity(exc)
        return DnsResolutionResult(
            provider=provider,
            host=host,
            status="failed",
            diagnosis="dns_lookup_failed_from_backend_runtime",
            duration_ms=_elapsed_ms(started_at),
            error=_sanitize_preflight_error(exc),
            error_code=error_code,
            error_name=error_name,
        )

    addresses = _unique_addresses(infos)
    if not addresses:
        return DnsResolutionResult(
            provider=provider,
            host=host,
            status="failed",
            diagnosis="dns_lookup_returned_no_addresses",
            duration_ms=_elapsed_ms(started_at),
            error="DNS lookup returned no addresses",
        )
    return DnsResolutionResult(
        provider=provider,
        host=host,
        status="resolved",
        diagnosis="provider_host_resolved_from_backend_runtime",
        duration_ms=_elapsed_ms(started_at),
        resolved_addresses=tuple(addresses),
    )


def _run_network_preflight_sync(
    provider: str,
    host: str,
    port: int,
    timeout_seconds: float,
) -> NetworkPreflightResult:
    started_at = perf_counter()
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        error_code, error_name = _socket_error_identity(exc)
        return NetworkPreflightResult(
            provider=provider,
            host=host,
            port=port,
            status="dns_failed",
            diagnosis="dns_lookup_failed_from_backend_runtime",
            duration_ms=_elapsed_ms(started_at),
            error=_sanitize_preflight_error(exc),
            error_code=error_code,
            error_name=error_name,
        )

    addresses = _unique_addresses(infos)
    last_error: str | None = None
    for address in addresses:
        try:
            connection = socket.create_connection((address, port), timeout=timeout_seconds)
            connection.close()
            return NetworkPreflightResult(
                provider=provider,
                host=host,
                port=port,
                status="reachable",
                diagnosis="provider_reachable_after_sync_network_error",
                duration_ms=_elapsed_ms(started_at),
                resolved_addresses=tuple(addresses),
                connected_address=address,
            )
        except OSError as exc:
            last_error = _sanitize_preflight_error(exc)
            last_error_code, last_error_name = _socket_error_identity(exc)

    return NetworkPreflightResult(
        provider=provider,
        host=host,
        port=port,
        status="tcp_failed",
        diagnosis="dns_resolved_but_tcp_connect_failed_from_backend_runtime",
        duration_ms=_elapsed_ms(started_at),
        resolved_addresses=tuple(addresses),
        error=last_error,
        error_code=last_error_code if last_error else None,
        error_name=last_error_name if last_error else None,
    )


def _unique_addresses(infos: list[Any]) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        try:
            address = str(info[4][0]).strip()
        except (IndexError, TypeError):
            continue
        if not address or address in seen:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


def _elapsed_ms(started_at: float) -> int:
    return int((perf_counter() - started_at) * 1000)


def _sanitize_preflight_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    text = str(getattr(exc, "strerror", "") or "") or type(exc).__name__
    text = " ".join(text.split())
    if len(text) <= 220:
        return text
    return f"{text[:220].rstrip()}..."


def _socket_error_identity(exc: BaseException) -> tuple[int | None, str | None]:
    raw_code = getattr(exc, "errno", None)
    try:
        error_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        error_code = None
    if error_code is None:
        return None, type(exc).__name__
    for name in dir(socket):
        if not name.startswith("EAI_"):
            continue
        if getattr(socket, name, None) == error_code:
            return error_code, name
    return error_code, type(exc).__name__


def _address_family(address: str | None) -> str:
    try:
        parsed = ipaddress.ip_address(str(address or "").strip())
    except ValueError:
        return "none" if not address else "unknown"
    return "ipv4" if parsed.version == 4 else "ipv6"


def _address_family_counts(addresses: tuple[str, ...]) -> tuple[int, int]:
    ipv4_count = 0
    ipv6_count = 0
    for address in addresses:
        family = _address_family(address)
        if family == "ipv4":
            ipv4_count += 1
        elif family == "ipv6":
            ipv6_count += 1
    return ipv4_count, ipv6_count


def _sync_dns_gate_targets(
    providers: list[Any] | tuple[Any, ...],
) -> tuple[tuple[str, str], ...]:
    targets: list[tuple[str, str]] = []
    seen_hosts: set[str] = set()
    for raw_provider in providers:
        provider = str(raw_provider or "").strip().lower()
        host = provider_preflight_host(provider)
        if not provider or not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        targets.append((provider, host))
        if len(targets) >= SYNC_DNS_GATE_PROVIDER_LIMIT:
            break
    if len(targets) == 1:
        targets.append((SYNC_DNS_CANARY_PROVIDER, SYNC_DNS_CANARY_HOST))
    return tuple(targets)


def _sync_dns_gate_status(
    probes: tuple[DnsResolutionResult, ...],
    *,
    expected_count: int,
) -> str:
    if len(probes) != expected_count:
        return "inconclusive"
    if any(probe.status == "resolved" for probe in probes):
        return "ready"
    if probes and all(probe.status == "temporary_failure" for probe in probes):
        return "temporary_failure"
    return "inconclusive"


def _sync_gate_probe_summary(probes: tuple[DnsResolutionResult, ...]) -> str:
    return ",".join(
        (
            f"{probe.provider}:{probe.status}:{probe.diagnosis}:"
            f"{probe.duration_ms}ms:{probe.error_name or 'none'}"
        )
        for probe in probes
    ) or "none"


def _safe_log_context(value: str | None, *, fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized and all(character.isalnum() or character in "_-" for character in normalized):
        return normalized[:80]
    return fallback


def _log_sync_gate_result(
    result: SyncNetworkGateResult,
    *,
    flow: str,
    mode: str,
    batch_id: str | None,
) -> None:
    log = logger.warning if result.status in {"temporary_failure", "blocked"} else logger.info
    log(
        "sync DNS gate probe flow=%s mode=%s batch_id=%s attempt=%s status=%s probes=%s",
        _safe_log_context(flow, fallback="unknown"),
        _safe_log_context(mode, fallback="unknown"),
        _safe_log_context(batch_id, fallback="none"),
        result.attempts,
        result.status,
        _sync_gate_probe_summary(result.probes),
    )


def collect_sanitized_resolver_facts() -> dict[str, Any]:
    resolver_facts = _resolver_config_facts()
    interface_facts = _network_interface_facts()
    route_facts = _default_route_facts()
    backend_mode = str(os.getenv("BREAKTWENTY_BACKEND_MODE") or "").strip().lower()
    if not backend_mode and Path("/.dockerenv").exists():
        backend_mode = "docker"
    if backend_mode not in {"embedded", "docker"}:
        backend_mode = "other"
    release_major = str(platform.release() or "").split(".", 1)[0]
    if not release_major.isdigit():
        release_major = "other"
    return {
        "runtime_mode": backend_mode,
        "os_family": str(platform.system() or "unknown").strip().lower(),
        "os_release_major": release_major,
        "runtime_arch": _normalized_architecture(platform.machine()),
        "python_version": f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
        "proxy_http_present": _env_any_present("HTTP_PROXY"),
        "proxy_https_present": _env_any_present("HTTPS_PROXY"),
        "proxy_all_present": _env_any_present("ALL_PROXY"),
        "proxy_bypass_present": _env_any_present("NO_PROXY"),
        **resolver_facts,
        **interface_facts,
        **route_facts,
    }


def _env_any_present(name: str) -> bool:
    return bool(str(os.getenv(name) or os.getenv(name.lower()) or "").strip())


def _normalized_architecture(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"x86_64", "amd64"}:
        return "x86_64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    if normalized in {"x86", "i386", "i686"}:
        return "x86"
    return "other"


def _resolver_config_facts() -> dict[str, Any]:
    path = Path("/etc/resolv.conf")
    source = "unavailable"
    nameserver_classes: set[str] = set()
    nameserver_count = 0
    search_domain_count = 0
    option_names: set[str] = set()
    try:
        resolved_path = str(path.resolve(strict=True)).lower()
        if "systemd/resolve/stub-resolv.conf" in resolved_path:
            source = "systemd_stub"
        elif "systemd/resolve" in resolved_path:
            source = "systemd_resolved"
        elif "networkmanager" in resolved_path:
            source = "networkmanager"
        else:
            source = "resolv_conf"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    safe_options = {
        "attempts",
        "edns0",
        "no-aaaa",
        "rotate",
        "single-request",
        "single-request-reopen",
        "timeout",
        "trust-ad",
        "use-vc",
    }
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        directive = parts[0].lower()
        if directive == "nameserver" and len(parts) >= 2:
            nameserver_count += 1
            nameserver_classes.add(_ip_privacy_class(parts[1]))
        elif directive in {"search", "domain"}:
            search_domain_count += max(len(parts) - 1, 0)
        elif directive == "options":
            for option in parts[1:]:
                option_name = option.split(":", 1)[0].lower()
                if option_name in safe_options:
                    option_names.add(option_name)
    return {
        "resolver_source": source,
        "resolver_nameserver_count": nameserver_count,
        "resolver_nameserver_classes": ",".join(sorted(nameserver_classes)) or "none",
        "resolver_search_domain_count": search_domain_count,
        "resolver_option_names": ",".join(sorted(option_names)) or "none",
    }


def _ip_privacy_class(value: str | None) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return "invalid"
    family = "ipv4" if address.version == 4 else "ipv6"
    if address.is_loopback:
        return f"{family}_loopback"
    if address.is_link_local:
        return f"{family}_link_local"
    if address.is_private:
        return f"{family}_private"
    if address.is_unspecified:
        return f"{family}_unspecified"
    return f"{family}_public"


def _network_interface_facts() -> dict[str, Any]:
    try:
        interface_names = [name for _, name in socket.if_nameindex()]
    except OSError:
        interface_names = []
    classes: dict[str, int] = {}
    for name in interface_names:
        interface_class = _network_interface_class(name)
        classes[interface_class] = classes.get(interface_class, 0) + 1
    return {
        "network_interface_count": len(interface_names),
        "vpn_like_interface_present": classes.get("vpn_like", 0) > 0,
        "vpn_like_interface_count": classes.get("vpn_like", 0),
        "network_interface_classes": ",".join(
            f"{key}:{classes[key]}" for key in sorted(classes)
        ) or "none",
    }


def _network_interface_class(name: str | None) -> str:
    normalized = str(name or "").strip().lower()
    if normalized in {"lo", "lo0"} or normalized.startswith("loopback"):
        return "loopback"
    vpn_markers = (
        "tun",
        "tap",
        "utun",
        "wg",
        "wireguard",
        "ppp",
        "vpn",
        "tailscale",
        "zerotier",
    )
    if any(marker in normalized for marker in vpn_markers):
        return "vpn_like"
    if normalized.startswith(("wl", "wifi", "wi-fi")):
        return "wireless"
    if normalized.startswith(("en", "eth")):
        return "wired"
    if normalized.startswith(("docker", "br-", "veth", "virbr")):
        return "virtual"
    return "other"


def _default_route_facts() -> dict[str, Any]:
    interface_class = "unavailable"
    route_present = False
    route_path = Path("/proc/net/route")
    try:
        for line in route_path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == "00000000":
                route_present = True
                interface_class = _network_interface_class(fields[0])
                break
    except OSError:
        pass

    default_source_class = "unavailable"
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        default_source_class = _ip_privacy_class(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        if probe is not None:
            probe.close()
    return {
        "default_route_present": route_present,
        "default_route_interface_class": interface_class,
        "default_source_address_class": default_source_class,
    }


def _log_sanitized_resolver_context(
    result: SyncNetworkGateResult,
    *,
    facts: dict[str, Any],
    flow: str,
    mode: str,
    batch_id: str | None,
) -> None:
    logger.warning(
        "sync DNS gate blocked flow=%s mode=%s batch_id=%s attempts=%s "
        "runtime_mode=%s os_family=%s os_release_major=%s runtime_arch=%s "
        "python_version=%s proxy_http_present=%s proxy_https_present=%s "
        "proxy_all_present=%s proxy_bypass_present=%s resolver_source=%s "
        "resolver_nameserver_count=%s resolver_nameserver_classes=%s "
        "resolver_search_domain_count=%s resolver_option_names=%s "
        "network_interface_count=%s vpn_like_interface_present=%s "
        "vpn_like_interface_count=%s network_interface_classes=%s "
        "default_route_present=%s default_route_interface_class=%s "
        "default_source_address_class=%s probes=%s",
        _safe_log_context(flow, fallback="unknown"),
        _safe_log_context(mode, fallback="unknown"),
        _safe_log_context(batch_id, fallback="none"),
        result.attempts,
        facts["runtime_mode"],
        facts["os_family"],
        facts["os_release_major"],
        facts["runtime_arch"],
        facts["python_version"],
        facts["proxy_http_present"],
        facts["proxy_https_present"],
        facts["proxy_all_present"],
        facts["proxy_bypass_present"],
        facts["resolver_source"],
        facts["resolver_nameserver_count"],
        facts["resolver_nameserver_classes"],
        facts["resolver_search_domain_count"],
        facts["resolver_option_names"],
        facts["network_interface_count"],
        facts["vpn_like_interface_present"],
        facts["vpn_like_interface_count"],
        facts["network_interface_classes"],
        facts["default_route_present"],
        facts["default_route_interface_class"],
        facts["default_source_address_class"],
        _sync_gate_probe_summary(result.probes),
    )


def _sync_network_archive_probe(probe: DnsResolutionResult) -> dict[str, Any]:
    allowed_hosts = {SYNC_DNS_CANARY_HOST}
    try:
        from app.provider_catalog import iter_provider_metadata

        allowed_hosts.update(
            str(((metadata.get("backend") or {}).get("networkPreflight") or {}).get("host") or "")
            for _provider, metadata in iter_provider_metadata()
        )
        allowed_hosts.discard("")
    except Exception:
        pass
    ipv4_count, ipv6_count = _address_family_counts(probe.resolved_addresses)
    return {
        "target": _safe_log_context(probe.provider, fallback="unknown"),
        "public_host": probe.host if probe.host in allowed_hosts else "unknown",
        "status": _safe_log_context(probe.status, fallback="unknown"),
        "diagnosis": _safe_log_context(probe.diagnosis, fallback="unknown"),
        "duration_ms": max(0, min(int(probe.duration_ms), 120_000)),
        "resolved_count": len(probe.resolved_addresses),
        "resolved_ipv4_count": ipv4_count,
        "resolved_ipv6_count": ipv6_count,
        "error_code": int(probe.error_code) if isinstance(probe.error_code, int) else None,
        "error_name": _safe_log_context(probe.error_name, fallback="none"),
    }


def _sync_network_archive_payload(
    result: SyncNetworkGateResult,
    *,
    resolver_facts: dict[str, Any],
    flow: str,
    mode: str,
    batch_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "code": SYNC_NETWORK_BLOCKED_CODE,
        "flow": _safe_log_context(flow, fallback="unknown"),
        "mode": _safe_log_context(mode, fallback="unknown"),
        "batch_id": _safe_log_context(batch_id, fallback="none"),
        "attempt_count": result.attempts,
        "attempts": [
            {
                "attempt": attempt_number,
                "probes": [
                    _sync_network_archive_probe(probe)
                    for probe in probes
                ],
            }
            for attempt_number, probes in enumerate(result.probe_attempts, start=1)
        ],
        "resolver_facts": dict(resolver_facts),
    }


async def _archive_sync_network_block(
    result: SyncNetworkGateResult,
    *,
    user_id: int,
    resolver_facts: dict[str, Any],
    flow: str,
    mode: str,
    batch_id: str | None,
) -> None:
    try:
        from app.services.support_auto_archive import archive_run

        event_id = _safe_log_context(batch_id, fallback="")
        if not event_id:
            event_id = f"dns-gate-{uuid4().hex[:12]}"
        run_dir = await archive_run(
            user_id=user_id,
            provider=SYNC_NETWORK_SUPPORT_PROVIDER,
            sync_id=event_id,
            trigger=SYNC_NETWORK_SUPPORT_TRIGGER,
            error="Temporary DNS lookup failure",
            extra_fields={
                "network_preflight": _sync_network_archive_payload(
                    result,
                    resolver_facts=resolver_facts,
                    flow=flow,
                    mode=mode,
                    batch_id=batch_id,
                ),
            },
        )
        if run_dir is None:
            logger.warning("sync DNS gate support archive was not written")
    except Exception as exc:
        logger.warning(
            "sync DNS gate support archive failed error_type=%s",
            type(exc).__name__,
        )
