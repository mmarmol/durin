"""Pre-emptive compaction trigger (OpenClaw-inspired Tier 2 A1).

Old behaviour: consolidator only fires when estimated_tokens exceeded the
input budget (``context_window - max_completion - safety``) — i.e. we
waited for the context wall before doing anything. Many turns shipped
huge prompts up to ~93% of the window before the first compaction.

New behaviour: consolidator fires when estimated_tokens exceeds
``preemptive_compact_ratio * context_window`` (default 0.5). Per-preset:
a 1M-window model wants ~0.15 (compact at 150K — paying for every token
shipped, you don't want to wait until 500K). Configured in
``ModelPresetConfig.preemptive_compact_ratio`` (per-model) or
``AgentDefaults.preemptive_compact_ratio`` (fallback). The
``consolidation_ratio`` now means "fraction of trigger threshold to
keep after compaction" so each round does substantial work.

Two later corrections are covered here as well:

* The trigger is clamped by ``_preemptive_ceiling``, which reserves only a
  *capped* slice of the window for output. Clamping against the full
  completion ceiling instead made the ratio inert above a model-dependent
  fraction (a 131K ``max_tokens`` on a 231K window pinned every ratio above
  ~0.43 to the same trigger).
* Below ``_SMALL_CTX_WINDOW_LIMIT`` the ratio is floored (raise-only), because
  the incompressible part of a prompt is a large fraction of a small window
  and a low ratio leaves no runway between the post-compaction floor and the
  next trigger.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import durin.agent.memory as memory_module
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import AgentDefaults, ModelPresetConfig
from durin.providers.base import GenerationSettings, LLMResponse


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    monkeypatch.setattr(memory_module, "current_telemetry", lambda: sink)


def _make_loop(
    tmp_path,
    *,
    context_window_tokens: int,
    consolidation_ratio: float = 0.5,
    preemptive_compact_ratio: float = 0.5,
    **loop_kwargs,
) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    _resp = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_resp)
    provider.chat_stream_with_retry = AsyncMock(return_value=_resp)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
        consolidation_ratio=consolidation_ratio,
        preemptive_compact_ratio=preemptive_compact_ratio,
        **loop_kwargs,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0
    return loop


def _session_with_messages(loop: AgentLoop, count: int):
    session = loop.sessions.get_or_create("cli:test")
    session.messages = []
    for i in range(count):
        session.messages.append({"role": "user", "content": f"u{i}"})
        session.messages.append({"role": "assistant", "content": f"a{i}"})
    loop.sessions.save(session)
    return session


def _stub_consolidator(
    *,
    window: int,
    max_completion: int,
    safety: int,
    ratio: float,
    cap: int | None = 256_000,
    block_limit: int | None = None,
):
    """Build a barebones Consolidator without the full LLM provider plumbing.

    ``cap`` defaults to the shipped absolute cap, so a stub triggers where a
    real consolidator would; pass ``None`` for the ratio alone."""
    from durin.agent.memory import Consolidator
    c = Consolidator.__new__(Consolidator)
    c.context_window_tokens = window
    c.max_completion_tokens = max_completion
    c.preemptive_compact_ratio = ratio
    c.preemptive_compact_max_tokens = cap
    c.context_block_limit = block_limit
    # ``_SAFETY_BUFFER`` is a class constant on the real class; set as
    # instance attribute here for direct override.
    c._SAFETY_BUFFER = safety
    # Patch the property's class attribute by assigning to a fresh class.
    return c


def test_threshold_property_uses_ratio(monkeypatch):
    """``_preemptive_trigger_tokens`` = ``window * ratio``, clamped by
    the input token budget so a misconfigured 0.99 ratio doesn't disable
    the hard ceiling."""
    from durin.agent.memory import Consolidator
    c = _stub_consolidator(window=1_000_000, max_completion=8192, safety=1024, ratio=0.15)
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 1024, raising=False)
    assert c._preemptive_trigger_tokens == 150_000


def test_threshold_clamped_by_preemptive_ceiling(monkeypatch):
    """A 0.99 ratio on a small window still respects the ceiling."""
    from durin.agent.memory import Consolidator
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 100, raising=False)
    c = _stub_consolidator(window=1000, max_completion=200, safety=100, ratio=0.99)
    # window * 0.99 = 990, but ceiling = 1000 - 200 - 2*100 = 600.
    assert c._preemptive_ceiling == 600
    assert c._preemptive_trigger_tokens == 600


def test_ceiling_ignores_an_oversized_completion_ceiling():
    """The bug this replaced: a completion ceiling near the window size made
    every ratio above a model-dependent fraction produce the same trigger."""
    big = _stub_consolidator(window=231_072, max_completion=131_072, safety=1024, ratio=0.5)
    small = _stub_consolidator(window=231_072, max_completion=32_768, safety=1024, ratio=0.5)
    # Both reserve the capped 32,768 — the 131,072 ceiling no longer bites.
    assert big._preemptive_ceiling == small._preemptive_ceiling == 231_072 - 32_768 - 2048
    # And the ratio is live: raising it moves the trigger.
    big.preemptive_compact_ratio = 0.85
    assert big._preemptive_trigger_tokens > small._preemptive_trigger_tokens


def test_ceiling_stays_strictly_under_the_runner_input_budget():
    """Loop invariant: a consolidation that lands at the ceiling still fits the
    runner, which is what lets an iteration-0 overflow mean "consolidation
    failed" rather than "the trigger was set too high"."""
    from durin.agent.runner import _MAX_OUTPUT_RESERVATION, input_budget_tokens

    for window, max_completion in (
        (231_072, 131_072), (200_000, 8192), (1_000_000, 65_536), (32_000, 4096),
    ):
        # The absolute cap only lowers the trigger, so the invariant holds with
        # it and without it — including a cap larger than the window itself.
        # A context_block_limit IS the runner's whole budget when set, below
        # the window's or above it.
        for cap in (None, 256_000, 10_000_000):
            for block_limit in (None, 20_000, 100_000, 2_000_000):
                c = _stub_consolidator(
                    window=window, max_completion=max_completion, safety=1024, ratio=0.99,
                    cap=cap, block_limit=block_limit,
                )
                runner_budget = input_budget_tokens(window, max_completion, block_limit)
                case = (window, max_completion, cap, block_limit)
                assert c._preemptive_ceiling < runner_budget, case
                assert c._preemptive_trigger_tokens < runner_budget, case
                # What the summarizing call may be handed never exceeds what
                # a run may send either.
                assert c._input_token_budget <= runner_budget, case
    assert _MAX_OUTPUT_RESERVATION == 32_768


def test_small_window_ratio_floor_is_raise_only():
    """Below the small-context limit the ratio is floored; an explicitly higher
    ratio is honoured, and large windows are untouched."""
    from durin.agent.memory import Consolidator

    small = _stub_consolidator(window=231_072, max_completion=8192, safety=1024, ratio=0.5)
    assert small._effective_compact_ratio == Consolidator._SMALL_CTX_MIN_RATIO

    small.preemptive_compact_ratio = 0.9
    assert small._effective_compact_ratio == 0.9  # raise-only, never lowered

    large = _stub_consolidator(
        window=Consolidator._SMALL_CTX_WINDOW_LIMIT, max_completion=8192, safety=1024, ratio=0.15,
    )
    assert large._effective_compact_ratio == 0.15


def test_threshold_falls_back_to_ceiling_on_invalid_ratio(monkeypatch):
    """Garbage ratio (0, negative, non-numeric) → ceiling-only trigger."""
    from durin.agent.memory import Consolidator
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 100, raising=False)

    c = _stub_consolidator(window=1000, max_completion=200, safety=100, ratio=0)
    assert c._preemptive_trigger_tokens == 600

    c.preemptive_compact_ratio = -0.5
    assert c._preemptive_trigger_tokens == 600

    c.preemptive_compact_ratio = "nonsense"  # type: ignore[assignment]
    assert c._preemptive_trigger_tokens == 600

    # The absolute cap does not depend on the ratio: it still bounds the
    # ceiling-only trigger.
    c.preemptive_compact_max_tokens = 500
    assert c._preemptive_trigger() == (500, "cap")


# ===========================================================================
# Absolute cap: the trigger is the smallest of the ratio's trigger, the cap
# and the ceiling. With the ratio alone, the default 0.5 on a 1M window fires
# only at 500K, so every long turn ships up to half a million tokens.
# ===========================================================================


def test_cap_bounds_a_million_token_window():
    c = _stub_consolidator(window=1_000_000, max_completion=8192, safety=1024, ratio=0.5)
    assert c._preemptive_trigger() == (256_000, "cap")
    assert c._preemptive_trigger_tokens == 256_000


def test_a_null_cap_leaves_the_ratio_alone():
    c = _stub_consolidator(
        window=1_000_000, max_completion=8192, safety=1024, ratio=0.5, cap=None,
    )
    assert c._preemptive_trigger() == (500_000, "ratio")


def test_the_cap_leaves_a_small_window_on_its_floor():
    """0.75 of 200K is already under the cap: nothing changes there."""
    c = _stub_consolidator(window=200_000, max_completion=8192, safety=1024, ratio=0.5)
    assert c._preemptive_trigger() == (150_000, "floor")


def test_the_cap_applies_after_the_small_window_floor():
    """400K is under the small-window limit, so the floor raises 0.5 to 0.75:
    300,000, which the cap then lowers."""
    c = _stub_consolidator(window=400_000, max_completion=8192, safety=1024, ratio=0.5)
    assert c._effective_compact_ratio == 0.75
    assert c._preemptive_trigger() == (256_000, "cap")


def test_a_configured_ratio_under_the_cap_is_kept():
    """A 1M preset that asked for 0.15 compacts at 150K, below the cap."""
    c = _stub_consolidator(window=1_000_000, max_completion=8192, safety=1024, ratio=0.15)
    assert c._preemptive_trigger() == (150_000, "ratio")


def test_the_ceiling_still_wins_when_it_is_the_smallest():
    """260K window: ceiling 260,000 - 8,192 - 2,048 = 249,760, under both the
    cap and 0.99 x 260,000 = 257,400."""
    c = _stub_consolidator(window=260_000, max_completion=8192, safety=1024, ratio=0.99)
    assert c._preemptive_ceiling == 249_760
    assert c._preemptive_trigger() == (249_760, "ceiling")


def test_a_block_limit_holds_the_trigger_under_the_runners_budget():
    """context_block_limit IS the runner's input budget when set: on a 1M
    window with a 100,000 limit the ratio's 500,000 and the cap's 256,000
    would both sit far above what a run may send."""
    from durin.agent.runner import input_budget_tokens

    c = _stub_consolidator(
        window=1_000_000, max_completion=8192, safety=1024, ratio=0.5, block_limit=100_000,
    )
    runner_budget = input_budget_tokens(1_000_000, 8192, 100_000)
    assert runner_budget == 100_000
    assert c._preemptive_ceiling == 100_000 - 1024
    assert c._preemptive_trigger() == (98_976, "block_limit")
    assert c._input_token_budget == 98_976


def test_a_block_limit_above_the_window_budget_changes_nothing():
    unlimited = _stub_consolidator(window=1_000_000, max_completion=8192, safety=1024, ratio=0.5)
    loose = _stub_consolidator(
        window=1_000_000, max_completion=8192, safety=1024, ratio=0.5, block_limit=2_000_000,
    )
    assert unlimited._preemptive_ceiling == loose._preemptive_ceiling == 1_000_000 - 8192 - 2048
    assert unlimited._preemptive_trigger() == loose._preemptive_trigger() == (256_000, "cap")
    assert unlimited._input_token_budget == loose._input_token_budget == 1_000_000 - 8192 - 1024


def test_the_loop_hands_its_block_limit_to_the_consolidator(tmp_path):
    from durin.agent.runner import input_budget_tokens, provider_max_output

    loop = _make_loop(tmp_path, context_window_tokens=1_000_000, context_block_limit=100_000)
    c = loop.consolidator
    # _make_loop zeroes the buffer for its tiny windows; this test needs the
    # real one, which is what keeps the trigger under the runner's budget.
    vars(c).pop("_SAFETY_BUFFER", None)
    assert c.context_block_limit == 100_000
    runner_budget = input_budget_tokens(
        loop.context_window_tokens, provider_max_output(loop.provider), loop.context_block_limit,
    )
    trigger, bound = c._preemptive_trigger()
    assert bound == "block_limit"
    assert trigger < 100_000
    assert trigger < runner_budget


def test_history_replay_stays_within_the_runners_budget(tmp_path):
    from durin.agent.runner import input_budget_tokens, provider_max_output

    limited = _make_loop(tmp_path, context_window_tokens=1_000_000, context_block_limit=100_000)
    runner_budget = input_budget_tokens(
        limited.context_window_tokens, provider_max_output(limited.provider), limited.context_block_limit,
    )
    assert limited._replay_token_budget() <= runner_budget

    # Unset, the replay budget is the window's input budget, as before.
    unlimited = _make_loop(tmp_path, context_window_tokens=1_000_000)
    assert unlimited._replay_token_budget() == input_budget_tokens(
        1_000_000, provider_max_output(unlimited.provider),
    )


def test_a_cap_above_the_window_changes_nothing():
    c = _stub_consolidator(
        window=1_000_000, max_completion=8192, safety=1024, ratio=0.5, cap=2_000_000,
    )
    assert c._preemptive_trigger() == (500_000, "ratio")


@pytest.mark.asyncio
async def test_preemptive_trigger_fires_below_input_budget(tmp_path, monkeypatch):
    """On a 200-token window the small-context floor raises the configured 0.5
    to 0.75, so the trigger is 150. estimated=150 fires consolidation — old
    behaviour would have waited for >200."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(
        tmp_path,
        context_window_tokens=200,
        preemptive_compact_ratio=0.5,
        consolidation_ratio=0.5,
    )
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=10)

    estimates = [150, 40]  # 150 >= trigger(150); after archive, 40 <= target(75)
    def mock_estimate(_session, *, session_summary=None):
        return (estimates.pop(0), "test")
    loop.consolidator.estimate_session_prompt_tokens = mock_estimate
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 1

    preempt_events = [e for e in telemetry.events if e[0] == "compaction.preemptive_trigger"]
    assert len(preempt_events) == 1
    payload = preempt_events[0][1]
    assert payload["trigger_tokens"] == 150
    assert payload["estimated_tokens"] == 150
    assert payload["ratio"] == 0.75
    # The floor set this trigger; the default cap was in force but far above it.
    assert payload["trigger_bound"] == "floor"
    assert payload["cap_tokens"] == 256_000

    done = [e for e in telemetry.events if e[0] == "compaction.completed"]
    assert len(done) == 1
    assert done[0][1]["rounds"] == 1
    assert done[0][1]["exit_reason"] == "target_reached"
    assert done[0][1]["estimated_before"] == 150
    assert done[0][1]["estimated_after"] == 40
    assert done[0][1]["trigger_bound"] == "floor"
    assert done[0][1]["cap_tokens"] == 256_000


@pytest.mark.asyncio
async def test_compaction_events_name_the_cap_when_it_sets_the_trigger(tmp_path, monkeypatch):
    """A 1M window at the default ratio: the cap, not the ratio, decides where
    compaction fires, and every event that reports the trigger says so."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(tmp_path, context_window_tokens=1_000_000)
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=10)

    # 300K is under the ratio's 500K but over the cap: it must compact.
    estimates = [300_000, 100_000]
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_k: (estimates.pop(0), "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100_000)

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 1

    (preempt,) = [e[1] for e in telemetry.events if e[0] == "compaction.preemptive_trigger"]
    assert preempt["trigger_tokens"] == 256_000
    assert preempt["trigger_bound"] == "cap"
    assert preempt["cap_tokens"] == 256_000
    assert preempt["ratio"] == 0.5
    (done,) = [e[1] for e in telemetry.events if e[0] == "compaction.completed"]
    assert done["trigger_tokens"] == 256_000
    assert done["target_tokens"] == 128_000
    assert done["trigger_bound"] == "cap"
    assert done["cap_tokens"] == 256_000


@pytest.mark.asyncio
async def test_compaction_events_report_the_numbers_the_trigger_came_from(tmp_path, monkeypatch):
    """A preset switch while a compaction awaits its summary must not pair the
    trigger with the next preset's cap, window or ceiling."""
    import asyncio

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)
    loop = _make_loop(tmp_path, context_window_tokens=1_000_000)
    c = loop.consolidator
    session = _session_with_messages(loop, count=5)
    estimates = iter([300_000, 100_000])
    c.estimate_session_prompt_tokens = lambda _s, **_k: (next(estimates), "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100_000)
    started, release = asyncio.Event(), asyncio.Event()

    async def _slow_archive(_messages):
        started.set()
        await release.wait()
        return "summary", {"entities": [], "topics": []}

    c.archive = _slow_archive
    task = asyncio.create_task(c.maybe_consolidate_by_tokens(session))
    await started.wait()
    c.set_provider(
        loop.provider, "roomy", 2_000_000,
        preemptive_compact_ratio=0.3, preemptive_compact_max_tokens=450_000,
    )
    release.set()
    await task

    (preempt,) = [e[1] for e in telemetry.events if e[0] == "compaction.preemptive_trigger"]
    (done,) = [e[1] for e in telemetry.events if e[0] == "compaction.completed"]
    for event in (preempt, done):
        assert event["trigger_tokens"] == 256_000
        assert event["trigger_bound"] == "cap"
        assert event["cap_tokens"] == 256_000
        assert event["context_window_tokens"] == 1_000_000
    assert preempt["ratio"] == 0.5
    assert preempt["budget_tokens"] == 1_000_000


@pytest.mark.asyncio
async def test_a_disabled_cap_is_reported_as_null(tmp_path, monkeypatch):
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(
        tmp_path, context_window_tokens=1_000_000, preemptive_compact_max_tokens=None,
    )
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=10)

    # Over the cap that is no longer there, under the ratio: nothing to do.
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_k: (300_000, "test")
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 0

    estimates = [520_000, 100_000]
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_k: (estimates.pop(0), "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100_000)
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 1

    (done,) = [e[1] for e in telemetry.events if e[0] == "compaction.completed"]
    assert done["trigger_tokens"] == 500_000
    assert done["trigger_bound"] == "ratio"
    assert done["cap_tokens"] is None


@pytest.mark.asyncio
async def test_below_trigger_skips_consolidation(tmp_path, monkeypatch):
    """estimated < trigger → no archive, no telemetry."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(
        tmp_path,
        context_window_tokens=200,
        preemptive_compact_ratio=0.5,  # trigger = 100
    )
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=5)

    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (80, "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 0
    assert [e for e in telemetry.events if e[0] == "compaction.preemptive_trigger"] == []


def test_per_preset_ratio_field_default_is_none():
    """ModelPresetConfig.preemptive_compact_ratio defaults to None — preset
    inherits from AgentDefaults.preemptive_compact_ratio when unset."""
    preset = ModelPresetConfig(model="some-model")
    assert preset.preemptive_compact_ratio is None


def test_per_preset_ratio_field_accepts_override():
    preset = ModelPresetConfig(model="big", preemptiveCompactRatio=0.15)
    assert preset.preemptive_compact_ratio == 0.15
    preset2 = ModelPresetConfig(model="big", preemptive_compact_ratio=0.2)
    assert preset2.preemptive_compact_ratio == 0.2


def test_agent_defaults_ratio_default_is_half():
    defaults = AgentDefaults()
    assert defaults.preemptive_compact_ratio == 0.5


def test_agent_defaults_ratio_validation_range():
    """Below 0.05 or above 0.99 is rejected — protects against accidental
    values like 0 (would disable trigger) or 1.0 (==budget = old broken
    behaviour)."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AgentDefaults(preemptive_compact_ratio=0.0)
    with pytest.raises(ValidationError):
        AgentDefaults(preemptive_compact_ratio=1.0)


def test_set_provider_applies_per_preset_ratio(tmp_path):
    """Switching presets through set_provider with a new ratio updates the
    consolidator's threshold for future turns."""
    loop = _make_loop(tmp_path, context_window_tokens=200, preemptive_compact_ratio=0.5)
    assert loop.consolidator.preemptive_compact_ratio == 0.5

    loop.consolidator.set_provider(
        loop.provider, "new-model", 1_000_000,
        preemptive_compact_ratio=0.15,
    )
    assert loop.consolidator.preemptive_compact_ratio == 0.15
    assert loop.consolidator.context_window_tokens == 1_000_000


def test_set_provider_without_a_ratio_uses_the_default_one(tmp_path):
    """set_provider with preemptive_compact_ratio=None — a preset that sets no
    ratio — runs with the loop's default (agents.defaults') ratio."""
    loop = _make_loop(tmp_path, context_window_tokens=200, preemptive_compact_ratio=0.3)
    loop.consolidator.set_provider(loop.provider, "x", 500)
    assert loop.consolidator.preemptive_compact_ratio == 0.3


def test_a_presets_ratio_does_not_stick_after_switching_away(tmp_path):
    """Regression: after a preset at 0.15, a preset that sets no ratio kept
    compacting at 0.15 instead of taking agents.defaults' ratio back."""
    loop = _make_loop(
        tmp_path,
        context_window_tokens=1_000_000,
        preemptive_compact_max_tokens=None,
        model_presets={
            "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
            "frugal": ModelPresetConfig(
                model="test-model",
                context_window_tokens=1_000_000,
                preemptive_compact_ratio=0.15,
            ),
        },
    )
    c = loop.consolidator
    loop.set_model_preset("frugal", publish_update=False)
    assert c._preemptive_trigger() == (150_000, "ratio")

    loop.set_model_preset("default", publish_update=False)
    assert c.preemptive_compact_ratio == 0.5
    assert c._preemptive_trigger() == (500_000, "ratio")


def test_agent_defaults_cap_is_256k_and_null_disables_it():
    assert AgentDefaults().preemptive_compact_max_tokens == 256_000
    assert AgentDefaults(preemptive_compact_max_tokens=None).preemptive_compact_max_tokens is None
    assert AgentDefaults(preemptiveCompactMaxTokens=300_000).preemptive_compact_max_tokens == 300_000


def test_the_cap_refuses_zero_and_negative_counts():
    """0 would compact on every turn; unset is written as null, never 0."""
    from pydantic import ValidationError
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            AgentDefaults(preemptive_compact_max_tokens=bad)
        with pytest.raises(ValidationError):
            ModelPresetConfig(model="m", preemptive_compact_max_tokens=bad)


def test_a_preset_cap_is_unset_unless_given():
    assert ModelPresetConfig(model="m").preemptive_compact_max_tokens is None
    preset = ModelPresetConfig(model="big", preemptiveCompactMaxTokens=400_000)
    assert preset.preemptive_compact_max_tokens == 400_000


def test_the_shipped_default_is_the_same_on_every_construction_path():
    """from_config passes agents.defaults' value; a loop or consolidator built
    directly (the SDK, tests) must land on the same default."""
    import inspect

    from durin.agent.memory import Consolidator

    shipped = AgentDefaults().preemptive_compact_max_tokens
    for ctor in (AgentLoop.__init__, Consolidator.__init__):
        param = inspect.signature(ctor).parameters["preemptive_compact_max_tokens"]
        assert param.default == shipped, ctor


def test_the_schema_offers_the_cap_as_a_nullable_positive_integer():
    """The settings editor reads this schema: a nullable integer with a
    minimum of 1 is what makes an emptied field save null and 0 be refused."""
    from durin.config.schema import Config

    schema = Config.model_json_schema(by_alias=False)
    for model, default in (("AgentDefaults", 256_000), ("ModelPresetConfig", None)):
        field = schema["$defs"][model]["properties"]["preemptive_compact_max_tokens"]
        assert {"type": "integer", "minimum": 1} in field["anyOf"], model
        assert {"type": "null"} in field["anyOf"], model
        assert field["default"] == default, model


def test_config_set_saves_null_and_refuses_zero(tmp_path, monkeypatch):
    """The path the settings editor and `durin config set` write through: null
    persists (it differs from the default, so the non-default save keeps it)
    and 0 is refused before anything is written."""
    from pydantic import ValidationError

    from durin.cli.config_cmd import apply_setting, parse_value
    from durin.config.loader import load_config, save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    key = "agents.defaults.preemptive_compact_max_tokens"
    canonical = Config().model_dump(mode="json", by_alias=False)

    with pytest.raises(ValidationError):
        apply_setting(canonical, key, parse_value("0"))

    save_config(apply_setting(canonical, key, parse_value("null")), path)
    assert load_config(path).agents.defaults.preemptive_compact_max_tokens is None


def test_a_preset_cap_applies_while_active_and_a_preset_without_one_inherits(tmp_path):
    """None on a preset means agents.defaults' cap, even right after a preset
    that set its own: the previous preset's value must not stick."""
    loop = _make_loop(tmp_path, context_window_tokens=1_000_000)
    c = loop.consolidator
    assert c._preemptive_trigger() == (256_000, "cap")

    c.set_provider(loop.provider, "big", 1_000_000, preemptive_compact_max_tokens=400_000)
    assert c._preemptive_trigger() == (400_000, "cap")

    c.set_provider(loop.provider, "other", 1_000_000)
    assert c.preemptive_compact_max_tokens == 256_000
    assert c._preemptive_trigger() == (256_000, "cap")


def test_a_preset_cap_applies_even_when_agents_defaults_has_none(tmp_path):
    loop = _make_loop(
        tmp_path, context_window_tokens=1_000_000, preemptive_compact_max_tokens=None,
    )
    c = loop.consolidator
    assert c._preemptive_trigger() == (500_000, "ratio")

    c.set_provider(loop.provider, "big", 1_000_000, preemptive_compact_max_tokens=300_000)
    assert c._preemptive_trigger() == (300_000, "cap")

    c.set_provider(loop.provider, "other", 1_000_000)
    assert c._preemptive_trigger() == (500_000, "ratio")


def test_a_model_preset_switch_carries_the_presets_cap_to_the_consolidator(tmp_path):
    """The real switch path: set_model_preset builds the preset's snapshot and
    applies it."""
    loop = _make_loop(
        tmp_path,
        context_window_tokens=1_000_000,
        model_presets={
            "default": ModelPresetConfig(model="test-model", context_window_tokens=1_000_000),
            "roomy": ModelPresetConfig(
                model="test-model",
                context_window_tokens=1_000_000,
                preemptive_compact_max_tokens=450_000,
            ),
        },
    )
    loop.set_model_preset("roomy", publish_update=False)
    assert loop.consolidator._preemptive_trigger() == (450_000, "cap")

    loop.set_model_preset("default", publish_update=False)
    assert loop.consolidator._preemptive_trigger() == (256_000, "cap")


def test_provider_snapshots_carry_the_presets_cap():
    """The gateway's snapshot loader and the static path both hand the
    preset's own cap to the loop; the default preset sets none."""
    from durin.agent.model_presets import build_static_preset_snapshot
    from durin.config.schema import Config
    from durin.providers.factory import build_provider_snapshot

    cfg = Config()
    cfg.agents.defaults.model = "gpt-4.1"
    cfg.agents.defaults.provider = "openai"
    cfg.providers.openai.api_key = "sk-test"
    cfg.model_presets["roomy"] = ModelPresetConfig(
        model="gpt-4.1", provider="openai", preemptive_compact_max_tokens=450_000,
    )

    assert build_provider_snapshot(cfg, preset_name="roomy").preemptive_compact_max_tokens == 450_000
    assert build_provider_snapshot(cfg).preemptive_compact_max_tokens is None
    static = build_static_preset_snapshot(MagicMock(), "roomy", cfg.model_presets["roomy"], cfg)
    assert static.preemptive_compact_max_tokens == 450_000


def test_from_config_threads_the_cap_to_the_consolidator(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from durin.config.schema import Config

    config = Config.model_validate({
        "agents": {
            "defaults": {
                "model": "openai/gpt-4.1",
                "workspace": str(tmp_path),
                "preemptive_compact_max_tokens": 180_000,
                "context_block_limit": 90_000,
            }
        },
    })
    fake_provider = MagicMock()
    fake_provider.get_default_model.return_value = "openai/gpt-4.1"
    fake_provider.generation = SimpleNamespace(
        max_tokens=4096, temperature=0.1, reasoning_effort=None
    )
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.loop.Consolidator") as mock_consolidator:
        mock_consolidator.return_value = MagicMock()
        AgentLoop.from_config(config)
    _, kwargs = mock_consolidator.call_args
    assert kwargs["preemptive_compact_max_tokens"] == 180_000
    assert kwargs["context_block_limit"] == 90_000


# ===========================================================================
# Real-usage veto: the probe estimate measures the raw unconsolidated tail,
# while the runner ships a microcompacted, budget-trimmed copy. A rough number
# over the trigger therefore does not prove the real prompt is over it, and
# right after a compaction the newest provider count still describes the
# pre-compaction prompt.
# ===========================================================================


def _veto_consolidator(*, window: int = 200, ratio: float = 0.5):
    c = _stub_consolidator(window=window, max_completion=0, safety=0, ratio=ratio)
    c._fit_baseline = {}
    c._awaiting_real_usage = {}
    return c


def _session_with_usage(loop, count: int, *, usage: int | None):
    """Session whose assistant turns carry a real provider prompt count."""
    session = _session_with_messages(loop, count)
    if usage is not None:
        for message in session.messages:
            if message["role"] == "assistant":
                message["usage_prompt_tokens"] = usage
    loop.sessions.save(session)
    return session


def test_veto_skips_when_provider_proved_the_prompt_fits(tmp_path):
    """Rough estimate over the trigger, provider's real count under it → skip."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 3, usage=trigger - 50)

    assert c._defer_to_real_usage(session, trigger + 40, trigger) == "provider_fit"


def test_veto_lapses_once_the_estimate_drifts_past_tolerance(tmp_path):
    """The veto is not permanent: real growth beyond the tolerance wins."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 3, usage=trigger - 50)

    assert c._defer_to_real_usage(session, trigger + 10, trigger) == "provider_fit"
    tolerated = max(c._FIT_GROWTH_FLOOR, int(trigger * c._FIT_GROWTH_RATIO))
    assert c._defer_to_real_usage(session, trigger + 10 + tolerated + 1, trigger) is None


def test_no_veto_when_the_provider_itself_reports_over_the_trigger(tmp_path):
    """A real count over the trigger is never vetoed — that is the ground truth."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 3, usage=trigger + 10)

    assert c._defer_to_real_usage(session, trigger + 40, trigger) is None


def test_no_veto_without_any_real_usage_anchor(tmp_path):
    """A session that has never had a provider count falls back to the estimate."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 3, usage=None)

    assert c._defer_to_real_usage(session, trigger + 40, trigger) is None


@pytest.mark.asyncio
async def test_second_compaction_is_deferred_until_real_usage_arrives(tmp_path, monkeypatch):
    """Regression: after a compaction the newest usage anchor still describes the
    pre-compaction prompt. Acting on it fires a second compaction against an
    already-shortened conversation (the observed 131K -> 92K -> 53K double drop)."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(tmp_path, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 10, usage=trigger + 60)

    # The estimator stays stubbornly over the trigger, as it does when the
    # probe counts raw tool results the runner never ships.
    monkeypatch.setattr(
        c, "estimate_session_prompt_tokens", lambda _s, **_k: (trigger + 60, "test"),
    )
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await c.maybe_consolidate_by_tokens(session)
    assert c.archive.await_count >= 1, "first compaction must run"
    first_rounds = c.archive.await_count

    # No new assistant turn since — the provider has not weighed in on the
    # shortened conversation yet.
    await c.maybe_consolidate_by_tokens(session)
    assert c.archive.await_count == first_rounds, "second compaction must be deferred"

    deferrals = [e for e in telemetry.events if e[0] == "compaction.deferred"]
    assert [d[1]["reason"] for d in deferrals] == ["post_compaction"]
    assert deferrals[0][1]["trigger_bound"] == "floor"
    assert deferrals[0][1]["cap_tokens"] == 256_000


@pytest.mark.asyncio
async def test_deferral_clears_once_a_fresh_provider_count_lands(tmp_path, monkeypatch):
    """The park is for exactly one turn: a new anchor past the watermark resumes
    normal accounting."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 3, usage=trigger + 60)

    c._awaiting_real_usage[session.key] = len(session.messages)
    assert c._defer_to_real_usage(session, trigger + 60, trigger) == "post_compaction"

    # A fresh assistant turn lands, carrying the provider's count for the
    # now-shorter conversation.
    session.messages.append(
        {"role": "assistant", "content": "fresh", "usage_prompt_tokens": trigger + 60},
    )
    assert c._defer_to_real_usage(session, trigger + 60, trigger) is None
    assert session.key not in c._awaiting_real_usage


@pytest.mark.asyncio
async def test_a_forced_compaction_is_not_vetoed(tmp_path, monkeypatch):
    """The forced compaction after an iteration-0 overflow runs whatever the
    provider's earlier count said: the overflow is newer, and proves the
    prompt does not fit. Deferring it left the retry to overflow again."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    c.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 10, usage=trigger - 50)
    monkeypatch.setattr(c, "estimate_session_prompt_tokens", lambda _s, **_k: (trigger + 10, "test"))
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 10)

    await c.maybe_consolidate_by_tokens(session)
    assert c.archive.await_count == 0, "an ordinary check is vetoed: provider_fit"

    await c.maybe_consolidate_by_tokens(session, force=True)
    after_first = c.archive.await_count
    assert after_first >= 1

    # The compaction just armed the post-compaction park; a second overflow
    # before any new provider count must still compact the turns since.
    assert session.key in c._awaiting_real_usage
    for i in range(5):
        session.messages.append({"role": "user", "content": f"later u{i}"})
        session.messages.append({"role": "assistant", "content": f"later a{i}"})
    await c.maybe_consolidate_by_tokens(session, force=True)
    assert c.archive.await_count > after_first


@pytest.mark.asyncio
async def test_a_forced_compaction_runs_under_the_trigger(tmp_path, monkeypatch):
    """The runner measured the real prompt over its budget; a rough estimate
    under the trigger is the one that is wrong, so the forced compaction still
    compacts down to the target."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    c.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    trigger = c._preemptive_trigger_tokens
    session = _session_with_messages(loop, 10)
    monkeypatch.setattr(c, "estimate_session_prompt_tokens", lambda _s, **_k: (trigger - 10, "test"))
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 10)

    await c.maybe_consolidate_by_tokens(session)
    assert c.archive.await_count == 0
    await c.maybe_consolidate_by_tokens(session, force=True)
    assert c.archive.await_count >= 1


def test_session_tracking_dicts_are_bounded():
    """Per-session veto state must not grow without bound on a long-lived gateway."""
    from durin.agent.memory import Consolidator

    store: dict[str, int] = {}
    for i in range(Consolidator._MAX_TRACKED_SESSIONS + 50):
        Consolidator._bounded_put(store, f"session:{i}", i)
    assert len(store) == Consolidator._MAX_TRACKED_SESSIONS
    assert "session:0" not in store          # oldest evicted
    assert f"session:{Consolidator._MAX_TRACKED_SESSIONS + 49}" in store


@pytest.mark.asyncio
async def test_compaction_binds_its_own_telemetry(tmp_path, monkeypatch):
    """Consolidation runs outside the loop's bind scope (BUILD runs before the
    runner binds; the post-SAVE path runs in a context where it was reset).
    Without a bind of its own every compaction.* event is silently dropped."""
    from durin.telemetry.logger import current_telemetry

    loop = _make_loop(tmp_path, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    c = loop.consolidator
    session = _session_with_usage(loop, 10, usage=None)

    assert current_telemetry() is None, "no ambient telemetry, as in production"

    seen: list[str] = []
    sink = _RecordingTelemetry()

    class _Sentinel:
        def log(self, event_type: str, data: dict) -> None:
            seen.append(event_type)
            sink.log(event_type, data)

    monkeypatch.setattr(
        "durin.telemetry.logger.get_session_logger", lambda *_a, **_k: _Sentinel(),
    )
    monkeypatch.setattr(
        c, "estimate_session_prompt_tokens",
        lambda _s, **_k: (c._preemptive_trigger_tokens + 60, "test"),
    )
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await c.maybe_consolidate_by_tokens(session)

    assert "compaction.completed" in seen
    assert current_telemetry() is None, "bind must be reset on the way out"


@pytest.mark.asyncio
async def test_replay_window_compaction_also_parks_real_usage(tmp_path, monkeypatch):
    """The replay-window path advances the cursor too, so it leaves the same
    stale anchor behind and must arm the same one-turn park."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 10, usage=trigger + 60)

    # Replay overflow archives a span and advances the cursor, exactly as the
    # real path does; the token loop then finds nothing left to do.
    async def _fake_replay(sess, _replay_max):
        sess.last_consolidated = 4
        return "replay summary", {"entities": [], "topics": []}

    monkeypatch.setattr(c, "_consolidate_replay_overflow", _fake_replay)
    monkeypatch.setattr(c, "estimate_session_prompt_tokens", lambda _s, **_k: (1, "test"))

    await c.maybe_consolidate_by_tokens(session)

    assert session.key in c._awaiting_real_usage
    assert c._defer_to_real_usage(session, trigger + 60, trigger) == "post_compaction"


def test_park_clears_when_the_file_cap_rebases_indexes(tmp_path):
    """The post-compaction park stores the message-list length at arm time and
    compares anchor INDEXES against it. enforce_file_cap trims the consolidated
    prefix and rebases every index (retain_recent_legal_suffix), which would
    leave the park comparing rebased indexes against a stale watermark — vetoing
    consolidation for several turns on any session living near the cap."""
    loop = _make_loop(tmp_path, context_window_tokens=200)
    c = loop.consolidator
    trigger = c._preemptive_trigger_tokens
    session = _session_with_usage(loop, 10, usage=trigger - 50)

    # Arm at the current length, as _post_compaction_hooks does.
    c._awaiting_real_usage[session.key] = len(session.messages)

    # File cap trims the prefix: indexes rebase, the list shrinks.
    session.messages = session.messages[8:]

    # The stale watermark must not read the (fresh, rebased) anchor as
    # pre-compaction. With the park dropped, normal accounting resumes —
    # here the provider's real count is under the trigger, so provider_fit.
    assert c._defer_to_real_usage(session, trigger + 40, trigger) != "post_compaction"
    assert session.key not in c._awaiting_real_usage
