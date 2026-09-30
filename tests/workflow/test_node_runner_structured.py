"""The forced ``deliver`` tool: schema-validated node output with in-node retry.

Mirrors the ``route`` verdict machinery, but with no fallback: a schema'd node
that cannot produce a valid payload has failed (typed NodeExecutionError, so
the run aborts naming it and failure-resume can retry it). Providers don't
reliably enforce JSON Schema, so validation is server-side and an invalid
payload is retried immediately with the exact validation error as feedback.
A length-truncated delivery is named as truncation (not misreported as a schema
miss) and a provider-error response fails fast with the provider's message.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunResult
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeExecutionError, NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import parse_workflow

SCHEMA = {
    "type": "object",
    "required": ["queries"],
    "properties": {"queries": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
}


def _schema_node():
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "Plan.",
         "output_schema": SCHEMA, "next": None},
    ]})
    return wf.nodes["plan"]


def _runner_with_deliver(tmp_path, deliver_responses):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(return_value=AgentRunResult(
        final_content="prose answer",
        messages=[{"role": "user", "content": "t"},
                  {"role": "assistant", "content": "prose answer"}],
    ))
    def _as_response(item):
        if isinstance(item, SimpleNamespace):        # full response (finish_reason etc.)
            return item
        return SimpleNamespace(tool_calls=[SimpleNamespace(name="deliver", arguments=item)] if item is not None else [])

    provider.chat_with_retry = AsyncMock(side_effect=[_as_response(i) for i in deliver_responses])
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def _req(node):
    return NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None)


def test_valid_payload_first_try_becomes_the_output(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [{"queries": ["a", "b"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    assert provider.chat_with_retry.await_count == 1


def test_invalid_payload_is_retried_with_the_validation_error(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"queries": []}, {"queries": ["fixed"]}])   # minItems violation, then valid
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["fixed"]}
    assert provider.chat_with_retry.await_count == 2
    retry_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    feedback = retry_messages[-1]["content"]
    assert "did not satisfy the output schema" in feedback
    assert "queries" in feedback                    # names where it failed


def test_exhausted_attempts_raise_a_typed_node_failure(tmp_path):
    nr, provider = _runner_with_deliver(
        tmp_path, [{"nope": 1}, {"nope": 2}, {"nope": 3}])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    assert "structured output failed" in str(exc.value.cause)
    assert provider.chat_with_retry.await_count == 3


def test_missing_tool_call_counts_as_a_failed_attempt(tmp_path):
    nr, provider = _runner_with_deliver(tmp_path, [None, {"queries": ["ok"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["ok"]}
    retry_messages = provider.chat_with_retry.await_args_list[1].kwargs["messages"]
    assert "no deliver tool call" in retry_messages[-1]["content"]


def test_truncated_invalid_payload_names_the_output_limit_and_recovers(tmp_path):
    truncated = SimpleNamespace(
        tool_calls=[SimpleNamespace(name="deliver", arguments={"queries": []})], finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [truncated, {"queries": ["ok"]}])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["ok"]}
    feedback = provider.chat_with_retry.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "output-token limit" in feedback
    assert "did not satisfy the output schema" not in feedback


def test_all_attempts_truncated_report_truncation_not_schema(tmp_path):
    def t():
        return SimpleNamespace(tool_calls=[SimpleNamespace(name="deliver", arguments={})], finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [t(), t(), t()])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    msg = str(exc.value.cause)
    assert "output-token limit" in msg
    assert "is a required property" not in msg
    assert provider.chat_with_retry.await_count == 3


def test_provider_error_response_fails_fast_with_the_provider_message(tmp_path):
    err = SimpleNamespace(tool_calls=[], finish_reason="error",
                          content="Error calling LLM: boom")
    nr, provider = _runner_with_deliver(tmp_path, [err])
    with pytest.raises(NodeExecutionError) as exc:
        nr(_req(_schema_node()))
    assert "provider error" in str(exc.value.cause)
    assert "boom" in str(exc.value.cause)
    assert provider.chat_with_retry.await_count == 1, "must not burn attempts on a dead provider"


def test_complete_valid_payload_is_accepted_even_when_flagged_length(tmp_path):
    r = SimpleNamespace(tool_calls=[SimpleNamespace(name="deliver", arguments={"queries": ["a"]})],
                        finish_reason="length")
    nr, provider = _runner_with_deliver(tmp_path, [r])
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a"]}


# ── `deliver` rides the node's tool list from turn 1 (cache-prefix preserving) ──


def _no_schema_node():
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "Plan.", "next": None},
    ]})
    return wf.nodes["plan"]


def _runner_with_fake_run(tmp_path, fake_run, chat_responses=()):
    """An AgentNodeRunner whose AgentRunner.run is a caller-supplied coroutine
    function — used where the fake needs to touch ``spec.tools`` itself (to
    simulate the model calling a tool mid-loop), unlike ``_runner_with_deliver``
    which only ever returns a canned result."""
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(side_effect=fake_run)
    provider.chat_with_retry = AsyncMock(side_effect=list(chat_responses)) if chat_responses else AsyncMock()
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path), default_model="test-model"), provider


def test_deliver_tool_is_registered_when_schema_declared(tmp_path):
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        await spec.tools.execute("deliver", {"queries": ["a"]})   # deliver early so no forced call is needed
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, _ = _runner_with_fake_run(tmp_path, fake_run)
    nr(_req(_schema_node()))
    assert "deliver" in seen["tools"]


def test_deliver_tool_absent_when_no_schema_declared(tmp_path):
    seen = {}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        return AgentRunResult(final_content="done", messages=[{"role": "user", "content": "t"}])

    nr, _ = _runner_with_fake_run(tmp_path, fake_run)
    nr(_req(_no_schema_node()))
    assert "deliver" not in seen["tools"]


def test_early_valid_deliver_ends_the_turn_with_the_payload(tmp_path):
    async def fake_run(spec):
        await spec.tools.execute("deliver", {"queries": ["a", "b"]})
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(tmp_path, fake_run)
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    provider.chat_with_retry.assert_not_called()   # no forced end-of-turn call


def test_early_invalid_deliver_returns_validation_error_and_continues(tmp_path):
    captured = {}

    async def fake_run(spec):
        captured["result"] = await spec.tools.execute("deliver", {"queries": []})  # minItems violation
        return AgentRunResult(final_content="prose", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(
        tmp_path, fake_run,
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(name="deliver", arguments={"queries": ["fixed"]})])])
    resp = nr(_req(_schema_node()))
    assert "did not satisfy the output schema" in captured["result"]
    assert "keep working" in captured["result"].lower()
    assert json.loads(resp.output) == {"queries": ["fixed"]}   # the forced call still ran and decided it
    provider.chat_with_retry.assert_awaited_once()


def test_forced_deliver_sends_full_tool_list(tmp_path):
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "tools": "default", "prompt": "Plan.",
         "output_schema": SCHEMA, "next": None},
    ]})
    node = wf.nodes["plan"]
    seen = {}

    async def fake_run(spec):
        seen["spec"] = spec
        return AgentRunResult(final_content="prose", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(
        tmp_path, fake_run,
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(name="deliver", arguments={"queries": ["a"]})])])
    nr(_req(node))

    loop_tools = seen["spec"].tools.get_definitions()
    assert len(loop_tools) > 1, "a real tool set, not just deliver alone"
    forced_kwargs = provider.chat_with_retry.await_args.kwargs
    assert forced_kwargs["tools"] == loop_tools
    assert forced_kwargs["tool_choice"] == {"type": "function", "function": {"name": "deliver"}}


def test_last_valid_deliver_wins(tmp_path):
    async def fake_run(spec):
        await spec.tools.execute("deliver", {"queries": ["first"]})
        await spec.tools.execute("deliver", {"queries": ["second"]})
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(tmp_path, fake_run)
    resp = nr(_req(_schema_node()))
    assert json.loads(resp.output) == {"queries": ["second"]}
    provider.chat_with_retry.assert_not_called()


def test_early_valid_deliver_skips_the_discarded_synthesis_call_on_exhaustion(tmp_path):
    """A node with both `output_schema` and `max_turns`: the model delivers validly
    mid-loop but the run still ends with stop_reason="max_iterations" (it kept working
    past its own delivery instead of stopping). The max_turns synthesis call — a second,
    tools-less LLM call whose only job is to force a text answer out of a node that
    didn't finish — must NOT fire here: its output would just be discarded once
    `_derive_structured_output` returns the already-captured payload, so making the
    call at all would be an extra LLM round-trip paying for nothing."""
    wf = parse_workflow({"name": "d", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "Plan.", "max_turns": 3,
         "output_schema": SCHEMA, "next": None},
    ]})
    node = wf.nodes["plan"]

    async def fake_run(spec):
        await spec.tools.execute("deliver", {"queries": ["a", "b"]})
        return AgentRunResult(
            final_content="partial", messages=[{"role": "user", "content": "t"}],
            stop_reason="max_iterations")

    nr, provider = _runner_with_fake_run(tmp_path, fake_run)
    resp = nr(_req(node))
    assert json.loads(resp.output) == {"queries": ["a", "b"]}
    assert nr.runner.run.await_count == 1          # no second (synthesis) AgentRunner.run call
    provider.chat_with_retry.assert_not_called()   # no forced deliver call either


# ── `deliver_file`: deliver a JSON draft from the working folder, no retyping ──


def _file_node(output_file=None, schema=SCHEMA):
    spec = {"id": "plan", "kind": "work", "tools": "default", "prompt": "Plan.", "next": None}
    if schema is not None:
        spec["output_schema"] = schema
    if output_file:
        spec["output_file"] = output_file
    return parse_workflow({"name": "d", "start": "plan", "nodes": [spec]}).nodes["plan"]


def _work_dir(tmp_path):
    d = tmp_path / "ws" / ".workflow" / "r1" / "work"
    d.mkdir(parents=True)
    return d


def _run_calls(tmp_path, node, work_dir, calls, chat_responses=(), output_file=None):
    """Run ``node`` in ``work_dir`` with a fake turn that makes ``calls`` in order:
    each is a ``(tool, args)`` pair, or a plain callable run between calls (the
    model editing its draft on disk). ``output_file`` is what the engine's main
    walk puts on the request when it will write the node's output file. Returns
    the node's tool names, every tool result in order, the node's response and
    the provider mock."""
    seen: dict = {"results": []}

    async def fake_run(spec):
        seen["tools"] = spec.tools.tool_names
        seen["deliver_description"] = (
            spec.tools.get("deliver").description if "deliver" in spec.tools else None)
        for call in calls:
            if callable(call):
                call()
                continue
            name, args = call
            seen["results"].append(await spec.tools.execute(name, args))
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, provider = _runner_with_fake_run(tmp_path / "ws", fake_run, chat_responses)
    resp = nr(NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                             run_id="r1", iteration=1, root_session_key=None,
                             output_dir=str(work_dir), output_file=output_file))
    return seen, resp, provider


def _forced(payload, name="deliver"):
    return SimpleNamespace(tool_calls=[SimpleNamespace(name=name, arguments=payload)])


def test_deliver_file_is_offered_to_a_schema_node_with_a_working_folder(tmp_path):
    work = _work_dir(tmp_path)
    seen, _, _ = _run_calls(tmp_path, _file_node(), work, [("deliver", {"queries": ["a"]})])
    assert "deliver_file" in seen["tools"]
    assert "deliver_file" in seen["deliver_description"]   # deliver points to the file option


def test_deliver_file_is_absent_without_a_schema_or_a_working_folder(tmp_path):
    work = _work_dir(tmp_path)
    seen, _, _ = _run_calls(tmp_path, _file_node(schema=None), work, [])
    assert "deliver_file" not in seen["tools"]

    # A node without file tools gets no working folder, so there is no draft to read.
    got = {}

    async def fake_run(spec):
        got["tools"] = spec.tools.tool_names
        got["deliver_description"] = spec.tools.get("deliver").description
        await spec.tools.execute("deliver", {"queries": ["a"]})
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    nr, _ = _runner_with_fake_run(tmp_path, fake_run)
    nr(_req(_schema_node()))
    assert "deliver_file" not in got["tools"]
    assert "deliver_file" not in got["deliver_description"]


def test_deliver_file_delivers_a_valid_draft_by_relative_or_absolute_path(tmp_path):
    work = _work_dir(tmp_path)
    (work / "draft.json").write_text(json.dumps({"queries": ["from", "file"]}), encoding="utf-8")

    for path in ("draft.json", str(work / "draft.json")):
        seen, resp, provider = _run_calls(
            tmp_path, _file_node(), work, [("deliver_file", {"path": path})])
        assert json.loads(resp.output) == {"queries": ["from", "file"]}
        assert seen["results"][0].startswith("Delivered")
        provider.chat_with_retry.assert_not_called()   # no forced end-of-turn call, no retyping


def test_deliver_file_refuses_paths_outside_the_working_folder(tmp_path):
    work = _work_dir(tmp_path)
    outside = tmp_path / "ws" / "elsewhere.json"
    outside.write_text(json.dumps({"queries": ["escaped"]}), encoding="utf-8")
    (work / "link.json").symlink_to(outside)

    seen, resp, provider = _run_calls(
        tmp_path, _file_node(), work,
        [("deliver_file", {"path": str(outside)}),
         ("deliver_file", {"path": "../../../elsewhere.json"}),
         ("deliver_file", {"path": "link.json"})],
        chat_responses=[_forced({"queries": ["forced"]})])
    assert len(seen["results"]) == 3
    for refusal in seen["results"]:
        assert "outside your working directory" in refusal
        assert str(work) in refusal
    # Nothing was captured: the forced end-of-turn delivery decided the output.
    assert json.loads(resp.output) == {"queries": ["forced"]}
    provider.chat_with_retry.assert_awaited_once()


def test_deliver_file_resolves_a_managed_path_where_write_file_writes_it(tmp_path):
    """write_file puts memory/x.json in the workspace's memory/, not the working
    folder: deliver_file names the same file by that path — and refuses it, as it
    lies outside the working folder — instead of reading a different one."""
    work = _work_dir(tmp_path)
    ws_draft = tmp_path / "ws" / "memory" / "draft.json"
    ws_draft.parent.mkdir(parents=True)
    ws_draft.write_text(json.dumps({"queries": ["workspace"]}), encoding="utf-8")
    (work / "memory").mkdir()
    (work / "memory" / "draft.json").write_text(json.dumps({"queries": ["folder"]}), encoding="utf-8")

    seen, resp, provider = _run_calls(
        tmp_path, _file_node(), work, [("deliver_file", {"path": "memory/draft.json"})],
        chat_responses=[_forced({"queries": ["forced"]})])
    assert "outside your working directory" in seen["results"][0]
    assert json.loads(resp.output) == {"queries": ["forced"]}


def test_deliver_file_names_the_failing_field_and_accepts_the_fixed_file(tmp_path):
    """A schema rejection names the field, so the model edits the draft in place and
    calls again — the payload is never retyped."""
    work = _work_dir(tmp_path)
    draft = work / "draft.json"
    draft.write_text(json.dumps({"queries": []}), encoding="utf-8")   # minItems violation

    def fix_in_place():
        draft.write_text(json.dumps({"queries": ["fixed"]}), encoding="utf-8")

    seen, resp, provider = _run_calls(
        tmp_path, _file_node(), work,
        [("deliver_file", {"path": "draft.json"}), fix_in_place,
         ("deliver_file", {"path": "draft.json"})])
    rejection = seen["results"][0]
    assert "did not satisfy the output schema" in rejection
    assert "at queries:" in rejection                 # the failing field, by path
    assert "draft.json" in rejection and "deliver_file again" in rejection
    assert json.loads(resp.output) == {"queries": ["fixed"]}
    provider.chat_with_retry.assert_not_called()


# A question note's body must contain "?"; this one is about 8,000 characters of
# statements, the size of the body a live rejection quoted back in full.
_QUESTION_SCHEMA = {"type": "object", "required": ["body"],
                    "properties": {"body": {"type": "string", "pattern": "\\?"}}}
_LONG_BODY = "## Problem Summary\n\n" + "A statement, not a question. " * 280


def test_a_rejection_cuts_a_long_failing_value_and_keeps_its_path(tmp_path):
    """jsonschema quotes the whole failing value in its message. Both delivery
    tools keep the field's path, why it failed and the start of the value, and
    cut the rest instead of repeating the whole body back to the model."""
    work = _work_dir(tmp_path)
    (work / "note.json").write_text(json.dumps({"body": _LONG_BODY}), encoding="utf-8")

    seen, resp, _ = _run_calls(
        tmp_path, _file_node(schema=_QUESTION_SCHEMA), work,
        [("deliver", {"body": _LONG_BODY}), ("deliver_file", {"path": "note.json"}),
         ("deliver", {"body": "Is the link still valid?"})])
    for rejection in seen["results"][:2]:
        assert "at body: '## Problem Summary" in rejection   # the path and the value's start
        assert "does not match" in rejection                  # why it failed
        assert _LONG_BODY not in rejection
        assert len(rejection) < 700
    assert json.loads(resp.output) == {"body": "Is the link still valid?"}


def test_a_validation_error_names_the_nested_path_and_keeps_a_short_value_whole():
    from durin.workflow.node_runner import _deliver_validation_error

    schema = {"type": "object", "properties": {"findings": {"type": "array", "items": {
        "type": "object", "properties": {"detail": {"type": "string", "maxLength": 20}}}}}}
    long_error = _deliver_validation_error(
        {"findings": [{"detail": "ok"}, {"detail": "x" * 5_000}]}, schema)
    assert long_error.startswith("at findings/1/detail: 'xxx")
    assert "is too long" in long_error
    assert len(long_error) < 400
    short_error = _deliver_validation_error({"findings": [{"detail": "y" * 21}]}, schema)
    assert short_error == f"at findings/0/detail: '{'y' * 21}' is too long"


def test_deliver_file_reports_a_draft_it_cannot_read(tmp_path):
    work = _work_dir(tmp_path)
    (work / "broken.json").write_text('{"queries": ["a",]}', encoding="utf-8")

    seen, _, _ = _run_calls(
        tmp_path, _file_node(), work,
        [("deliver_file", {"path": "broken.json"}),
         ("deliver_file", {"path": "missing.json"})],
        chat_responses=[_forced({"queries": ["forced"]})])
    invalid, missing = seen["results"]
    assert "not valid JSON" in invalid and "line 1" in invalid
    assert "No file" in missing and "missing.json" in missing


class _DraftAfterMissing(LLMProvider):
    """Calls deliver_file before its draft exists, writes the draft with the real
    write_file tool, then calls deliver_file again with the SAME path. A forced
    end-of-turn call gets no delivery, so only the in-loop call can succeed."""

    def __init__(self, work):
        super().__init__()
        self.work = work
        self.step = 0

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        if isinstance(tool_choice, dict):
            return LLMResponse(content="no delivery", finish_reason="stop")
        self.step += 1
        call = {1: ("deliver_file", {"path": "draft.json"}),
                2: ("write_file", {"path": str(self.work / "draft.json"),
                                   "content": json.dumps({"queries": ["late"]})}),
                3: ("deliver_file", {"path": "draft.json"})}.get(self.step)
        if call is None:
            return LLMResponse(content="done", finish_reason="stop")
        return LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
            ToolCallRequest(id=f"c{self.step}", name=call[0], arguments=call[1])])


def test_deliver_file_retried_with_the_same_path_after_writing_the_draft(tmp_path):
    """The agent loop blocks an identical call that failed with an "Error" reply.
    Writing the missing draft leaves the deliver_file call identical, so its
    rejection must not read as such a failure."""
    work = _work_dir(tmp_path)
    nr = AgentNodeRunner(AgentRunner(_DraftAfterMissing(work)),
                         SessionManager(workspace=tmp_path / "ws"), default_model="test-model")
    resp = nr(NodeRunRequest(node=_file_node(), task="t", upstream_output=None,
                             shared_context=[], run_id="r1", iteration=1,
                             root_session_key=None, output_dir=str(work)))
    assert json.loads(resp.output) == {"queries": ["late"]}


def test_delivery_replies_say_the_engine_writes_the_output_file(tmp_path):
    """The engine writes output_file only when the step ends; both replies say so,
    so the model does not hand-write a file the engine will overwrite."""
    work = _work_dir(tmp_path)
    (work / "draft.json").write_text(json.dumps({"queries": ["a"]}), encoding="utf-8")

    seen, _, _ = _run_calls(
        tmp_path, _file_node(output_file="plan.json"), work,
        [("deliver", {"queries": ["a"]}), ("deliver_file", {"path": "draft.json"})],
        output_file="plan.json")
    for reply in seen["results"]:
        assert "plan.json" in reply
        assert "when this step ends" in reply
        assert "do not write" in reply.lower()

    # A node without output_file has no output file to mention.
    seen, _, _ = _run_calls(tmp_path, _file_node(), work, [("deliver", {"queries": ["a"]})])
    assert "when this step ends" not in seen["results"][0]


def test_delivery_replies_name_no_file_the_engine_will_not_write(tmp_path):
    """A node declaring output_file dispatched where the engine does not write it
    (a parallel branch, fan-out worker or detached node — the request carries no
    output_file) must not be told the engine writes it, nor to leave it alone."""
    work = _work_dir(tmp_path)
    (work / "draft.json").write_text(json.dumps({"queries": ["a"]}), encoding="utf-8")

    seen, _, _ = _run_calls(
        tmp_path, _file_node(output_file="plan.json"), work,
        [("deliver", {"queries": ["a"]}), ("deliver_file", {"path": "draft.json"})])
    for reply in seen["results"]:
        assert reply.startswith("Delivered")
        assert "plan.json" not in reply
        assert "when this step ends" not in reply


class _ScriptedDelivery(LLMProvider):
    """Delivers ``{"v": "x"}`` on a schema'd node's first round, then stops; a node
    without ``deliver`` answers with a two-item list (a fan-out's planner). Keeps
    every tool result it was sent so a test can read the delivery replies."""

    def __init__(self):
        super().__init__()
        self.tool_results: list[str] = []

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, **kwargs):
        names = [t["function"]["name"] for t in (tools or [])]
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if "deliver" in names and not tool_msgs:
            return LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[
                ToolCallRequest(id="c1", name="deliver", arguments={"v": "x"})])
        self.tool_results.extend(str(m.get("content")) for m in tool_msgs)
        if "deliver" not in names:
            return LLMResponse(content="- a\n- b", finish_reason="stop")
        return LLMResponse(content="done", finish_reason="stop")


_OUT_NODE = {"kind": "work", "tools": "default", "prompt": "p", "output_file": "out.json",
             "output_schema": {"type": "object", "required": ["v"],
                               "properties": {"v": {"type": "string"}}}}


@pytest.mark.parametrize("shape,spec", [
    ("main walk", {"start": "a", "nodes": [{"id": "a", **_OUT_NODE, "next": None}]}),
    ("parallel branch", {"start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["b1"], "next": None},
        {"id": "b1", **_OUT_NODE}]}),
    ("fan-out worker", {"start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "prompt": "list", "next": "fan"},
        {"id": "fan", "kind": "parallel", "list_from": "plan", "worker": "w", "next": None},
        {"id": "w", **_OUT_NODE}]}),
    ("detached node", {"start": "side", "nodes": [
        {"id": "side", **_OUT_NODE, "detached": True, "next": "end"},
        {"id": "end", "kind": "work", "prompt": "e", "next": None}]}),
])
def test_the_reply_names_the_output_file_only_where_the_engine_writes_it(tmp_path, shape, spec):
    """Real engine and runner: wherever a node with output_file runs, its delivery
    reply mentions the file exactly when the engine then writes it."""
    from durin.workflow import run_log
    from durin.workflow.engine import WorkflowEngine

    ws = tmp_path / "ws"
    provider = _ScriptedDelivery()
    nr = AgentNodeRunner(AgentRunner(provider), SessionManager(workspace=ws),
                         default_model="test-model")
    wf = parse_workflow({"name": "wf", **spec})
    result = WorkflowEngine(node_runner=nr, run_id_factory=lambda: "r1",
                            workspace=str(ws)).run(wf, "go")
    assert result.status == "completed", shape

    work = run_log.read_manifest(ws, "wf", "r1")["work_dir"]
    written = (Path(work) / "out.json").is_file()
    replies = [r for r in provider.tool_results if r.startswith("Delivered")]
    assert replies, f"{shape}: the node never delivered"
    for reply in replies:
        assert ("out.json" in reply) == written, f"{shape}: {reply!r} (file written: {written})"


# ── the forced end-of-turn call is read by tool name ──
# A provider may send the named tool_choice as "auto" (the Anthropic provider does
# with thinking on, and after the model refuses a forced tool), so the answer can
# be any tool in the list.


def test_forced_call_answered_with_deliver_file_reads_the_draft(tmp_path):
    work = _work_dir(tmp_path)
    (work / "draft.json").write_text(json.dumps({"queries": ["drafted"]}), encoding="utf-8")

    _, resp, provider = _run_calls(
        tmp_path, _file_node(), work, [],
        chat_responses=[_forced({"path": "draft.json"}, name="deliver_file")])
    assert json.loads(resp.output) == {"queries": ["drafted"]}
    provider.chat_with_retry.assert_awaited_once()


def test_forced_call_answered_with_an_invalid_draft_names_its_failing_field(tmp_path):
    schema = {"type": "object", "required": ["summary"],
              "properties": {"summary": {"type": "string", "maxLength": 5}}}
    work = _work_dir(tmp_path)
    (work / "draft.json").write_text(json.dumps({"summary": "far too long"}), encoding="utf-8")

    _, resp, provider = _run_calls(
        tmp_path, _file_node(schema=schema), work, [],
        chat_responses=[_forced({"path": "draft.json"}, name="deliver_file"),
                        _forced({"summary": "short"})])
    assert json.loads(resp.output) == {"summary": "short"}
    feedback = provider.chat_with_retry.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "draft.json" in feedback and "at summary:" in feedback
    assert "required property" not in feedback      # the draft was read, not {"path": ...}
    assert "call `deliver`" in feedback.lower()


def test_forced_call_answered_with_another_tool_is_not_the_payload(tmp_path):
    """Another tool's arguments are never read as the node's output — not even
    when the schema has no required field and would accept them."""
    open_schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    work = _work_dir(tmp_path)

    _, resp, provider = _run_calls(
        tmp_path, _file_node(schema=open_schema), work, [],
        chat_responses=[_forced({"path": "notes.md"}, name="read_file"),
                        _forced({"summary": "real"})])
    assert json.loads(resp.output) == {"summary": "real"}
    feedback = provider.chat_with_retry.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "no deliver tool call" in feedback
