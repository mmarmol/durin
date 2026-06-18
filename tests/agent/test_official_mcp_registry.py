"""Task 4 — OfficialMcpRegistry HTTP adapter (injected fake http, no network)."""
import pytest

from durin.agent.mcp_registry import OfficialMcpRegistry


class _FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    async def get_json(self, url):
        self.calls.append(url)
        return self.payload


_PAGE = {
    "servers": [
        {"server": {"name": "io.github.acme/jira", "description": "Jira",
                    "packages": [{"transport": {"type": "stdio"}}]}, "_meta": {}},
    ],
    "metadata": {"count": 1, "nextCursor": None},
}


@pytest.mark.asyncio
async def test_search_returns_hits():
    reg = OfficialMcpRegistry(http=_FakeHTTP(_PAGE))
    hits = await reg.search("jira", limit=10)
    assert hits[0].ref == "io.github.acme/jira"
    assert hits[0].registry == "official"
    assert "search=jira" in reg._http.calls[0]


@pytest.mark.asyncio
async def test_describe_parses_latest_version():
    payload = {"server": {"name": "io.x/jira", "version": "2.0.0",
                          "packages": [{"transport": {"type": "stdio"}, "runtimeHint": "npx",
                                        "identifier": "@x/jira", "version": "2.0.0"}]}}
    reg = OfficialMcpRegistry(http=_FakeHTTP(payload))
    detail = await reg.describe("io.x/jira")
    assert detail is not None
    assert detail.version == "2.0.0"
    assert detail.packages[0].runtime_hint == "npx"
    assert "io.x%2Fjira" in reg._http.calls[0] or "io.x/jira" in reg._http.calls[0]


@pytest.mark.asyncio
async def test_fetch_page_returns_servers_and_cursor():
    page = {"servers": [{"server": {"name": "io.x/a"}}], "metadata": {"nextCursor": "c2"}}
    reg = OfficialMcpRegistry(http=_FakeHTTP(page))
    servers, cursor = await reg.fetch_page()
    assert servers[0]["name"] == "io.x/a"
    assert cursor == "c2"
