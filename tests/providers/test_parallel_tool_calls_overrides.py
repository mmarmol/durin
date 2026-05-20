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
    p = _make_provider({"GLM-5.1": False})
    assert p._resolve_parallel_tool_calls("glm-5.1") is False
    assert p._resolve_parallel_tool_calls("GLM-5.1-Latest") is False
    assert p._resolve_parallel_tool_calls("openrouter/zai/glm-5.1") is False


def test_resolver_first_match_wins():
    """Dict insertion order decides which override fires when multiple
    substrings match the same model."""
    p = _make_provider({"glm": True, "glm-5.1": False})
    assert p._resolve_parallel_tool_calls("glm-5.1") is True


def test_resolver_no_match_returns_none():
    p = _make_provider({"glm-5.1": False})
    assert p._resolve_parallel_tool_calls("gpt-4o") is None


def test_resolver_falls_back_to_default_model_when_unspecified():
    p = _make_provider({"glm-5.1": False}, default_model="glm-5.1")
    assert p._resolve_parallel_tool_calls(None) is False


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
