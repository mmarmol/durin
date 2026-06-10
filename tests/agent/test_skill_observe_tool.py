"""skill_observe tool — logs live skill feedback to the observation queue."""
import asyncio
import json

from durin.agent.skill_observations import open_observations
from durin.agent.tools.skill_observe import SkillObserveTool


def _run(tool, **kw):
    base = {"skill": "deploy-gateway", "kind": "correction",
            "issue": "user corrected the wheel build step",
            "improvement": "build from local dist, not PyPI"}
    base.update(kw)
    return json.loads(asyncio.run(tool.execute(**base)))


def test_tool_name(tmp_path):
    tool = SkillObserveTool(workspace=tmp_path)
    assert tool.name == "skill_observe"


def test_execute_logs_open_observation(tmp_path):
    ws = tmp_path / "ws"
    tool = SkillObserveTool(workspace=ws)
    out = _run(tool)
    assert out["ok"] is True and out["id"] == 1
    assert len(open_observations(ws)) == 1


def test_execute_propagates_session_key(tmp_path):
    ws = tmp_path / "ws"
    tool = SkillObserveTool(workspace=ws, session_key="sess-42")
    _run(tool)
    assert open_observations(ws)[0]["sessions"] == ["sess-42"]


def test_execute_surfaces_store_errors(tmp_path):
    ws = tmp_path / "ws"
    tool = SkillObserveTool(workspace=ws)
    out = _run(tool, kind="vibe")
    assert "error" in out


def test_create_reads_ctx(tmp_path):
    class Ctx:
        workspace = tmp_path
        session_key = "k1"

    tool = SkillObserveTool.create(Ctx())
    _run(tool)
    assert open_observations(tmp_path)[0]["sessions"] == ["k1"]


def test_set_context_binds_session_key_per_request(tmp_path):
    # The in-loop registry is built ONCE without a session; the loop binds the
    # session per request via ContextAware.set_context (found live 2026-06-10:
    # observations logged with sessions=[] because create() captured None).
    from durin.agent.tools.context import ContextAware, RequestContext

    ws = tmp_path / "ws"
    tool = SkillObserveTool(workspace=ws)
    assert isinstance(tool, ContextAware)
    tool.set_context(RequestContext(channel="cli", chat_id="direct",
                                    session_key="cli:direct"))
    _run(tool)
    assert open_observations(ws)[0]["sessions"] == ["cli:direct"]


def test_set_context_falls_back_to_channel_chat(tmp_path):
    from durin.agent.tools.context import RequestContext

    ws = tmp_path / "ws"
    tool = SkillObserveTool(workspace=ws)
    tool.set_context(RequestContext(channel="telegram", chat_id="42",
                                    session_key=None))
    _run(tool)
    assert open_observations(ws)[0]["sessions"] == ["telegram:42"]
