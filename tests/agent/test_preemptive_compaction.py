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


def _stub_consolidator(*, window: int, max_completion: int, safety: int, ratio: float):
    """Build a barebones Consolidator without the full LLM provider plumbing."""
    from durin.agent.memory import Consolidator
    c = Consolidator.__new__(Consolidator)
    c.context_window_tokens = window
    c.max_completion_tokens = max_completion
    c.preemptive_compact_ratio = ratio
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
    from durin.agent.runner import _MAX_OUTPUT_RESERVATION, _SNIP_SAFETY_BUFFER, _output_reservation

    for window, max_completion in (
        (231_072, 131_072), (200_000, 8192), (1_000_000, 65_536), (32_000, 4096),
    ):
        c = _stub_consolidator(
            window=window, max_completion=max_completion, safety=1024, ratio=0.99,
        )
        runner_budget = window - _output_reservation(max_completion) - _SNIP_SAFETY_BUFFER
        assert c._preemptive_ceiling < runner_budget, (window, max_completion)
        assert c._preemptive_trigger_tokens < runner_budget, (window, max_completion)
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

    done = [e for e in telemetry.events if e[0] == "compaction.completed"]
    assert len(done) == 1
    assert done[0][1]["rounds"] == 1
    assert done[0][1]["exit_reason"] == "target_reached"
    assert done[0][1]["estimated_before"] == 150
    assert done[0][1]["estimated_after"] == 40


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


def test_set_provider_without_explicit_ratio_preserves_current(tmp_path):
    """set_provider with preemptive_compact_ratio=None leaves the current
    value untouched (the global default → preset case where preset doesn't
    override)."""
    loop = _make_loop(tmp_path, context_window_tokens=200, preemptive_compact_ratio=0.3)
    loop.consolidator.set_provider(loop.provider, "x", 500)
    assert loop.consolidator.preemptive_compact_ratio == 0.3


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
        return "replay summary"

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
