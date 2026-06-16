from durin.agent.skill_registry import SkillsShRegistry


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


async def test_skillssh_maps_items_to_github_refs(monkeypatch):
    payload = {"skills": [
        {"id": "openai/skills/pdf", "source": "openai/skills", "skillId": "pdf",
         "name": "pdf", "installs": 1200}]}
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, payload)))
    hits = await SkillsShRegistry().search("pdf", limit=5)
    assert len(hits) == 1
    assert hits[0].ref == "github:openai/skills/pdf"
    assert hits[0].registry == "skills.sh"
    assert hits[0].name == "pdf"
    assert hits[0].signals == {"installs": 1200}


async def test_skillssh_non_200_returns_empty(monkeypatch):
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(500, {})))
    assert await SkillsShRegistry().search("x", limit=5) == []
