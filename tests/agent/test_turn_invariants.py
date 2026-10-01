"""Every turn of a session keeps the turn pipeline's invariants, whatever the
scenario: what the user was told is what the session saved, no user message
is lost to compaction, no request asks for more than the window holds and
the precheck counts each one as sent, the prompt's head stays the same
unless one of its inputs changed, and compaction neither thrashes nor fails
a turn that fits.

Each seed is a scenario of ``tests.agent.turn_harness``: ``PROFILES`` names
what each kind exercises, and a seed's numbers are drawn from it. The suite
runs three seeds of every profile; ``DURIN_INVARIANT_SEEDS=500`` runs seeds
0 to 499 instead. A failure lists the violations with their seed and turn;
``python -m tests.agent.turn_harness <seed>`` replays one and prints each
turn."""

from __future__ import annotations

import os
from collections import Counter

import pytest

from tests.agent.turn_harness import PROFILES, TurnDriver, Violation, generate, scenario_health

_QUICK = 3 * len(PROFILES)
_SEEDS = int(os.environ.get("DURIN_INVARIANT_SEEDS") or _QUICK)


def _report(describe: str, violations: list[Violation], seed: int) -> str:
    counts = Counter(v.invariant for v in violations)
    lines = [describe, f"{len(violations)} violations: {dict(counts)}"]
    shown: Counter = Counter()
    for violation in violations:
        shown[violation.invariant] += 1
        if shown[violation.invariant] <= 3:
            lines.append(f"  {violation}")
    lines.append(f"replay: python -m tests.agent.turn_harness {seed}")
    return "\n".join(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(_SEEDS))
async def test_every_turn_keeps_the_invariants(tmp_path, seed):
    scenario = generate(seed)
    driver = TurnDriver(scenario, tmp_path)
    violations = await driver.run()

    assert violations == [], _report(scenario.describe(), violations, seed)
    # A scenario that never reached what it was built to exercise would
    # pass every check without testing it.
    assert scenario_health(driver) == [], scenario.describe()


def _losing_pass(monkeypatch, how: str) -> None:
    """Make the nightly pass lose each message larger than its budget: leave
    it out of the span (``skips``), or make no call for it and still move
    its cursor past it (``cursor``)."""
    import durin.memory.session_summary_dream as nightly
    from durin.utils.runtime import summary_token_count

    if how == "skips":
        real = nightly.runs_that_fit

        def runs(messages, budget, *, line, count):
            return real([m for m in messages if count(line(m)) <= budget], budget, line=line, count=count)

        monkeypatch.setattr(nightly, "runs_that_fit", runs)
    else:
        monkeypatch.setattr(
            nightly, "truncate_to_tokens", lambda text, budget: text if summary_token_count(text) <= budget else "",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["skips", "cursor"])
async def test_the_invariant_catches_a_nightly_pass_that_loses_a_large_message(tmp_path, monkeypatch, how):
    """A dream whose memory model is smaller than the loop's covers messages
    larger than that model takes. A nightly pass that left such a message
    out, or moved its cursor past it without summarizing it, would lose it
    for good: compaction skips whatever the cursor covers. content.no_loss
    catches both, on the scenario the real pass keeps clean."""
    from tests.agent.turn_harness import profile_small_dream

    seed = next(s for s in range(len(PROFILES)) if PROFILES[s] is profile_small_dream)
    (tmp_path / "real").mkdir()
    (tmp_path / "losing").mkdir()
    assert await TurnDriver(generate(seed), tmp_path / "real").run() == []

    _losing_pass(monkeypatch, how)
    violations = await TurnDriver(generate(seed), tmp_path / "losing").run()

    lost = [v for v in violations if v.invariant == "content.no_loss"]
    assert lost, _report(generate(seed).describe(), violations, seed)
    assert all("no summarizing call received it at all" in v.detail for v in lost), lost


def _broken_cursor(monkeypatch, how: str) -> None:
    """Break what keeps the nightly pass and the summarizers apart: read the
    cursor as the bare position it recorded (``positional``), as it was before
    it named its message, or summarize the head it covers anyway
    (``untrimmed``), as /compact and the /new record did."""
    import json
    from pathlib import Path

    import durin.memory.session_summary_dream as nightly
    from durin.agent.memory import Consolidator
    from durin.memory.extract_runner import _meta_path

    if how == "positional":
        def positional(jsonl_path, messages):
            sidecar = _meta_path(Path(jsonl_path))
            record = json.loads(sidecar.read_text(encoding="utf-8")).get("summary_cursor") if sidecar.exists() else None
            return int(record["position"]) if isinstance(record, dict) else 0

        monkeypatch.setattr(nightly, "summarized_count", positional)
    else:
        monkeypatch.setattr(Consolidator, "_unsummarized", lambda self, session, chunk: chunk)


@pytest.mark.asyncio
@pytest.mark.parametrize(("how", "invariant"), [("positional", "content.no_loss"), ("untrimmed", "content.summarized_once")])
async def test_the_invariants_catch_a_nightly_cursor_that_does_not_hold(tmp_path, monkeypatch, how, invariant):
    """/new and the file cap renumber a session's messages under the nightly
    cursor, and /compact and the /new record summarize what compaction
    archives. Read as a position, the cursor covers messages the pass never
    saw, and they are archived unsummarized; ignored, the turns it covers are
    summarized twice. The mixed profile shows both, on the scenario the real
    code keeps clean."""
    from tests.agent.turn_harness import profile_nightly_mix

    seed = next(s for s in range(len(PROFILES)) if PROFILES[s] is profile_nightly_mix)
    (tmp_path / "real").mkdir()
    (tmp_path / "broken").mkdir()
    assert await TurnDriver(generate(seed), tmp_path / "real").run() == []

    _broken_cursor(monkeypatch, how)
    violations = await TurnDriver(generate(seed), tmp_path / "broken").run()

    assert any(v.invariant == invariant for v in violations), _report(generate(seed).describe(), violations, seed)
