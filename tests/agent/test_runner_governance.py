"""Tests for AgentRunner context governance: backfill, orphan cleanup, microcompact, snip_history."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.providers.base import LLMResponse

# Several tests call the runner's internals directly, before run() would
# resolve a window-scaled cap, so specs carry an explicit one.
_MAX_TOOL_RESULT_CHARS = 16_000


def _make_loop(tmp_path):
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


def _microcompact_spec(**overrides):
    from durin.agent.runner import AgentRunSpec

    params = dict(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    params.update(overrides)
    return AgentRunSpec(**params)

async def test_runner_uses_raw_messages_when_context_governance_fails():
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    captured_messages: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        captured_messages[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    initial_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    runner = AgentRunner(provider)
    runner._snip_history = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    result = await runner.run(AgentRunSpec(
        initial_messages=initial_messages,
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.final_content == "done"
    assert captured_messages == initial_messages
def test_snip_history_drops_orphaned_tool_results_from_trimmed_slice(monkeypatch):
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "tool call",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "tool output"},
        {"role": "assistant", "content": "after tool"},
    ]
    spec = AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=2000,
        context_block_limit=100,
    )

    monkeypatch.setattr("durin.agent.runner.estimate_prompt_tokens_chain", lambda *_args, **_kwargs: (500, None))
    token_sizes = {
        "old user": 120,
        "tool call": 120,
        "tool output": 40,
        "after tool": 40,
        "system": 0,
    }
    monkeypatch.setattr(
        "durin.agent.runner.estimate_message_tokens",
        lambda msg: token_sizes.get(str(msg.get("content")), 40),
    )

    trimmed = runner._snip_history(spec, messages)

    # After the fix, the user message is recovered so the sequence is valid
    # for providers that require system → user (e.g. GLM error 1214).
    assert trimmed[0]["role"] == "system"
    non_system = [m for m in trimmed if m["role"] != "system"]
    assert non_system[0]["role"] == "user", f"Expected user after system, got {non_system[0]['role']}"
async def test_backfill_missing_tool_results_inserts_error():
    """Orphaned tool_use (no matching tool_result) should get a synthetic error."""
    from durin.agent.runner import _BACKFILL_CONTENT, AgentRunner

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
                {"id": "call_b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "name": "exec", "content": "ok"},
    ]
    result = AgentRunner._backfill_missing_tool_results(messages)
    tool_msgs = [m for m in result if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    backfilled = [m for m in tool_msgs if m.get("tool_call_id") == "call_b"]
    assert len(backfilled) == 1
    assert backfilled[0]["content"] == _BACKFILL_CONTENT
    assert backfilled[0]["name"] == "read_file"


def test_drop_orphan_tool_results_removes_unmatched_tool_messages():
    from durin.agent.runner import AgentRunner

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_ok", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "name": "read_file", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_orphan", "name": "exec", "content": "stale"},
        {"role": "assistant", "content": "after tool"},
    ]

    cleaned = AgentRunner._drop_orphan_tool_results(messages)

    assert cleaned == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_ok", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "name": "read_file", "content": "ok"},
        {"role": "assistant", "content": "after tool"},
    ]


@pytest.mark.asyncio
async def test_backfill_noop_when_complete():
    """Complete message chains should not be modified."""
    from durin.agent.runner import AgentRunner

    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_x", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_x", "name": "exec", "content": "done"},
        {"role": "assistant", "content": "all good"},
    ]
    result = AgentRunner._backfill_missing_tool_results(messages)
    assert result is messages  # same object — no copy


@pytest.mark.asyncio
async def test_runner_drops_orphan_tool_results_before_model_request():
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    captured_messages: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        captured_messages[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old user"},
            {"role": "tool", "tool_call_id": "call_orphan", "name": "exec", "content": "stale"},
            {"role": "assistant", "content": "after orphan"},
            {"role": "user", "content": "new prompt"},
        ],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert all(
        message.get("tool_call_id") != "call_orphan"
        for message in captured_messages
        if message.get("role") == "tool"
    )
    assert result.messages[2]["tool_call_id"] == "call_orphan"
    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_backfill_repairs_model_context_without_shifting_save_turn_boundary(tmp_path):
    """Historical backfill should not duplicate old tail messages on persist."""
    from durin.agent.loop import AgentLoop
    from durin.agent.runner import _BACKFILL_CONTENT
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    response = LLMResponse(content="new answer", tool_calls=[], usage={})
    provider.chat_with_retry = AsyncMock(return_value=response)
    provider.chat_stream_with_retry = AsyncMock(return_value=response)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.messages = [
        {"role": "user", "content": "old user", "timestamp": "2026-01-01T00:00:00"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
            "timestamp": "2026-01-01T00:00:01",
        },
        {"role": "assistant", "content": "old tail", "timestamp": "2026-01-01T00:00:02"},
    ]
    loop.sessions.save(session)

    result = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="new prompt")
    )

    assert result is not None
    assert result.content == "new answer"

    request_messages = provider.chat_with_retry.await_args.kwargs["messages"]
    synthetic = [
        message
        for message in request_messages
        if message.get("role") == "tool" and message.get("tool_call_id") == "call_missing"
    ]
    assert len(synthetic) == 1
    assert synthetic[0]["content"] == _BACKFILL_CONTENT

    session_after = loop.sessions.get_or_create("cli:test")
    assert [
        {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "tool_call_id", "name", "tool_calls"}
        }
        for message in session_after.messages
    ] == [
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "old tail"},
        {"role": "user", "content": "new prompt"},
        {"role": "assistant", "content": "new answer"},
    ]


@pytest.mark.asyncio
async def test_runner_backfill_only_mutates_model_context_not_returned_messages():
    """Runner should repair orphaned tool calls for the model without rewriting result.messages."""
    from durin.agent.runner import _BACKFILL_CONTENT, AgentRunner, AgentRunSpec

    provider = MagicMock()
    captured_messages: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        captured_messages[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []

    initial_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "old tail"},
        {"role": "user", "content": "new prompt"},
    ]

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=initial_messages,
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    synthetic = [
        message
        for message in captured_messages
        if message.get("role") == "tool" and message.get("tool_call_id") == "call_missing"
    ]
    assert len(synthetic) == 1
    assert synthetic[0]["content"] == _BACKFILL_CONTENT

    assert [
        {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "tool_call_id", "name", "tool_calls"}
        }
        for message in result.messages
    ] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_missing",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "old tail"},
        {"role": "user", "content": "new prompt"},
        {"role": "assistant", "content": "done"},
    ]


# ---------------------------------------------------------------------------
# Pruning of old tool results (rare batches near the limit)
# ---------------------------------------------------------------------------


def _results(count: int, chars: int, name: str = "exec", prefix: str = "c") -> list[dict]:
    """``count`` calls of ``name``, each answered with a ``chars``-long result.

    Digits cycle rather than one repeated character: a single repeated
    character tiktoken-compresses far below the ~4 chars/token an estimate
    assumes, which would undercut the pressure math.
    """
    content = "".join(str(i % 10) for i in range(chars))
    out: list[dict] = []
    for i in range(count):
        out.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"{prefix}{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        out.append({"role": "tool", "tool_call_id": f"{prefix}{i}", "name": name, "content": content})
    return out


def _conversation(*parts: list[dict]) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    for part in parts:
        messages.extend(part)
    return messages


def _windowed_spec(tmp_path=None, window: int = 8_000):
    return _microcompact_spec(context_window_tokens=window, workspace=tmp_path, session_key="sess")


def _tool_contents(view: list[dict]) -> list[str]:
    return [str(m["content"]) for m in view if m.get("role") == "tool"]


def test_no_pruning_below_the_trigger():
    """Well under 80% of the input budget, every old result stays in full."""
    from durin.agent.runner import AgentRunner, _PruneState

    messages = _conversation(_results(14, 1_200))
    state = _PruneState()
    view = AgentRunner(MagicMock())._microcompact(
        _microcompact_spec(context_window_tokens=200_000), messages, MagicMock(), state=state,
    )
    assert view == messages
    assert state.batches == 0


def test_one_batch_prunes_old_results_and_keeps_the_newest(tmp_path):
    from durin.agent.runner import AgentRunner, _PruneState

    messages = _conversation(_results(14, 1_200))
    state = _PruneState()
    view = AgentRunner(MagicMock())._microcompact(_windowed_spec(tmp_path), messages, MagicMock(), state=state)
    contents = _tool_contents(view)
    assert state.batches == 1
    assert "result trimmed" in contents[0]
    assert 'read_file(path="' in contents[0]
    assert "instead of re-running" in contents[0]
    assert contents[-1] == _tool_contents(messages)[-1]


def test_pruned_results_stay_pruned_and_the_prompt_only_grows_at_its_end(tmp_path):
    from durin.agent.runner import AgentRunner, _PruneState

    runner, state = AgentRunner(MagicMock()), _PruneState()
    spec = _windowed_spec(tmp_path, window=32_000)
    messages = _conversation(_results(60, 1_200))
    first = runner._microcompact(spec, messages, MagicMock(), state=state)
    assert state.batches == 1
    grown = messages + _results(1, 1_200, prefix="n")
    second = runner._microcompact(spec, grown, MagicMock(), state=state)
    assert second[: len(first)] == first
    assert state.batches == 1


def test_after_a_batch_new_results_do_not_prune_on_every_call(tmp_path):
    """Every result that still fits under the trigger after a batch is added
    without a new batch. How many fit is measured: the placeholders carry
    the spill file's absolute path, so their size depends on where tmp_path
    is (an xdist worker's is longer)."""
    from durin.agent.runner import _MICROCOMPACT_PRESSURE_RATIO, AgentRunner, _PruneState
    from durin.utils.helpers import estimate_prompt_tokens_chain

    runner, state = AgentRunner(MagicMock()), _PruneState()
    spec = _windowed_spec(tmp_path, window=32_000)

    def estimate(view):
        # The size _microcompact measures a request by.
        return estimate_prompt_tokens_chain(
            MagicMock(), spec.model, view, runner._active_tool_definitions(spec))[0]

    messages = _conversation(_results(60, 1_200))
    view = runner._microcompact(spec, messages, MagicMock(), state=state)
    assert state.batches == 1
    trigger = runner._input_budget(spec, MagicMock()) * _MICROCOMPACT_PRESSURE_RATIO
    after_batch = estimate(view)
    per_result = estimate(view + _results(1, 1_200, prefix="n0-")) - after_batch
    fit = min(10, int((trigger - after_batch) // per_result))
    # Without the pressure trigger, the results the new ones push out of the
    # protected recent window free enough for another batch within eight
    # additions; fewer could not show that bug.
    assert fit >= 8, f"only {fit} results fit after the batch: tmp_path is too long for this window"
    for n in range(fit):
        messages = messages + _results(1, 1_200, prefix=f"n{n}-")
        runner._microcompact(spec, messages, MagicMock(), state=state)
    assert state.batches == 1


def test_a_batch_must_free_enough(tmp_path):
    """Over the trigger, but pruning would free less than 5% of the budget:
    nothing is rewritten, since a rewrite costs a cache write from there on."""
    from durin.agent.runner import AgentRunner, _PruneState

    memory = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "m0", "type": "function", "function": {"name": "memory_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "m0", "name": "memory_search", "content": "memory hit " * 2_000},
    ]
    messages = _conversation(memory, _results(4, 520))
    state = _PruneState()
    view = AgentRunner(MagicMock())._microcompact(_windowed_spec(tmp_path), messages, MagicMock(), state=state)
    assert state.batches == 0
    assert view == messages


def test_protection_of_recent_results_is_bounded_by_tokens(tmp_path):
    """The newest results stay in full only while together they fit in a
    fifth of the budget; the newest always does."""
    from durin.agent.runner import AgentRunner, _PruneState

    messages = _conversation(_results(4, 6_000))
    view = AgentRunner(MagicMock())._microcompact(
        _windowed_spec(tmp_path), messages, MagicMock(), state=_PruneState(),
    )
    contents = _tool_contents(view)
    assert contents[-1] == _tool_contents(messages)[-1]
    assert all("result trimmed" in c for c in contents[:-1])


def test_no_window_means_no_pruning():
    """Without a known window there is no budget to measure against."""
    from durin.agent.runner import AgentRunner, _PruneState

    messages = _conversation(_results(30, 5_000))
    state = _PruneState()
    view = AgentRunner(MagicMock())._microcompact(_microcompact_spec(), messages, MagicMock(), state=state)
    assert view == messages
    assert state.batches == 0


def test_short_and_non_prunable_results_are_never_pruned(tmp_path):
    from durin.agent.runner import AgentRunner, _PruneState

    skills = _results(3, 3_000, name="skill_view", prefix="s")
    short = _results(10, 300, prefix="short")
    messages = _conversation(skills, short, _results(12, 1_200))
    state = _PruneState()
    view = AgentRunner(MagicMock())._microcompact(_windowed_spec(tmp_path), messages, MagicMock(), state=state)
    assert state.batches == 1
    for original, seen in zip(messages, view):
        if original.get("role") != "tool":
            continue
        if original["name"] == "skill_view" or len(original["content"]) < 500:
            assert seen == original


def test_a_pruned_result_that_was_already_saved_keeps_its_path(tmp_path):
    """A result that was already spilled keeps its file path and read_file
    hint, so it stays recoverable — not an opaque placeholder."""
    from durin.agent.runner import AgentRunner, _PruneState

    spill_path = tmp_path / ".durin" / "tool-results" / "sess" / "c0.txt"
    spill_path.parent.mkdir(parents=True, exist_ok=True)
    spill_path.write_text("FULL ORIGINAL OUTPUT")
    preview = "P" * 1200
    marker = (
        "[tool output persisted]\n"
        f"Full output saved to: {spill_path}\n"
        "Original size: 50000 chars\n"
        f"Preview:\n{preview}\n...\n(Read the saved file if you need the full output.)"
    )
    messages = _conversation(_results(14, 1_200))
    messages[3] = {**messages[3], "content": marker}  # the oldest result
    view = AgentRunner(MagicMock())._microcompact(
        _windowed_spec(tmp_path), messages, MagicMock(), state=_PruneState(),
    )
    content = _tool_contents(view)[0]
    assert str(spill_path) in content
    assert "read_file" in content
    assert preview not in content
    assert "began:" not in content


def test_a_pruned_result_that_was_never_saved_is_saved_on_the_spot(tmp_path):
    from durin.agent.runner import AgentRunner, _PruneState

    messages = _conversation(_results(14, 1_200))
    original = _tool_contents(messages)[0]
    view = AgentRunner(MagicMock())._microcompact(
        _windowed_spec(tmp_path), messages, MagicMock(), state=_PruneState(),
    )
    content = _tool_contents(view)[0]
    spill_file = tmp_path / ".durin" / "tool-results" / "sess" / "c0.txt"
    assert spill_file.exists()
    assert spill_file.read_text() == original
    assert str(spill_file) in content
    assert "began:" in content


def test_governance_repairs_orphans_after_snip():
    """After _snip_history clips an assistant+tool_calls, the second
    _drop_orphan_tool_results pass must clean up the resulting orphans."""
    from durin.agent.runner import AgentRunner

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old msg"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "tc_old", "type": "function",
                         "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "tc_old", "name": "search",
         "content": "old result"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new msg"},
    ]

    # Simulate snipping that keeps only the tail: drop the assistant with
    # tool_calls but keep its tool result (orphan).
    snipped = [
        {"role": "system", "content": "system"},
        {"role": "tool", "tool_call_id": "tc_old", "name": "search",
         "content": "old result"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new msg"},
    ]

    cleaned = AgentRunner._drop_orphan_tool_results(snipped)
    # The orphan tool result should be removed.
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "tc_old"
        for m in cleaned
    )


def test_governance_fallback_still_repairs_orphans():
    """When full governance fails, the fallback must still run
    _drop_orphan_tool_results and _backfill_missing_tool_results."""
    from durin.agent.runner import AgentRunner

    # Messages with an orphan tool result (no matching assistant tool_call).
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "orphan_tc", "name": "read",
         "content": "stale"},
        {"role": "assistant", "content": "hi"},
    ]

    repaired = AgentRunner._drop_orphan_tool_results(messages)
    repaired = AgentRunner._backfill_missing_tool_results(repaired)
    # Orphan tool result should be gone.
    assert not any(m.get("tool_call_id") == "orphan_tc" for m in repaired)
def test_snip_history_preserves_user_message_after_truncation(monkeypatch):
    """When _snip_history truncates messages and the only user message ends up
    outside the kept window, the method must recover the nearest user message
    so the resulting sequence is valid for providers like GLM (which reject
    system→assistant with error 1214).

    This reproduces the exact scenario from the bug report:
    - Normal interaction: user asks, assistant calls tool, tool returns,
      assistant replies.
    - Injection adds a phantom user message, triggering more tool calls.
    - _snip_history activates, keeping only recent assistant/tool pairs.
    - The injected user message is in the truncated prefix and gets lost.
    """
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)

    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "previous reply"},
        {"role": "user", "content": ".durin的同目录"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "tc_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "tool output 1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "tc_2", "type": "function", "function": {"name": "exec", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "tc_2", "content": "tool output 2"},
    ]

    spec = AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=2000,
        context_block_limit=100,
    )

    # Make estimate_prompt_tokens_chain report above budget so _snip_history activates.
    monkeypatch.setattr("durin.agent.runner.estimate_prompt_tokens_chain", lambda *_a, **_kw: (500, None))
    # Make kept window small: only the last 2 messages fit the budget.
    token_sizes = {
        "system": 0,
        "previous reply": 200,
        ".durin的同目录": 80,
        "tool output 1": 80,
        "tool output 2": 80,
    }
    monkeypatch.setattr(
        "durin.agent.runner.estimate_message_tokens",
        lambda msg: token_sizes.get(str(msg.get("content")), 100),
    )

    trimmed = runner._snip_history(spec, messages)

    # The first non-system message MUST be user (not assistant).
    non_system = [m for m in trimmed if m.get("role") != "system"]
    assert non_system, "trimmed should contain at least one non-system message"
    assert non_system[0]["role"] == "user", (
        f"First non-system message must be 'user', got '{non_system[0]['role']}'. "
        f"Roles: {[m['role'] for m in trimmed]}"
    )


def test_snip_history_no_user_at_all_falls_back_gracefully(monkeypatch):
    """Edge case: if non_system has zero user messages, _snip_history should
    still return a valid sequence (not crash or produce system→assistant)."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    runner = AgentRunner(provider)

    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "reply"},
        {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        {"role": "assistant", "content": "reply 2"},
        {"role": "tool", "tool_call_id": "tc_2", "content": "result 2"},
    ]

    spec = AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=2000,
        context_block_limit=100,
    )

    monkeypatch.setattr("durin.agent.runner.estimate_prompt_tokens_chain", lambda *_a, **_kw: (500, None))
    monkeypatch.setattr(
        "durin.agent.runner.estimate_message_tokens",
        lambda msg: 100,
    )

    trimmed = runner._snip_history(spec, messages)

    # Should not crash.  The result should still be a valid list.
    assert isinstance(trimmed, list)
    # Must have at least system.
    assert any(m.get("role") == "system" for m in trimmed)
    # The _enforce_role_alternation safety net must be able to fix whatever
    # _snip_history returns here — verify it produces a valid sequence.
    from durin.providers.base import LLMProvider
    fixed = LLMProvider._enforce_role_alternation(trimmed)
    non_system = [m for m in fixed if m["role"] != "system"]
    if non_system:
        assert non_system[0]["role"] in ("user", "tool"), (
            f"Safety net should ensure first non-system is user/tool, got {non_system[0]['role']}"
        )


def _over_budget_run(*, caller_compacts_on_overflow: bool = False):
    """A run on a 12,000-token block limit whose request is over it: a
    system prompt, a long history, the current question, and tool schemas of
    a few thousand tokens. The history alone would fit beside the system
    prompt; beside the schemas too, only once its oldest turns are dropped."""
    from durin.agent.runner import AgentRunSpec

    schemas = [
        {"type": "function", "function": {
            "name": f"tool_{i}", "description": "does a thing " * 200,
            "parameters": {"type": "object", "properties": {}},
        }}
        for i in range(5)
    ]
    tools = MagicMock()
    tools.get_definitions.return_value = schemas
    messages = [{"role": "system", "content": "system prompt " * 300}]
    for i in range(30):
        messages += [
            {"role": "user", "content": f"question {i} " + "words " * 400},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    messages.append({"role": "user", "content": "the current question"})
    extra = {"caller_compacts_on_overflow": True} if caller_compacts_on_overflow else {}
    spec = AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=200_000,
        context_block_limit=12_000,
        **extra,
    )
    return spec, schemas


def test_snip_history_leaves_room_for_everything_the_request_sends():
    """A request sends the tool schemas and the current question with the
    history. The trim kept history up to the budget less the system prompt,
    so its view was still over the budget by the schemas' size."""
    from durin.agent.runner import AgentRunner
    from durin.utils.helpers import estimate_prompt_tokens

    spec, schemas = _over_budget_run()
    view = AgentRunner(MagicMock())._snip_history(spec, spec.initial_messages)

    assert estimate_prompt_tokens(view, schemas) <= 12_000
    assert view[0]["role"] == "system"
    assert view[-1]["content"] == "the current question"
    assert len(view) < len(spec.initial_messages)


@pytest.mark.asyncio
async def test_a_run_its_trim_can_fit_is_sent_not_stopped():
    """With the schemas counted, dropping the oldest turns fits the request,
    so the run calls the model instead of stopping on the precheck overflow;
    what it sends fits the budget."""
    from durin.agent.runner import AgentRunner
    from durin.utils.helpers import estimate_prompt_tokens

    spec, _schemas = _over_budget_run()
    sent: list[int] = []

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        sent.append(estimate_prompt_tokens(messages, tools))
        return LLMResponse(content="done")

    provider = MagicMock()
    provider.chat_with_retry = _chat
    result = await AgentRunner(provider).run(spec)

    assert result.stop_reason == "completed"
    assert sent and max(sent) <= 12_000


@pytest.mark.asyncio
@pytest.mark.parametrize("words", [3, 20])
async def test_a_history_of_many_short_messages_is_trimmed_to_fit(words):
    """The precheck's estimate joins every message's text with a newline,
    a token of its own per message; the trim summed the messages one by one
    without it. On a history of short messages it kept a view over the
    budget by about one token per message kept, which no tool result could
    make up for, so the precheck stopped the run instead of sending it."""
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.utils.helpers import estimate_prompt_tokens

    tools = MagicMock()
    tools.get_definitions.return_value = [
        {"type": "function", "function": {
            "name": "tool", "description": "does a thing " * 200,
            "parameters": {"type": "object", "properties": {}},
        }},
    ]
    messages = [{"role": "system", "content": "system prompt " * 300}]
    for i in range(3_000):
        # No trailing space: one would absorb the newline into its token.
        messages += [
            {"role": "user", "content": f"question {i} " + ("short words " * words).strip()},
            {"role": "assistant", "content": f"answer {i} " + ("fine " * words).strip()},
        ]
    messages.append({"role": "user", "content": "the current question"})
    sent: list[int] = []

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        sent.append(estimate_prompt_tokens(messages, tools))
        return LLMResponse(content="done")

    provider = MagicMock()
    provider.chat_with_retry = _chat
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=200_000,
        context_block_limit=30_000,
    ))

    assert result.stop_reason == "completed"
    assert sent and max(sent) <= 30_000


@pytest.mark.asyncio
async def test_a_request_with_its_task_state_never_asks_past_the_window():
    """A request appends the task state when it changed during the run. The
    precheck, and the output cap it sizes, counted the request without it.
    With an output ceiling above the reserved share of the window, that cap
    fills what the counted prompt leaves, and a task state longer than the
    safety buffer took the request past the window."""
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.utils.helpers import estimate_prompt_tokens

    tools = MagicMock()
    tools.get_definitions.return_value = []
    messages = [{"role": "system", "content": "system prompt"}]
    for i in range(13):
        messages += [
            {"role": "user", "content": f"question {i} " + "words " * 1500},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    messages.append({"role": "user", "content": "the current question " + "words " * 2500})
    spec = AgentRunSpec(
        initial_messages=messages,
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=60_000,
        max_tokens=50_000,
        task_state_provider=lambda: [f"- decision {i}: " + "detail " * 40 for i in range(70)],
    )
    requests: list[int] = []

    async def _chat(*_args, messages=None, tools=None, max_tokens=None, **_kwargs):
        requests.append(estimate_prompt_tokens(messages, tools) + max_tokens)
        return LLMResponse(content="done")

    provider = MagicMock()
    provider.chat_with_retry = _chat
    await AgentRunner(provider).run(spec)

    assert requests and max(requests) <= 60_000


@pytest.mark.asyncio
async def test_a_caller_that_compacts_gets_the_overflow_instead_of_a_trim():
    """The chat loop compacts the history it replays and retries when its
    first request does not fit: it summarizes what a trim would drop. For
    such a caller the first request leaves the history whole and the run
    stops on the overflow."""
    from durin.agent.runner import AgentRunner

    spec, _schemas = _over_budget_run(caller_compacts_on_overflow=True)
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="never"))
    result = await AgentRunner(provider).run(spec)

    assert result.stop_reason == "mid_turn_precheck_overflow"
    provider.chat_with_retry.assert_not_awaited()
