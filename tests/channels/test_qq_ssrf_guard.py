"""A2 (channels): the QQ media-download path fetches user-supplied URLs via
aiohttp, so it gets the same resolve-and-pin SSRF guard as web_fetch — here
via a custom aiohttp resolver. These exercise the resolver in isolation
(no botpy needed)."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from durin.channels.qq import _SSRFGuardResolver  # noqa: E402


def _resolve_to(ip: str):
    def _r(host, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
    return _r


@pytest.mark.asyncio
async def test_qq_resolver_pins_public_ip_and_keeps_hostname():
    with patch("durin.security.network.socket.getaddrinfo", _resolve_to("93.184.216.34")):
        res = await _SSRFGuardResolver().resolve("example.com", 443)
    assert res[0]["host"] == "93.184.216.34"     # connect target = validated IP
    assert res[0]["hostname"] == "example.com"   # Host header + TLS SNI keep the hostname


@pytest.mark.asyncio
async def test_qq_resolver_blocks_private_target():
    with patch("durin.security.network.socket.getaddrinfo", _resolve_to("169.254.169.254")):
        with pytest.raises(OSError):
            await _SSRFGuardResolver().resolve("rebind.example", 80)
