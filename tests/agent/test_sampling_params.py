"""Per-model sampling params (top_p / top_k / repeat_penalty) in the runner.

``top_p`` is a standard OpenAI param → ``kwargs["top_p"]``. ``top_k`` and
``repeat_penalty`` are non-standard (ollama / LM Studio read them from
``extra_body``) → ``kwargs["extra_body"]``. A spec with none of the three set
must not emit any of those keys and must not spuriously create ``extra_body``.
"""

from __future__ import annotations

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.registry import ToolRegistry


def _spec(**overrides) -> AgentRunSpec:
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=ToolRegistry(),
        model="qwythos-9b",
        max_iterations=1,
        max_tool_result_chars=1000,
        **overrides,
    )


def _kwargs(spec: AgentRunSpec) -> dict:
    return AgentRunner.__new__(AgentRunner)._build_request_kwargs(
        spec, spec.initial_messages, tools=None,
    )


def test_top_p_threads_as_standard_kwarg() -> None:
    kwargs = _kwargs(_spec(top_p=0.9))
    assert kwargs["top_p"] == 0.9
    # Standard param never touches extra_body.
    assert "extra_body" not in kwargs


def test_top_k_and_repeat_penalty_route_into_extra_body() -> None:
    kwargs = _kwargs(_spec(top_k=20, repeat_penalty=1.05))
    assert kwargs["extra_body"]["top_k"] == 20
    assert kwargs["extra_body"]["repeat_penalty"] == 1.05
    # Non-standard params must not leak as top-level kwargs.
    assert "top_k" not in kwargs
    assert "repeat_penalty" not in kwargs


def test_all_three_set_together() -> None:
    kwargs = _kwargs(_spec(top_p=0.8, top_k=40, repeat_penalty=1.1))
    assert kwargs["top_p"] == 0.8
    assert kwargs["extra_body"] == {"top_k": 40, "repeat_penalty": 1.1}


def test_none_set_emits_nothing() -> None:
    kwargs = _kwargs(_spec())
    assert "top_p" not in kwargs
    assert "extra_body" not in kwargs
