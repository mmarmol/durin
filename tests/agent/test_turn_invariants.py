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
