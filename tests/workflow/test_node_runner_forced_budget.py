"""A node's forced calls stay inside the input budget its work loop kept.

Live, a judge that never called `route` got a forced `route` call built from its
whole unpruned history: 57,507 prompt tokens against the node's own budget of
56,320, and 12,719 more than the pruned request the loop had just sent. The
forced calls (the `route` verdict, the re-entry assessment, the delivery) now go
out the way the loop would send its next request: the same pruned results, byte
for byte, fitted under the same budget.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path
from typing import Any

from durin.agent.runner import AgentRunner
from durin.config.schema import Config, ModelEntry
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from durin.session.manager import SessionManager
from durin.utils.helpers import estimate_prompt_tokens
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode

WINDOW = 40_000
# The window minus the output the provider holds back (its default max_tokens)
# and the runner's safety buffer.
BUDGET = WINDOW - 4_096 - 1_024
# Enough reads that the whole history outgrows the budget, so the loop prunes
# old results in a batch while the node works.
READS = 50
_TEXT = "The share link opened a preview, then the download failed with a 404. " * 45
_SENT_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def _sent(message: dict[str, Any]) -> dict[str, Any]:
    return {k: message[k] for k in _SENT_KEYS if k in message}


class _Reader(LLMProvider):
    """Reads one file per loop request, then answers in text without calling
    `route` or `deliver`. Every request reports its size as usage, as a real
    provider does, so the runner's estimates anchor on what it sent. A forced
    call is answered with the tool it names, and the output cap it asked for
    is recorded."""

    def __init__(self, files, forced_args, output_cap=None):
        super().__init__()
        if output_cap is not None:
            self.generation = dataclasses.replace(self.generation, max_tokens=output_cap)
        self.files = files
        self.forced_args = forced_args
        self.loop: list[list[dict]] = []
        self.forced: list[tuple[list[dict], list[dict]]] = []
        self.forced_max_tokens: list[int] = []

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        sent = [_sent(m) for m in messages]
        usage = {"prompt_tokens": estimate_prompt_tokens(sent, tools), "completion_tokens": 5}
        if isinstance(tool_choice, dict):
            name = tool_choice["function"]["name"]
            self.forced.append((sent, tools))
            self.forced_max_tokens.append(kwargs["max_tokens"])
            return LLMResponse(content=None, finish_reason="tool_calls", usage=usage, tool_calls=[
                ToolCallRequest(id=f"forced{len(self.forced)}", name=name,
                                arguments=self.forced_args[name])])
        self.loop.append(sent)
        n = len(self.loop)
        if n <= len(self.files):
            return LLMResponse(content="", finish_reason="tool_calls", usage=usage, tool_calls=[
                ToolCallRequest(id=f"c{n}", name="read_file", arguments={"path": str(self.files[n - 1])})])
        return LLMResponse(content="Everything I read checks out.", usage=usage)


def _config(window: int) -> Config:
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "test-model"
    config.providers.zai_coding_plan.models["test-model"] = ModelEntry(context_window_tokens=window)
    return config


def _run(tmp_path, node: WorkNode, *, window: int | None = WINDOW, reads: int = READS,
         output_cap: int | None = None, assess: str = "deliver"):
    """Run *node* in a workspace given as a relative path, from inside it.

    Every request then has the same size wherever tmp_path is. With absolute
    paths it did not: the model's read_file calls and the pruned results'
    placeholders (which name each saved file) carried tmp_path over a hundred
    times, so how long tmp_path was moved where the loop's batches fell, and
    for some lengths put the forced call past the batch trigger."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = []
    for n in range(reads):
        path = Path("docs") / f"doc{n}.txt"
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(f"document {n}\n{_TEXT}", encoding="utf-8")
        files.append(path)
    model = _Reader(files, {
        "route": {"label": "PASS"},
        "assess": {"verdict": assess},
        "deliver": {"summary": "fine"},
    }, output_cap=output_cap)
    with contextlib.chdir(tmp_path):
        nr = AgentNodeRunner(AgentRunner(model), SessionManager(workspace=Path(".")),
                             default_model="test-model",
                             app_config=_config(window) if window is not None else None)
        resp = nr(NodeRunRequest(node=node, task="t", upstream_output="the work",
                                 shared_context=[], run_id="r1", iteration=1,
                                 root_session_key=None))
    return model, resp


def _pruned(request: list[dict]) -> set[str]:
    return {m["tool_call_id"] for m in request
            if m.get("role") == "tool" and "result trimmed" in str(m.get("content"))}


def _assert_sent_from_the_loops_view(model: _Reader) -> None:
    last_loop = model.loop[-1]
    assert _pruned(last_loop), "the loop must have pruned results for this test to mean anything"
    forced, tools = model.forced[0]
    assert estimate_prompt_tokens(forced, tools) <= BUDGET
    assert forced[: len(last_loop)] == last_loop      # the loop's own view, byte for byte
    assert _pruned(last_loop) <= _pruned(forced)      # no pruned result comes back in full


def test_the_forced_route_call_is_sent_from_the_loops_pruned_view(tmp_path):
    model, resp = _run(tmp_path, WorkNode(id="g", prompt="Judge.", tools="default",
                                          on_pass=None, on_fail="g"))
    assert resp.route_label == "PASS"
    _assert_sent_from_the_loops_view(model)


def test_the_forced_delivery_is_sent_from_the_loops_pruned_view(tmp_path):
    schema = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
    model, resp = _run(tmp_path, WorkNode(id="g", prompt="Summarize.", tools="default",
                                          output_schema=schema, next=None))
    assert json.loads(resp.output) == {"summary": "fine"}
    _assert_sent_from_the_loops_view(model)


def test_the_reentry_assessment_stays_inside_the_budget(tmp_path):
    model, _ = _run(tmp_path, WorkNode(id="g", prompt="Gather.", tools="default",
                                       max_turns=READS, max_reentries=1, next=None))
    forced, tools = model.forced[0]
    assert tools[-1]["function"]["name"] == "assess"
    assert estimate_prompt_tokens(forced, tools) <= BUDGET
    assert _pruned(model.loop[READS - 1]) <= _pruned(forced)


def test_the_synthesis_request_keeps_what_the_loop_pruned(tmp_path):
    """The synthesis run continues the loop's messages: it re-sends them as the
    loop last did, pruned results and all, instead of pruning afresh."""
    model, _ = _run(tmp_path, WorkNode(id="g", prompt="Gather.", tools="default",
                                       max_turns=READS, next=None))
    last_loop, synthesis = model.loop[READS - 1], model.loop[-1]
    assert _pruned(last_loop)
    assert synthesis[: len(last_loop)] == last_loop


def test_a_reentry_keeps_what_the_loop_pruned(tmp_path):
    model, _ = _run(tmp_path, WorkNode(id="g", prompt="Gather.", tools="default",
                                       max_turns=READS, max_reentries=1, next=None),
                    assess="continue")
    last_loop, reentry = model.loop[READS - 1], model.loop[READS]
    assert _pruned(last_loop)
    assert reentry[: len(last_loop)] == last_loop


# ── the forced calls' output cap leaves room for their prompt ────────────────

# The box's GLM output cap, far more than a 64K window has left beside a
# long node's prompt: a provider that checks prompt + max_tokens against the
# window rejects such a request.
BIG_CAP = 131_072
CAP_WINDOW = 64_000
CAP_READS = 10
SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}


def _cap_nodes() -> list[WorkNode]:
    return [
        WorkNode(id="route", prompt="Judge.", tools="default", on_pass=None, on_fail="route"),
        WorkNode(id="deliver", prompt="Summarize.", tools="default", output_schema=SCHEMA, next=None),
        WorkNode(id="assess", prompt="Gather.", tools="default",
                 max_turns=CAP_READS, max_reentries=1, next=None),
    ]


def test_the_forced_calls_ask_for_no_more_output_than_the_window_has_room_for(tmp_path):
    """Clamped the way the work loop clamps its own requests: the output cap,
    at most what the window leaves after the prompt (and the safety buffer)."""
    for node in _cap_nodes():
        model, _ = _run(tmp_path / node.id, node, window=CAP_WINDOW, reads=CAP_READS,
                        output_cap=BIG_CAP)
        assert model.forced, node.id
        for (sent, tools), max_tokens in zip(model.forced, model.forced_max_tokens):
            assert max_tokens < BIG_CAP, node.id
            assert estimate_prompt_tokens(sent, tools) + max_tokens <= CAP_WINDOW, node.id


def test_without_a_window_the_forced_calls_keep_the_models_output_cap(tmp_path):
    for node in _cap_nodes():
        model, _ = _run(tmp_path / node.id, node, window=None, reads=CAP_READS, output_cap=BIG_CAP)
        assert model.forced_max_tokens and set(model.forced_max_tokens) == {BIG_CAP}, node.id
