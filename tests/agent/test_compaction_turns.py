"""Compaction over real turns: ``process_direct`` runs BUILD's consolidation,
the runner, SAVE and the background consolidation after it, against a fake
provider that reports, as the prompt's usage, the tiktoken count of what it
was sent. The unit tests elsewhere pin the trigger arithmetic; these pin what
a session actually does turn after turn."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse, ToolCallRequest
from durin.utils.helpers import estimate_prompt_tokens

_REPLY = "Here is a reply. " * 40
_OVERFLOW_REPLY = "Error: prompt overflow"


def _turn_text(i: int) -> str:
    return f"turn {i}: please remember fact number {i}. " + ("more context words " * 400)


def _marker(i: int) -> str:
    """What only turn *i*'s own message carries (every turn text starts with it)."""
    return f"turn {i}:"


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


async def _run_turns(
    tmp_path,
    *,
    turns: int,
    window: int,
    turn_model: str | None = None,
    agents_md: str | None = None,
    texts: list[str] | None = None,
    tool_turns: tuple[int, ...] = (),
    overflow_turns: tuple[int, ...] = (),
    session_messages: list[dict[str, Any]] | None = None,
    **loop_kwargs: Any,
) -> dict[str, Any]:
    """Run *turns* turns on one session.

    Each reply is tagged ``REPLY-<n>`` so a delivered reply can be looked up
    in the saved session. On a turn in *tool_turns* the model first lists the
    workspace, then answers. On a turn in *overflow_turns* the first run
    stops before its first call, as it does when the incoming message pushes
    a prompt at its ceiling over the budget, and the turn retries."""
    if agents_md is not None:
        # Part of every prompt's fixed part, like the system prompt itself.
        (tmp_path / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=8192)
    provider.estimate_prompt_tokens.return_value = (0, "none")
    main_prompts: list[int] = []
    # The calls made without tools (the summary, decision-log and learnings
    # calls of a compaction), as (system prompt, input) pairs.
    side_calls: list[tuple[str, str]] = []
    calls = {"n": 0}
    turn = {"i": -1}

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        calls["n"] += 1
        prompt_tokens = estimate_prompt_tokens(messages or [], tools or None)
        usage = {"prompt_tokens": max(1, prompt_tokens), "completion_tokens": 10}
        if not tools:
            side_calls.append((_text_of(messages[0]), _text_of(messages[-1])))
        if tools:
            main_prompts.append(prompt_tokens)
            if turn["i"] in tool_turns and not any(m.get("role") == "tool" for m in _this_turn(messages)):
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id=f"call_{calls['n']}", name="list_dir", arguments={"path": "."})],
                    usage=usage,
                )
        return LLMResponse(content=f"REPLY-{calls['n']} {_REPLY}", usage=usage)

    def _this_turn(messages):
        start = max(
            (idx for idx, m in enumerate(messages) if _marker(turn["i"]) in _text_of(m)),
            default=len(messages),
        )
        return messages[start:]

    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
        context_window_tokens=window, **loop_kwargs,
    )
    background: list = []

    def _schedule(coro):
        # Run the post-SAVE consolidation inline, between turns, as a quiet
        # gateway would; anything else scheduled is not part of this test.
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "maybe_consolidate_by_tokens":
            background.append(coro)
        else:
            coro.close()

    loop._schedule_background = _schedule  # type: ignore[method-assign]
    archives: list[int] = []
    real_archive = loop.consolidator.archive

    async def _archive(messages):
        archives.append(len(messages))
        return await real_archive(messages)

    loop.consolidator.archive = _archive  # type: ignore[method-assign]
    attempts: list[dict[str, Any]] = []
    real_run = loop.runner.run

    async def _run(spec):
        if turn["i"] in overflow_turns and not any(a["turn"] == turn["i"] for a in attempts):
            spec = dataclasses.replace(spec, context_block_limit=1_000)
        result = await real_run(spec)
        attempts.append({
            "turn": turn["i"], "stop_reason": result.stop_reason,
            "prompt": list(spec.initial_messages),
        })
        return result

    loop.runner.run = _run  # type: ignore[method-assign]
    session = loop.sessions.get_or_create("cli:sim")
    if session_messages is not None:
        session.messages = [dict(m) for m in session_messages]
        loop.sessions.save(session)
    replies: list[str | None] = []
    compactions: list[int] = []
    tails: list[str | None] = []
    for i in range(turns):
        turn["i"] = i
        before = len(archives)
        text = texts[i] if texts is not None else _turn_text(i)
        out = await loop.process_direct(text, session_key="cli:sim", model_preset=turn_model)
        while background:
            await background.pop(0)
        replies.append(out.content if out is not None else None)
        compactions.append(len(archives) - before)
        saved = loop.sessions.get_or_create("cli:sim").messages
        tails.append(saved[-1].get("role") if saved else None)
    return {
        "loop": loop,
        "session": loop.sessions.get_or_create("cli:sim"),
        "replies": replies,
        "compactions": compactions,
        "provider_calls": calls["n"],
        "main_prompts": main_prompts,
        "side_calls": side_calls,
        "attempts": attempts,
        "tails": tails,
    }


def _assert_turns_saved(result: dict[str, Any], *, from_index: int = 0) -> None:
    """Every reply the user got is in the saved session, the session
    alternates (no two user messages in a row, no tool result without the
    assistant message that called it) and never ends on a user message, and
    no prompt a turn started from carries that turn's own message twice."""
    messages = result["session"].messages[from_index:]
    saved = "\n".join(_text_of(m) for m in messages if m.get("role") == "assistant")
    for reply in result["replies"]:
        assert reply, result["replies"]
        if not reply.startswith(_OVERFLOW_REPLY):
            assert reply.split(" ", 1)[0] in saved, (reply[:40], [m.get("role") for m in messages])
    roles = [m.get("role") for m in messages]
    assert not any(a == b == "user" for a, b in zip(roles, roles[1:])), roles
    called: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls") or []:
            called.add(call["id"])
        if message.get("role") == "tool":
            assert message.get("tool_call_id") in called, roles
    assert result["tails"] == ["assistant"] * len(result["tails"]), result["tails"]
    for attempt in result["attempts"]:
        text = "\n".join(_text_of(m) for m in attempt["prompt"])
        assert text.count(_marker(attempt["turn"])) == 1, (attempt["turn"], attempt["stop_reason"])


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [9, 30])
async def test_a_session_at_the_ceiling_recovers_from_an_overflow(tmp_path, turns):
    """On a window whose trigger is its ceiling, one safety buffer under the
    runner's budget, the incoming message can push the prompt over the budget
    after BUILD found it under the trigger. The forced compaction that follows
    must run: the provider's earlier count said the previous prompt fit, and
    letting that veto it left the retry to overflow too, on every later turn.

    The rescued turn's reply must reach the session too. Its retry rebuilt the
    history with the turn's own message in it, already saved at BUILD, so the
    prompt carried that message twice and SAVE skipped one message too many:
    the reply was lost, the session ended on a user message, and every later
    turn merged into it and lost its reply the same way."""
    result = await _run_turns(tmp_path, turns=turns, window=40_000)

    failed = [r for r in result["replies"] if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1
    assert any(a["stop_reason"] == "mid_turn_precheck_overflow" for a in result["attempts"])
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_rescued_turn_that_used_tools_is_saved_whole(tmp_path):
    """A turn rescued by the overflow retry that calls a tool: the assistant
    message carrying the call is saved with its result, not the result alone."""
    result = await _run_turns(
        tmp_path, turns=4, window=200_000, tool_turns=(2,), overflow_turns=(2,),
    )

    assert [a["stop_reason"] for a in result["attempts"] if a["turn"] == 2] == [
        "mid_turn_precheck_overflow", "completed",
    ]
    _assert_turns_saved(result)
    saved = result["session"].messages
    turn_start = next(i for i, m in enumerate(saved) if _text_of(m).startswith(_marker(2)))
    assert [m["role"] for m in saved[turn_start:turn_start + 4]] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_a_message_too_large_to_fit_leaves_later_turns_saved(tmp_path):
    """A message that cannot fit even after the forced compaction fails the
    turn. The session keeps an assistant message saying so, rather than ending
    on the user message, so the short turns after it are answered and saved."""
    texts = [
        "turn 0: hello", "turn 1: hello",
        "turn 2: " + ("a very long paste " * 3000),
        "turn 3: short again", "turn 4: short", "turn 5: short",
    ]
    result = await _run_turns(tmp_path, turns=6, window=40_000, texts=texts)

    assert result["replies"][2].startswith(_OVERFLOW_REPLY)
    assert [r.startswith(_OVERFLOW_REPLY) for r in result["replies"][3:]] == [False] * 3
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_turn_merged_into_an_unanswered_message_keeps_its_reply(tmp_path):
    """A session that already ends on a user message (a turn saved without a
    reply by an older version): the build merges the new message into it, and
    SAVE must still save the reply, or every later turn merges the same way."""
    unanswered = [
        {"role": "user", "content": "an earlier question", "timestamp": "2026-09-30T10:00:00"},
        {"role": "assistant", "content": "an earlier answer", "timestamp": "2026-09-30T10:00:01"},
        {"role": "user", "content": "a question that got no reply", "timestamp": "2026-09-30T10:01:00"},
    ]
    result = await _run_turns(tmp_path, turns=3, window=200_000, session_messages=unanswered)

    # The two unanswered messages stay as they were; from there on it alternates.
    _assert_turns_saved(result, from_index=len(unanswered))


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [20_000, 1])
async def test_a_cap_under_the_fixed_prompt_does_not_compact_every_turn(tmp_path, cap):
    """A cap under the prompt's fixed part (here about 26,000 tokens of
    system prompt and tool schemas) compacted on every turn from the second:
    37 provider calls for 10 turns instead of 10. The cap never goes under
    the minimum, so these turns fit without a compaction."""
    result = await _run_turns(tmp_path, turns=10, window=200_000, preemptive_compact_max_tokens=cap)

    assert result["compactions"] == [0] * 10
    assert result["provider_calls"] == 10


@pytest.mark.asyncio
async def test_a_trigger_under_the_fixed_prompt_does_not_compact_every_turn(tmp_path):
    """The minimum cannot know the prompt: here a 40,000-token AGENTS.md on
    top of the system prompt and tool schemas puts its fixed part over the
    trigger (0.05 of a 1M window). No compaction can get such a prompt under
    the trigger; each one archived the turn before, and the next turn
    compacted again. After one, the session now waits until it has grown by
    a compaction's runway before trying again."""
    result = await _run_turns(
        tmp_path, turns=8, window=1_000_000, agents_md="guidance " * 40_000,
        preemptive_compact_ratio=0.05,
    )

    failed = [r for r in result["replies"] if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) <= 1, result["compactions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("words", [35_000, 36_000, 37_000])
async def test_a_fixed_prompt_just_under_the_trigger_does_not_compact_every_turn(tmp_path, words):
    """An AGENTS.md of 35,000 to 37,000 words under a 64,000 cap puts the
    fixed part just under the trigger: each compaction archived all it could
    and still left less than a turn of room, so 5 to 11 of 12 turns compacted
    (up to 45 provider calls) and the session never waited, since only a
    compaction ending over the trigger was remembered. One that leaves less
    than a quarter of a normal runway under the trigger is remembered too."""
    result = await _run_turns(
        tmp_path, turns=12, window=1_000_000, agents_md="guidance " * words,
        preemptive_compact_max_tokens=64_000,
    )

    assert sum(1 for count in result["compactions"] if count) <= 2, result["compactions"]
    assert result["provider_calls"] <= 20, result["provider_calls"]
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_small_window_filled_by_its_fixed_prompt_compacts_on_a_runway(tmp_path):
    """A 64,000-token window compacts at 48,000 (the small-window floor); a
    20,000-word AGENTS.md leaves each compaction about a turn of room under
    that, and 12 of 14 turns compacted. The next compaction now waits for a
    normal runway, which the window's ceiling cuts short here."""
    result = await _run_turns(tmp_path, turns=14, window=64_000, agents_md="guidance " * 20_000)

    assert sum(1 for count in result["compactions"] if count) <= 4, result["compactions"]
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_block_limit_keeps_a_large_window_session_under_it(tmp_path):
    """context_block_limit is the runner's whole budget: on a 1M window with a
    40,000 limit the session must compact before it reaches the limit, not
    wait for the cap's 256,000 and fail every turn past 40,000."""
    from durin.agent.runner import input_budget_tokens

    result = await _run_turns(tmp_path, turns=12, window=1_000_000, context_block_limit=40_000)

    failed = [r for r in result["replies"] if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1
    budget = input_budget_tokens(1_000_000, 8192, 40_000)
    assert max(result["main_prompts"]) <= budget


@pytest.mark.asyncio
async def test_a_turn_on_a_smaller_model_compacts_by_that_models_limits(tmp_path):
    """A cron job's or a persona's model runs the turn on its own window. On a
    loop whose model has 1M, a turn on a 45,000-token model must compact by
    the smaller window, not by the loop's 256,000 trigger: past its own
    budget every turn failed, since the forced compaction aimed at half of
    256,000."""
    from durin.agent.runner import input_budget_tokens
    from durin.config.schema import ModelPresetConfig

    presets = {
        "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
        "small": ModelPresetConfig(model="test-model", context_window_tokens=45_000),
    }
    result = await _run_turns(
        tmp_path, turns=10, window=1_000_000, turn_model="small", model_presets=presets,
    )

    failed = [r for r in result["replies"] if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1
    assert max(result["main_prompts"]) <= input_budget_tokens(45_000, 8192)


@pytest.mark.asyncio
async def test_a_turn_on_a_larger_model_is_summarized_whole(tmp_path):
    """A turn's model sizes the chunks a compaction archives, but the summary,
    the decision log and the learnings are written by the loop's own model,
    within its input budget. On a loop whose model has 32,768 tokens and turns
    on a 1M preset, a chunk several times that budget was cut down to it, and
    most of the span was never summarized. A span is now summarized in as
    many calls as that budget needs, and no call's input is cut."""
    import tiktoken

    from durin.config.schema import ModelPresetConfig
    from durin.utils.prompt_templates import render_template

    presets = {
        "default": ModelPresetConfig(model="test-model", context_window_tokens=32_768),
        "big": ModelPresetConfig(
            model="test-model", context_window_tokens=1_000_000, preemptive_compact_max_tokens=100_000,
        ),
    }
    texts = [f"turn {i}: fact {i}. " + ("more context words " * 2000) for i in range(14)]
    result = await _run_turns(
        tmp_path, turns=14, window=32_768, turn_model="big", model_presets=presets, texts=texts,
    )

    budget = result["loop"].consolidator._input_token_budget
    encoding = tiktoken.get_encoding("cl100k_base")
    for _system, text in result["side_calls"]:
        assert "... (truncated)" not in text
        assert len(encoding.encode(text)) <= budget
    archive_prompt = render_template("agent/consolidator_archive.md", strip=True)
    summarized = "\n".join(text for system, text in result["side_calls"] if system == archive_prompt)
    session = result["session"]
    archived = [m for m in session.messages[:session.last_consolidated] if m.get("content")]
    assert len(archived) > 4
    assert [m["content"] for m in archived if m["content"] not in summarized] == []
