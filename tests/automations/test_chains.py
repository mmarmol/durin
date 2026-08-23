"""Tests for the chain trigger graph: save-time cycle rejection and
outcome-filtered targets.
"""

import pytest

from durin.automations.chains import chain_targets, validate_chain_edges
from durin.automations.spec import AutomationError, parse_automation
from durin.automations.store import automations_dir, save_automation


def _spec(name, workflow="wf", *, chains_to=None, chain_when="any", enabled=True):
    data: dict = {"name": name, "workflow": workflow, "enabled": enabled}
    if chains_to is not None:
        data["triggers"] = [{"source": "chain", "chain_automation": chains_to, "chain_when": chain_when}]
    return parse_automation(data)


# --- validate_chain_edges: cycle rejection -------------------------------


def test_self_chain_rejected(tmp_path):
    spec = _spec("a", chains_to="a")
    with pytest.raises(AutomationError):
        validate_chain_edges(tmp_path, spec)


def test_two_node_cycle_rejected_at_the_closing_save(tmp_path):
    save_automation(tmp_path, _spec("a", chains_to="b"))  # A -> B: fine, no cycle yet

    with pytest.raises(AutomationError):
        save_automation(tmp_path, _spec("b", chains_to="a"))  # B -> A: closes it

    # the rejected save never landed
    assert not (automations_dir(tmp_path) / "b.json").exists()


def test_three_node_cycle_rejected_at_the_closing_save(tmp_path):
    save_automation(tmp_path, _spec("a", chains_to="b"))
    save_automation(tmp_path, _spec("b", chains_to="c"))  # A -> B -> C: still a DAG

    with pytest.raises(AutomationError):
        save_automation(tmp_path, _spec("c", chains_to="a"))  # C -> A: closes it

    assert not (automations_dir(tmp_path) / "c.json").exists()


def test_editing_the_closer_to_drop_the_edge_passes(tmp_path):
    save_automation(tmp_path, _spec("a", chains_to="b"))
    save_automation(tmp_path, _spec("b", chains_to="c"))

    with pytest.raises(AutomationError):
        save_automation(tmp_path, _spec("c", chains_to="a"))

    # edit the closer (C) to drop the cycle-closing edge entirely — must pass
    save_automation(tmp_path, _spec("c"))

    assert {p.name for p in automations_dir(tmp_path).glob("*.json")} == {"a.json", "b.json", "c.json"}


def test_edit_is_validated_against_its_new_edge_not_the_stale_on_disk_one(tmp_path):
    """The spec being saved REPLACES its on-disk version in the graph.

    B currently chains to D (harmless, saved). A chains to B. Redirecting B to
    chain to A instead closes a 2-cycle (A -> B -> A) — this must be rejected
    using B's NEW edge, not silently validated against B's stale on-disk edge
    to D (which would let a real cycle slip through undetected).
    """
    save_automation(tmp_path, _spec("d"))
    save_automation(tmp_path, _spec("b", chains_to="d"))
    save_automation(tmp_path, _spec("a", chains_to="b"))

    with pytest.raises(AutomationError):
        save_automation(tmp_path, _spec("b", chains_to="a"))

    # B's on-disk file must still be the last GOOD version (chains to D)
    from durin.automations.store import load_automation

    assert load_automation(tmp_path, "b").triggers[0].chain_automation == "d"


def test_malformed_sibling_skipped_cycle_still_detected(tmp_path):
    save_automation(tmp_path, _spec("a", chains_to="b"))
    (automations_dir(tmp_path) / "broken.json").write_text("{not json")

    with pytest.raises(AutomationError):
        save_automation(tmp_path, _spec("b", chains_to="a"))


def test_malformed_sibling_never_blocks_an_unrelated_save(tmp_path):
    automations_dir(tmp_path).mkdir(parents=True)
    (automations_dir(tmp_path) / "broken.json").write_text("{not json")

    save_automation(tmp_path, _spec("standalone"))  # must not raise either way

    assert (automations_dir(tmp_path) / "standalone.json").exists()


def test_unrelated_pre_existing_cycle_does_not_block_a_save_that_ignores_it(tmp_path):
    """validate_chain_edges only rejects a cycle THIS save would close.

    Two automations already form a cycle on disk via direct file writes
    (bypassing save_automation, e.g. hand-edited or migrated data). A save of
    an unrelated third automation must not be blocked by a graph problem it
    never touches.
    """
    import json

    d = automations_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "x.json").write_text(json.dumps({
        "name": "x", "workflow": "wf",
        "triggers": [{"source": "chain", "chain_automation": "y", "chain_when": "any"}],
    }))
    (d / "y.json").write_text(json.dumps({
        "name": "y", "workflow": "wf",
        "triggers": [{"source": "chain", "chain_automation": "x", "chain_when": "any"}],
    }))

    save_automation(tmp_path, _spec("z"))  # must not raise

    assert (d / "z.json").exists()


# --- chain_targets: outcome-filtered matching -----------------------------


def test_chain_targets_exact_when_match(tmp_path):
    save_automation(tmp_path, _spec("downstream", chains_to="upstream", chain_when="achieved"))

    matches = chain_targets(tmp_path, finished="upstream", outcome="achieved")

    assert [s.name for s, _ in matches] == ["downstream"]
    assert matches[0][1].chain_automation == "upstream"


def test_chain_targets_exact_when_mismatch_excluded(tmp_path):
    save_automation(tmp_path, _spec("downstream", chains_to="upstream", chain_when="achieved"))

    assert chain_targets(tmp_path, finished="upstream", outcome="completed") == []
    assert chain_targets(tmp_path, finished="upstream", outcome="failed") == []


@pytest.mark.parametrize("outcome", ["achieved", "completed", "failed"])
def test_chain_targets_any_matches_all_three_chainable_outcomes(tmp_path, outcome):
    save_automation(tmp_path, _spec("downstream", chains_to="upstream", chain_when="any"))

    matches = chain_targets(tmp_path, finished="upstream", outcome=outcome)

    assert [s.name for s, _ in matches] == ["downstream"]


@pytest.mark.parametrize("outcome", ["rejected", "interrupted", "paused", "queued"])
@pytest.mark.parametrize("chain_when", ["any", "achieved", "completed", "failed"])
def test_chain_targets_non_chainable_outcomes_never_match(tmp_path, chain_when, outcome):
    save_automation(tmp_path, _spec("downstream", chains_to="upstream", chain_when=chain_when))

    assert chain_targets(tmp_path, finished="upstream", outcome=outcome) == []


def test_chain_targets_excludes_disabled_automations(tmp_path):
    save_automation(tmp_path, _spec("downstream", chains_to="upstream", enabled=False))

    assert chain_targets(tmp_path, finished="upstream", outcome="achieved") == []


def test_chain_targets_ignores_non_chain_triggers(tmp_path):
    data = {
        "name": "downstream",
        "workflow": "wf",
        "triggers": [{"source": "schedule", "schedule": {"kind": "every", "every_ms": 60000}, "task": "check"}],
    }
    save_automation(tmp_path, parse_automation(data))

    assert chain_targets(tmp_path, finished="upstream", outcome="achieved") == []


def test_chain_targets_wrong_upstream_name_excluded(tmp_path):
    save_automation(tmp_path, _spec("downstream", chains_to="someone-else"))

    assert chain_targets(tmp_path, finished="upstream", outcome="achieved") == []


def test_chain_targets_ascending_name_order(tmp_path):
    for name in ("zeta", "alpha", "mid"):
        save_automation(tmp_path, _spec(name, chains_to="upstream"))

    matches = chain_targets(tmp_path, finished="upstream", outcome="achieved")

    assert [s.name for s, _ in matches] == ["alpha", "mid", "zeta"]


def test_chain_targets_only_matching_trigger_returned_when_mixed(tmp_path):
    """An automation with both a matching and a non-matching chain trigger
    yields exactly one tuple — for the matching trigger only."""
    data = {
        "name": "downstream",
        "workflow": "wf",
        "triggers": [
            {"source": "chain", "chain_automation": "other", "chain_when": "any"},
            {"source": "chain", "chain_automation": "upstream", "chain_when": "failed"},
        ],
    }
    save_automation(tmp_path, parse_automation(data))

    matches = chain_targets(tmp_path, finished="upstream", outcome="failed")

    assert len(matches) == 1
    assert matches[0][1].chain_automation == "upstream"
    assert matches[0][1].chain_when == "failed"


def test_chain_targets_on_missing_automations_dir_is_empty(tmp_path):
    assert chain_targets(tmp_path / "nowhere", finished="upstream", outcome="achieved") == []
