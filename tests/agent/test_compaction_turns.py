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


def _window_leaving(tmp_path, room: int) -> int:
    """The window whose input budget leaves *room* tokens beside the system
    prompt and the tool definitions a session in *tmp_path* starts with.

    Measured rather than fixed: the system prompt names the workspace and
    the skills it lists by absolute path, so its size depends on where the
    tree is checked out, and a test at a window's edge would pass in one
    checkout and fail in another."""
    from durin.agent.runner import input_budget_tokens

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=8192)
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    system = loop.context.build_system_prompt(None, channel="cli")
    fixed = estimate_prompt_tokens([{"role": "system", "content": system}], loop.tools.get_definitions())
    overhead = 100_000 - input_budget_tokens(100_000, 8192)
    return fixed + room + overhead


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
    session_metadata: dict[str, Any] | None = None,
    compaction: bool = True,
    **loop_kwargs: Any,
) -> dict[str, Any]:
    """Run *turns* turns on one session.

    Each reply is tagged ``REPLY-<n>`` so a delivered reply can be looked up
    in the saved session. On a turn in *tool_turns* the model first lists the
    workspace, then answers. On a turn in *overflow_turns* the first run
    stops before its first call, as it does when the incoming message pushes
    a prompt at its ceiling over the budget, and the turn retries (the turn
    needs history before it for that: without, no compaction could help)."""
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
    if not compaction:
        # A compaction that cannot shrink the session (its lock timed out, its
        # summary failed): the rest of the turn has to cope without it.
        async def _no_compaction(*_args, **_kwargs):
            return None

        loop.consolidator.maybe_consolidate_by_tokens = _no_compaction  # type: ignore[method-assign]
    attempts: list[dict[str, Any]] = []
    real_run = loop.runner.run

    async def _run(spec):
        if turn["i"] in overflow_turns and not any(a["turn"] == turn["i"] for a in attempts):
            # A budget the request fits without its history and not with
            # it: an overflow a compaction can cure.
            fixed = loop.runner._history_free_parts(spec, list(spec.initial_messages), spec.provider)
            spec = dataclasses.replace(spec, context_block_limit=fixed[0] + 1)
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
    session.metadata.update(session_metadata or {})
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
    turn merged into it and lost its reply the same way.

    The window leaves 6,000 tokens of the input budget beside the system
    prompt and the tool definitions, measured where the test runs: a fixed
    40,000 put a checkout with longer paths over the edge."""
    result = await _run_turns(tmp_path, turns=turns, window=_window_leaving(tmp_path, 6_000))

    failed = [r for r in result["replies"] if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1
    assert any(a["stop_reason"] == "mid_turn_precheck_overflow" for a in result["attempts"])
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_small_window_session_keeps_answering_as_its_summary_grows(tmp_path):
    """The session summary is capped at 16,000 characters, about 4,700
    tokens, whatever the window. On a window whose budget leaves 6,000
    tokens beside the system prompt and the tool definitions, the summary
    grew until it, the task state and one message no longer fit: from then
    on every turn failed, each after a forced compaction that could not make
    room. A prompt now carries the summary whole only while it fits beside
    the message and the history, and a quarter of what the message leaves
    otherwise."""
    from durin.agent.runner import input_budget_tokens

    window = _window_leaving(tmp_path, 6_000)
    result = await _run_turns(tmp_path, turns=45, window=window)

    failed = [i for i, r in enumerate(result["replies"]) if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == []
    assert max(result["main_prompts"]) <= input_budget_tokens(window, 8192)
    _assert_turns_saved(result)
    summaries = [_summary_part(_text_of(a["prompt"][0])) for a in result["attempts"]]
    assert any("older parts of this summary are left out" in s for s in summaries)


@pytest.mark.asyncio
async def test_a_summary_never_crowds_out_the_turn_it_is_carried_by(tmp_path):
    """With 1,900 tokens of the input budget beside the system prompt and
    the tool definitions, a turn's message (about 1,600 tokens with its
    runtime context) fits, but not with a quarter of that room taken by the
    summary as well: from the first summary on, every turn failed even after
    a compaction dropped all of its history. The summary now gives way to
    the turn's own message."""
    window = _window_leaving(tmp_path, 1_900)
    result = await _run_turns(tmp_path, turns=6, window=window)

    failed = [i for i, r in enumerate(result["replies"]) if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == []
    assert sum(result["compactions"]) >= 1
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_decision_log_gives_way_to_the_turn_it_is_carried_by(tmp_path):
    """The decision log rides in every turn's runtime context. With 1,700
    tokens of the input budget beside the system prompt and the tool
    definitions, a turn's message fits, but not with a decision log of about
    500 tokens as well: every turn failed, however much history went. In a
    prompt the log now gives way first, its oldest entries left out."""
    from durin.session.decision_log import DECISION_LOG_KEY

    decisions = [
        {"text": f"decision {i}: " + "we chose the second approach because it is simpler " * 5,
         "ts": "", "source": "auto"}
        for i in range(10)
    ]
    window = _window_leaving(tmp_path, 1_700)
    result = await _run_turns(
        tmp_path, turns=3, window=window, session_metadata={DECISION_LOG_KEY: decisions},
    )

    failed = [i for i, r in enumerate(result["replies"]) if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == []
    message = _text_of(result["attempts"][0]["prompt"][-1])
    assert "decision 9:" in message
    assert "decision 0:" not in message
    _assert_turns_saved(result)


def _summary_part(system: str) -> str:
    start = system.find("=== ARCHIVED SUMMARY")
    return system[start:] if start >= 0 else ""


@pytest.mark.asyncio
async def test_the_system_prompt_holds_across_turns_of_different_lengths(tmp_path):
    """On a window small enough to cut the summary, the cut followed each
    message's length: the system prompt changed on nearly every turn whose
    message differed in length from the one before, though the stored summary
    had not, and every provider's prompt cache missed the whole prompt. With
    the summary unchanged, the system prompt now stays the same."""
    from durin.memory.session_summary_store import write_session_summary

    blocks = [f"- Span {i}: worked on item {i}, fixed /srv/app{i}/config.yaml. " + "detail " * 50 for i in range(60)]
    write_session_summary(tmp_path, "cli:sim", "\n\n---\n".join(blocks)[-15_900:], last_active="2026-09-30")
    sizes = [30, 400, 60, 900, 20, 300, 800, 40, 700, 100, 50, 600]
    texts = [f"turn {i}: " + "note " * n for i, n in enumerate(sizes)]
    result = await _run_turns(tmp_path, turns=len(texts), window=_window_leaving(tmp_path, 4_000), texts=texts)

    systems: dict[int, str] = {}
    for attempt in result["attempts"]:
        if attempt["stop_reason"] == "completed":
            systems[attempt["turn"]] = _text_of(attempt["prompt"][0])
    compared = changed = 0
    for i in range(1, len(texts)):
        if result["compactions"][i - 1] or result["compactions"][i] or i not in systems or i - 1 not in systems:
            continue
        compared += 1
        changed += systems[i] != systems[i - 1]
    assert "older parts of this summary are left out" in systems[0]
    assert compared >= 4, result["compactions"]
    assert changed == 0, (changed, compared)


@pytest.mark.asyncio
async def test_the_compaction_probe_carries_the_summary_the_turn_carries(tmp_path):
    """The compaction check measures the next turn's prompt with a probe
    build, which bounds the summary as the turn's build does. It left the
    tool definitions out of that room, about 20,000 tokens more than the
    turn has: on a 64,000-token window with a 25,000-word AGENTS.md the
    probe measured a summary more than twice the one the turn carried."""
    from durin.memory.session_summary_store import write_session_summary
    from durin.utils.helpers import estimate_text_tokens

    result = await _run_turns(tmp_path, turns=0, window=64_000, agents_md="guidance " * 25_000)
    loop = result["loop"]
    session = result["session"]
    session.messages = [
        {"role": "user", "content": "question " + "word " * 200, "timestamp": "2026-09-30T10:00:00"},
        {"role": "assistant", "content": "answer " + "word " * 200, "timestamp": "2026-09-30T10:00:01"},
    ]
    loop.sessions.save(session)
    blocks = [f"- Span {i}: worked on item {i}. " + "detail " * 60 for i in range(40)]
    write_session_summary(tmp_path, session.key, "\n\n---\n".join(blocks)[:15_900], last_active="2026-09-30")
    turn = loop.context.build_messages(
        history=session.get_history(max_messages=loop._max_messages, include_timestamps=True),
        current_message="[token-probe]", channel="cli", chat_id="sim",
        session_summary=loop._format_pending_summary(session), session_metadata=session.metadata,
        session_key=session.key, tools=loop.tools.get_definitions(),
        input_budget_tokens=loop._turn_input_budget(None), probe=True,
    )
    probes: list[list[dict[str, Any]]] = []
    real_build = loop.consolidator._build_messages

    def _capture(**kwargs):
        probes.append(real_build(**kwargs))
        return probes[-1]

    loop.consolidator._build_messages = _capture  # type: ignore[method-assign]
    loop.consolidator.estimate_session_prompt_tokens(session)

    turn_summary = _summary_part(_text_of(turn[0]))
    probe_summary = _summary_part(_text_of(probes[0][0]))
    assert turn_summary, "the window should cut the summary, not drop it"
    assert estimate_text_tokens(probe_summary) == estimate_text_tokens(turn_summary)


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
async def test_a_fixed_prompt_near_the_ceiling_compacts_once_a_turn_and_answers(tmp_path):
    """A 25,000-word AGENTS.md on a 64,000-token window leaves about 3,900
    tokens under the ceiling, where the wait for a runway stops: room for
    the summary and one exchange of history, not two. Every turn after the
    first few therefore has to drop the exchange before it, and compacts to
    do so; what the session must not do is fail a turn, send past the
    budget, or compact twice in one turn."""
    from durin.agent.runner import input_budget_tokens

    result = await _run_turns(tmp_path, turns=16, window=64_000, agents_md="guidance " * 25_000)

    failed = [i for i, r in enumerate(result["replies"]) if not r or r.startswith(_OVERFLOW_REPLY)]
    assert failed == []
    assert max(result["main_prompts"]) <= input_budget_tokens(64_000, 8192)
    assert max(result["compactions"]) <= 1, result["compactions"]
    _assert_turns_saved(result)


@pytest.mark.asyncio
async def test_a_fixed_part_over_the_budget_fails_at_once_and_says_what_fills_it(tmp_path):
    """An AGENTS.md of 35,000 words on a 64,000-token window puts the system
    prompt and the tool definitions alone over the input budget: no
    compaction can make any turn fit. Every turn still forced a compaction
    before failing, and failed with an error that told the user to send the
    request again, as if compacting would help."""
    from durin.agent.runner import input_budget_tokens

    result = await _run_turns(tmp_path, turns=0, window=64_000, agents_md="guidance " * 35_000)
    loop = result["loop"]
    forced: list[bool] = []
    real_check = loop.consolidator.maybe_consolidate_by_tokens

    async def _check(session, **kwargs):
        forced.append(bool(kwargs.get("force")))
        return await real_check(session, **kwargs)

    loop.consolidator.maybe_consolidate_by_tokens = _check  # type: ignore[method-assign]
    replies = [
        (await loop.process_direct(_turn_text(i), session_key="cli:sim")).content for i in range(3)
    ]

    assert True not in forced
    assert result["main_prompts"] == []
    budget = f"{input_budget_tokens(64_000, 8192):,}"
    for reply in replies:
        assert reply.startswith(_OVERFLOW_REPLY), reply
        assert "AGENTS.md" in reply and budget in reply, reply
        assert "send it again" not in reply, reply


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
async def test_a_turn_compaction_cannot_shrink_still_fits_its_budget(tmp_path):
    """A turn replaying a history over its 60,000-token block limit, with a
    compaction that cannot shrink it, stops on its first request as the loop
    wants: it compacts and retries. The retry is still over, so the trim
    drops the oldest history down to what the limit leaves beside the system
    prompt, the tool schemas and the current message, and the turn is
    answered. With room left for the system prompt alone, the retry
    overflowed too and the turn failed."""
    seed: list[dict[str, Any]] = []
    for i in range(40):
        seed += [
            {"role": "user", "content": f"question {i} " + "long words " * 1500,
             "timestamp": "2026-09-30T10:00:00"},
            {"role": "assistant", "content": f"answer {i}", "timestamp": "2026-09-30T10:00:01"},
        ]
    result = await _run_turns(
        tmp_path, turns=1, window=1_000_000, texts=["turn 0: next"], session_messages=seed,
        compaction=False, context_block_limit=60_000,
    )

    assert [a["stop_reason"] for a in result["attempts"]] == ["mid_turn_precheck_overflow", "completed"]
    assert max(result["main_prompts"]) <= 60_000
    _assert_turns_saved(result, from_index=len(seed))


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
async def test_status_and_the_footer_measure_against_the_trigger_the_turns_use(tmp_path, monkeypatch):
    """A persona whose model has a 45,000-token window runs its session's
    turns on that model, which compacts at 33,750 whatever the loop's own 1M
    model would. /status and the CLI footer measured the session against the
    loop's 256,000; /status is right before the session's first turn too."""
    import durin.cli.footer as footer
    from durin.cli.footer import build_footer_text
    from durin.config.schema import Config, ModelPresetConfig, PersonaConfig

    presets = {
        "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
        "small": ModelPresetConfig(model="test-model", context_window_tokens=45_000),
    }
    config = Config()
    config.personas["brief"] = PersonaConfig(model="small")
    result = await _run_turns(
        tmp_path, turns=0, window=1_000_000, model_presets=presets, app_config=config,
        session_metadata={"persona": "brief"},
    )
    loop = result["loop"]
    monkeypatch.setattr(loop.consolidator, "estimate_session_prompt_tokens", lambda *_a, **_k: (16_875, "test"))
    monkeypatch.setattr(footer, "_token_estimate", lambda _session: 16_875)

    status = await loop.process_direct("/status", session_key="cli:sim")
    assert "(50% to compaction)" in status.content

    await loop.process_direct("turn 0: hello", session_key="cli:sim")
    assert build_footer_text(loop, "cli", "sim")["context_pct"] == 50
    status = await loop.process_direct("/status", session_key="cli:sim")
    assert "(50% to compaction)" in status.content


@pytest.mark.asyncio
async def test_a_persona_session_is_measured_with_the_persona_soul(tmp_path):
    """A persona's turns are built with its own SOUL, but the compaction
    check measured the session with the default one. With a SOUL of about
    15,000 tokens the check under-counted every prompt by that much: no turn
    compacted at the trigger, and the prompt grew until a request overflowed
    the budget at the precheck and the turn had to compact and retry."""
    from durin.config.schema import Config, ModelPresetConfig, PersonaConfig
    from durin.souls.store import SoulStore

    presets = {
        "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
        "small": ModelPresetConfig(model="test-model", context_window_tokens=64_000),
    }
    config = Config()
    config.memory.file_watcher.enabled = False
    config.personas["long"] = PersonaConfig(soul="long", model="small")
    SoulStore(tmp_path).write("long", "You are Long. " + "rule " * 15_000)
    result = await _run_turns(
        tmp_path, turns=14, window=1_000_000, model_presets=presets, app_config=config,
        session_metadata={"persona": "long"},
    )

    assert [a["stop_reason"] for a in result["attempts"]] == ["completed"] * 14
    assert sum(result["compactions"]) >= 1
    # /status measures the session the same way: with the persona's SOUL.
    loop, session = result["loop"], result["session"]
    estimate = loop.session_prompt_estimate(session, channel="cli", chat_id="sim")
    default_soul, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    assert estimate - default_soul > 14_000, (estimate, default_soul)


@pytest.mark.asyncio
async def test_a_system_message_on_a_persona_session_runs_on_the_persona_model(tmp_path):
    """A sub-agent's result on a persona session starts a turn of its own,
    which ran on the loop's model with the default SOUL and checked
    compaction by the loop's model. The check replaced the limits the
    persona's turns recorded and forgot the fixed-prompt level they reached:
    the footer showed the loop's trigger, and the next persona turn
    compacted again at once."""
    from durin.bus.events import InboundMessage
    from durin.config.schema import Config, ModelPresetConfig, PersonaConfig
    from durin.souls.store import SoulStore

    presets = {
        "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
        "small": ModelPresetConfig(model="test-model", context_window_tokens=64_000),
    }
    config = Config()
    # Not part of this test: left running, the memory watcher re-indexes the
    # session summary after the test's event loop has closed.
    config.memory.file_watcher.enabled = False
    config.personas["brief"] = PersonaConfig(soul="terse", model="small")
    SoulStore(tmp_path).write("terse", "You are Terse: you answer in five words.")
    # A fixed part between the small model's trigger (48,000) and its
    # ceiling, so its compactions record the fixed-prompt level.
    result = await _run_turns(
        tmp_path, turns=6, window=1_000_000, model_presets=presets, app_config=config,
        session_metadata={"persona": "brief"}, agents_md="guidance " * 22_000,
    )
    loop, key = result["loop"], "cli:sim"
    consolidator = loop.consolidator
    assert key in consolidator._compaction_floor
    assert consolidator.session_trigger(key) == 48_000
    windows: list[int | None] = []
    systems: list[str] = []
    real_run = loop.runner.run

    async def _run(spec):
        windows.append(spec.context_window_tokens)
        systems.append(_text_of(spec.initial_messages[0]))
        return await real_run(spec)

    loop.runner.run = _run  # type: ignore[method-assign]
    background: list = []
    loop._schedule_background = background.append  # type: ignore[method-assign]
    await loop._process_message(InboundMessage(
        channel="system", sender_id="subagent", chat_id=key,
        content="the sub-agent found three files", metadata={"subagent_task_id": "t-1"},
    ))
    for coro in background:
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "maybe_consolidate_by_tokens":
            await coro
        else:
            coro.close()

    assert windows == [64_000]
    assert "You are Terse" in systems[0]
    assert key in consolidator._compaction_floor
    assert consolidator.session_trigger(key) == 48_000
    archived: list[int] = []
    real_archive = consolidator.archive

    async def _archive(messages):
        archived.append(len(messages))
        return await real_archive(messages)

    consolidator.archive = _archive  # type: ignore[method-assign]
    await loop.process_direct("turn 6: a short follow-up", session_key=key)
    assert archived == []


@pytest.mark.asyncio
async def test_a_system_message_turn_carries_the_summary_its_compaction_wrote(tmp_path):
    """A system message's turn read the session summary before its own
    compaction check ran. When that check archived turns, the history it
    built from no longer held them and the summary it carried did not cover
    them either: the archived turns vanished from the prompt."""
    from durin.bus.events import InboundMessage

    seed = []
    for i in range(30):
        seed += [
            {"role": "user", "content": _turn_text(i), "timestamp": "2026-09-30T10:00:00"},
            {"role": "assistant", "content": f"reply {i}", "timestamp": "2026-09-30T10:00:01"},
        ]
    result = await _run_turns(tmp_path, turns=0, window=60_000, session_messages=seed)
    loop, key = result["loop"], "cli:sim"
    systems: list[str] = []
    real_run = loop.runner.run

    async def _run(spec):
        systems.append(_text_of(spec.initial_messages[0]))
        return await real_run(spec)

    loop.runner.run = _run  # type: ignore[method-assign]
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]
    await loop._process_message(InboundMessage(
        channel="system", sender_id="subagent", chat_id=key,
        content="the sub-agent found three files", metadata={"subagent_task_id": "t-1"},
    ))

    assert loop.sessions.get_or_create(key).last_consolidated > 0
    assert "ARCHIVED SUMMARY" in systems[0]


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


@pytest.mark.asyncio
async def test_a_failed_exchange_is_not_summarized(tmp_path):
    """A turn that failed (its prompt overflowed, or the model call failed)
    leaves the user's message and a placeholder in the session. The
    placeholder says nothing worth summarizing, so compaction leaves it out;
    the user's message is what they asked, which a later turn may retry, so
    it is summarized like any other."""
    from durin.agent.runner import (
        _PERSISTED_MODEL_ERROR_PLACEHOLDER,
        _PERSISTED_OVERFLOW_PLACEHOLDER,
    )
    from durin.utils.prompt_templates import render_template

    def _failed(i: int, placeholder: str) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": f"FAILED-{i} " + "more context words " * 400,
             "timestamp": "2026-09-30T10:00:00"},
            {"role": "assistant", "content": placeholder, "timestamp": "2026-09-30T10:00:01"},
        ]

    seed = _failed(0, _PERSISTED_OVERFLOW_PLACEHOLDER) + _failed(1, _PERSISTED_MODEL_ERROR_PLACEHOLDER)
    for i in range(2, 30):
        seed += [
            {"role": "user", "content": _turn_text(i), "timestamp": "2026-09-30T10:00:00"},
            {"role": "assistant", "content": f"reply {i}", "timestamp": "2026-09-30T10:00:01"},
        ]
        if i in (10, 11):
            seed += _failed(i, _PERSISTED_OVERFLOW_PLACEHOLDER)
    result = await _run_turns(tmp_path, turns=1, window=60_000, session_messages=seed)

    archive_prompt = render_template("agent/consolidator_archive.md", strip=True)
    summarized = "\n".join(text for system, text in result["side_calls"] if system == archive_prompt)
    assert result["session"].last_consolidated > 30
    assert "turn 2:" in summarized
    assert "FAILED-0 " in summarized and "FAILED-10 " in summarized
    assert "[Turn stopped" not in summarized
    assert "[Assistant reply unavailable" not in summarized


@pytest.mark.asyncio
async def test_a_request_retried_after_a_model_error_is_summarized(tmp_path):
    """The model call fails, the user answers "try again" and the retry
    succeeds. Compaction summarized the span without the user's original
    message, which it dropped with the failure placeholder: the summary had
    "try again" and the answer, and never the request it answered."""
    from durin.utils.prompt_templates import render_template

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=8192)
    provider.estimate_prompt_tokens.return_value = (0, "none")
    calls = {"main": 0}
    summarized: list[str] = []
    archive_prompt = render_template("agent/consolidator_archive.md", strip=True)

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        if not tools:
            if _text_of(messages[0]) == archive_prompt:
                summarized.append(_text_of(messages[-1]))
            return LLMResponse(content="- summary bullet", usage={"prompt_tokens": 10, "completion_tokens": 5})
        calls["main"] += 1
        if calls["main"] == 1:
            return LLMResponse(content="Error calling LLM: 503 overloaded", finish_reason="error", usage={})
        return LLMResponse(content="Done: the billing service is configured.",
                           usage={"prompt_tokens": 100, "completion_tokens": 5})

    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
                     context_window_tokens=200_000)
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]
    request = "Set up the new billing service: blue theme, port 8443, vault path secret/billing/prod."
    await loop.process_direct(request, session_key="cli:retry")
    await loop.process_direct("try again", session_key="cli:retry")
    await loop.process_direct("/compact", session_key="cli:retry")

    text = "\n".join(summarized)
    assert "port 8443, vault path secret/billing/prod" in text
    assert "try again" in text
    assert "[Assistant reply unavailable" not in text
