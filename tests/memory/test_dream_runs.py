"""Durable dream-run store + skill-curation-action surfacing in the digest."""
from durin.memory.dream_digest import map_dream_event
from durin.memory.dream_runs import read_dream_runs, record_dream_run


def test_record_and_read_roundtrip_newest_first(tmp_path):
    record_dream_run(tmp_path, {"skills_improved": 1, "at_ms": 1000})
    record_dream_run(tmp_path, {"skills_improved": 2, "at_ms": 2000})
    runs = read_dream_runs(tmp_path)
    assert [r["at_ms"] for r in runs] == [2000, 1000]  # newest first
    assert runs[0]["skills_improved"] == 2


def test_record_survives_and_is_durable_on_disk(tmp_path):
    # The store is a plain file under the workspace — survives process/gateway
    # restart and telemetry retention (the whole point).
    record_dream_run(tmp_path, {"sessions": 3, "at_ms": 500})
    assert (tmp_path / "memory" / ".dream_runs.jsonl").exists()
    assert read_dream_runs(tmp_path)[0]["sessions"] == 3


def test_read_empty_when_absent(tmp_path):
    assert read_dream_runs(tmp_path) == []


def test_curation_action_surfaces_which_skill_and_how():
    # The gap the operator hit: "1 skill improved" with no which/how. Now the
    # applied action maps to a feed item naming the skill and the verb.
    items = map_dream_event(
        "skill.curation_action",
        {"action": "restructure", "skill": "qr-code-reader", "applied": True}, 123)
    assert len(items) == 1
    assert items[0]["summary"] == "Restructured skill `qr-code-reader`"
    assert items[0]["ref"] == "qr-code-reader"
    assert items[0]["ref_kind"] == "skill"


def test_curation_action_skips_unapplied():
    # skips / failed actions are not feed-worthy noise
    assert map_dream_event(
        "skill.curation_action",
        {"action": "restructure", "skill": "x", "applied": False}, 1) == []
