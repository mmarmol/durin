"""Compaction over real turns: ``process_direct`` runs BUILD's consolidation,
the runner, SAVE and the background consolidation after it, against a fake
provider that reports, as the prompt's usage, the tiktoken count of what it
was sent. The unit tests elsewhere pin the trigger arithmetic; these pin what
a session actually does turn after turn."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse
from durin.utils.helpers import estimate_prompt_tokens

_REPLY = "Here is a reply. " * 40


def _turn_text(i: int) -> str:
    return f"turn {i}: please remember fact number {i}. " + ("more context words " * 400)


async def _run_turns(
    tmp_path, *, turns: int, window: int, turn_model: str | None = None, **loop_kwargs: Any,
) -> dict[str, Any]:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=8192)
    provider.estimate_prompt_tokens.return_value = (0, "none")
    main_prompts: list[int] = []
    calls = {"n": 0}

    async def _chat(*_args, messages=None, tools=None, **_kwargs):
        calls["n"] += 1
        prompt_tokens = estimate_prompt_tokens(messages or [], tools or None)
        if tools:
            main_prompts.append(prompt_tokens)
        return LLMResponse(
            content=_REPLY, usage={"prompt_tokens": max(1, prompt_tokens), "completion_tokens": 10},
        )

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
    replies: list[str | None] = []
    compactions: list[int] = []
    for i in range(turns):
        before = len(archives)
        out = await loop.process_direct(_turn_text(i), session_key="cli:sim", model_preset=turn_model)
        while background:
            await background.pop(0)
        replies.append(out.content if out is not None else None)
        compactions.append(len(archives) - before)
    return {
        "loop": loop,
        "replies": replies,
        "compactions": compactions,
        "provider_calls": calls["n"],
        "main_prompts": main_prompts,
    }


@pytest.mark.asyncio
async def test_a_session_at_the_ceiling_recovers_from_an_overflow(tmp_path):
    """On a window whose trigger is its ceiling, one safety buffer under the
    runner's budget, the incoming message can push the prompt over the budget
    after BUILD found it under the trigger. The forced compaction that follows
    must run: the provider's earlier count said the previous prompt fit, and
    letting that veto it left the retry to overflow too, on every later turn."""
    result = await _run_turns(tmp_path, turns=9, window=40_000)

    failed = [r for r in result["replies"] if not r or "prompt overflow" in r]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1


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
async def test_a_block_limit_keeps_a_large_window_session_under_it(tmp_path):
    """context_block_limit is the runner's whole budget: on a 1M window with a
    40,000 limit the session must compact before it reaches the limit, not
    wait for the cap's 256,000 and fail every turn past 40,000."""
    from durin.agent.runner import input_budget_tokens

    result = await _run_turns(tmp_path, turns=12, window=1_000_000, context_block_limit=40_000)

    failed = [r for r in result["replies"] if not r or "prompt overflow" in r]
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

    failed = [r for r in result["replies"] if not r or "prompt overflow" in r]
    assert failed == [], result["replies"]
    assert sum(result["compactions"]) >= 1
    assert max(result["main_prompts"]) <= input_budget_tokens(45_000, 8192)
