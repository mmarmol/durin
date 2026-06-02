"""Tests for durin.security.network — SSRF protection and internal URL detection."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from durin.security.network import (
    configure_ssrf_whitelist,
    contains_internal_url,
    validate_url_target,
)


def _fake_resolve(host: str, results: list[str]):
    """Return a getaddrinfo mock that maps the given host to fake IP results."""
    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")
    return _resolver


# ---------------------------------------------------------------------------
# validate_url_target — scheme / domain basics
# ---------------------------------------------------------------------------

def test_rejects_non_http_scheme():
    ok, err = validate_url_target("ftp://example.com/file")
    assert not ok
    assert "http" in err.lower()


def test_rejects_missing_domain():
    ok, err = validate_url_target("http://")
    assert not ok


# ---------------------------------------------------------------------------
# validate_url_target — blocked private/internal IPs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ip,label", [
    ("127.0.0.1", "loopback"),
    ("127.0.0.2", "loopback_alt"),
    ("10.0.0.1", "rfc1918_10"),
    ("172.16.5.1", "rfc1918_172"),
    ("192.168.1.1", "rfc1918_192"),
    ("169.254.169.254", "metadata"),
    ("0.0.0.0", "zero"),
])
def test_blocks_private_ipv4(ip: str, label: str):
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("evil.com", [ip])):
        ok, err = validate_url_target("http://evil.com/path")
        assert not ok, f"Should block {label} ({ip})"
        assert "private" in err.lower() or "blocked" in err.lower()


def test_blocks_ipv6_loopback():
    def _resolver(hostname, port, family=0, type_=0):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))]
    with patch("durin.security.network.socket.getaddrinfo", _resolver):
        ok, err = validate_url_target("http://evil.com/")
        assert not ok


# ---------------------------------------------------------------------------
# validate_url_target — allows public IPs
# ---------------------------------------------------------------------------

def test_allows_public_ip():
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("example.com", ["93.184.216.34"])):
        ok, err = validate_url_target("http://example.com/page")
        assert ok, f"Should allow public IP, got: {err}"


def test_allows_normal_https():
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("github.com", ["140.82.121.3"])):
        ok, err = validate_url_target("https://github.com/HKUDS/durin")
        assert ok


# ---------------------------------------------------------------------------
# contains_internal_url — shell command scanning
# ---------------------------------------------------------------------------

def test_detects_curl_metadata():
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("169.254.169.254", ["169.254.169.254"])):
        assert contains_internal_url('curl -s http://169.254.169.254/computeMetadata/v1/')


def test_detects_wget_localhost():
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])):
        assert contains_internal_url("wget http://localhost:8080/secret")


def test_allows_normal_curl():
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("example.com", ["93.184.216.34"])):
        assert not contains_internal_url("curl https://example.com/api/data")


def test_no_urls_returns_false():
    assert not contains_internal_url("echo hello && ls -la")


# ---------------------------------------------------------------------------
# SSRF whitelist — allow specific CIDR ranges (#2669)
# ---------------------------------------------------------------------------

def test_blocks_cgnat_by_default():
    """100.64.0.0/10 (CGNAT / Tailscale) is blocked by default."""
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, _ = validate_url_target("http://ts.local/api")
        assert not ok


def test_whitelist_allows_cgnat():
    """Whitelisting 100.64.0.0/10 lets Tailscale addresses through."""
    configure_ssrf_whitelist(["100.64.0.0/10"])
    try:
        with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
            ok, err = validate_url_target("http://ts.local/api")
            assert ok, f"Whitelisted CGNAT should be allowed, got: {err}"
    finally:
        configure_ssrf_whitelist([])


def test_whitelist_does_not_affect_other_blocked():
    """Whitelisting CGNAT must not unblock other private ranges."""
    configure_ssrf_whitelist(["100.64.0.0/10"])
    try:
        with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("evil.com", ["10.0.0.1"])):
            ok, _ = validate_url_target("http://evil.com/secret")
            assert not ok
    finally:
        configure_ssrf_whitelist([])


def test_whitelist_invalid_cidr_ignored():
    """Invalid CIDR entries are silently skipped."""
    configure_ssrf_whitelist(["not-a-cidr", "100.64.0.0/10"])
    try:
        with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
            ok, _ = validate_url_target("http://ts.local/api")
            assert ok
    finally:
        configure_ssrf_whitelist([])


# ---------------------------------------------------------------------------
# A2: resolve-and-pin SSRF guard (closes the validate-then-refetch TOCTOU)
# ---------------------------------------------------------------------------

import httpx  # noqa: E402

from durin.security.network import (  # noqa: E402
    SSRFError,
    SSRFGuardTransport,
    resolve_and_validate,
)


class TestResolveAndValidate:
    def test_returns_ip_for_public_host(self):
        with patch("socket.getaddrinfo", _fake_resolve("example.com", ["93.184.216.34"])):
            assert resolve_and_validate("example.com") == "93.184.216.34"

    def test_blocks_host_resolving_to_private(self):
        with patch("socket.getaddrinfo", _fake_resolve("evil.com", ["127.0.0.1"])):
            with pytest.raises(SSRFError):
                resolve_and_validate("evil.com")

    def test_accepts_public_ip_literal(self):
        assert resolve_and_validate("93.184.216.34") == "93.184.216.34"

    def test_blocks_private_ip_literal(self):
        with pytest.raises(SSRFError):
            resolve_and_validate("169.254.169.254")


@pytest.mark.asyncio
class TestSSRFGuardTransport:
    async def test_pins_public_host_to_ip_and_preserves_host_and_sni(self):
        captured: dict = {}

        async def fake_super(self, request):
            captured["url"] = request.url
            captured["host_header"] = request.headers.get("Host")
            captured["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200)

        with patch("httpx.AsyncHTTPTransport.handle_async_request", fake_super), \
             patch("socket.getaddrinfo", _fake_resolve("example.com", ["93.184.216.34"])):
            transport = SSRFGuardTransport()
            request = httpx.Request("GET", "https://example.com/path?q=1")
            await transport.handle_async_request(request)

        assert captured["url"].host == "93.184.216.34"   # pinned to the validated IP
        assert captured["url"].path == "/path"           # path/query preserved
        assert captured["host_header"] == "example.com"  # Host header kept
        assert captured["sni"] == "example.com"          # TLS verifies against the hostname

    async def test_blocks_private_target_without_connecting(self):
        connected = False

        async def fake_super(self, request):
            nonlocal connected
            connected = True
            return httpx.Response(200)

        with patch("httpx.AsyncHTTPTransport.handle_async_request", fake_super), \
             patch("socket.getaddrinfo", _fake_resolve("rebind.com", ["169.254.169.254"])):
            transport = SSRFGuardTransport()
            request = httpx.Request("GET", "https://rebind.com/latest/meta-data/")
            with pytest.raises(SSRFError):
                await transport.handle_async_request(request)

        assert not connected, "guard must block before any connection to the private target"
