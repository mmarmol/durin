"""A span compaction archives reaches the summarizer whole: cut at message
boundaries into calls that each fit the summarizing model's input budget,
in order, none of it left out, and only a single message larger than that
budget cut.

Each seed draws a span (messages of a few tokens to twice the budget, of
every role, some empty, some failure placeholders, some spelling tiktoken's
special tokens) and a summarizer budget, then checks the pieces
``Consolidator._summarizer_pieces`` cuts and the text every summarizing call
of ``Consolidator.archive_pieces`` received. ``DURIN_INVARIANT_SEEDS`` raises
how many seeds run to a tenth of its value."""

from __future__ import annotations

import os
import random
from types import SimpleNamespace

import pytest
import tiktoken

from durin.agent.memory import Consolidator, MemoryStore
from durin.providers.base import GenerationSettings, LLMResponse
from durin.session.manager import SessionManager
from durin.utils.runtime import (
    MODEL_ERROR_PLACEHOLDER,
    OVERFLOW_PLACEHOLDER,
    without_failure_placeholders,
)

_SEEDS = max(40, int(os.environ.get("DURIN_INVARIANT_SEEDS") or 0) // 10)
_SPECIAL = ("<|endoftext|>", "<|fim_prefix|>", "<|fim_middle|>", "<|fim_suffix|>", "<|endofprompt|>")
_TRUNCATED = "... (truncated)"
_WORDS = "deploy the service on port 8443 and keep the vault path in the config file".split()
_ENCODING = tiktoken.get_encoding("cl100k_base")


def _count(text: str) -> int:
    return len(_ENCODING.encode(text, disallowed_special=()))


def _text(rng: random.Random, tokens: int, n: int) -> str:
    words = [f"m{n}"] + [rng.choice(_WORDS) for _ in range(max(0, tokens - 1))]
    if rng.random() < 0.2:
        words.insert(rng.randrange(len(words) + 1), rng.choice(_SPECIAL))
    return " ".join(words)


def _span(rng: random.Random, budget: int) -> list[dict]:
    messages = []
    for n in range(rng.randint(1, 40)):
        roll = rng.random()
        if roll < 0.05:
            messages.append({"role": "assistant", "content": rng.choice((MODEL_ERROR_PLACEHOLDER, OVERFLOW_PLACEHOLDER))})
            continue
        if roll < 0.08:
            messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}"}]})
            continue
        size = rng.choice((rng.randint(1, 40), rng.randint(40, max(41, budget // 3)), rng.randint(budget // 2, 2 * budget)))
        role = rng.choice(("user", "assistant", "tool"))
        messages.append({"role": role, "content": _text(rng, size, n), "timestamp": f"2026-09-30T10:{n % 60:02d}:00"})
    return messages


def _consolidator(tmp_path, budget: int, inputs: list[str]) -> Consolidator:
    async def chat_with_retry(**kwargs):
        inputs.append(kwargs["messages"][-1]["content"])
        return LLMResponse(content=f"- summary {len(inputs)}", usage={})

    provider = SimpleNamespace(chat_with_retry=chat_with_retry, generation=GenerationSettings(max_tokens=1_024))
    consolidator = Consolidator(
        store=MemoryStore(tmp_path), provider=provider, model="summarizer", sessions=SessionManager(tmp_path),
        # The input budget is the window less the output ceiling and one
        # safety buffer.
        context_window_tokens=budget + 1_024 + Consolidator._SAFETY_BUFFER,
        build_messages=lambda **_kwargs: [], get_tool_definitions=lambda: [], max_completion_tokens=1_024,
    )
    assert consolidator._input_token_budget == budget
    return consolidator


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(_SEEDS))
async def test_a_span_reaches_the_summarizer_whole_and_in_order(tmp_path, seed):
    rng = random.Random(seed)
    budget = rng.choice((300, 800, 2_000, 6_000, 20_000))
    span = _span(rng, budget)
    inputs: list[str] = []
    consolidator = _consolidator(tmp_path, budget, inputs)
    context = f"seed {seed}, budget {budget}, {len(span)} messages"

    pieces = consolidator._summarizer_pieces(span)
    # In order, covering the whole span, none empty.
    assert [m for piece in pieces for m in piece] == span, context
    assert all(pieces), context
    # A span that fits is one call.
    if _count(MemoryStore._format_messages(span)) <= budget:
        assert len(pieces) == 1, context
    # Every piece fits, but one of a single message larger than the budget.
    for piece in pieces:
        if len(piece) > 1:
            assert _count(MemoryStore._format_messages(piece)) <= budget, context

    await consolidator.archive_pieces(span)
    kept = without_failure_placeholders(span)
    for message in kept:
        line = MemoryStore._format_messages([message])
        if not line:
            continue
        whole = any(line in text for text in inputs)
        # Only a message no call can take whole may be cut, and then alone.
        if not whole:
            assert _count(line) > budget, f"{context}: {message['content'][:30]!r} was left out or cut"
    for text in inputs:
        if text.endswith(_TRUNCATED):
            assert text.count("\n[") == 0, f"{context}: a call of several messages was cut"
    # The placeholders of failed turns are left out.
    assert not any(MODEL_ERROR_PLACEHOLDER in t or OVERFLOW_PLACEHOLDER in t for t in inputs), context


@pytest.mark.parametrize("seed", range(4))
def test_a_span_that_fills_its_budget_exactly_is_one_call(tmp_path, seed):
    """The random spans above rarely land on their budget; this one is sized
    to it: its text is exactly as many tokens as one call takes."""
    rng = random.Random(seed)
    span = [m for m in _span(rng, 2_000) if m.get("content")][:8]
    budget = _count(MemoryStore._format_messages(span))
    consolidator = _consolidator(tmp_path, budget, [])

    calls = len(consolidator._summarizer_pieces(span))
    assert calls == 1, f"seed {seed}: {len(span)} messages of {budget} tokens in {calls} calls"
