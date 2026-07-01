"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import suppress
from urllib.parse import urlparse

import httpx

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)

_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Normalize IPv4-mapped IPv6 (::ffff:127.0.0.1 etc.) to the embedded IPv4 so
    # the v4 blocklist catches it — otherwise it's an SSRF bypass to localhost,
    # RFC-1918, and cloud metadata (::ffff:169.254.169.254).
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if _allowed_networks and any(addr in net for net in _allowed_networks):
        return False
    return any(addr in net for net in _BLOCKED_NETWORKS)


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Returns (ok, error_message).  When ok is True, error_message is empty.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_private(addr):
            return False, f"Blocked: {hostname} resolves to private/internal address {addr}"

    return True, ""


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


async def validate_url_target_async(url: str) -> tuple[bool, str]:
    """Async wrapper: run the blocking DNS resolution of validate_url_target on a
    worker thread so it never stalls the event loop."""
    return await asyncio.to_thread(validate_url_target, url)


async def validate_resolved_url_async(url: str) -> tuple[bool, str]:
    """Async wrapper: run the blocking DNS resolution of validate_resolved_url on a
    worker thread so it never stalls the event loop."""
    return await asyncio.to_thread(validate_resolved_url, url)


class SSRFError(Exception):
    """A fetch target resolved to a private/internal address, or was
    unresolvable — raised by the connect-time SSRF guard."""


def resolve_and_validate(host: str) -> str:
    """Resolve *host* to a public IP, rejecting private/internal targets.

    Returns the validated IP string to pin the connection to. Raises
    :class:`SSRFError` when the host is unresolvable or resolves to (or is)
    a private/internal address. Pinning the connection to this exact IP is
    what closes the validate-then-refetch DNS-rebinding TOCTOU: validation
    and the connection use the *same* resolution.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_private(literal):
            raise SSRFError(f"blocked private/internal address: {host}")
        return str(literal)

    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"cannot resolve hostname: {host}") from e

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addrs:
        raise SSRFError(f"no usable address for {host}")
    # Match validate_url_target's posture: any private address fails the host.
    for addr in addrs:
        if _is_private(addr):
            raise SSRFError(f"{host} resolves to private/internal address {addr}")
    return str(addrs[0])


class SSRFGuardTransport(httpx.AsyncHTTPTransport):
    """httpx transport that resolves + validates the target host once and
    pins the connection to that IP, preserving the ``Host`` header and TLS
    SNI (so certificate verification still matches the hostname).

    This closes the validate-then-refetch DNS-rebinding TOCTOU (A2): the
    validation and the actual connection use the *same* resolution. Because
    httpx routes every request — including each redirect hop — through the
    transport, redirects are re-validated and re-pinned automatically, so
    callers don't need a manual per-hop check.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host:
            ip = await asyncio.to_thread(resolve_and_validate, host)  # raises SSRFError; DNS off-loop
            if ip != host:
                # Pin to the validated IP; keep Host (already set on the
                # request) and pin TLS SNI to the original hostname.
                request.url = request.url.copy_with(host=ip)
                request.extensions["sni_hostname"] = host
        return await super().handle_async_request(request)


def ssrf_safe_async_client(*, proxy: str | None = None, **kwargs) -> httpx.AsyncClient:
    """Build an ``httpx.AsyncClient`` that pins each connection to a
    validated IP (SSRF guard) when no proxy is configured.

    With a proxy, egress (and thus SSRF policy) is the proxy's job, so the
    guard transport is skipped and the proxy is used as before.
    """
    if proxy:
        return httpx.AsyncClient(proxy=proxy, **kwargs)
    kwargs.setdefault("transport", SSRFGuardTransport())
    return httpx.AsyncClient(**kwargs)


def contains_internal_url(command: str) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address."""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url)
        if not ok:
            return True
    return False
