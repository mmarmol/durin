"""The default node runner runs an agent turn and persists the node session."""

from unittest.mock import AsyncMock, MagicMock, patch

from durin.agent.runner import AgentRunResult
from durin.agent.tools.base import Tool
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMProvider
from durin.session import lineage
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode


def _runner(sessions, fake_result):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    from durin.agent.runner import AgentRunner
    ar = AgentRunner(provider)
    ar.run = AsyncMock(return_value=fake_result)
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def test_runs_node_and_persists_session_with_lineage(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    sessions.save(Session(key="websocket:abc"))  # the invoking session (root)
    fake = AgentRunResult(
        final_content="node done",
        messages=[{"role": "user", "content": "t"}, {"role": "assistant", "content": "node done"}],
    )
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="build", model=None, context="own", prompt="Build it.", next=None),
        task="make X", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key="websocket:abc",
    )
    resp = nr(req)
    assert resp.output == "node done"
    assert resp.session_key == "workflow:r1:build:1"

    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:build:1")
    assert lineage.parent_of(reloaded.metadata) == "websocket:abc"
    assert reloaded.metadata[lineage.ORIGIN_TYPE] == "workflow_node"
    assert reloaded.metadata[lineage.ORIGIN_ID] == "r1:build:1"
    assert any(m.get("content") == "node done" for m in reloaded.messages)


def test_upstream_output_is_in_the_user_turn(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[{"role": "assistant", "content": "ok"}])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="b", prompt="Review.", next=None),
        task="review the work", upstream_output="PRIOR-OUTPUT", shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    user_turns = [m for m in spec.initial_messages if m["role"] == "user"]
    assert any("PRIOR-OUTPUT" in m["content"] for m in user_turns)


def test_node_model_overrides_default(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", model="big-model", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.model == "big-model"


def test_persist_failure_is_best_effort(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="done", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="n", prompt="p.", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    with patch.object(nr.sessions, "save", side_effect=OSError("disk full")):
        resp = nr(req)
    assert resp.output == "done"      # output still returned despite persist failure
    assert resp.session_key is None   # best-effort: persist failed -> no session key


def test_named_skills_are_injected_into_the_system_prompt(tmp_path):
    skill_dir = tmp_path / "skills" / "pdf-extract"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pdf-extract\n---\nUse pdftotext to pull text from PDFs.\n"
    )
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", prompt="Do the task.", next=None, skills=("pdf-extract",)),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert "Do the task." in system          # node's own framing preserved
    assert "pdftotext" in system             # the named skill's body injected
    assert "pdf-extract" in system           # the skill header (frontmatter stripped)


def test_no_skills_leaves_system_prompt_unchanged(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", prompt="Just this.", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert system == "Just this."


def test_default_tools_node_gets_real_tools(tmp_path):
    from durin.workflow.spec import WorkNode
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", tools="default", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.tools.has("read_file")    # default tool set is wired in


def test_none_tools_node_gets_empty_registry(tmp_path):
    from durin.workflow.spec import WorkNode
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", next=None),   # tools defaults to "none"
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert not spec.tools.has("read_file")   # no tools for a 'none' node


def test_plan_mode_makes_a_node_read_only(tmp_path):
    from durin.workflow.spec import WorkNode
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", tools="default", mode="plan", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.tools.has("read_file")          # read tools survive plan mode
    assert not spec.tools.has("write_file")     # write tools are dropped
    assert not spec.tools.has("exec")
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert "PLAN MODE" in system.upper()        # the read-only posture is injected


def test_build_mode_keeps_all_tools_and_adds_no_posture(tmp_path):
    from durin.workflow.spec import WorkNode
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", tools="default", prompt="do it", next=None),  # mode defaults to build
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.tools.has("read_file") and spec.tools.has("write_file")     # build = full set
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert system == "do it"                    # build adds no posture suffix


class _FakeMcpTool(Tool):
    _plugin_discoverable = False

    def __init__(self, name: str) -> None:
        self._n = name

    @property
    def name(self):
        return self._n

    @property
    def description(self):
        return "fake mcp tool"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        import asyncio
        return f"ran on loop {id(asyncio.get_running_loop())}"


def test_mcp_tools_scoped_to_selected_servers(tmp_path):
    live = ToolRegistry()
    live.register(_FakeMcpTool("mcp_github-mcp-server_create_issue"))
    live.register(_FakeMcpTool("mcp_github-mcp-server_search"))
    live.register(_FakeMcpTool("mcp_atlassian-mcp-server_create_page"))
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    nr._live_tool_registry = live
    nr._main_loop = object()
    req = NodeRunRequest(
        node=WorkNode(id="a", mcps=("github-mcp-server",), next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    names = nr.runner.run.call_args.args[0].tools.tool_names
    assert "mcp_github-mcp-server_create_issue" in names   # selected server's tools
    assert "mcp_github-mcp-server_search" in names
    assert "mcp_atlassian-mcp-server_create_page" not in names   # other server excluded


def test_routing_agent_node_gets_a_verdict_instruction(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="PASS", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="g", prompt="is it good?", on_pass="x", on_fail="g"),
        task="t", upstream_output="work", shared_context=[],
        run_id="r", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert "PASS" in system and "FAIL" in system


def test_non_routing_node_does_not_get_verdict_instruction(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="b", prompt="do the work", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert system == "do the work"


def test_file_tool_node_is_told_its_output_and_prev_folders(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    node = WorkNode(id="b", tools="default", next=None)
    req = NodeRunRequest(
        node=node, task="t", upstream_output="prev text", shared_context=[],
        run_id="r", iteration=1, root_session_key=None,
        output_dir="/ws/.workflow/r/b/1",
        upstream_artifact_dir="/ws/.workflow/r/a/1",
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    user_turns = [m for m in spec.initial_messages if m["role"] == "user"]
    user_content = " ".join(m["content"] for m in user_turns)
    assert "/ws/.workflow/r/b/1" in user_content   # write-here path
    assert "/ws/.workflow/r/a/1" in user_content   # read-prev path


def test_no_tool_node_is_not_told_about_folders(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    node = WorkNode(id="b", next=None)   # tools defaults to "none"
    req = NodeRunRequest(
        node=node, task="t", upstream_output=None, shared_context=[],
        run_id="r", iteration=1, root_session_key=None,
        output_dir="/ws/.workflow/r/b/1",
        upstream_artifact_dir="/ws/.workflow/r/a/1",
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    user_turns = [m for m in spec.initial_messages if m["role"] == "user"]
    user_content = " ".join(m["content"] for m in user_turns)
    assert "/.workflow/" not in user_content   # no folder lines for no-tools node


def test_cross_loop_tool_marshals_to_owner_loop():
    import asyncio
    import threading
    from durin.workflow.node_runner import _CrossLoopTool

    owner = asyncio.new_event_loop()
    threading.Thread(target=owner.run_forever, daemon=True).start()
    try:
        adapter = _CrossLoopTool(_FakeMcpTool("mcp_x_y"), owner)

        async def caller():
            # caller runs on a DIFFERENT loop than `owner`; the inner tool must
            # nonetheless execute on `owner` (where a real MCP session would live).
            return await adapter.execute()

        result = asyncio.run(caller())
        assert f"ran on loop {id(owner)}" in result
    finally:
        owner.call_soon_threadsafe(owner.stop)


def _fake_sessions(tmp_path=None):
    """Minimal SessionManager stub: satisfies _persist without touching disk."""
    sessions = MagicMock()
    sessions.workspace = MagicMock()
    sessions.workspace.resolve.return_value = MagicMock(__str__=lambda s: "/tmp")
    parent_session = MagicMock()
    parent_session.metadata = {}
    sessions.get_or_create.return_value = parent_session
    sessions.save = MagicMock()
    return sessions


def test_node_persona_applies_soul_and_model(monkeypatch):
    captured = {}

    class R:
        final_content = "ok"
        messages = []

    class Runner:
        async def run(self, spec):
            captured["system"] = spec.initial_messages[0]["content"]
            captured["model"] = spec.model
            return R()

    from durin.workflow import node_runner as nr_mod
    monkeypatch.setattr(nr_mod, "resolve_persona", lambda cfg, name, ws=None: ("ENGINEER SOUL", "persona-model"))
    nr = nr_mod.AgentNodeRunner(Runner(), sessions=_fake_sessions(), default_model="m", app_config=object())
    node = WorkNode(id="a", persona="engineer")
    nr(NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[], run_id="r", iteration=1, root_session_key=None))
    assert "ENGINEER SOUL" in captured["system"] and captured["model"] == "persona-model"


def test_node_persona_degrades_gracefully_without_app_config():
    # A surface that builds AgentNodeRunner without an app_config (app_config=None) must
    # not crash on a persona-bearing node — it falls back to the default model, no soul.
    captured = {}

    class R:
        final_content = "ok"
        messages = []

    class Runner:
        async def run(self, spec):
            captured["model"] = spec.model
            return R()

    from durin.workflow import node_runner as nr_mod
    nr = nr_mod.AgentNodeRunner(Runner(), sessions=_fake_sessions(), default_model="m")  # no app_config
    node = WorkNode(id="a", persona="engineer")
    nr(NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[], run_id="r", iteration=1, root_session_key=None))
    assert captured["model"] == "m"   # fell back to default; no crash


# ── max_turns node runner tests ───────────────────────────────────────────────


def test_max_turns_injects_budget_note_into_system_prompt(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="done", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", prompt="Do the work.", next=None, max_turns=4),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    system = [m for m in spec.initial_messages if m["role"] == "system"][0]["content"]
    assert "Do the work." in system
    assert "4" in system
    assert "rounds of tool use" in system


def test_max_turns_sets_run_max_iterations(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="done", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", next=None, max_turns=7),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.max_iterations == 7


def test_no_max_turns_uses_global_max_iterations(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="done", messages=[])
    provider = MagicMock(spec=AgentRunResult)
    from durin.agent.runner import AgentRunner
    from durin.providers.base import LLMProvider
    ar = AgentRunner(MagicMock(spec=LLMProvider))
    ar.run = AsyncMock(return_value=fake)
    nr = AgentNodeRunner(ar, sessions, default_model="m", max_iterations=50)
    req = NodeRunRequest(
        node=WorkNode(id="a", next=None),   # no max_turns
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.max_iterations == 50


def test_max_turns_exhausted_triggers_synthesis_call(tmp_path):
    # First run hits max_iterations; second run synthesizes with no tools.
    sessions = SessionManager(workspace=tmp_path)
    first_result = AgentRunResult(
        final_content="partial",
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "partial"},
        ],
        stop_reason="max_iterations",
    )
    synthesis_result = AgentRunResult(
        final_content="synthesized answer",
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "partial"},
            {"role": "user", "content": "give your best final answer"},
            {"role": "assistant", "content": "synthesized answer"},
        ],
        stop_reason="completed",
    )
    from durin.agent.runner import AgentRunner
    from durin.providers.base import LLMProvider
    ar = AgentRunner(MagicMock(spec=LLMProvider))
    ar.run = AsyncMock(side_effect=[first_result, synthesis_result])
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    req = NodeRunRequest(
        node=WorkNode(id="a", next=None, max_turns=3),
        task="task", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    resp = nr(req)

    assert ar.run.call_count == 2
    # Second call must use an empty tool registry and max_iterations=1.
    second_spec = ar.run.call_args_list[1].args[0]
    assert second_spec.max_iterations == 1
    assert not second_spec.tools.tool_names   # empty ToolRegistry
    assert "final answer" in second_spec.initial_messages[-1]["content"].lower()
    # Output comes from the synthesis, not the canned "max_iterations" string.
    assert resp.output == "synthesized answer"


def test_max_turns_within_budget_no_second_call(tmp_path):
    # First run completes within budget (stop_reason != max_iterations).
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(
        final_content="done within budget",
        messages=[{"role": "assistant", "content": "done within budget"}],
        stop_reason="completed",
    )
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", next=None, max_turns=6),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    resp = nr(req)
    # Only one runner.run call — no synthesis step needed.
    assert nr.runner.run.call_count == 1
    assert resp.output == "done within budget"


def test_no_max_turns_never_triggers_synthesis_even_on_max_iterations(tmp_path):
    # Without max_turns the node runner must behave exactly as before this feature.
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(
        final_content="hit limit",
        messages=[],
        stop_reason="max_iterations",
    )
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", next=None),   # no max_turns
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    resp = nr(req)
    assert nr.runner.run.call_count == 1
    assert resp.output == "hit limit"
