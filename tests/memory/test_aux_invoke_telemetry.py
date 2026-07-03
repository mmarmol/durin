"""A failed purpose invoke leaves a visible trace: `aux.invoke_failure` names
the (purpose, provider, model) that failed, so failure-open consumers (judge
gate, dream passes) are observable instead of silently degraded."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.config.schema import Config
from durin.memory import llm_invoke


def _cfg() -> Config:
    c = Config()
    c.agents.defaults.provider = "nvidia"
    c.agents.defaults.model = "nemotron-3"
    c.providers.nvidia.api_key = "k"
    return c


def test_invoke_failure_emits_event_and_reraises(monkeypatch):
    events = []
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.emit_tool_event",
        lambda name, data: events.append((name, data)))

    def boom(prompt, *, preset, config, temperature):
        raise RuntimeError("404 page not found")

    with patch.object(llm_invoke, "aux_llm_invoke", boom), \
         patch("durin.config.loader.load_config", return_value=_cfg()):
        with pytest.raises(RuntimeError):
            llm_invoke.judge_llm_invoke("judge this")

    assert len(events) == 1
    name, data = events[0]
    assert name == "aux.invoke_failure"
    assert data["purpose"] == "judge"
    assert data["provider"] == "nvidia"
    assert data["model"] == "nemotron-3"
    assert "404" in data["error_head"]
