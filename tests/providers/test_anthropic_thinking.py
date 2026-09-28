"""Tests for Anthropic provider thinking / reasoning_effort modes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from durin.providers.anthropic_provider import AnthropicProvider


def _make_provider(model: str = "claude-sonnet-4-6") -> AnthropicProvider:
    with patch("anthropic.AsyncAnthropic"):
        return AnthropicProvider(api_key="sk-test", default_model=model)


def _build(provider: AnthropicProvider, reasoning_effort: str | None, **overrides):
    defaults = dict(
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=reasoning_effort,
        tool_choice=None,
        supports_caching=False,
    )
    defaults.update(overrides)
    return provider._build_kwargs(**defaults)


def test_adaptive_sets_type_adaptive() -> None:
    kw = _build(_make_provider(), "adaptive")
    assert kw["thinking"] == {"type": "adaptive"}


def test_adaptive_forces_temperature_one() -> None:
    kw = _build(_make_provider(), "adaptive")
    assert kw["temperature"] == 1.0


def test_adaptive_does_not_inflate_max_tokens() -> None:
    kw = _build(_make_provider(), "adaptive", max_tokens=2048)
    assert kw["max_tokens"] == 2048


def test_adaptive_no_budget_tokens() -> None:
    kw = _build(_make_provider(), "adaptive")
    assert "budget_tokens" not in kw["thinking"]


def test_high_uses_enabled_with_budget() -> None:
    kw = _build(_make_provider(), "high", max_tokens=4096)
    assert kw["thinking"]["type"] == "enabled"
    assert kw["thinking"]["budget_tokens"] == max(8192, 4096)
    assert kw["max_tokens"] >= kw["thinking"]["budget_tokens"] + 4096


def test_low_uses_small_budget() -> None:
    kw = _build(_make_provider(), "low")
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_none_does_not_enable_thinking() -> None:
    kw = _build(_make_provider(), None)
    assert "thinking" not in kw
    assert kw["temperature"] == 0.7


def test_opus_4_7_omits_temperature_adaptive() -> None:
    kw = _build(_make_provider("claude-opus-4-7"), "adaptive")
    assert "temperature" not in kw
    assert kw["thinking"] == {"type": "adaptive"}


def test_opus_4_7_omits_temperature_enabled() -> None:
    """Enabled thinking (high) must also omit temperature for opus-4-7."""
    kw = _build(_make_provider("claude-opus-4-7"), "high", max_tokens=4096)
    assert "temperature" not in kw
    assert kw["thinking"]["type"] == "enabled"


def test_opus_4_7_omits_temperature_none() -> None:
    """Without thinking, opus-4-7 must still omit temperature (API rejects it)."""
    kw = _build(_make_provider("claude-opus-4-7"), None)
    assert "temperature" not in kw
    assert "thinking" not in kw


def test_reasoning_effort_string_none_does_not_enable_thinking() -> None:
    """reasoning_effort='none' must not enable thinking — treated same as disabled."""
    kw = _build(_make_provider(), "none")
    assert "thinking" not in kw
    assert kw["temperature"] == 0.7


def test_redacted_thinking_is_kept_and_sent_back_in_order() -> None:
    """Anthropic requires every thinking and redacted_thinking block of a tool
    turn to come back unchanged and in order; dropping the redacted one breaks
    the conversation."""
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="first", signature="sig1"),
            SimpleNamespace(type="redacted_thinking", data="opaque-bytes"),
            SimpleNamespace(type="thinking", thinking="second", signature="sig2"),
            SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"path": "a"}),
        ],
        stop_reason="tool_use",
        usage=None,
    )

    parsed = AnthropicProvider._parse_response(response)
    blocks = AnthropicProvider._assistant_blocks({
        "role": "assistant",
        "content": None,
        "thinking_blocks": parsed.thinking_blocks,
        "tool_calls": [{"id": "t1", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a"}'}}],
    })

    assert blocks == [
        {"type": "thinking", "thinking": "first", "signature": "sig1"},
        {"type": "redacted_thinking", "data": "opaque-bytes"},
        {"type": "thinking", "thinking": "second", "signature": "sig2"},
        {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a"}},
    ]
