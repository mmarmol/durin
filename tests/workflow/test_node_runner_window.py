"""A workflow node runs under the context window of the model it uses.

Without a window the runner has no input budget for the node: no mid-turn
precheck, no pruning of old tool results near the limit, and a fixed result
cap. The node gets the window the same way a /model pick resolves it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunner, AgentRunResult
from durin.config.schema import Config, ModelEntry, ModelPresetConfig
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
    """A config whose default model is the one the node runner was built
    for, as the workflow service builds it, with a per-model window."""
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "test-model"
    config.providers.zai_coding_plan.models[model] = ModelEntry(context_window_tokens=window)
    return config


class _FakeProvider:
    def __init__(self, key: str, model: str) -> None:
        self.provider_key = key
        self._model = model

    def get_default_model(self) -> str:
        return self._model


def _node_window(tmp_path, config: Config, node: WorkNode, monkeypatch) -> int | None:
    """The window a node runs with, on a node runner built the way the
    workflow service builds it (the default preset's provider and model)."""
    monkeypatch.setattr(
        "durin.providers.factory.make_provider",
        lambda config, preset=None, **_: _FakeProvider(preset.provider, preset.model),
    )
    preset = config.resolve_default_preset()
    runner = AgentRunner(_FakeProvider(preset.provider, preset.model))

    async def fake_run(spec):
        return AgentRunResult(final_content="ok", messages=list(spec.initial_messages))

    runner.run = AsyncMock(side_effect=fake_run)
    nr = AgentNodeRunner(runner, SessionManager(workspace=tmp_path), default_model=preset.model, app_config=config)
    nr(_req(node))
    return runner.run.call_args.args[0].context_window_tokens


def test_the_default_models_window_comes_from_agents_defaults(tmp_path, monkeypatch) -> None:
    """An uncataloged default model gets the window the user set in
    agents.defaults, as the chat does."""
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "my-local-model"
    config.agents.defaults.context_window_tokens = 32_768
    window = _node_window(tmp_path, config, WorkNode(id="a", prompt="p", next=None), monkeypatch)
    assert window == 32_768


def test_a_node_on_a_named_preset_gets_that_presets_window(tmp_path, monkeypatch) -> None:
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "my-local-model"
    config.model_presets["fast"] = ModelPresetConfig(
        model="small-model", provider="zai_coding_plan", context_window_tokens=24_000, max_tokens=4096,
    )
    window = _node_window(tmp_path, config, WorkNode(id="a", model="fast", prompt="p", next=None), monkeypatch)
    assert window == 24_000


def test_a_node_on_a_preset_that_names_no_limits_runs_on_its_models_own(tmp_path, monkeypatch) -> None:
    """A judge node's preset names its model and a temperature, nothing else.
    It must run with that model's window and output cap, not 65,536 / 8,192:
    with those the judge pruned its history twice and its verdict was cut at
    8,192 output tokens."""
    import durin.providers.provider_catalog as pc
    from durin.providers.provider_catalog import ModelInfo

    monkeypatch.setattr(pc, "_load_index", lambda: {"zai_coding_plan": [
        ModelInfo(id="glm-5.3", max_input_tokens=1_000_000, max_output_tokens=131_072),
    ]})
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "my-local-model"
    config.model_presets["judge-cold"] = ModelPresetConfig(
        model="glm-5.3", provider="zai_coding_plan", temperature=0.0,
    )
    built: list[ModelPresetConfig] = []

    def _make_provider(config, preset=None, **_):
        built.append(preset)
        return _FakeProvider(preset.provider, preset.model)

    monkeypatch.setattr("durin.providers.factory.make_provider", _make_provider)
    runner = AgentRunner(_FakeProvider("zai_coding_plan", "my-local-model"))

    async def fake_run(spec):
        return AgentRunResult(final_content="ok", messages=list(spec.initial_messages))

    runner.run = AsyncMock(side_effect=fake_run)
    nr = AgentNodeRunner(runner, SessionManager(workspace=tmp_path), default_model="my-local-model", app_config=config)
    nr(_req(WorkNode(id="judge", model="judge-cold", prompt="p", next=None)))
    spec = runner.run.call_args.args[0]
    assert spec.model == "glm-5.3"
    assert spec.context_window_tokens == 1_000_000
    [node_preset] = built
    assert (node_preset.max_tokens, node_preset.temperature) == (131_072, 0.0)


def test_a_nodes_window_is_capped_by_the_fallback_models(tmp_path, monkeypatch) -> None:
    """A failover model must fit the same prompt, so the chat caps its window
    by every fallback's; a node gets the same cap."""
    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "my-local-model"
    config.agents.defaults.context_window_tokens = 64_000
    config.model_presets["tiny"] = ModelPresetConfig(
        model="tiny-model", provider="zai_coding_plan", context_window_tokens=20_000, max_tokens=4096,
    )
    config.agents.defaults.fallback_models = ["tiny"]
    window = _node_window(tmp_path, config, WorkNode(id="a", prompt="p", next=None), monkeypatch)
    assert window == 20_000


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


def test_a_node_without_file_tools_gets_no_place_to_save_results(tmp_path) -> None:
    """A node with only MCP tools cannot open a saved file, so an oversized
    result is cut inline to the cap instead of becoming a pointer it cannot
    follow."""
    nr = _node_runner(tmp_path, _config_with_window("test-model", 150_000))
    nr(_req(WorkNode(id="a", prompt="Do it.", tools="none", mcps=("docs",), next=None)))
    spec = nr.runner.run.call_args.args[0]
    assert spec.workspace is None


def test_a_nodes_large_results_are_saved_where_it_can_read_them(tmp_path) -> None:
    """An oversized or pruned result is replaced by a pointer to a saved file
    only when the run knows a workspace and a session key; without them it is
    cut with no way back."""
    nr = _node_runner(tmp_path, _config_with_window("test-model", 150_000))
    nr(_req(WorkNode(id="a", prompt="Do it.", tools="default", next=None)))
    spec = nr.runner.run.call_args.args[0]
    assert spec.workspace is not None
    assert spec.workspace.resolve() == tmp_path.resolve()
    assert spec.session_key == "workflow:r1:a:1"
