"""A node's forced calls stay inside the input budget its work loop kept.

Live, a judge that never called `route` got a forced `route` call built from its
whole unpruned history: 57,507 prompt tokens against the node's own budget of
56,320, and 12,719 more than the pruned request the loop had just sent. The
forced calls (the `route` verdict, the re-entry assessment, the delivery) now go
out the way the loop would send its next request: the same pruned results, byte
for byte, fitted under the same budget.
"""

from __future__ import annotations

import json
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
    call is answered with the tool it names."""

    def __init__(self, files, forced_args):
        super().__init__()
        self.files = files
        self.forced_args = forced_args
        self.loop: list[list[dict]] = []
        self.forced: list[tuple[list[dict], list[dict]]] = []

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, tools=None, tool_choice=None, **kwargs):
        sent = [_sent(m) for m in messages]
        usage = {"prompt_tokens": estimate_prompt_tokens(sent, tools), "completion_tokens": 5}
        if isinstance(tool_choice, dict):
            name = tool_choice["function"]["name"]
            self.forced.append((sent, tools))
            return LLMResponse(content=None, finish_reason="tool_calls", usage=usage, tool_calls=[
                ToolCallRequest(id=f"forced{len(self.forced)}", name=name,
                                arguments=self.forced_args[name])])
        self.loop.append(sent)
        n = len(self.loop)
        if n <= len(self.files):
            return LLMResponse(content="", finish_reason="tool_calls", usage=usage, tool_calls=[
                ToolCallRequest(id=f"c{n}", name="read_file", arguments={"path": str(self.files[n - 1])})])
        return LLMResponse(content="Everything I read checks out.", usage=usage)


def _config() -> Config:
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "test-model"
    config.providers.zai_coding_plan.models["test-model"] = ModelEntry(context_window_tokens=WINDOW)
    return config


def _run(tmp_path, node: WorkNode):
    files = []
    for n in range(READS):
        path = tmp_path / "docs" / f"doc{n}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"document {n}\n{_TEXT}", encoding="utf-8")
        files.append(path)
    model = _Reader(files, {
        "route": {"label": "PASS"},
        "assess": {"verdict": "deliver"},
        "deliver": {"summary": "fine"},
    })
    nr = AgentNodeRunner(AgentRunner(model), SessionManager(workspace=tmp_path),
                         default_model="test-model", app_config=_config())
    resp = nr(NodeRunRequest(node=node, task="t", upstream_output="the work", shared_context=[],
                             run_id="r1", iteration=1, root_session_key=None))
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
