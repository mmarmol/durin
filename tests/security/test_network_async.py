import asyncio

import httpx
import pytest

from durin.security import network


def test_validate_url_target_async_matches_sync_for_private():
    url = "http://127.0.0.1/"
    assert asyncio.run(network.validate_url_target_async(url)) == network.validate_url_target(url)


def test_validate_url_target_async_blocks_private_literal():
    ok, msg = asyncio.run(network.validate_url_target_async("http://127.0.0.1/"))
    assert ok is False
    assert "private" in msg.lower() or "internal" in msg.lower()


def test_validate_url_target_async_allows_public_literal():
    ok, msg = asyncio.run(network.validate_url_target_async("http://8.8.8.8/"))
    assert ok is True
    assert msg == ""


def test_validate_resolved_url_async_blocks_private_literal():
    ok, msg = asyncio.run(network.validate_resolved_url_async("http://10.0.0.1/"))
    assert ok is False


def test_transport_blocks_private_host_via_offloaded_resolve():
    transport = network.SSRFGuardTransport()
    req = httpx.Request("GET", "http://127.0.0.1/")
    with pytest.raises(network.SSRFError):
        asyncio.run(transport.handle_async_request(req))
