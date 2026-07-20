"""Path-guard: generic write tools refuse writes under the skills/ registry."""

import pytest

from durin.agent.tools.filesystem import EditFileTool, WriteFileTool
from durin.agent.tools.path_utils import resolve_workspace_path


def test_resolve_denies_skills_subdir(tmp_path):
    ws = tmp_path
    (ws / "skills").mkdir()
    with pytest.raises(PermissionError):
        resolve_workspace_path("skills/x/SKILL.md", ws, allowed_dir=ws,
                               denied_subdirs=[ws / "skills"])


def test_resolve_allows_drafts(tmp_path):
    ws = tmp_path
    out = resolve_workspace_path("skill-drafts/x/SKILL.md", ws, allowed_dir=ws,
                                 denied_subdirs=[ws / "skills"])
    assert out == (ws / "skill-drafts" / "x" / "SKILL.md").resolve()


@pytest.mark.asyncio
async def test_write_tool_refuses_skills(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "skills").mkdir()
    tool = WriteFileTool(workspace=ws, allowed_dir=ws)
    result = await tool.execute(path="skills/evil/SKILL.md", content="x")
    assert "Error" in result and "skill-drafts" in result  # redirected to the draft flow


@pytest.mark.asyncio
async def test_write_tool_allows_drafts(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteFileTool(workspace=ws, allowed_dir=ws)
    result = await tool.execute(path="skill-drafts/emailer/SKILL.md", content="hi")
    assert "Successfully wrote" in result


@pytest.mark.asyncio
async def test_edit_tool_refuses_skills(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    skill_file = ws / "skills" / "evil" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("original", encoding="utf-8")
    tool = EditFileTool(workspace=ws, allowed_dir=ws)
    result = await tool.execute(path="skills/evil/SKILL.md", old_text="original", new_text="hacked")
    assert "Error" in result and "skill-drafts" in result
    assert skill_file.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_edit_tool_allows_drafts(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    draft_file = ws / "skill-drafts" / "emailer" / "SKILL.md"
    draft_file.parent.mkdir(parents=True)
    draft_file.write_text("draft", encoding="utf-8")
    tool = EditFileTool(workspace=ws, allowed_dir=ws)
    result = await tool.execute(
        path="skill-drafts/emailer/SKILL.md", old_text="draft", new_text="updated draft",
    )
    assert "Successfully edited" in result
    assert draft_file.read_text(encoding="utf-8") == "updated draft"


@pytest.mark.asyncio
async def test_write_tool_guard_skills_dir_false_allows_isolated_staging_write(tmp_path):
    """Internal callers whose `workspace` is an isolated, non-live copy (e.g.
    skill_restructure's throwaway staging dir, which mirrors the live layout as
    staging/skills/<name>/ purely so path references line up) opt out via
    guard_skills_dir=False — the guard exists to protect the *live* registry,
    not every directory that happens to be named "skills"."""
    staging = tmp_path / "staging"
    (staging / "skills").mkdir(parents=True)
    tool = WriteFileTool(workspace=staging, allowed_dir=staging, guard_skills_dir=False)
    result = await tool.execute(path="skills/qr/scripts/decode.py", content="x")
    assert "Successfully wrote" in result
