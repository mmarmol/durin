from types import SimpleNamespace

from durin.agent.skill_registry import SkillSearchHit, SkillsShRegistry, build_adapters
from durin.agent.tools.skill_search import SkillSearchTool


async def test_skill_search_returns_hits(monkeypatch):
    async def fake_search(query, *, adapters, allowlist, limit):
        return [SkillSearchHit(name="pdf", ref="github:o/r/pdf", registry="skills.sh",
                               description="d", signals={"installs": 9})]
    monkeypatch.setattr("durin.agent.skill_registry.search_registries", fake_search)
    tool = SkillSearchTool(workspace="/tmp",
                           registries=[SimpleNamespace(kind="skills.sh", enabled=True)],
                           allowlist=[])
    out = await tool.execute(query="pdf")
    assert out["hits"][0]["ref"] == "github:o/r/pdf"
    assert out["hits"][0]["registry"] == "skills.sh"
    assert "skill_import" in out["note"]


async def test_skill_search_requires_query():
    tool = SkillSearchTool(workspace="/tmp", registries=[], allowlist=[])
    out = await tool.execute(query="")
    assert "error" in out


def test_build_adapters_only_enabled_skillssh():
    regs = [
        SimpleNamespace(kind="skills.sh", enabled=True),
        SimpleNamespace(kind="skills.sh", enabled=False),
        SimpleNamespace(kind="clawhub", enabled=True),
    ]
    adapters = build_adapters(regs)
    assert len(adapters) == 1
    assert isinstance(adapters[0], SkillsShRegistry)
