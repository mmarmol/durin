"""Per-entity relation cap. Soft 50 / hard 200.

Decision (2026-06-06): alert-only "de momento" — emit telemetry + log at both
thresholds but NEVER block the write or drop a relation (no data loss). The
hard-cap rejection is deferred until mega-hubs prove real.
"""
from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.entity_relation_cap import (
    HARD_RELATION_CAP,
    SOFT_RELATION_CAP,
    check_relation_cap,
)
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _rel(i: int) -> FieldPatch:
    return FieldPatch(kind="relation", value={"to": f"company:c{i}", "type": "partner"},
                      author="agent", source_ref="s", at=NOW)


def test_check_relation_cap_decision_logic():
    assert check_relation_cap(entity_ref="x", current_count=0, adding=10).action == "ok"
    # crossing the soft cap (was ≤50, becomes >50) → warn
    assert check_relation_cap(entity_ref="x", current_count=49, adding=2).action == "warn"
    assert check_relation_cap(entity_ref="x", current_count=SOFT_RELATION_CAP, adding=1).action == "warn"
    # already over the soft cap, not crossing again → ok
    assert check_relation_cap(entity_ref="x", current_count=60, adding=1).action == "ok"
    # crossing the hard cap → reject
    assert check_relation_cap(entity_ref="x", current_count=HARD_RELATION_CAP - 1, adding=5).action == "reject"


def test_relation_cap_warns_at_soft_without_data_loss(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as T
    events = []
    monkeypatch.setattr(T, "emit_tool_event", lambda ev, data: events.append((ev, data)))
    write_entity(tmp_path, "company:hub", [_rel(i) for i in range(55)],
                 create=True, name="Hub")
    page = EntityPage.from_file(tmp_path / "memory/entities/company/hub.md")
    assert len(page.relations) == 55                    # de momento: no data loss
    warned = [d for ev, d in events if ev == "memory.entity_relation_cap_warned"]
    assert warned and warned[0]["entity_ref"] == "company:hub"


def test_relation_cap_alerts_at_hard_without_data_loss(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as T
    events = []
    monkeypatch.setattr(T, "emit_tool_event", lambda ev, data: events.append((ev, data)))
    write_entity(tmp_path, "company:mega", [_rel(i) for i in range(HARD_RELATION_CAP + 1)],
                 create=True, name="Mega")
    page = EntityPage.from_file(tmp_path / "memory/entities/company/mega.md")
    assert len(page.relations) == HARD_RELATION_CAP + 1  # de momento: alert-only, no drop
    rejected = [d for ev, d in events if ev == "memory.entity_relation_cap_rejected"]
    assert rejected and rejected[0]["entity_ref"] == "company:mega"
