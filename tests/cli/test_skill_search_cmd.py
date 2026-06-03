from typer.testing import CliRunner

from durin.agent.skill_registry import SkillSearchHit
from durin.cli.skill_cmd import skill_app


def test_skill_search_cmd_renders_hits(monkeypatch):
    async def fake(query, *, adapters, allowlist, limit):
        return [SkillSearchHit(name="pdfkit", ref="github:o/r/pdfkit",
                               registry="skills.sh", signals={"installs": 5692})]
    monkeypatch.setattr("durin.agent.skill_registry.search_registries", fake)
    res = CliRunner().invoke(skill_app, ["search", "pdf"])
    assert res.exit_code == 0, res.output
    assert "pdfkit" in res.output
    assert "skills.sh" in res.output


def test_skill_search_cmd_no_hits(monkeypatch):
    async def fake(query, *, adapters, allowlist, limit):
        return []
    monkeypatch.setattr("durin.agent.skill_registry.search_registries", fake)
    res = CliRunner().invoke(skill_app, ["search", "nope"])
    assert res.exit_code == 0, res.output
