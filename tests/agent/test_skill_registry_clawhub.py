from durin.agent.skill_registry import ClawHubRegistry, build_adapters


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
    def json(self):
        return self._p
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, resp, calls):
        self._resp, self._calls = resp, calls
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, **kw):
        self._calls.append((url, kw))
        return self._resp


async def test_clawhub_searches_search_endpoint_with_q(monkeypatch):
    # ClawHub's `/skills` endpoint is a recency LIST that ignores unknown params;
    # the actual ranked search lives at `/search?q=`. Pin the contract so we can
    # never regress back to the no-op list endpoint.
    calls = []
    payload = {"results": [{"slug": "git", "displayName": "Git",
                            "summary": "version control", "downloads": 15658,
                            "score": 4.14}]}
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, payload), calls))
    hits = await ClawHubRegistry().search("git", limit=5)
    url, kw = calls[0]
    assert url.endswith("/search")
    assert kw["params"]["q"] == "git"
    assert "search" not in kw["params"]
    assert len(hits) == 1
    assert hits[0].ref == "clawhub:git"
    assert hits[0].registry == "clawhub"
    assert hits[0].name == "Git"
    assert hits[0].description == "version control"
    # clawhub's `downloads` is its acquisition-count signal — surfaced as
    # `installs` so the search UI can display + sort it alongside skills.sh.
    assert hits[0].signals.get("installs") == 15658


async def test_clawhub_parses_results_key(monkeypatch):
    calls = []
    payload = {"results": [{"slug": "web-scraper", "displayName": "Web Scraper",
                            "summary": "scrape sites"}]}
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, payload), calls))
    hits = await ClawHubRegistry().search("scrape", limit=5)
    assert len(hits) == 1
    assert hits[0].ref == "clawhub:web-scraper"
    assert hits[0].name == "Web Scraper"
    assert hits[0].signals == {}  # no downloads → no installs signal


async def test_clawhub_bare_list_and_non_200(monkeypatch):
    calls = []
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, [{"slug": "a", "name": "A"}]), calls))
    hits = await ClawHubRegistry().search("a", limit=5)
    assert hits[0].ref == "clawhub:a"
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(500, {}), calls))
    assert await ClawHubRegistry().search("x", limit=5) == []


def test_build_adapters_includes_clawhub():
    from types import SimpleNamespace
    adapters = build_adapters([SimpleNamespace(kind="clawhub", enabled=True)])
    assert len(adapters) == 1
    assert isinstance(adapters[0], ClawHubRegistry)
