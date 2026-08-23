"""Workflow node model refs resolve through the SAME preset machinery the chat
loop's ``/model`` picker uses: a preset name, a "provider model" pair, or a
plain model name. This closes a latent bug (a persona's pair ref used to reach
the provider as a raw "provider model" string) and lets per-model generation
params (declared under `providers.<name>.models` or on the persona itself)
reach node-level calls, including the reuse-identity gate.
"""

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunner, AgentRunResult
from durin.config.schema import Config, ModelEntry
from durin.providers.base import GenerationSettings, LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeRunRequest, WorkflowEngine
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode, parse_workflow


def _req(node, **kw):
    return NodeRunRequest(
        node=node, task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None, **kw,
    )


def _fake_sessions():
    sessions = MagicMock()
    sessions.workspace = MagicMock()
    sessions.workspace.resolve.return_value = MagicMock(__str__=lambda s: "/tmp")
    parent_session = MagicMock()
    parent_session.metadata = {}
    sessions.get_or_create.return_value = parent_session
    sessions.save = MagicMock()
    return sessions


def _default_provider(*, provider_key="default-provider", **generation_kwargs):
    provider = MagicMock(spec=LLMProvider)
    provider.provider_key = provider_key
    provider.generation = GenerationSettings(**generation_kwargs)
    return provider


def _config_with_api_key(provider_name: str) -> Config:
    """A real Config with a fake API key on *provider_name*, so make_provider
    (exercised for real in these tests — only the network call itself would
    ever go out, and none of these tests trigger one) doesn't fail on the
    "no API key configured" guard that has nothing to do with what's under
    test here."""
    cfg = Config()
    getattr(cfg.providers, provider_name).api_key = "test-key"
    return cfg


# ── pair ref: never raw, cached across nodes ─────────────────────────────────


def test_persona_pair_ref_resolves_bare_model_not_the_raw_pair(monkeypatch, tmp_path):
    from durin.workflow import node_runner as nr_mod

    default_provider = _default_provider()
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=_config_with_api_key("openai"))
    monkeypatch.setattr(nr_mod, "resolve_persona",
                        lambda cfg, name, ws=None: (None, "openai gpt-4o-mini", None))

    resp = nr(_req(WorkNode(id="a", persona="assistant", next=None)))

    spec = ar.run.call_args.args[0]
    assert spec.model == "gpt-4o-mini"            # bare model name
    assert "openai" not in spec.model             # never the raw "provider model" pair
    assert spec.provider is not default_provider  # a dedicated client for the pair's provider
    assert resp.model == "gpt-4o-mini"
    assert resp.provider == "openai"


def test_persona_pair_ref_builds_provider_once_and_reuses_it(monkeypatch, tmp_path):
    """A second node sharing the same pair ref must reuse the cached provider —
    make_provider is called exactly once across both node executions."""
    from durin.workflow import node_runner as nr_mod

    build_calls = []
    real_make_provider = __import__("durin.providers.factory", fromlist=["make_provider"]).make_provider

    def counting_make_provider(config, **kw):
        build_calls.append(1)
        return real_make_provider(config, **kw)

    monkeypatch.setattr("durin.providers.factory.make_provider", counting_make_provider)
    monkeypatch.setattr(nr_mod, "resolve_persona",
                        lambda cfg, name, ws=None: (None, "openai gpt-4o-mini", None))

    default_provider = _default_provider()
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=_config_with_api_key("openai"))

    nr(_req(WorkNode(id="a", persona="assistant", next=None)))
    nr(_req(WorkNode(id="b", persona="assistant", next=None)))

    assert len(build_calls) == 1


# ── plain node.model with a per-model config override ────────────────────────


def test_plain_model_with_differing_config_entry_gets_a_dedicated_provider(tmp_path):
    cfg = _config_with_api_key("zai_coding_plan")
    cfg.providers.zai_coding_plan.models["glm-mini"] = ModelEntry(temperature=0)

    default_provider = _default_provider(provider_key="zai_coding_plan", temperature=0.7)
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=cfg)

    nr(_req(WorkNode(id="a", model="glm-mini", next=None)))
    spec_a = ar.run.call_args.args[0]
    assert spec_a.model == "glm-mini"
    assert spec_a.provider.generation.temperature == 0        # the entry's override
    assert spec_a.provider is not default_provider            # dedicated, not the shared default

    # A second node with no override keeps using the shared default client.
    nr(_req(WorkNode(id="b", next=None)))
    spec_b = ar.run.call_args.args[0]
    assert spec_b.provider is default_provider
    assert spec_b.provider.generation.temperature == 0.7


def test_plain_model_with_top_p_entry_reaches_the_dedicated_provider(tmp_path):
    # adhoc_preset_config used to silently drop top_p/top_k/repeat_penalty
    # from a ModelEntry — the override-detection saw the difference and built
    # a dedicated provider, but that provider never actually carried the
    # declared param. Covers the fix directly (mirrors the temperature test).
    cfg = _config_with_api_key("zai_coding_plan")
    cfg.providers.zai_coding_plan.models["glm-mini"] = ModelEntry(top_p=0.5)

    default_provider = _default_provider(provider_key="zai_coding_plan")
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=cfg)

    nr(_req(WorkNode(id="a", model="glm-mini", next=None)))
    spec_a = ar.run.call_args.args[0]
    assert spec_a.model == "glm-mini"
    assert spec_a.provider.generation.top_p == 0.5             # the entry's override
    assert spec_a.provider is not default_provider             # dedicated, not the shared default


def test_plain_model_with_no_config_entry_reuses_the_default_client(tmp_path):
    # The common case: a bare model name with nothing configured for it must
    # NOT pay for a dedicated provider build.
    cfg = Config()
    default_provider = _default_provider(provider_key="zai_coding_plan")
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=cfg)

    nr(_req(WorkNode(id="a", model="some-other-model", next=None)))
    spec = ar.run.call_args.args[0]
    assert spec.model == "some-other-model"
    assert spec.provider is default_provider


# ── persona.temperature overrides the resolved generation ────────────────────


def test_persona_temperature_overrides_entry_and_changes_params_hash(monkeypatch, tmp_path):
    import dataclasses

    from durin.command.builtin import adhoc_preset_config
    from durin.workflow import node_runner as nr_mod
    from durin.workflow.provenance import params_hash

    cfg = _config_with_api_key("zai_coding_plan")
    cfg.providers.zai_coding_plan.models["glm-mini"] = ModelEntry(temperature=0)
    default_provider = _default_provider(provider_key="zai_coding_plan", temperature=0.7)
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=cfg)

    # The entry's resolved generation (temperature=0, everything else falls
    # back through adhoc_preset_config's own chain — catalog then schema
    # defaults, same as any other ad-hoc-resolved model).
    entry_generation = adhoc_preset_config(cfg, "zai_coding_plan", "glm-mini").to_generation_settings()

    # No persona: node.model alone carries the entry's temperature=0.
    plain_identity = nr.reuse_identity(WorkNode(id="a", model="glm-mini", next=None))
    assert plain_identity["params_hash"] == params_hash(entry_generation)

    # A persona referencing the SAME model, but with its own temperature: the
    # persona's temperature wins over the entry's, so calls carry it...
    monkeypatch.setattr(nr_mod, "resolve_persona",
                        lambda cfg, name, ws=None: (None, "glm-mini", 0.9))
    nr(_req(WorkNode(id="b", persona="p", next=None)))
    spec = ar.run.call_args.args[0]
    assert spec.temperature == 0.9

    # ...and params_hash reflects the persona's temperature, not the entry's.
    persona_identity = nr.reuse_identity(WorkNode(id="b", persona="p", next=None))
    assert persona_identity["params_hash"] != plain_identity["params_hash"]
    assert persona_identity["params_hash"] == params_hash(
        dataclasses.replace(entry_generation, temperature=0.9))


# ── unresolvable ref: fail open, never reuse ──────────────────────────────────


def test_unresolvable_ref_falls_back_to_default_and_disables_reuse(tmp_path):
    # A "provider model" pair naming a provider with no configured API key is a
    # genuine resolution failure (make_provider itself raises) — not a
    # contrived test double gap.
    cfg = Config()
    default_provider = _default_provider()
    ar = AgentRunner(default_provider)
    ar.run = AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=cfg)

    node = WorkNode(id="a", model="openai gpt-4o", next=None)
    nr(_req(node))
    spec = ar.run.call_args.args[0]
    assert spec.model == "default-model"          # fell back — NEVER the raw pair string
    assert spec.provider is default_provider
    assert nr.reuse_identity(node) is None        # unknown identity never reuses


# ── end-to-end reuse gate: identical -> reused; temperature change -> not ────


def test_reuse_gate_is_sensitive_to_a_persona_temperature_change(monkeypatch, tmp_path):
    from durin.workflow import node_runner as nr_mod

    default_provider = _default_provider(provider_key="d")
    ar = AgentRunner(default_provider)

    async def fake_run(spec):
        await spec.tools.execute("deliver", {"x": 1})
        return AgentRunResult(final_content="", messages=[{"role": "user", "content": "t"}])

    ar.run = AsyncMock(side_effect=fake_run)
    nr = AgentNodeRunner(ar, SessionManager(workspace=tmp_path),
                         default_model="default-model", app_config=None)

    persona_temperature = {"value": None}
    monkeypatch.setattr(nr_mod, "resolve_persona",
                        lambda cfg, name, ws=None: (None, None, persona_temperature["value"]))

    wf = parse_workflow({
        "name": "reuse-temp", "start": "producer",
        "output": {"file": True, "artifacts": [{"path": "out.json"}]},
        "nodes": [{"id": "producer", "kind": "work", "persona": "p", "reuse": "if-unchanged",
                   "output_schema": {"type": "object"}, "output_file": "out.json", "next": None}],
    })
    engine = WorkflowEngine(nr, workspace=str(tmp_path))

    first = engine.run(wf, "t", work_key="ticket-1")
    assert first.runs[0].status == "ok"

    second = engine.run(wf, "t", work_key="ticket-1")
    assert second.runs[0].status == "reused"      # identical producer identity

    persona_temperature["value"] = 0.9             # the persona now pins a temperature
    third = engine.run(wf, "t", work_key="ticket-1")
    assert third.runs[0].status == "ok"           # params_hash moved -> NOT reused
