from durin.agent import skills_store as ss
from durin.agent.skill_registry import SkillSearchHit


def test_web_skill_search_returns_hits(monkeypatch, tmp_path):
    async def fake(query, *, adapters, allowlist, limit):
        return [SkillSearchHit(name="pdf", ref="github:o/r/pdf", registry="skills.sh",
                               description="d", signals={"installs": 9})]
    monkeypatch.setattr("durin.agent.skill_registry.search_registries", fake)
    status, payload = ss.web_skill_search(tmp_path, "pdf", 0)
    assert status == 200
    assert payload["hits"][0]["ref"] == "github:o/r/pdf"
    assert payload["hits"][0]["registry"] == "skills.sh"


def test_web_skill_search_empty_query(tmp_path):
    status, payload = ss.web_skill_search(tmp_path, "", 0)
    assert status == 400 or payload.get("hits") == []
