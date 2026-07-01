import asyncio

import pytest

from durin.channels.qq import _SSRFGuardResolver


def test_resolver_rejects_private_host():
    resolver = _SSRFGuardResolver()
    with pytest.raises(OSError):
        asyncio.run(resolver.resolve("127.0.0.1"))


def test_resolver_offloads_via_to_thread(monkeypatch):
    resolver = _SSRFGuardResolver()
    seen = {"thread": False}
    real_to_thread = asyncio.to_thread

    async def spy(func, *args, **kwargs):
        seen["thread"] = True
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("durin.channels.qq.asyncio.to_thread", spy)
    res = asyncio.run(resolver.resolve("8.8.8.8"))
    assert seen["thread"] is True
    assert res[0]["host"] == "8.8.8.8"
