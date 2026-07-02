from durin.agent.skills_store import Attribution, attribution_to_trailers


def test_attribution_to_trailers_emits_present_fields_only():
    assert attribution_to_trailers(None) == {}
    assert attribution_to_trailers(Attribution(actor="user")) == {"Actor": "user"}
    assert attribution_to_trailers(
        Attribution(actor="agent", session="s1", agent="claude-opus-4-8")
    ) == {"Actor": "agent", "Session": "s1", "Agent": "claude-opus-4-8"}


def test_attribution_drops_empty_strings():
    assert attribution_to_trailers(Attribution(actor="agent", session="", agent=None)) == {"Actor": "agent"}


from pathlib import Path

from durin.agent.skills_store import (
    _store,
    apply_skill_edit,
    dream_create_skill,
)


def _top_msg(ws: Path) -> str:
    return _store(ws).log(max_entries=1)[0].message


def test_dream_create_stamps_attribution(tmp_path: Path):
    dream_create_skill(tmp_path, "made", "---\nname: made\n---\nbody\n", "because",
                       attribution=Attribution(actor="agent", session="s1", agent="m1"))
    msg = _top_msg(tmp_path)
    assert "Actor: agent" in msg and "Session: s1" in msg and "Agent: m1" in msg


def test_apply_skill_edit_stamps_attribution(tmp_path: Path):
    dream_create_skill(tmp_path, "made", "---\nname: made\n---\nbody\n", "init")
    apply_skill_edit(tmp_path, "made", old="\nbody\n", new="\nbody2\n", rationale="improve",
                     attribution=Attribution(actor="agent", session="s2"))
    msg = _top_msg(tmp_path)
    assert "skill(made): improve" in msg and "Actor: agent" in msg and "Session: s2" in msg


def test_attribution_none_yields_bare_commit(tmp_path: Path):
    dream_create_skill(tmp_path, "made", "---\nname: made\n---\nbody\n", "init")
    msg = _top_msg(tmp_path)
    assert "Actor:" not in msg  # unchanged from today


import asyncio

from durin.agent.tools.context import RequestContext
from durin.agent.tools.skill_write import SkillWriteTool


def test_skill_write_tool_stamps_session_and_model(tmp_path: Path):
    tool = SkillWriteTool(workspace=tmp_path)
    tool.set_context(RequestContext(channel="web", chat_id="c1", session_key="sess-xyz",
                                    metadata={"model": "claude-opus-4-8"}))
    asyncio.run(tool.execute(name="made", content="---\nname: made\n---\nbody\n", rationale="why"))
    msg = _top_msg(tmp_path)
    assert "Actor: agent" in msg and "Session: sess-xyz" in msg and "Agent: claude-opus-4-8" in msg
