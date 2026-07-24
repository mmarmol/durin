"""Budget-exhaustion re-entry (max_reentries/reentry_prompt) and the schema-aware
exhaustion path: the synthesis prompt names the output schema's required fields and
the forced ``deliver`` instruction enumerates them up front.

Re-entry is author opt-in. On exhaustion the runner first asks the model, via a
forced one-call ``assess`` tool, whether essential work is missing; only a
``continue`` verdict spends a re-entry — anything else (including an assessment
failure) degrades to the pre-feature synthesis path.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import parse_workflow

SCHEMA = {
    "type": "object",
    "required": ["rootCause", "findings"],
    "properties": {"rootCause": {"type": "string"}, "findings": {"type": "string"}},
}


def _node(**fields):
    raw = {"id": "a", "kind": "work", "prompt": "Work.", "next": None, "max_turns": 3}
    raw.update(fields)
    return parse_workflow({"name": "w", "start": "a", "nodes": [raw]}).nodes["a"]


def _req(node):
    return NodeRunRequest(node=node, task="t", upstream_output=None, shared_context=[],
                          run_id="r1", iteration=1, root_session_key=None)


def _exhausted(content="partial"):
    return AgentRunResult(
        final_content=content,
        messages=[{"role": "user", "content": "t"},
                  {"role": "assistant", "content": content}],
        stop_reason="max_iterations",
    )


def _completed(content="final answer"):
    return AgentRunResult(
        final_content=content,
        messages=[{"role": "user", "content": "t"},
                  {"role": "assistant", "content": content}],
        stop_reason="completed",
    )


def _assess(verdict):
    return SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"verdict": verdict})])


def _runner(tmp_path, run_results, chat_responses=()):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(side_effect=list(run_results))
    provider.chat = AsyncMock(side_effect=list(chat_responses))
    return AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                           default_model="test-model"), provider


# ── re-entry ──────────────────────────────────────────────────────────────────


def test_continue_verdict_spends_a_reentry_with_fresh_budget(tmp_path):
    nr, provider = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed()],
        chat_responses=[_assess("continue")],
    )
    resp = nr(_req(_node(max_reentries=1)))

    assert nr.runner.run.await_count == 2      # first run + re-entry, no synthesis
    first_spec = nr.runner.run.await_args_list[0].args[0]
    reentry_spec = nr.runner.run.await_args_list[1].args[0]
    assert reentry_spec.max_iterations == 3    # the node's own budget, again
    # Same toolset as the first run — re-entry is more gathering, not synthesis.
    assert reentry_spec.tools.tool_names == first_spec.tools.tool_names
    granted = reentry_spec.initial_messages[-1]["content"]
    assert "3 more rounds" in granted
    assert "close the essential gaps" in granted   # generic steer (no reentry_prompt)
    assert resp.output == "final answer"


def test_reentry_prompt_replaces_the_generic_steer(tmp_path):
    nr, _ = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed()],
        chat_responses=[_assess("continue")],
    )
    nr(_req(_node(max_reentries=1, reentry_prompt="Verify pending claims, then deliver.")))
    granted = nr.runner.run.await_args_list[1].args[0].initial_messages[-1]["content"]
    assert "Verify pending claims, then deliver." in granted
    assert "close the essential gaps" not in granted


def test_deliver_verdict_goes_straight_to_synthesis(tmp_path):
    nr, provider = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed("synthesized")],
        chat_responses=[_assess("deliver")],
    )
    resp = nr(_req(_node(max_reentries=1)))

    assert provider.chat.await_count == 1      # the assessment
    assert nr.runner.run.await_count == 2      # first run + synthesis
    synthesis_spec = nr.runner.run.await_args_list[1].args[0]
    assert synthesis_spec.max_iterations == 1
    assert not synthesis_spec.tools.tool_names
    assert resp.output == "synthesized"


def test_assessment_failure_degrades_to_synthesis(tmp_path):
    nr, _ = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed("synthesized")],
        chat_responses=[RuntimeError("provider down")],
    )
    resp = nr(_req(_node(max_reentries=1)))
    assert nr.runner.run.await_count == 2      # no re-entry was attempted
    assert resp.output == "synthesized"


def test_reentry_budget_is_bounded_then_synthesis_runs(tmp_path):
    # One re-entry granted; the re-entry exhausts again -> no second assessment,
    # the synthesis fallback closes the node.
    nr, provider = _runner(
        tmp_path,
        run_results=[_exhausted(), _exhausted("still partial"), _completed("synthesized")],
        chat_responses=[_assess("continue")],
    )
    resp = nr(_req(_node(max_reentries=1)))
    assert provider.chat.await_count == 1
    assert nr.runner.run.await_count == 3      # first + re-entry + synthesis
    assert resp.output == "synthesized"


def test_no_reentries_means_no_assessment_call(tmp_path):
    nr, provider = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed("synthesized")],
    )
    nr(_req(_node()))                          # max_reentries defaults to 0
    assert provider.chat.await_count == 0


# ── schema-aware exhaustion path ──────────────────────────────────────────────


def test_synthesis_prompt_names_required_schema_fields(tmp_path):
    nr, provider = _runner(
        tmp_path,
        run_results=[_exhausted(), _completed("synthesized")],
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(
            arguments={"rootCause": "x", "findings": "y"})])],   # the deliver call
    )
    nr(_req(_node(output_schema=SCHEMA)))
    synthesis_prompt = nr.runner.run.await_args_list[1].args[0].initial_messages[-1]["content"]
    assert "no further tool calls" in synthesis_prompt
    assert "not a statement of intent" in synthesis_prompt
    assert "rootCause" in synthesis_prompt and "findings" in synthesis_prompt


def test_synthesis_prompt_without_schema_demands_full_content_only(tmp_path):
    nr, _ = _runner(tmp_path, run_results=[_exhausted(), _completed("synthesized")])
    nr(_req(_node()))
    synthesis_prompt = nr.runner.run.await_args_list[1].args[0].initial_messages[-1]["content"]
    assert "not a statement of intent" in synthesis_prompt
    assert "required" not in synthesis_prompt


def test_deliver_instruction_enumerates_required_fields(tmp_path):
    nr, provider = _runner(
        tmp_path,
        run_results=[_completed("prose answer")],
        chat_responses=[SimpleNamespace(tool_calls=[SimpleNamespace(
            arguments={"rootCause": "x", "findings": "y"})])],
    )
    resp = nr(_req(_node(output_schema=SCHEMA)))
    instruction = provider.chat.await_args_list[0].kwargs["messages"][-1]["content"]
    assert "every required field" in instruction
    assert "rootCause" in instruction and "findings" in instruction
    assert json.loads(resp.output) == {"rootCause": "x", "findings": "y"}
