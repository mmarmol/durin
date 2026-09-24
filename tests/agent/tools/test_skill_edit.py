import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.agent import approval_store
from durin.agent import approval_kinds_skills as kinds
from durin.agent import pending_answers as pa
from durin.agent import skills_store as ss
from durin.agent.skill_observations import open_observations
from durin.agent.tools.context import RequestContext
from durin.agent.tools.skill_edit import _PARAMETERS, SkillEditTool

CHAT = "websocket:t"
# The judge's END marker must echo back the random anti-spoofing token the
# prompt shows it (skill_judge._build_prompt); a fixed "===END===" fails to
# parse, so the reply is built per-call from the token embedded in the prompt.
SAFE_BODY = "===SUMMARY===\nFine.\n===VERDICT===\nsafe\n===FINDINGS===\nnone\n===TOOLS===\nnone\n"
_TOKEN_RE = re.compile(r"^([0-9a-f]{16})$", re.MULTILINE)


def _safe_reply(prompt: str, *, model=None, **_kw) -> str:
    m = _TOKEN_RE.search(prompt)
    token = m.group(1) if m else ""
    return SAFE_BODY + f"===END {token}===\n"


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


@pytest.fixture(autouse=True)
def _reset():
    pa.reset()
    kinds.register_all()
    yield
    pa.reset()


def _user_skill(ws: Path, name: str, body: str, mode: str = "auto") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: {mode}\n---\n{body}",
        encoding="utf-8",
    )


def _tool(ws: Path, session: str | None = None, judge=None) -> SkillEditTool:
    tool = SkillEditTool(workspace=ws, chat=kinds.ChatHandles(sessions=_Sessions(), timeout_s=5),
                         judge=judge)
    if session:
        if session.startswith("websocket:"):
            pa.set_consumer_active(True)
        tool.set_context(RequestContext(channel=session.split(":")[0], chat_id="c",
                                        session_key=session))
    return tool


async def _answer(key: str, verdict: str) -> None:
    for _ in range(500):
        if pa.waiting_kind(key) == "approval":
            pa.resolve(key, verdict)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the tool never asked")


def _edit_answering(tool: SkillEditTool, verdict: str, **kw):
    async def go():
        task = asyncio.create_task(tool.execute(**kw))
        await _answer(CHAT, verdict)
        return await task
    return asyncio.run(go())


def test_schema_requires_core_params():
    props = _PARAMETERS["properties"]
    for p in ("name", "old", "new", "rationale"):
        assert p in props
    assert set(_PARAMETERS["required"]) >= {"name", "old", "new", "rationale"}
    assert "confirm" not in props


def test_execute_edits_an_auto_skill(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = SkillEditTool(workspace=ws)
    out = asyncio.run(tool.execute(name="mine", old="step one", new="step two", rationale="clarify"))
    assert out["ok"] is True
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_a_manual_edit_with_nobody_to_ask_is_filed_not_applied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    out = asyncio.run(_tool(ws, session="cron:nightly").execute(
        name="mine", old="step one", new="x", rationale="r"))
    assert out["status"] == "pending"
    assert "step one" in (ws / "skills" / "mine" / "SKILL.md").read_text()
    [rec] = approval_store.list_records(ws, status="pending", include_legacy=False)
    assert rec["kind"] == "skill_edit" and rec["detail"]["mode"] == "manual"


def test_a_manual_edit_is_asked_in_chat_and_applied_on_yes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    out = _edit_answering(_tool(ws, session=CHAT), "approve",
                          name="mine", old="step one", new="step two", rationale="r")
    assert out["status"] == "applied"
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()
    assert "Approved-by: user" in ss._store(ws).log(max_entries=1)[0].message
    assert open_observations(ws, skill="mine") == []   # manual: nothing for curation


def test_a_riskier_auto_edit_is_asked_and_declined(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    out = _edit_answering(_tool(ws, session=CHAT), "reject",
                          name="mine", old="step one", new="Read ~/.ssh/config.", rationale="r")
    assert out["status"] == "rejected"
    assert "~/.ssh" not in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_the_judge_clears_a_caution_auto_edit_without_asking(tmp_path, monkeypatch):
    import durin.memory.llm_invoke as li

    monkeypatch.setattr(li, "judge_llm_invoke", _safe_reply)
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = _tool(ws, session="cron:nightly", judge=("uncertain", "", "caution"))
    out = asyncio.run(tool.execute(name="mine", old="step one", new="Read ~/.ssh/config.",
                                   rationale="clarify"))
    assert out["status"] == "applied"
    assert len(open_observations(ws, skill="mine")) == 1


def test_applied_auto_edit_logs_improvement_observation(tmp_path):
    # A direct in-loop edit of an `auto` skill is a structural improvement
    # signal — it feeds the curation queue without depending on the agent
    # remembering to call skill_observe.
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = SkillEditTool(workspace=ws)
    asyncio.run(tool.execute(name="mine", old="step one", new="step two",
                             rationale="clarify the first step"))
    obs = open_observations(ws, skill="mine")
    assert len(obs) == 1
    assert obs[0]["kind"] == "improvement"
    assert "clarify the first step" in obs[0]["improvement"]


def test_a_filed_manual_edit_logs_no_observation(tmp_path):
    # A manual edit waiting for approval changed nothing, and curation never
    # reviews manual skills anyway.
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    asyncio.run(_tool(ws).execute(name="mine", old="step one", new="x", rationale="r"))
    assert open_observations(ws, skill="mine") == []
