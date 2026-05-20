"""Per-model gating for ``parallel_tool_calls`` (OpenClaw-inspired Tier 1).

Some models misbehave when the OpenAI ``parallel_tool_calls=true`` flag is
sent — they over-emit calls, hallucinate args, or return a 400. The config
knob ``agents.defaults.parallel_tool_calls`` maps model-name substrings to
True/False, with first match winning. This file pins the resolver + the
injection point in ``_build_kwargs``.
"""

from __future__ import annotations

from durin.providers.openai_compat_provider import OpenAICompatProvider


def _make_provider(overrides: dict[str, bool] | None, *, default_model: str = "glm-5.1"):
    return OpenAICompatProvider(
        api_key="sk-test",
        api_base="https://api.example.com/v1",
        default_model=default_model,
        parallel_tool_calls_overrides=overrides,
    )


def test_resolver_returns_none_when_no_overrides_configured():
    """Empty / None overrides → preserve provider default (no injection)."""
    p = _make_provider(None)
    assert p._resolve_parallel_tool_calls("glm-5.1") is None
    assert p._resolve_parallel_tool_calls(None) is None


def test_resolver_matches_substring_case_insensitive():
    """Resolver returns ``(value, needle)`` tuple so telemetry can name
    the matching config entry."""
    p = _make_provider({"GLM-5.1": False})
    assert p._resolve_parallel_tool_calls("glm-5.1") == (False, "GLM-5.1")
    assert p._resolve_parallel_tool_calls("GLM-5.1-Latest") == (False, "GLM-5.1")
    assert p._resolve_parallel_tool_calls("openrouter/zai/glm-5.1") == (False, "GLM-5.1")


def test_resolver_first_match_wins():
    """Dict insertion order decides which override fires when multiple
    substrings match the same model. Resolver reports the winning needle."""
    p = _make_provider({"glm": True, "glm-5.1": False})
    assert p._resolve_parallel_tool_calls("glm-5.1") == (True, "glm")


def test_resolver_no_match_returns_none():
    p = _make_provider({"glm-5.1": False})
    assert p._resolve_parallel_tool_calls("gpt-4o") is None


def test_resolver_falls_back_to_default_model_when_unspecified():
    p = _make_provider({"glm-5.1": False}, default_model="glm-5.1")
    assert p._resolve_parallel_tool_calls(None) == (False, "glm-5.1")


def test_build_kwargs_injects_parallel_tool_calls_with_tools():
    """The injection happens inside ``_build_kwargs`` and lands at the
    top-level of the request payload (not inside ``extra_body``)."""
    p = _make_provider({"glm-5.1": False})
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    kwargs = p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="glm-5.1",
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert kwargs["parallel_tool_calls"] is False
    assert kwargs["tools"] == tools


def test_build_kwargs_does_not_inject_when_no_tools():
    """The OpenAI API rejects ``parallel_tool_calls`` when no ``tools`` are
    present — the override must be silently skipped in that case."""
    p = _make_provider({"glm-5.1": False})
    kwargs = p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="glm-5.1",
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert "parallel_tool_calls" not in kwargs


def test_build_kwargs_does_not_inject_when_no_match():
    """No matching override → no injection, even when tools are present."""
    p = _make_provider({"glm-5.1": False})
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    kwargs = p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="gpt-4o",
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert "parallel_tool_calls" not in kwargs


def test_build_kwargs_true_override_sets_true():
    """Symmetry: an override of True is just as expressible as False
    (rare in practice, but useful for opt-in providers that default off)."""
    p = _make_provider({"gpt-4o": True})
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    kwargs = p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="gpt-4o",
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )
    assert kwargs["parallel_tool_calls"] is True


# ---------------------------------------------------------------------------
# Telemetry — audit follow-up P1.2a
# ---------------------------------------------------------------------------


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    import durin.providers.openai_compat_provider as mod
    monkeypatch.setattr(mod, "current_telemetry", lambda: sink)


def _kwargs_call(provider, model: str) -> None:
    """Helper: invoke _build_kwargs once with a tool list, ignore result."""
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    provider._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model=model,
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )


def test_telemetry_emitted_when_injection_fires(monkeypatch):
    """First injection for a (model, value, needle) triple emits
    ``provider.parallel_tool_calls_injected`` with the model name,
    boolean value, and the matching config needle."""
    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    p = _make_provider({"glm-5.1": False})
    _kwargs_call(p, "glm-5.1")

    events = [e for e in sink.events if e[0] == "provider.parallel_tool_calls_injected"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload == {"model": "glm-5.1", "value": False, "match_needle": "glm-5.1"}


def test_telemetry_deduped_per_triple(monkeypatch):
    """Repeated calls for the SAME (model, value, needle) emit at most
    one event per process — avoids per-request spam."""
    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    p = _make_provider({"glm-5.1": False})
    for _ in range(5):
        _kwargs_call(p, "glm-5.1")

    events = [e for e in sink.events if e[0] == "provider.parallel_tool_calls_injected"]
    assert len(events) == 1


def test_telemetry_distinct_per_distinct_model(monkeypatch):
    """A different model that matches the same needle still gets its own
    event — gives ops visibility per concrete model in production."""
    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    p = _make_provider({"glm-5.1": False})
    _kwargs_call(p, "glm-5.1")
    _kwargs_call(p, "openrouter/zai/glm-5.1-air")

    events = [e for e in sink.events if e[0] == "provider.parallel_tool_calls_injected"]
    assert len(events) == 2


def test_telemetry_not_emitted_when_no_match(monkeypatch):
    """A non-matching model produces no telemetry event."""
    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    p = _make_provider({"glm-5.1": False})
    _kwargs_call(p, "gpt-4o")

    events = [e for e in sink.events if e[0] == "provider.parallel_tool_calls_injected"]
    assert events == []


def test_telemetry_not_emitted_when_no_tools(monkeypatch):
    """No tools → no injection → no telemetry."""
    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    p = _make_provider({"glm-5.1": False})
    p._build_kwargs(
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        model="glm-5.1",
        max_tokens=100,
        temperature=0.1,
        reasoning_effort=None,
        tool_choice=None,
    )

    events = [e for e in sink.events if e[0] == "provider.parallel_tool_calls_injected"]
    assert events == []
