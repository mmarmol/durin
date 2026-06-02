import asyncio
from pathlib import Path
from types import SimpleNamespace

from durin.command.builtin import cmd_skills
from durin.command.router import CommandContext


def _ctx(workspace: Path, args: str) -> CommandContext:
    msg = SimpleNamespace(channel="test", chat_id="c1", metadata={})
    loop = SimpleNamespace(workspace=workspace)
    return CommandContext(msg=msg, session=None, key="/skills", raw=f"/skills {args}",
                          args=args, loop=loop)


def _user_skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nBody\n", encoding="utf-8"
    )


def test_skills_list_shows_skills_with_mode(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    out = asyncio.run(cmd_skills(_ctx(ws, "list")))
    assert "mine" in out.content
    assert "manual" in out.content.lower()


def test_skills_mode_sets_and_reports(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine")
    out = asyncio.run(cmd_skills(_ctx(ws, "mode mine auto")))
    assert "auto" in out.content.lower()
    from durin.agent.skills_store import read_mode
    assert read_mode(ws, "mine") == "auto"


def test_skills_mode_usage_on_bad_args(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = asyncio.run(cmd_skills(_ctx(ws, "mode")))
    assert "usage" in out.content.lower()
