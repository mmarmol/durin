"""Tests for the ``context_transform`` hook on ``AgentRunSpec``.

Pi-inspired: a one-shot callback that gets the message list right
before each provider request, lets you prune/inject without touching
the rest of the loop. We test the contract — called every request,
return-value semantics, exception safety — not the use case.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.agent.runner import AgentRunner, AgentRunSpec


def _make_runner_and_spec(transform):
    """Build a barebones runner + spec wired for ``_build_request_kwargs``."""
    runner = AgentRunner(provider=MagicMock())
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=1000,
        context_transform=transform,
    )
    return runner, spec


def test_hook_replaces_messages_with_returned_list():
    """When the hook returns a list, that list is what reaches the LLM."""
    replacement = [{"role": "user", "content": "replaced"}]
    runner, spec = _make_runner_and_spec(lambda _m: replacement)

    kwargs = runner._build_request_kwargs(
        spec, [{"role": "user", "content": "original"}], tools=None,
    )
    assert kwargs["messages"] == replacement


def test_hook_can_mutate_in_place_and_return_same_list():
    """Mutating then returning the same list also works."""
    def mutate(msgs):
        msgs.append({"role": "system", "content": "appended"})
        return msgs

    runner, spec = _make_runner_and_spec(mutate)
    kwargs = runner._build_request_kwargs(
        spec, [{"role": "user", "content": "x"}], tools=None,
    )
    # The hook saw a copy (line 1 in _build_request_kwargs creates list(messages))
    # but its returned list IS used as-is.
    assert any(m.get("content") == "appended" for m in kwargs["messages"])


def test_hook_returning_none_keeps_original():
    """A ``None`` return is a no-op, original messages pass through."""
    runner, spec = _make_runner_and_spec(lambda _m: None)
    original = [{"role": "user", "content": "keep me"}]
    kwargs = runner._build_request_kwargs(spec, original, tools=None)
    assert kwargs["messages"] is original


def test_hook_raising_exception_is_swallowed():
    """A broken hook must NEVER break the agent loop."""
    def boom(_m):
        raise RuntimeError("hook went wrong")

    runner, spec = _make_runner_and_spec(boom)
    original = [{"role": "user", "content": "survive"}]
    kwargs = runner._build_request_kwargs(spec, original, tools=None)
    # Despite the exception, we got messages back unchanged.
    assert kwargs["messages"] == original


def test_no_hook_set_is_unchanged_default():
    """Default ``None`` hook means messages flow through unchanged."""
    runner, spec = _make_runner_and_spec(None)
    original = [{"role": "user", "content": "default"}]
    kwargs = runner._build_request_kwargs(spec, original, tools=None)
    assert kwargs["messages"] is original


def test_hook_receives_a_copy_not_the_caller_list():
    """The hook must not be able to mutate the caller's list — only its
    own copy. (If it WANTS to apply changes, it returns the new list.)"""
    captured: list[list] = []

    def capture(msgs):
        captured.append(msgs)
        msgs.append({"role": "system", "content": "should not leak"})
        return None  # return None ⇒ original is kept

    runner, spec = _make_runner_and_spec(capture)
    original = [{"role": "user", "content": "untouched"}]
    kwargs = runner._build_request_kwargs(spec, original, tools=None)

    # Hook saw a list (and was able to mutate it locally)…
    assert captured[0][-1]["content"] == "should not leak"
    # …but the original passed in is intact (we passed a copy to the hook).
    assert original == [{"role": "user", "content": "untouched"}]
    # And the kwargs reflect the original since hook returned None.
    assert kwargs["messages"] is original


# ---------------------------------------------------------------------------
# Re-sanitize after the hook (OpenClaw-inspired Tier 1).
#
# A transform that trims for token budget can drop messages mid-way through
# a tool_use/tool_result pair. Anthropic and OpenAI both reject those
# mismatches with a 400 ("tool_use_id ... was not found in `tool_use` blocks
# of the previous assistant message"). The pre-call sanitize pipeline can't
# catch this because it ran on the untransformed list. The runner must
# re-pair orphans after the hook returns.
# ---------------------------------------------------------------------------


def test_orphan_tool_results_dropped_when_hook_strips_assistant():
    """Transform drops the assistant tool_use, leaving an orphan tool_result.
    Re-sanitize must drop that orphan before it reaches the LLM."""

    original = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_dir", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "list_dir", "content": "ok"},
        {"role": "user", "content": "follow-up"},
    ]

    # Hook drops the assistant tool_call entirely — mimics aggressive
    # token-budget pruning that grabs the middle of the history.
    def trim(msgs):
        return [m for m in msgs if not (m.get("role") == "assistant" and m.get("tool_calls"))]

    runner, spec = _make_runner_and_spec(trim)
    kwargs = runner._build_request_kwargs(spec, original, tools=None)
    result = kwargs["messages"]
    # The orphan tool_result must be gone.
    assert not any(m.get("role") == "tool" for m in result)
    assert [m["role"] for m in result] == ["user", "user"]


def test_orphan_tool_use_backfilled_when_hook_strips_tool_result():
    """Transform drops the tool_result, leaving the assistant's tool_use
    unmatched. Re-sanitize must insert a synthetic result so the request
    is still well-formed."""

    original = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_42",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_42", "name": "read_file", "content": "x"},
        {"role": "assistant", "content": "done"},
    ]

    def drop_tool_results(msgs):
        return [m for m in msgs if m.get("role") != "tool"]

    runner, spec = _make_runner_and_spec(drop_tool_results)
    kwargs = runner._build_request_kwargs(spec, original, tools=None)
    result = kwargs["messages"]
    # A synthetic tool_result for call_42 was inserted.
    tool_messages = [m for m in result if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_42"


def test_sanitize_failure_after_hook_is_caught(monkeypatch):
    """If the post-hook sanitize raises, the transformed list still reaches
    the LLM as-is — better an imperfect request than a swallowed turn."""
    from durin.agent import runner as runner_mod

    transformed = [{"role": "user", "content": "after hook"}]
    runner, spec = _make_runner_and_spec(lambda _m: transformed)

    def explode(_messages):
        raise RuntimeError("sanitize boom")

    monkeypatch.setattr(runner_mod.AgentRunner, "_drop_orphan_tool_results", staticmethod(explode))

    kwargs = runner._build_request_kwargs(spec, [{"role": "user", "content": "x"}], tools=None)
    assert kwargs["messages"] is transformed
