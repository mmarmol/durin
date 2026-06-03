from durin.agent.skill_registry import ClawHubRegistry, SkillSearchHit, build_adapters


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
    def json(self):
        return self._p
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, **kw):
        return self._resp


async def test_clawhub_maps_items(monkeypatch):
    payload = {"items": [{"slug": "web-scraper", "displayName": "Web Scraper",
                          "summary": "scrape sites"}]}
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, payload)))
    hits = await ClawHubRegistry().search("scrape", limit=5)
    assert len(hits) == 1
    assert hits[0].ref == "clawhub:web-scraper"
    assert hits[0].registry == "clawhub"
    assert hits[0].name == "Web Scraper"


async def test_clawhub_bare_list_and_non_200(monkeypatch):
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, [{"slug": "a", "name": "A"}])))
    hits = await ClawHubRegistry().search("a", limit=5)
    assert hits[0].ref == "clawhub:a"
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(500, {})))
    assert await ClawHubRegistry().search("x", limit=5) == []


def test_build_adapters_includes_clawhub():
    from types import SimpleNamespace
    adapters = build_adapters([SimpleNamespace(kind="clawhub", enabled=True)])
    assert len(adapters) == 1
    assert isinstance(adapters[0], ClawHubRegistry)
