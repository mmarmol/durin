"""The skill_acquire_seed tool gates ONE ref via acquire_safe_seed."""
import asyncio

from durin.agent.tools.skill_acquire_seed import SkillAcquireSeedTool


def test_tool_name(tmp_path):
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=[])
    assert tool.name == "skill_acquire_seed"


def test_execute_returns_seed(monkeypatch, tmp_path):
    async def _fake(workspace, source, *, allowlist):
        return {"name": "pdf", "source": source, "content": "body"}

    monkeypatch.setattr("durin.agent.skill_acquire.acquire_safe_seed", _fake)
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=["github:acme"])
    out = asyncio.run(tool.execute(source="github:acme/pdf"))
    assert out["seed"]["source"] == "github:acme/pdf"


def test_execute_no_seed(monkeypatch, tmp_path):
    async def _none(workspace, source, *, allowlist):
        return None

    monkeypatch.setattr("durin.agent.skill_acquire.acquire_safe_seed", _none)
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=[])
    out = asyncio.run(tool.execute(source="github:acme/pdf"))
    assert out["seed"] is None
    assert "note" in out


def test_execute_missing_source(tmp_path):
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=["github:acme"])
    out = asyncio.run(tool.execute(source=""))
    assert out["seed"] is None


def test_acquire_seed_excluded_from_core_autoload():
    # Path A uses raw tools; the gated seed tool must not auto-load into the main loop.
    assert "core" not in getattr(SkillAcquireSeedTool, "_scopes", {"core"})
