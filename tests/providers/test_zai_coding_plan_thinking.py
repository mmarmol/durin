"""The Z.ai Coding Plan endpoint obeys a configured reasoning effort of "none".

The endpoint thinks by default. "none" is never sent as an effort, so the only
thing that can turn reasoning off is the provider's own switch,
``{"thinking": {"type": "disabled"}}`` — the request must carry it.
"""

from __future__ import annotations

from typing import Any

from durin.providers.openai_compat_provider import OpenAICompatProvider
from durin.providers.registry import find_by_name


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_key="test-key",
        default_model="glm-test",
        spec=find_by_name("zai_coding_plan"),
    )


def _kwargs(reasoning_effort: str | None) -> dict[str, Any]:
    return _provider()._build_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=None, model=None, max_tokens=100,
        temperature=0.7, reasoning_effort=reasoning_effort, tool_choice=None,
    )


def test_none_sends_the_thinking_switch_off():
    kwargs = _kwargs("none")
    assert "reasoning_effort" not in kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_an_effort_is_sent_with_thinking_on():
    for effort in ("low", "high"):
        kwargs = _kwargs(effort)
        assert kwargs["reasoning_effort"] == effort
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


def test_unset_sends_no_reasoning_parameter():
    kwargs = _kwargs(None)
    assert "reasoning_effort" not in kwargs
    assert "extra_body" not in kwargs
