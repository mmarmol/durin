"""B-19 (audit fourth pass, 2026-05-29): enforce the per-entity
relation cap documented in `docs/architecture/memory/01_data_and_entities.md`
§4.4 (soft 50 / hard 200).

Pre-B-19 the doc carried the numbers as documented intent, but
`durin/memory/dream_apply.py` never counted relations or rejected
new ones. An entity could legitimately accumulate 500 relations
with no signal. The cost of enforcement is small enough (~30 LOC)
that keeping the gap as a deferred backlog item was wrong shape —
this module ships the brake pedal so a mega-hub R2 risk surfaces in
telemetry before sub-paging (B-14) becomes necessary.
"""

from __future__ import annotations

import pytest


_SOFT_LIMIT = 50
_HARD_LIMIT = 200


def _make_relations(n: int) -> list[dict]:
    return [
        {"to": f"person:peer_{i:04d}", "type": "knows"}
        for i in range(n)
    ]


def test_below_soft_cap_does_not_warn_or_reject() -> None:
    """An entity with 49 relations is below the soft cap — apply
    succeeds, no cap event fires."""
    from durin.memory.entity_relation_cap import check_relation_cap

    decision = check_relation_cap(
        entity_ref="person:marcelo",
        current_count=49,
        adding=0,
    )
    assert decision.action == "ok"
    assert decision.cap_warned is False
    assert decision.cap_rejected is False


def test_crossing_soft_cap_warns_but_accepts() -> None:
    """49 + 5 = 54 crosses the soft cap (50). The apply proceeds
    but a warning fires so dashboards can spot growing mega-hubs."""
    from durin.memory.entity_relation_cap import check_relation_cap

    decision = check_relation_cap(
        entity_ref="person:marcelo",
        current_count=49,
        adding=5,
    )
    assert decision.action == "warn"
    assert decision.cap_warned is True
    assert decision.cap_rejected is False
    assert decision.new_count == 54


def test_at_soft_cap_does_not_warn() -> None:
    """Exactly 50 is the threshold but not over it — the warn
    semantic should be 'crossed the cap', not 'hit the cap'."""
    from durin.memory.entity_relation_cap import check_relation_cap

    decision = check_relation_cap(
        entity_ref="person:marcelo",
        current_count=50,
        adding=0,
    )
    assert decision.cap_warned is False


def test_hard_cap_rejects_the_add() -> None:
    """At the hard cap, the apply must be rejected. The decision
    carries the action 'reject' so the caller can fail the apply
    deterministically."""
    from durin.memory.entity_relation_cap import check_relation_cap

    decision = check_relation_cap(
        entity_ref="person:marcelo",
        current_count=200,
        adding=1,
    )
    assert decision.action == "reject"
    assert decision.cap_rejected is True


def test_partial_add_rejects_if_total_would_exceed() -> None:
    """Adding 5 to an entity at 198 (would land at 203) is
    rejected — the cap is on the final count, not the per-call
    delta."""
    from durin.memory.entity_relation_cap import check_relation_cap

    decision = check_relation_cap(
        entity_ref="person:marcelo",
        current_count=198,
        adding=5,
    )
    assert decision.action == "reject"


def test_constants_match_documented_cap_values() -> None:
    """Doc 01 §4.4 documents the exact numbers; the module must
    use the same constants so doc and code do not drift."""
    from durin.memory.entity_relation_cap import (
        HARD_RELATION_CAP, SOFT_RELATION_CAP,
    )

    assert SOFT_RELATION_CAP == _SOFT_LIMIT
    assert HARD_RELATION_CAP == _HARD_LIMIT


def test_dream_apply_emits_warn_event_at_soft_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: Dream apply on an entity that crosses the soft
    cap emits `memory.entity_relation_cap_warned` and continues
    with the write."""
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type="person", name="Marcelo", aliases=[],
        relations=_make_relations(48), body="b",
    )
    page_path = (
        tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    )
    page.save(page_path)

    events: list[tuple[str, dict]] = []
    import durin.agent.tools._telemetry as tel
    monkeypatch.setattr(
        tel, "emit_tool_event",
        lambda t, d: events.append((t, d)),
    )

    # Apply 5 new relations → total 53, crosses soft cap (50).
    from durin.memory.dream_apply import apply_dream_output
    from durin.memory.dream_patch_parser import ParsedDreamOutput

    new_relations = [
        {
            "op": "add",
            "path": "/relations/-",
            "value": {"to": f"person:new_{i}", "type": "knows"},
            "provenance": "episodic/test.md",
        }
        for i in range(5)
    ]
    parsed = ParsedDreamOutput(
        patch_ops=new_relations,
        body_delta="",
        commit_message="add 5 relations",
    )

    apply_dream_output(
        workspace=tmp_path,
        entity_ref="person:marcelo",
        parsed=parsed,
        trigger="manual",
        cursor_after="2026-05-29",
    )

    warned = [
        d for t, d in events
        if t == "memory.entity_relation_cap_warned"
    ]
    assert len(warned) == 1
    assert warned[0]["entity_ref"] == "person:marcelo"
    assert warned[0]["new_count"] == 53


def test_dream_apply_rejects_at_hard_cap(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: Dream apply on an entity already at the hard
    cap returns a failure (does NOT silently accept) and emits
    `memory.entity_relation_cap_rejected`."""
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type="person", name="Marcelo", aliases=[],
        relations=_make_relations(200), body="b",
    )
    page_path = (
        tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    )
    page.save(page_path)

    events: list[tuple[str, dict]] = []
    import durin.agent.tools._telemetry as tel
    monkeypatch.setattr(
        tel, "emit_tool_event",
        lambda t, d: events.append((t, d)),
    )

    from durin.memory.dream_apply import (
        DreamApplyFailureKind, apply_dream_output,
    )
    from durin.memory.dream_patch_parser import ParsedDreamOutput

    parsed = ParsedDreamOutput(
        patch_ops=[{
            "op": "add",
            "path": "/relations/-",
            "value": {"to": "person:one_too_many", "type": "knows"},
            "provenance": "episodic/test.md",
        }],
        body_delta="",
        commit_message="add one over the cap",
    )

    result = apply_dream_output(
        workspace=tmp_path,
        entity_ref="person:marcelo",
        parsed=parsed,
        trigger="manual",
        cursor_after="2026-05-29",
    )

    assert result.failure_kind == DreamApplyFailureKind.VALIDATION
    rejected = [
        d for t, d in events
        if t == "memory.entity_relation_cap_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["entity_ref"] == "person:marcelo"
