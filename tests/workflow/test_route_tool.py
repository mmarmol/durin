"""Tests for the `route` tool call (forced end-of-turn and early/mid-turn) in
AgentNodeRunner.

Covers:
1. When provider.chat_with_retry returns a valid route tool call, the engine uses that label as
   the routing verdict — even when the node's text output contains no parseable label.
2. When provider.chat_with_retry raises, the engine gracefully falls back to parsing the node's
   text output (route_label is None), preserving existing behaviour.
3. A valid `route` call anywhere in the work loop decides the verdict unconditionally and skips
   the forced call — exactly symmetric with an early valid `deliver` call (see
   test_node_runner_structured.py). An invalid label is neither captured nor treated as decided.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from durin.agent.runner import AgentRunner, AgentRunResult
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest, WorkflowEngine
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode, parse_workflow


def _make_node_runner(tmp_path, mock_provider):
    """Build an AgentNodeRunner whose AgentRunner uses mock_provider."""
    from durin.agent.runner import AgentRunner

    ar = AgentRunner(mock_provider)
    sessions = SessionManager(workspace=tmp_path)
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def _multi_way_workflow():
    return parse_workflow({
        "name": "triage",
        "start": "gate",
        "nodes": [
            {
                "id": "gate",
                "kind": "work",
                "prompt": "Triage the request.",
                "cases": {
                    "NEED_INFO": None,
                    "PROCEED": "worker",
                    "DECLINE": None,
                },
            },
            {"id": "worker", "kind": "work", "next": None},
        ],
    })


def test_route_tool_verdict_overrides_unparseable_text(tmp_path):
    """When provider.chat_with_retry returns a route tool call with a valid label, the engine
    must route by that label — even though the node's text output has no parseable label."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    # The agent's main turn: returns text with NO clean label so text-parse would fail.
    ambiguous_output = "I think we should continue."
    main_result = AgentRunResult(
        final_content=ambiguous_output,
        messages=[{"role": "assistant", "content": ambiguous_output}],
    )

    # provider.chat_with_retry is called for the forced route tool call and returns NEED_INFO.
    route_tool_call = SimpleNamespace(
        name="route",
        arguments={"label": "NEED_INFO"},
    )
    route_response = SimpleNamespace(tool_calls=[route_tool_call])
    mock_provider.chat_with_retry = AsyncMock(return_value=route_response)

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=main_result)):
        result = engine.run(wf, "help me")

    # NEED_INFO maps to None (terminal) so the workflow ends here.
    assert result.status == "completed"
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    # The engine recorded the route_label in the NodeRun trace.
    assert gate_run.route_label == "NEED_INFO"
    # The worker node must NOT have run (NEED_INFO is a terminal target).
    assert not any(r.node_id == "worker" for r in result.runs)


def test_route_tool_failure_falls_back_to_text_parse(tmp_path):
    """When provider.chat_with_retry raises, route_label is None and the engine falls back to
    parsing the node's text output — existing behaviour is preserved."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    # The agent's main turn: the last line is exactly the label so text-parse matches it.
    parseable_output = "The request looks valid.\nPROCEED"
    main_result = AgentRunResult(
        final_content=parseable_output,
        messages=[{"role": "assistant", "content": parseable_output}],
    )

    # The worker node returns something so the workflow can complete. It has no routing,
    # so _derive_route_label is never called for it — no conflict with the failing mock.
    worker_result = AgentRunResult(
        final_content="done",
        messages=[{"role": "assistant", "content": "done"}],
    )

    # provider.chat_with_retry raises — the route tool call fails.
    mock_provider.chat_with_retry = AsyncMock(side_effect=Exception("provider unavailable"))

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    results_iter = iter([main_result, worker_result])
    with patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(side_effect=lambda *a, **k: next(results_iter))):
        result = engine.run(wf, "help me")

    # Text-parse of the output matches "PROCEED" (the last line) and routes to the worker.
    assert result.status == "completed"
    assert any(r.node_id == "worker" for r in result.runs)
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    # route_label is None because the route tool call failed; the engine used text-parse.
    assert gate_run.route_label == "PROCEED"


# ── `route` rides the node's tool list from turn 1 (cache-prefix preserving) ──


def test_last_valid_route_call_wins(tmp_path):
    """Mirrors deliver's ``test_last_valid_deliver_wins``: the captured label is
    overwritten on every VALID call, so the LAST one decides — even though an
    earlier call in the same turn also passed a valid label. The forced call
    never fires: a valid capture decides the verdict unconditionally."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    mock_provider.chat_with_retry = AsyncMock()

    async def fake_run(spec):
        # The engine also dispatches the "worker" node through this same patched
        # run(); only the "gate" node's registry has `route` registered.
        if spec.tools.has("route"):
            await spec.tools.execute("route", {"label": "DECLINE"})
            await spec.tools.execute("route", {"label": "PROCEED"})
        return AgentRunResult(
            final_content="", messages=[{"role": "assistant", "content": ""}],
        )

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    mock_provider.chat_with_retry.assert_not_called()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_forced_route_call_tools_match_the_loop_exactly(tmp_path):
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"

    seen = {}

    async def fake_run(spec):
        # The engine also dispatches the "worker" node through this same patched
        # run(); only the "gate" node's registry has `route` registered.
        if spec.tools.has("route"):
            seen["spec"] = spec
        return AgentRunResult(
            final_content="proceed with it",
            messages=[{"role": "assistant", "content": "proceed with it"}],
        )

    route_response = SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"label": "PROCEED"})])
    mock_provider.chat_with_retry = AsyncMock(return_value=route_response)

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        engine.run(wf, "help me")

    loop_tools = seen["spec"].tools.get_definitions()
    assert "route" in {t["function"]["name"] for t in loop_tools}   # registered from turn 1
    forced_kwargs = mock_provider.chat_with_retry.await_args.kwargs
    assert forced_kwargs["tools"] == loop_tools
    assert forced_kwargs["tool_choice"] == {"type": "function", "function": {"name": "route"}}


# ── a valid route call decides the verdict unconditionally, symmetric with ──
# ── how a valid early `deliver` call ends a schema'd node's turn ────────────


def test_valid_route_call_ends_the_turn_no_forced_call(tmp_path):
    """A valid `route` call decides the verdict immediately, no matter where in
    the turn it happens — exactly symmetric with
    test_early_valid_deliver_ends_the_turn_with_the_payload
    (tests/workflow/test_node_runner_structured.py). The forced end-of-turn
    call is skipped entirely."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    # If the forced call fired anyway it would route to DECLINE instead of the
    # captured PROCEED — belt and suspenders alongside assert_not_called().
    mock_provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(tool_calls=[
            SimpleNamespace(arguments={"label": "DECLINE"})]))

    async def fake_run(spec):
        if spec.tools.has("route"):
            ack = await spec.tools.execute("route", {"label": "PROCEED"})
            assert "recorded" in ack.lower()
            return AgentRunResult(
                final_content="", messages=[{"role": "assistant", "content": ""}])
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    mock_provider.chat_with_retry.assert_not_called()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_invalid_label_gets_actionable_ack_and_forced_call_still_decides(tmp_path):
    """An invalid label (not one of the node's cases) is neither captured nor
    treated as decided — it gets a specific, actionable ack naming the allowed
    labels, the loop continues, and the end-of-turn forced call remains
    authoritative, exactly as when nothing was ever captured."""
    wf = _multi_way_workflow()

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    mock_provider.chat_with_retry = AsyncMock(
        return_value=SimpleNamespace(tool_calls=[
            SimpleNamespace(arguments={"label": "PROCEED"})]))

    captured = {}

    async def fake_run(spec):
        if spec.tools.has("route"):
            captured["ack"] = await spec.tools.execute("route", {"label": "BOGUS"})
            return AgentRunResult(
                final_content="still working",
                messages=[{"role": "assistant", "content": "still working"}])
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        result = engine.run(wf, "help me")

    assert "not one of the allowed labels" in captured["ack"]
    for label in ("NEED_INFO", "PROCEED", "DECLINE"):
        assert label in captured["ack"]
    mock_provider.chat_with_retry.assert_awaited_once()
    gate_run = next(r for r in result.runs if r.node_id == "gate")
    assert gate_run.route_label == "PROCEED"
    assert any(r.node_id == "worker" for r in result.runs)


def test_route_tool_absent_when_node_has_no_routing(tmp_path):
    """A node with no `on_pass`/`on_fail`/`cases` never registers `route` at
    all — unchanged by this fix."""
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "prompt": "p", "next": None}]})

    mock_provider = MagicMock(spec=LLMProvider)
    mock_provider.get_default_model.return_value = "test-model"
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        return AgentRunResult(final_content="done",
                              messages=[{"role": "assistant", "content": "done"}])

    node_runner = _make_node_runner(tmp_path, mock_provider)
    engine = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")

    with patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=fake_run)):
        engine.run(wf, "help me")

    assert "route" not in seen["tools"]


# ── the node is asked to end with `route`, and a valid call ends its turn ────


class _Model(LLMProvider):
    """A routing node's model over the real agent loop: each loop request gets
    the next scripted reply; a forced call (``tool_choice`` names a tool) is
    recorded and answered with a `route` call carrying ``forced_label``."""

    def __init__(self, replies, forced_label="PASS"):
        super().__init__()
        self.replies = list(replies)
        self.loop_requests: list[list[dict]] = []
        self.forced_requests: list[list[dict]] = []
        self.forced_label = forced_label

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        # Copies: the runner goes on appending to the list it sent.
        if isinstance(tool_choice, dict):
            self.forced_requests.append(list(messages))
            return LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                ToolCallRequest(id="forced", name="route", arguments={"label": self.forced_label})])
        self.loop_requests.append(list(messages))
        return self.replies.pop(0)


def _route(label, content, reason=None, call_id="r1"):
    args = {"label": label} if reason is None else {"label": label, "reason": reason}
    return LLMResponse(content=content, finish_reason="tool_calls", tool_calls=[
        ToolCallRequest(id=call_id, name="route", arguments=args)])


def _run_gate(tmp_path, node, model):
    nr = AgentNodeRunner(AgentRunner(model), SessionManager(workspace=tmp_path),
                         default_model="test-model")
    return nr(NodeRunRequest(node=node, task="t", upstream_output="the work", shared_context=[],
                             run_id="r1", iteration=1, root_session_key=None))


def _system(model):
    return model.loop_requests[0][0]["content"]


def test_a_binary_gate_is_asked_to_end_by_calling_route(tmp_path):
    model = _Model([_route("PASS", "All three claims hold.\n\nPASS")])
    _run_gate(tmp_path, WorkNode(id="g", prompt="Judge the work.", on_pass=None, on_fail="g"), model)
    system = _system(model)
    assert "`route`" in system and "ends your turn" in system
    assert "PASS" in system and "FAIL" in system
    assert "last line" in system        # the text fallback still has its verdict line


def test_a_multi_way_node_is_asked_for_its_own_labels_not_pass_fail(tmp_path):
    model = _Model([_route("READY", "The spec is concrete.\n\nREADY")])
    node = WorkNode(id="g", prompt="Decide.", cases={"READY": None, "NEED_INFO": None})
    resp = _run_gate(tmp_path, node, model)
    system = _system(model)
    assert "READY, NEED_INFO" in system and "`route`" in system
    assert "PASS" not in system and "FAIL" not in system
    assert resp.route_label == "READY"


def test_a_valid_route_call_ends_the_turn_and_its_text_is_the_output(tmp_path):
    """The verdict comes with the assessment in one reply: the node asks no
    further question of the model, and a FAIL's feedback is that reply's text."""
    feedback = "Row 3 is wrong: the total is 40,085 bytes, not 40 KB.\n\nFAIL"
    model = _Model([_route("FAIL", feedback)])
    resp = _run_gate(tmp_path, WorkNode(id="g", prompt="Judge.", on_pass=None, on_fail="g"), model)
    assert resp.route_label == "FAIL"
    assert resp.output == feedback
    assert len(model.loop_requests) == 1        # no request after the verdict
    assert model.forced_requests == []          # and no forced call


def test_a_route_call_without_text_gives_its_reason_as_the_output(tmp_path):
    model = _Model([_route("FAIL", None, reason="The expiry date is wrong.")])
    resp = _run_gate(tmp_path, WorkNode(id="g", prompt="Judge.", on_pass=None, on_fail="g"), model)
    assert resp.route_label == "FAIL"
    assert resp.output == "The expiry date is wrong."


def test_a_route_call_after_the_assessment_keeps_that_text_as_the_output(tmp_path):
    """The assessment came in an earlier reply of the turn and the verdict alone
    after it: the node's output is that assessment, not an empty reply."""
    model = _Model([
        LLMResponse(content="Row 3 is wrong: recompute it from the CSV.", finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="t1", name="no_such_tool", arguments={})]),
        _route("FAIL", None, call_id="r2"),
    ])
    resp = _run_gate(tmp_path, WorkNode(id="g", prompt="Judge.", on_pass=None, on_fail="g"), model)
    assert resp.route_label == "FAIL"
    assert resp.output == "Row 3 is wrong: recompute it from the CSV."
    assert len(model.loop_requests) == 2 and model.forced_requests == []


def test_a_route_call_with_nothing_said_does_not_end_the_turn(tmp_path):
    """No text in the turn and no reason: ending there would hand the next step
    an empty verdict, so the model is asked again — and told to write it."""
    model = _Model([
        _route("FAIL", None),
        LLMResponse(content="The expiry date is wrong: 14 days, not 30.", tool_calls=[]),
    ])
    resp = _run_gate(tmp_path, WorkNode(id="g", prompt="Judge.", on_pass=None, on_fail="g"), model)
    assert len(model.loop_requests) == 2 and model.forced_requests == []
    ack = model.loop_requests[1][-1]
    assert ack["role"] == "tool" and "write" in ack["content"].lower()
    assert resp.route_label == "FAIL"
    assert resp.output == "The expiry date is wrong: 14 days, not 30."


def test_a_needs_input_route_carries_the_questions_written_before_it(tmp_path):
    wf = parse_workflow({"name": "ask", "start": "gate", "nodes": [
        {"id": "gate", "kind": "work", "prompt": "GATE",
         "cases": {"READY": None, "NEED_INFO": "__needs_input__"}},
    ]})
    model = _Model([
        LLMResponse(content="Which environment: staging or production?", finish_reason="tool_calls",
                    tool_calls=[ToolCallRequest(id="t1", name="no_such_tool", arguments={})]),
        _route("NEED_INFO", None, call_id="r2"),
    ])
    engine = WorkflowEngine(
        node_runner=AgentNodeRunner(AgentRunner(model), SessionManager(workspace=tmp_path),
                                    default_model="test-model"),
        run_id_factory=lambda: "r1")
    result = engine.run(wf, "deploy it")
    assert result.status == "needs_input"
    assert result.final_output == "Which environment: staging or production?"


def test_an_invalid_label_keeps_the_turn_going_and_the_forced_call_decides(tmp_path):
    model = _Model([_route("MAYBE", "Unsure."), LLMResponse(content="Checked: it holds.", tool_calls=[])],
                   forced_label="PASS")
    resp = _run_gate(tmp_path, WorkNode(id="g", prompt="Judge.", on_pass=None, on_fail="g"), model)
    assert len(model.loop_requests) == 2
    assert len(model.forced_requests) == 1
    assert resp.route_label == "PASS"
    assert resp.output == "Checked: it holds."


def test_a_fail_sent_with_route_reaches_the_producer_as_feedback(tmp_path):
    """Through the engine: the text that came with the FAIL call is what the
    producing step is asked to address on its next pass."""
    wf = parse_workflow({"name": "loop", "start": "make", "nodes": [
        {"id": "make", "kind": "work", "prompt": "MAKER", "next": "check"},
        {"id": "check", "kind": "work", "prompt": "CHECKER", "on_pass": None, "on_fail": "make"},
    ]})
    verdicts = [_route("FAIL", "Add the missing unit test.\n\nFAIL", call_id="v1"),
                _route("PASS", "The test is there now.\n\nPASS", call_id="v2")]
    maker_inputs: list[str] = []

    class _Both(_Model):
        async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
            if messages[0]["content"].startswith("MAKER"):
                maker_inputs.append(messages[-1]["content"])
                return LLMResponse(content="made it", tool_calls=[])
            return verdicts.pop(0)

    engine = WorkflowEngine(
        node_runner=AgentNodeRunner(AgentRunner(_Both([])), SessionManager(workspace=tmp_path),
                                    default_model="test-model"),
        run_id_factory=lambda: "r1")
    result = engine.run(wf, "build it")
    assert result.status == "completed"
    assert "Reviewer feedback (address this):\nAdd the missing unit test." in maker_inputs[1]
    assert [r.passed for r in result.runs if r.node_id == "check"] == [False, True]
