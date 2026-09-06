from __future__ import annotations

import socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.network_preflight import (
    DnsResolutionResult,
    NetworkPreflightResult,
    _network_interface_facts,
    _resolver_config_facts,
    _run_dns_resolution_sync,
    _run_network_preflight_sync,
    collect_sanitized_resolver_facts,
    provider_preflight_host,
    run_provider_dns_resolution,
    run_provider_network_preflight,
    run_sync_network_gate,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class NetworkPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_provider_resolves_preflight_host(self) -> None:
        self.assertEqual("secure.scotiabank.com", provider_preflight_host("scotiabank"))

    async def test_unknown_provider_skips_preflight(self) -> None:
        result = await run_provider_network_preflight("demo")

        self.assertEqual("not_configured", result.status)
        self.assertEqual("no_provider_preflight_host_configured", result.diagnosis)

    async def test_unknown_provider_skips_dns_resolution(self) -> None:
        result = await run_provider_dns_resolution("demo")

        self.assertEqual("not_configured", result.status)
        self.assertEqual("no_provider_preflight_host_configured", result.diagnosis)

    async def test_temporary_dns_failure_is_distinguished(self) -> None:
        with patch(
            "app.services.network_preflight.socket.getaddrinfo",
            side_effect=socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        ):
            result = _run_dns_resolution_sync("demo", "bank.example", 443)

        self.assertEqual("temporary_failure", result.status)
        self.assertEqual("temporary_dns_lookup_failure_from_backend_runtime", result.diagnosis)
        self.assertEqual(socket.EAI_AGAIN, result.error_code)
        self.assertEqual("EAI_AGAIN", result.error_name)

    async def test_permanent_dns_failure_does_not_look_temporary(self) -> None:
        with patch(
            "app.services.network_preflight.socket.getaddrinfo",
            side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
        ):
            result = _run_dns_resolution_sync("demo", "bank.example", 443)

        self.assertEqual("failed", result.status)
        self.assertEqual("dns_lookup_failed_from_backend_runtime", result.diagnosis)

    async def test_dns_resolution_returns_addresses_without_connecting(self) -> None:
        with patch(
            "app.services.network_preflight.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 443)),
            ],
        ):
            result = _run_dns_resolution_sync("demo", "bank.example", 443)

        self.assertEqual(
            DnsResolutionResult(
                provider="demo",
                host="bank.example",
                status="resolved",
                diagnosis="provider_host_resolved_from_backend_runtime",
                duration_ms=result.duration_ms,
                resolved_addresses=("203.0.113.10",),
            ),
            result,
        )

    async def test_dns_failure_returns_dns_failed(self) -> None:
        with patch(
            "app.services.network_preflight.socket.getaddrinfo",
            side_effect=socket.gaierror(-3, "Temporary failure in name resolution"),
        ):
            result = _run_network_preflight_sync("demo", "bank.example", 443, 0.1)

        self.assertEqual("dns_failed", result.status)
        self.assertEqual("dns_lookup_failed_from_backend_runtime", result.diagnosis)
        self.assertIn("Temporary failure in name resolution", result.error or "")

    async def test_tcp_failure_returns_tcp_failed_after_dns_resolution(self) -> None:
        with (
            patch(
                "app.services.network_preflight.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 443)),
                ],
            ),
            patch(
                "app.services.network_preflight.socket.create_connection",
                side_effect=TimeoutError("timed out"),
            ),
        ):
            result = _run_network_preflight_sync("demo", "bank.example", 443, 0.1)

        self.assertEqual("tcp_failed", result.status)
        self.assertEqual("dns_resolved_but_tcp_connect_failed_from_backend_runtime", result.diagnosis)
        self.assertEqual(("203.0.113.10",), result.resolved_addresses)
        self.assertIn("timed out", result.error or "")

    async def test_success_returns_reachable(self) -> None:
        connection = _FakeConnection()
        with (
            patch(
                "app.services.network_preflight.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 443)),
                ],
            ),
            patch(
                "app.services.network_preflight.socket.create_connection",
                return_value=connection,
            ),
        ):
            result = _run_network_preflight_sync("demo", "bank.example", 443, 0.1)

        self.assertEqual("reachable", result.status)
        self.assertEqual("provider_reachable_after_sync_network_error", result.diagnosis)
        self.assertEqual("203.0.113.10", result.connected_address)
        self.assertTrue(connection.closed)

    async def test_log_fields_exclude_resolved_ip_addresses(self) -> None:
        result = NetworkPreflightResult(
            provider="demo",
            host="bank.example",
            port=443,
            status="reachable",
            diagnosis="provider_reachable_after_sync_network_error",
            duration_ms=12,
            resolved_addresses=("203.0.113.10", "203.0.113.11"),
            connected_address="203.0.113.10",
        )

        self.assertEqual(
            {
                "preflight_status": "reachable",
                "diagnosis": "provider_reachable_after_sync_network_error",
                "host": "bank.example",
                "port": 443,
                "resolved_count": 2,
                "resolved_ipv4_count": 2,
                "resolved_ipv6_count": 0,
                "connected_address_family": "ipv4",
                "error": None,
                "error_code": None,
                "error_name": None,
                "duration_ms": 12,
            },
            result.log_fields(),
        )
        self.assertNotIn("203.0.113.10", str(result.log_fields()))

    async def test_sync_gate_proceeds_when_any_target_resolves(self) -> None:
        temporary = DnsResolutionResult(
            provider="rbc",
            host="www1.royalbank.com",
            status="temporary_failure",
            diagnosis="temporary_dns_lookup_failure_from_backend_runtime",
            duration_ms=1,
            error_code=socket.EAI_AGAIN,
            error_name="EAI_AGAIN",
        )
        resolved = DnsResolutionResult(
            provider="td",
            host="easyweb.td.com",
            status="resolved",
            diagnosis="provider_host_resolved_from_backend_runtime",
            duration_ms=1,
            resolved_addresses=("203.0.113.10",),
        )
        with patch(
            "app.services.network_preflight._run_dns_resolution",
            new=AsyncMock(side_effect=[temporary, resolved]),
        ):
            result = await run_sync_network_gate(
                ["rbc", "td"],
                user_id=None,
                flow="batch",
                mode="auto",
                retry_delays_seconds=(),
            )

        self.assertEqual("ready", result.status)
        self.assertEqual(1, result.attempts)

    async def test_single_provider_gate_adds_independent_dns_canary(self) -> None:
        resolved = DnsResolutionResult(
            provider="rbc",
            host="www1.royalbank.com",
            status="resolved",
            diagnosis="provider_host_resolved_from_backend_runtime",
            duration_ms=1,
            resolved_addresses=("203.0.113.10",),
        )
        probe = AsyncMock(return_value=resolved)
        with patch(
            "app.services.network_preflight._run_dns_resolution",
            new=probe,
        ):
            result = await run_sync_network_gate(
                ["rbc"],
                user_id=None,
                flow="single_sync",
                mode="manual",
                retry_delays_seconds=(),
            )

        self.assertEqual("ready", result.status)
        self.assertEqual(2, probe.await_count)
        self.assertEqual(
            ("internet_canary", "example.com"),
            probe.await_args_list[1].args[:2],
        )

    async def test_sync_gate_blocks_after_repeated_temporary_failures(self) -> None:
        temporary_results = [
            DnsResolutionResult(
                provider=provider,
                host=f"{provider}.example",
                status="temporary_failure",
                diagnosis="temporary_dns_lookup_failure_from_backend_runtime",
                duration_ms=1,
                error_code=socket.EAI_AGAIN,
                error_name="EAI_AGAIN",
            )
            for _ in range(3)
            for provider in ("rbc", "td")
        ]
        with (
            patch(
                "app.services.network_preflight._run_dns_resolution",
                new=AsyncMock(side_effect=temporary_results),
            ),
            patch(
                "app.services.network_preflight.collect_sanitized_resolver_facts",
                return_value={
                    "runtime_mode": "embedded",
                    "os_family": "linux",
                    "os_release_major": "6",
                    "runtime_arch": "x86_64",
                    "python_version": "3.12",
                    "proxy_http_present": False,
                    "proxy_https_present": False,
                    "proxy_all_present": False,
                    "proxy_bypass_present": False,
                    "resolver_source": "systemd_stub",
                    "resolver_nameserver_count": 1,
                    "resolver_nameserver_classes": "ipv4_loopback",
                    "resolver_search_domain_count": 0,
                    "resolver_option_names": "edns0",
                    "network_interface_count": 2,
                    "vpn_like_interface_present": False,
                    "vpn_like_interface_count": 0,
                    "network_interface_classes": "loopback:1,wired:1",
                    "default_route_present": True,
                    "default_route_interface_class": "wired",
                    "default_source_address_class": "ipv4_private",
                },
            ),
        ):
            result = await run_sync_network_gate(
                ["rbc", "td"],
                user_id=None,
                flow="batch",
                mode="auto",
                retry_delays_seconds=(0.0, 0.0),
            )

        self.assertEqual("blocked", result.status)
        self.assertEqual(3, result.attempts)
        self.assertEqual(
            "dns_resolution_temporarily_unavailable",
            result.blocker_payload()["code"],
        )

    async def test_sync_gate_is_inconclusive_on_permanent_provider_failure(self) -> None:
        temporary = DnsResolutionResult(
            provider="rbc",
            host="www1.royalbank.com",
            status="temporary_failure",
            diagnosis="temporary_dns_lookup_failure_from_backend_runtime",
            duration_ms=1,
        )
        permanent = DnsResolutionResult(
            provider="td",
            host="easyweb.td.com",
            status="failed",
            diagnosis="dns_lookup_failed_from_backend_runtime",
            duration_ms=1,
        )
        with patch(
            "app.services.network_preflight._run_dns_resolution",
            new=AsyncMock(side_effect=[temporary, permanent]),
        ):
            result = await run_sync_network_gate(
                ["rbc", "td"],
                user_id=None,
                flow="batch",
                mode="manual",
                retry_delays_seconds=(0.0,),
            )

        self.assertEqual("inconclusive", result.status)
        self.assertEqual(1, result.attempts)

    async def test_sanitized_resolver_facts_never_return_raw_network_values(self) -> None:
        resolver_text = (
            "nameserver 10.44.55.66\n"
            "search secret.corporate.example\n"
            "options edns0 timeout:2 trust-ad\n"
        )
        with (
            patch.object(
                Path,
                "resolve",
                return_value=Path("/run/systemd/resolve/stub-resolv.conf"),
            ),
            patch.object(Path, "read_text", return_value=resolver_text),
        ):
            resolver_facts = _resolver_config_facts()
        with patch(
            "app.services.network_preflight.socket.if_nameindex",
            return_value=[
                (1, "lo"),
                (2, "employee-name-wg0"),
                (3, "company-secret-interface"),
            ],
        ):
            interface_facts = _network_interface_facts()
        with patch.dict(
            "app.services.network_preflight.os.environ",
            {
                "HTTP_PROXY": "http://user:password@private.proxy.example:8080",
                "BREAKTWENTY_BACKEND_MODE": "embedded",
            },
            clear=True,
        ):
            with patch(
                "app.services.network_preflight._resolver_config_facts",
                return_value=resolver_facts,
            ), patch(
                "app.services.network_preflight._network_interface_facts",
                return_value=interface_facts,
            ), patch(
                "app.services.network_preflight._default_route_facts",
                return_value={
                    "default_route_present": True,
                    "default_route_interface_class": "vpn_like",
                    "default_source_address_class": "ipv4_private",
                },
            ):
                facts = collect_sanitized_resolver_facts()

        serialized = str(facts)
        self.assertNotIn("10.44.55.66", serialized)
        self.assertNotIn("secret.corporate.example", serialized)
        self.assertNotIn("employee-name-wg0", serialized)
        self.assertNotIn("company-secret-interface", serialized)
        self.assertNotIn("private.proxy.example", serialized)
        self.assertNotIn("password", serialized)
        self.assertTrue(facts["proxy_http_present"])
        self.assertTrue(facts["vpn_like_interface_present"])
        self.assertEqual("ipv4_private", facts["resolver_nameserver_classes"])

    async def test_blocked_sync_gate_creates_sanitized_network_support_archive(self) -> None:
        raw_address = "10.77.88.99"
        raw_error = "resolver secret user:password@private.example"
        temporary_results = [
            DnsResolutionResult(
                provider=provider,
                host="private.internal.example",
                status="temporary_failure",
                diagnosis="temporary_dns_lookup_failure_from_backend_runtime",
                duration_ms=3,
                resolved_addresses=(raw_address,),
                error=raw_error,
                error_code=socket.EAI_AGAIN,
                error_name="EAI_AGAIN",
            )
            for _ in range(2)
            for provider in ("rbc", "td")
        ]
        resolver_facts = {
            "runtime_mode": "docker",
            "os_family": "linux",
            "os_release_major": "6",
            "runtime_arch": "x86_64",
            "python_version": "3.12",
            "proxy_http_present": True,
            "proxy_https_present": False,
            "proxy_all_present": False,
            "proxy_bypass_present": True,
            "resolver_source": "systemd_resolved",
            "resolver_nameserver_count": 2,
            "resolver_nameserver_classes": "ipv4_private,ipv6_public",
            "resolver_search_domain_count": 1,
            "resolver_option_names": "edns0,trust-ad",
            "network_interface_count": 4,
            "vpn_like_interface_present": True,
            "vpn_like_interface_count": 1,
            "network_interface_classes": "loopback:1,vpn_like:1,wireless:1",
            "default_route_present": True,
            "default_route_interface_class": "wireless",
            "default_source_address_class": "ipv4_private",
        }
        archive = AsyncMock(return_value=Path("/tmp/network-run"))
        with (
            patch(
                "app.services.network_preflight._run_dns_resolution",
                new=AsyncMock(side_effect=temporary_results),
            ),
            patch(
                "app.services.network_preflight.collect_sanitized_resolver_facts",
                return_value=resolver_facts,
            ),
            patch(
                "app.services.support_auto_archive.archive_run",
                new=archive,
            ),
        ):
            result = await run_sync_network_gate(
                ["rbc", "td"],
                user_id=42,
                flow="batch",
                mode="manual",
                batch_id="syncbatch-safe123",
                retry_delays_seconds=(0.0,),
            )

        self.assertEqual("blocked", result.status)
        archive.assert_awaited_once()
        archive_kwargs = archive.await_args.kwargs
        self.assertEqual(42, archive_kwargs["user_id"])
        self.assertEqual("network", archive_kwargs["provider"])
        self.assertEqual("sync_dns_gate_blocked", archive_kwargs["trigger"])
        snapshot = archive_kwargs["extra_fields"]["network_preflight"]
        self.assertEqual(2, snapshot["attempt_count"])
        self.assertEqual(2, len(snapshot["attempts"]))
        self.assertEqual(resolver_facts, snapshot["resolver_facts"])
        serialized = str(snapshot)
        self.assertNotIn(raw_address, serialized)
        self.assertNotIn(raw_error, serialized)
        self.assertNotIn("private.internal.example", serialized)
        self.assertEqual(
            "unknown",
            snapshot["attempts"][0]["probes"][0]["public_host"],
        )


if __name__ == "__main__":
    unittest.main()
