"""Chain trigger graph: save-time cycle rejection and outcome-filtered targets.

An automation may chain off another's outcome (an ``AutomationTrigger`` with
``source == "chain"``). That edge creates two distinct concerns:

- A save must never close a cycle across chain edges — a ring of automations
  chaining into each other would fire forever. ``validate_chain_edges`` reads
  RAW JSON off every sibling in ``automations_dir``, the same way
  ``durin/registry_graph.py`` reads workflow/loop definitions: this runs at
  save time, before the candidate spec has landed on disk, so the spec being
  validated REPLACES whatever its own on-disk file currently says rather than
  being unioned with it — editing an automation to drop the edge that used to
  close a cycle must validate clean. A malformed sibling file is skipped
  rather than raising: it must never make the check fail open (skip the cycle
  scan entirely) or fail closed (block an unrelated save).

- At runtime, a finished automation's outcome should fire every enabled
  automation with a matching chain trigger. That is a read of already-valid,
  already-parsed specs, so ``chain_targets`` goes through
  ``durin.automations.store.list_automations`` directly rather than the raw
  JSON used for cycle detection.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.automations.spec import AutomationError, AutomationSpec, AutomationTrigger
from durin.automations.store import automations_dir, list_automations

# Runtime chain-hop budget: AutomationsRuntime.fire refuses to dispatch a
# chain once chain_depth reaches this cap. Declared beside the graph it
# bounds; cycle rejection here makes the cap a backstop for long acyclic
# chains, not the only thing standing between a bad graph and an infinite loop.
CHAIN_HOP_CAP = 8

# The only outcome classes a chain can ever fire on. "interrupted", "rejected",
# "paused", and anything else are terminal-but-not-chainable states — they
# never match, not even a chain_when of "any".
_CHAINABLE_OUTCOMES = ("achieved", "completed", "failed")


def _raw_chain_edges(directory: Path) -> dict[str, list[str]]:
    """name -> chain_automation targets, read from raw JSON on disk.

    Skips dotfiles and any file that fails to parse as a JSON object — same
    contract as ``registry_graph._definitions``: a broken sibling is invisible
    to the scan, never fatal to it.
    """
    edges: dict[str, list[str]] = {}
    if not directory.is_dir():
        return edges
    for p in sorted(directory.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if not isinstance(name, str):
            continue
        targets: list[str] = []
        triggers = data.get("triggers")
        if isinstance(triggers, list):
            for t in triggers:
                if isinstance(t, dict) and t.get("source") == "chain":
                    target = t.get("chain_automation")
                    if isinstance(target, str):
                        targets.append(target)
        edges[name] = sorted(targets)
    return edges


def validate_chain_edges(workspace: str | Path, spec: AutomationSpec) -> None:
    """Raise AutomationError if saving ``spec`` would close a chain cycle.

    ``spec`` stands in for its own on-disk file in the graph (replaced, not
    added to), so this only ever rejects a cycle that saving ``spec`` itself
    would create or preserve — not pre-existing cycles among unrelated
    automations this save doesn't touch.
    """
    edges = _raw_chain_edges(automations_dir(workspace))
    edges[spec.name] = sorted(
        t.chain_automation for t in spec.triggers if t.source == "chain" and t.chain_automation
    )

    in_progress: set[str] = set()
    visited: set[str] = set()

    def _find_cycle(node: str) -> list[str] | None:
        if node in in_progress:
            # A revisit only means THIS save closes a cycle when the node
            # we've come back around to is the root under validation. A
            # revisit of some OTHER in-progress node indicates a cycle
            # elsewhere in the graph that spec.name merely has a path into —
            # not one it closes — so this branch reports nothing and lets
            # its sibling edges keep exploring instead of raising here.
            return [node] if node == spec.name else None
        if node in visited:
            return None
        visited.add(node)
        in_progress.add(node)
        for nxt in edges.get(node, ()):
            cycle = _find_cycle(nxt)
            if cycle is not None:
                return [node, *cycle]
        in_progress.discard(node)
        return None

    cycle = _find_cycle(spec.name)
    if cycle is not None:
        raise AutomationError(
            f"automation '{spec.name}' would close a chain cycle: {' -> '.join(cycle)}"
        )


def chain_targets(
    workspace: str | Path, *, finished: str, outcome: str
) -> list[tuple[AutomationSpec, AutomationTrigger]]:
    """Every enabled automation whose chain trigger fires off ``finished``'s outcome.

    Only "achieved" | "completed" | "failed" ever match — chain_when "any"
    means "any of those three", not "any outcome at all": a finish that
    classified as "interrupted", "rejected", or "paused" (or anything else)
    matches nothing here, regardless of chain_when.

    Returns one ``(spec, trigger)`` pair per matching chain trigger, in
    ascending order of automation name.
    """
    if outcome not in _CHAINABLE_OUTCOMES:
        return []
    out: list[tuple[AutomationSpec, AutomationTrigger]] = []
    for target in sorted(list_automations(workspace), key=lambda s: s.name):
        if not target.enabled:
            continue
        for trig in target.triggers:
            if trig.source != "chain" or trig.chain_automation != finished:
                continue
            if trig.chain_when == "any" or trig.chain_when == outcome:
                out.append((target, trig))
    return out
