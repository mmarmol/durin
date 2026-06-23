"""Pure diagnostic signal for workflow self-improvement.

Reduces a list of per-run records (from run_log) into per-node trouble counts: how often
a node loops back (ran more than once in a run) and how often a decision gate fails. A
node crosses into a *candidate* for improvement only when a symptom recurs across runs
(a single bad run is noise). No LLM, no IO — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A symptom must recur across at least this many runs to be worth proposing a change for.
RECURRENCE_FLOOR = 2


@dataclass
class Diagnostics:
    total_runs: int = 0
    loop_backs: dict[str, int] = field(default_factory=dict)   # node_id -> #runs it ran >1×
    gate_fails: dict[str, int] = field(default_factory=dict)   # decision id -> #runs it failed
    max_visits_aborts: int = 0

    def candidates(self, floor: int = RECURRENCE_FLOOR) -> set[str]:
        """Node/gate ids whose trouble recurs at or above the floor."""
        return (
            {n for n, c in self.loop_backs.items() if c >= floor}
            | {n for n, c in self.gate_fails.items() if c >= floor}
        )


def compute_diagnostics(records: list[dict]) -> Diagnostics:
    d = Diagnostics(total_runs=len(records))
    for rec in records:
        if rec.get("status") == "max_visits":
            d.max_visits_aborts += 1
        max_iter: dict[str, int] = {}
        failed_gates: set[str] = set()
        for r in rec.get("runs", []):
            nid = r.get("node_id")
            if nid is None:
                continue
            max_iter[nid] = max(max_iter.get(nid, 0), r.get("iteration", 1))
            if r.get("passed") is False:   # decision node that routed to on_fail
                failed_gates.add(nid)
        for nid, mi in max_iter.items():
            if mi > 1:                     # the node ran more than once → a loop-back
                d.loop_backs[nid] = d.loop_backs.get(nid, 0) + 1
        for nid in failed_gates:
            d.gate_fails[nid] = d.gate_fails.get(nid, 0) + 1
    return d
