import asyncio
import json

from durin.agent.tools.web import WebFetchTool


def test_fetch_one_blocks_private_target_without_network():
    tool = WebFetchTool()
    out = asyncio.run(tool._fetch_one("http://127.0.0.1/secret"))
    parsed = json.loads(out)
    assert "validation failed" in parsed["error"].lower()
    assert parsed["url"] == "http://127.0.0.1/secret"


def test_fetch_one_uses_async_validator(monkeypatch):
    tool = WebFetchTool()
    called = {"async": False}

    async def fake_async(url):
        called["async"] = True
        return False, "blocked for test"

    monkeypatch.setattr("durin.agent.tools.web.validate_url_target_async", fake_async)
    out = asyncio.run(tool._fetch_one("http://example.com/"))
    assert called["async"] is True
    assert "blocked for test" in out
