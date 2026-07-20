import asyncio
import json

from durin.agent.tools.skill_discard import SkillDiscardTool
from durin.agent.tools.skill_publish import SkillPublishTool

BODY = "---\nname: emailer\ndescription: parse email. use when a .eml needs reading.\n---\nbody\n"


def _draft(ws, name, body):
    d = ws / "skill-drafts" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def test_publish_tool_activates_draft(tmp_path):
    _draft(tmp_path, "emailer", BODY)
    out = json.loads(asyncio.run(SkillPublishTool(workspace=tmp_path).execute(name="emailer")))
    assert out.get("ok"), out
    assert (tmp_path / "skills" / "emailer" / "SKILL.md").exists()


def test_discard_tool_removes_draft(tmp_path):
    _draft(tmp_path, "emailer", BODY)
    out = json.loads(asyncio.run(SkillDiscardTool(workspace=tmp_path).execute(name="emailer")))
    assert out.get("ok") is True
    assert not (tmp_path / "skill-drafts" / "emailer").exists()


def test_tools_are_discoverable():
    import durin.agent.tools as tools_pkg
    from durin.agent.tools.loader import ToolLoader
    discovered = {c.__name__ for c in ToolLoader(tools_pkg).discover()}
    assert {"SkillPublishTool", "SkillDiscardTool"} <= discovered
