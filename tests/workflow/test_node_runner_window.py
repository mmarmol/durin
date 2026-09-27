"""A workflow node runs under the context window of the model it uses.

Without a window the runner has no input budget for the node: no mid-turn
precheck, no pruning of old tool results near the limit, and a fixed result
cap. The node gets the window the same way a /model pick resolves it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunner, AgentRunResult
from durin.config.schema import Config, ModelEntry
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode


def _node_runner(tmp_path, config: Config | None) -> AgentNodeRunner:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.provider_key = "zai_coding_plan"
    runner = AgentRunner(provider)

    async def fake_run(spec):
        return AgentRunResult(
            final_content="ok",
            messages=list(spec.initial_messages) + [{"role": "assistant", "content": "ok"}],
        )

    runner.run = AsyncMock(side_effect=fake_run)
    return AgentNodeRunner(
        runner, SessionManager(workspace=tmp_path), default_model="test-model", app_config=config,
    )


def _config_with_window(model: str, window: int) -> Config:
    config = Config()
    config.providers.zai_coding_plan.models[model] = ModelEntry(context_window_tokens=window)
    return config


def _req(node: WorkNode) -> NodeRunRequest:
    return NodeRunRequest(
        node=node, task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )


def test_a_node_runs_under_its_models_window(tmp_path) -> None:
    nr = _node_runner(tmp_path, _config_with_window("test-model", 150_000))
    nr(_req(WorkNode(id="a", prompt="Do it.", next=None)))
    spec = nr.runner.run.call_args.args[0]
    assert spec.context_window_tokens == 150_000
    # Left unset so the runner scales the per-result cap from that window.
    assert spec.max_tool_result_chars is None


def test_a_node_with_its_own_model_gets_that_models_window(tmp_path) -> None:
    config = _config_with_window("big-model", 231_072)
    nr = _node_runner(tmp_path, config)
    nr(_req(WorkNode(id="a", model="big-model", prompt="Do it.", next=None)))
    spec = nr.runner.run.call_args.args[0]
    assert spec.model == "big-model"
    assert spec.context_window_tokens == 231_072


def test_without_config_a_node_has_no_window(tmp_path) -> None:
    nr = _node_runner(tmp_path, None)
    nr(_req(WorkNode(id="a", prompt="Do it.", next=None)))
    assert nr.runner.run.call_args.args[0].context_window_tokens is None


def test_a_nodes_large_results_are_saved_where_it_can_read_them(tmp_path) -> None:
    """An oversized or pruned result is replaced by a pointer to a saved file
    only when the run knows a workspace and a session key; without them it is
    cut with no way back."""
    nr = _node_runner(tmp_path, _config_with_window("test-model", 150_000))
    nr(_req(WorkNode(id="a", prompt="Do it.", next=None)))
    spec = nr.runner.run.call_args.args[0]
    assert spec.workspace is not None
    assert spec.workspace.resolve() == tmp_path.resolve()
    assert spec.session_key == "workflow:r1:a:1"
