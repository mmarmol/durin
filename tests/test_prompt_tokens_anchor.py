"""Tests for the ``usage_prompt_tokens`` anchor and the anchored
estimator path in ``estimate_prompt_tokens_chain``.

Pi-inspired: when the runner gets a real ``response.usage.prompt_tokens``
from the provider, we stamp it on the assistant message we just
persisted. ``latest_prompt_tokens_anchor`` finds the most recent stamp
and the anchored estimator uses it as the baseline — so we only need
tiktoken for messages that came AFTER the last LLM call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.utils.helpers import (
    build_assistant_message,
    estimate_prompt_tokens_chain,
    latest_prompt_tokens_anchor,
)


# ---------------------------------------------------------------------------
# build_assistant_message — stamp behaviour
# ---------------------------------------------------------------------------


def test_build_stamps_prompt_tokens_when_provided():
    msg = build_assistant_message("hi", prompt_tokens=1234)
    assert msg["usage_prompt_tokens"] == 1234


def test_build_skips_stamp_when_prompt_tokens_zero():
    """Synthetic / placeholder messages pass 0 or omit; no anchor stamp."""
    msg = build_assistant_message("hi", prompt_tokens=0)
    assert "usage_prompt_tokens" not in msg


def test_build_skips_stamp_when_prompt_tokens_none():
    msg = build_assistant_message("hi")
    assert "usage_prompt_tokens" not in msg


def test_build_coerces_prompt_tokens_to_int():
    msg = build_assistant_message("hi", prompt_tokens=42.7)
    assert msg["usage_prompt_tokens"] == 42


# ---------------------------------------------------------------------------
# latest_prompt_tokens_anchor — walk-backwards behaviour
# ---------------------------------------------------------------------------


def test_anchor_returns_none_for_empty_or_unstamped_messages():
    assert latest_prompt_tokens_anchor([]) is None
    assert latest_prompt_tokens_anchor([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]) is None


def test_anchor_finds_most_recent_stamped_message():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first reply", "usage_prompt_tokens": 100},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "second reply", "usage_prompt_tokens": 250},
        {"role": "user", "content": "third (unsent)"},  # no anchor here
    ]
    anchor = latest_prompt_tokens_anchor(msgs)
    assert anchor == (3, 250)


def test_anchor_skips_non_dict_entries():
    msgs = [
        None,
        "weird",
        {"role": "assistant", "content": "x", "usage_prompt_tokens": 50},
    ]
    assert latest_prompt_tokens_anchor(msgs) == (2, 50)


def test_anchor_ignores_zero_or_negative_stamps():
    """Stamps must be positive ints to count. Zero or negative are
    treated as missing (matches build_assistant_message's contract that
    only positive counts get persisted)."""
    msgs = [
        {"role": "assistant", "content": "earlier", "usage_prompt_tokens": 80},
        {"role": "assistant", "content": "later", "usage_prompt_tokens": 0},
    ]
    assert latest_prompt_tokens_anchor(msgs) == (0, 80)


# ---------------------------------------------------------------------------
# estimate_prompt_tokens_chain — anchored fast path
# ---------------------------------------------------------------------------


def test_estimate_uses_anchored_when_stamp_present():
    """With an anchor, the chain estimator returns
    (anchor_tokens + tail_estimate, 'anchored') and does NOT consult the
    provider counter at all."""
    provider_counter = MagicMock(return_value=(999_999, "provider_counter"))
    provider = MagicMock()
    provider.estimate_prompt_tokens = provider_counter

    msgs = [
        {"role": "user", "content": "ask"},
        {
            "role": "assistant", "content": "answer with tools",
            "usage_prompt_tokens": 5000,
        },
        {"role": "tool", "tool_call_id": "c1", "content": "tool output text"},
    ]
    tokens, source = estimate_prompt_tokens_chain(provider, "m", msgs, tools=None)

    assert source == "anchored"
    assert tokens >= 5000  # anchor baseline preserved
    assert tokens < 5500   # tail tiktoken estimate is small
    provider_counter.assert_not_called()


def test_estimate_returns_anchor_value_exact_when_tail_empty():
    """If the anchored message is the last one, the answer is the
    stamp itself — no tail to estimate."""
    msgs = [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": "last", "usage_prompt_tokens": 800},
    ]
    tokens, source = estimate_prompt_tokens_chain(MagicMock(), "m", msgs)
    assert tokens == 800
    assert source == "anchored"


def test_estimate_falls_back_to_provider_counter_without_anchor():
    """No stamped message ⇒ provider counter wins over tiktoken."""
    provider = MagicMock()
    provider.estimate_prompt_tokens = MagicMock(return_value=(777, "provider_counter"))

    tokens, source = estimate_prompt_tokens_chain(
        provider, "m",
        [{"role": "user", "content": "no anchor here"}],
    )
    assert tokens == 777
    assert source == "provider_counter"


def test_estimate_falls_back_to_tiktoken_when_no_anchor_no_provider():
    provider = MagicMock(spec=[])  # no estimate_prompt_tokens method
    tokens, source = estimate_prompt_tokens_chain(
        provider, "m",
        [{"role": "user", "content": "hello world"}],
    )
    assert source == "tiktoken"
    assert tokens > 0


def test_estimate_anchored_path_is_robust_to_messages_after_anchor():
    """Realistic flow: anchor at msg 3, then a user follow-up + a
    couple tool results appended before the next compaction check."""
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "first ask"},
        {"role": "assistant", "content": "first reply",
         "usage_prompt_tokens": 1200},
        {"role": "user", "content": "follow-up question with more text"},
        {"role": "tool", "tool_call_id": "x", "content": "tool result"},
    ]
    tokens, source = estimate_prompt_tokens_chain(MagicMock(), "m", msgs)
    assert source == "anchored"
    # Anchor (1200) plus a modest tail estimate — under 1500 is plenty
    # of room.
    assert 1200 < tokens < 1500
