"""The dedup pass's first judge has its own reasoning setting.

``memory.dream.auto_absorb.judge_reasoning_effort`` replaces the memory
preset's ``reasoning_effort`` for the first judge's provider calls only. The
tests drive the real chain (``run_refine_pass`` -> ``run_refine`` ->
``default_llm_invoke`` -> ``aux_llm_invoke``) and intercept where the preset
is consumed: ``make_provider``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.config.loader import load_config, save_config
from durin.config.schema import AutoAbsorbConfig, Config
from durin.memory import refine_dream
from durin.memory.dream_passes import run_refine_pass
from durin.memory.field_patch import FieldPatch
from durin.memory.llm_invoke import default_llm_invoke
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import _verdicts_path, run_refine
from durin.providers.base import LLMResponse as ProviderResponse

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
PRESET_EFFORT = "high"
VERDICT = "===VERDICT===\n{v}\n===CONFIDENCE===\n{c}\n===REASONING===\nstub\n===END==="


def _pair(ws: Path) -> None:
    for ref, name in (("topic:a", "Alpha one"), ("topic:b", "Alpha two")):
        write_entity(ws, ref, [FieldPatch(kind="alias", value="shared", author="agent",
                                          source_ref="s", at=NOW)], create=True, name=name)


@pytest.fixture
def provider_seam(monkeypatch):
    """A memory model configured with a thinking effort of its own, behind a
    fake provider built the way ``make_provider`` returns one; ``calls`` holds
    the preset each provider was built from."""
    cfg = Config()
    cfg.agents.defaults.provider = "nvidia"
    cfg.agents.defaults.model = "model-x"
    cfg.agents.defaults.reasoning_effort = PRESET_EFFORT
    cfg.providers.nvidia.api_key = "k"
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    calls: list = []
    reply = {"text": VERDICT.format(v="different", c=90)}

    class _Provider:
        async def chat_with_retry(self, messages, tools=None, model=None, **kw):
            return ProviderResponse(content=reply["text"], usage={})

    def fake_make_provider(config, *, preset):
        calls.append(preset)
        return _Provider()

    monkeypatch.setattr("durin.providers.factory.make_provider", fake_make_provider)
    return calls, reply


def test_unset_the_judge_runs_on_the_memory_presets_own_effort(tmp_path, provider_seam):
    calls, _ = provider_seam
    _pair(tmp_path)
    run_refine_pass(tmp_path)
    assert calls and {p.reasoning_effort for p in calls} == {PRESET_EFFORT}


def test_set_the_first_judge_alone_gets_the_override(tmp_path, provider_seam):
    calls, _ = provider_seam
    _pair(tmp_path)
    run_refine_pass(tmp_path, judge_reasoning_effort="low")
    assert calls
    for preset in calls:
        assert preset.reasoning_effort == "low"
        assert (preset.provider, preset.model) == ("nvidia", "model-x")
    # Another pass's invoke is not bound to the override.
    calls.clear()
    default_llm_invoke("anything")
    assert [p.reasoning_effort for p in calls] == [PRESET_EFFORT]


def test_the_investigating_judge_is_not_handed_the_setting(tmp_path, provider_seam, monkeypatch):
    calls, reply = provider_seam
    reply["text"] = VERDICT.format(v="same", c=80)  # borderline: escalates
    seen: list[dict] = []

    def fake_escalate(workspace, a, b, **kw):
        seen.append(kw)
        raise RuntimeError("stop here")  # best-effort tier 2: the pair is kept

    monkeypatch.setattr(refine_dream, "_escalate_judge", fake_escalate)
    _pair(tmp_path)
    run_refine_pass(tmp_path, escalate_floor=70, judge_reasoning_effort="low")
    assert seen, "the pair should have escalated"
    assert "llm_invoke" not in seen[0] and "reasoning_effort" not in seen[0]
    assert all(p.reasoning_effort == "low" for p in calls)  # tier 1 only


def test_an_injected_invoke_is_used_as_given(tmp_path):
    _pair(tmp_path)
    seen: list[dict] = []

    def injected(prompt, **kw):
        seen.append(kw)
        return VERDICT.format(v="different", c=90)

    run_refine(tmp_path, llm_invoke=injected, judge_reasoning_effort="low")
    assert seen and all("reasoning_effort" not in kw for kw in seen)


def _entries(ws: Path) -> list[dict]:
    return list(json.loads(_verdicts_path(ws).read_text()).values())


def test_a_remembered_verdict_records_the_setting_and_survives_its_change(tmp_path):
    _pair(tmp_path)
    run_refine(tmp_path, llm_invoke=lambda p, **kw: VERDICT.format(v="different", c=90),
               judge_reasoning_effort="low")
    entries = _entries(tmp_path)
    assert [e["effort"] for e in entries] == ["low"]

    for effort in ("high", None):
        again: list = []

        def inv(p, **kw):
            again.append(p)
            return VERDICT.format(v="different", c=90)

        out = run_refine(tmp_path, llm_invoke=inv, judge_reasoning_effort=effort)
        assert not again
        assert [s["reason"] for s in out["skipped"]] == ["cached_verdict"]


def test_a_verdict_written_with_the_setting_unset_has_no_effort_field(tmp_path):
    _pair(tmp_path)
    run_refine(tmp_path, llm_invoke=lambda p, **kw: VERDICT.format(v="different", c=90))
    assert all("effort" not in e for e in _entries(tmp_path))


def test_the_refine_end_event_carries_the_setting_only_when_set(tmp_path, monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("durin.agent.tools._telemetry.emit_tool_event",
                        lambda name, data: events.append((name, data)))
    inv = lambda p, **kw: VERDICT.format(v="different", c=90)  # noqa: E731
    _pair(tmp_path)
    run_refine_pass(tmp_path, llm_invoke=inv, judge_reasoning_effort="low")
    run_refine_pass(tmp_path, llm_invoke=inv)
    ends = [d for n, d in events if n == "memory.dream.end" and d.get("kind") == "refine"]
    assert ends[0]["judge_reasoning_effort"] == "low"
    assert "judge_reasoning_effort" not in ends[1]


def test_the_config_key_defaults_to_none_and_loads_from_both_spellings():
    assert AutoAbsorbConfig().judge_reasoning_effort is None
    assert AutoAbsorbConfig.model_validate({"judge_reasoning_effort": "low"}).judge_reasoning_effort == "low"
    assert AutoAbsorbConfig.model_validate({"judgeReasoningEffort": "none"}).judge_reasoning_effort == "none"


def test_a_config_without_the_key_is_saved_unchanged_and_a_set_key_round_trips(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(), path)
    assert "judge_reasoning_effort" not in path.read_text()
    assert load_config(path).memory.dream.auto_absorb.judge_reasoning_effort is None

    cfg = Config()
    cfg.memory.dream.auto_absorb.judge_reasoning_effort = "low"
    save_config(cfg, path)
    assert load_config(path).memory.dream.auto_absorb.judge_reasoning_effort == "low"
