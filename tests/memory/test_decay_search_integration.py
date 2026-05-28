"""Temporal decay applied in the search pipeline (audit A9).

Per doc 03 §10 and the enumeration-by-class decision recorded in
doc 11 A9 (2026-05-28):

- `episodic` / `session_summary` decay at 90 / 120-day half-life.
- `entity_page` / `stable` / `corpus` do NOT decay (class default
  is null).

The per-entry override (frontmatter `evergreen` / `decay_half_life`)
is NOT applied in the search pipeline — only the class default.

Per [[feedback-sync-tests-exercise-behavior]]: these tests exercise
the real decay function. Per [[feedback-verify-quantifiers]]: each
expected value is computed from the formula, not copied from a
golden file.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from durin.memory.decay import (
    CLASS_HALF_LIFE_DEFAULTS,
    apply_class_decay,
)


# ---------------------------------------------------------------------------
# unit-level: apply_class_decay (pure function)
# ---------------------------------------------------------------------------


# Midnight UTC so that `valid_from="YYYY-MM-DD"` (parsed at 00:00)
# minus this gives an exact whole-day delta — keeps the
# verify_quantifiers tolerance tight.
_FIXED_NOW = datetime(2026, 5, 28, 0, 0, 0, tzinfo=timezone.utc)


def test_episodic_today_keeps_score() -> None:
    """A hit dated today gets factor == 1.0 (no penalty)."""
    score, factor = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso="2026-05-28",
        now=_FIXED_NOW,
    )
    assert factor == 1.0
    assert score == 1.0


def test_episodic_one_half_life_old_factor_is_e_minus_1() -> None:
    """At exactly 90 days (one half-life for `episodic`), factor =
    exp(-1) ≈ 0.368. Verifies the formula numerically."""
    # 2026-05-28 minus 90 days = 2026-02-27.
    score, factor = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso="2026-02-27",
        now=_FIXED_NOW,
    )
    expected = math.exp(-1.0)
    assert math.isclose(factor, expected, abs_tol=1e-3), (
        f"expected ≈ {expected:.4f}, got {factor:.4f}"
    )
    assert math.isclose(score, expected, abs_tol=1e-3)


def test_episodic_five_half_lives_is_below_one_percent() -> None:
    """At 5× half-life (~450 days) the factor rounds to ≤ 0.7% per
    doc 03 §10.2."""
    # 2026-05-28 minus 450 days ≈ 2025-03-04. Use a safely older date.
    score, factor = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso="2025-03-04",
        now=_FIXED_NOW,
    )
    assert factor < 0.01, (
        f"five-half-lives should give factor < 0.01; got {factor:.6f}"
    )


def test_entity_page_does_not_decay() -> None:
    """Entity pages have null half-life → score passes through even
    with an ancient valid_from."""
    score, factor = apply_class_decay(
        score=0.5,
        class_name="entity_page",
        valid_from_iso="2020-01-01",
        now=_FIXED_NOW,
    )
    assert factor == 1.0
    assert score == 0.5


def test_entity_alias_also_does_not_decay() -> None:
    """The FTS5 index uses `type="entity"` while LanceDB uses
    `class_name="entity_page"`. Both map to no-decay."""
    _, factor = apply_class_decay(
        score=0.5, class_name="entity",
        valid_from_iso="2020-01-01", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_stable_does_not_decay() -> None:
    """`stable` was explicitly marked durable by user/agent."""
    _, factor = apply_class_decay(
        score=0.5, class_name="stable",
        valid_from_iso="2020-01-01", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_corpus_does_not_decay() -> None:
    """`corpus` valid_from is the INGEST date, not content date —
    decay would penalise old books in the pipeline, not info age."""
    _, factor = apply_class_decay(
        score=0.5, class_name="corpus",
        valid_from_iso="2020-01-01", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_session_summary_decays_at_120_day_half_life() -> None:
    """Session summaries age more slowly than episodic — 120-day
    half-life."""
    # 2026-05-28 minus 120 days = 2026-01-28.
    _, factor = apply_class_decay(
        score=1.0,
        class_name="session_summary",
        valid_from_iso="2026-01-28",
        now=_FIXED_NOW,
    )
    expected = math.exp(-1.0)
    assert math.isclose(factor, expected, abs_tol=1e-3)


def test_empty_valid_from_does_not_decay() -> None:
    """Missing timestamp → safe-failure: no decay."""
    _, factor = apply_class_decay(
        score=1.0, class_name="episodic",
        valid_from_iso="", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_malformed_valid_from_does_not_decay() -> None:
    """Garbage timestamp → safe-failure: no decay."""
    _, factor = apply_class_decay(
        score=1.0, class_name="episodic",
        valid_from_iso="not-a-date", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_future_dated_entry_does_not_decay() -> None:
    """Clock skew / optimistic timestamps should NOT get a score
    boost — decay only acts in the past."""
    _, factor = apply_class_decay(
        score=1.0, class_name="episodic",
        valid_from_iso="2026-12-31",
        now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_unknown_class_does_not_decay() -> None:
    """Unknown class → safe-failure: no decay."""
    _, factor = apply_class_decay(
        score=1.0, class_name="custom_type",
        valid_from_iso="2020-01-01", now=_FIXED_NOW,
    )
    assert factor == 1.0


def test_iso_with_full_timestamp_parses_correctly() -> None:
    """Full RFC3339 timestamps (with time component) also parse —
    not just YYYY-MM-DD."""
    _, factor = apply_class_decay(
        score=1.0, class_name="episodic",
        valid_from_iso="2026-02-27T10:30:00Z",
        now=_FIXED_NOW,
    )
    expected = math.exp(-1.0)
    assert math.isclose(factor, expected, abs_tol=1e-2)


# ---------------------------------------------------------------------------
# config / schema invariants
# ---------------------------------------------------------------------------


def test_class_half_life_defaults_match_doc_03_section_10() -> None:
    """Doc 03 §10.2 specifies the per-class half-lives. The constant
    in code is the source of truth; this test guards against a
    silent revert."""
    assert CLASS_HALF_LIFE_DEFAULTS["episodic"] == 90
    assert CLASS_HALF_LIFE_DEFAULTS["session_summary"] == 120
    assert CLASS_HALF_LIFE_DEFAULTS["entity"] is None
    # A9 added entity_page as an alias so FTS5/LanceDB callers both
    # resolve correctly.
    assert CLASS_HALF_LIFE_DEFAULTS["entity_page"] is None
    assert CLASS_HALF_LIFE_DEFAULTS["stable"] is None
    assert CLASS_HALF_LIFE_DEFAULTS["corpus"] is None


def test_temporal_decay_config_default_is_enabled() -> None:
    """Default ON per doc 03 §10.6 (revised from the original
    'disabled by default' draft)."""
    from durin.config.schema import MemoryTemporalDecayConfig

    cfg = MemoryTemporalDecayConfig()
    assert cfg.enabled is True


def test_telemetry_event_registered() -> None:
    """`memory.recall.decay` is in the event registry so emitters
    don't get orphan-event warnings."""
    from durin.telemetry.schema import EVENTS

    assert "memory.recall.decay" in EVENTS


# ---------------------------------------------------------------------------
# pipeline-level: _temporal_decay_step reorders fused hits + emits event
# ---------------------------------------------------------------------------


def _make_fused_hit(uri: str, score: float):
    from durin.memory.rrf_fusion import FusedHit

    return FusedHit(uri=uri, score=score, sources=(), ranks=())


def test_pipeline_decay_reorders_old_below_recent(
    monkeypatch,
) -> None:
    """An old episodic with a higher pre-decay score still ranks
    below a recent one if the decay penalty is large enough."""
    from durin.memory import search_pipeline

    # `old` had a higher fused score (0.9) but is 365 days old →
    # decay factor exp(-365/90) ≈ 0.0183 → decayed = ~0.0165.
    # `new` had a lower fused score (0.5) but is today's →
    # factor 1.0 → decayed = 0.5.
    fused_in = [
        _make_fused_hit("memory/episodic/old", 0.9),
        _make_fused_hit("memory/episodic/new", 0.5),
    ]
    vector_meta = {
        "memory/episodic/old": {
            "type": "episodic", "valid_from": "2025-05-28",
        },
        "memory/episodic/new": {
            "type": "episodic", "valid_from": "2026-05-28",
        },
    }

    out = search_pipeline._temporal_decay_step(
        fused_in, vector_meta=vector_meta, lexical_meta={},
        grep_meta=None, now=_FIXED_NOW,
    )

    # `new` now ranks first; the old one fell.
    assert out[0].uri == "memory/episodic/new"
    assert out[1].uri == "memory/episodic/old"
    assert out[0].score == 0.5  # unchanged (factor 1.0)
    assert out[1].score < 0.02  # heavily decayed


def test_pipeline_decay_skips_entity_pages(monkeypatch) -> None:
    """Entity-page hits keep their score even with ancient
    valid_from — order is preserved."""
    from durin.memory import search_pipeline

    fused_in = [
        _make_fused_hit("memory/entity_page/marcelo", 0.9),
        _make_fused_hit("memory/episodic/note", 0.5),
    ]
    vector_meta = {
        "memory/entity_page/marcelo": {
            # vector index stores "entity_page"; the meta normalises
            # to "entity" in _resolve_meta. Both are no-op for decay.
            "type": "entity_page", "valid_from": "2020-01-01",
        },
        "memory/episodic/note": {
            "type": "episodic", "valid_from": "2026-05-28",
        },
    }

    out = search_pipeline._temporal_decay_step(
        fused_in, vector_meta=vector_meta, lexical_meta={},
        grep_meta=None, now=_FIXED_NOW,
    )
    # Entity page kept its score (0.9), so it stays on top.
    assert out[0].uri == "memory/entity_page/marcelo"
    assert out[0].score == 0.9


def test_pipeline_decay_emits_telemetry(monkeypatch) -> None:
    """A pass through the decay step emits one
    `memory.recall.decay` event with the right aggregates."""
    from durin.memory import search_pipeline

    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.emit_tool_event",
        lambda t, d: captured.append((t, d)),
    )

    fused_in = [
        _make_fused_hit("memory/episodic/old", 0.9),
        _make_fused_hit("memory/entity_page/marcelo", 0.7),
        _make_fused_hit("memory/episodic/new", 0.5),
    ]
    vector_meta = {
        "memory/episodic/old": {
            "type": "episodic", "valid_from": "2025-05-28",
        },
        "memory/entity_page/marcelo": {
            "type": "entity_page", "valid_from": "2020-01-01",
        },
        "memory/episodic/new": {
            "type": "episodic", "valid_from": "2026-05-28",
        },
    }

    search_pipeline._temporal_decay_step(
        fused_in, vector_meta=vector_meta, lexical_meta={},
        grep_meta=None, now=_FIXED_NOW,
    )

    decay_events = [e for e in captured if e[0] == "memory.recall.decay"]
    assert len(decay_events) == 1
    payload = decay_events[0][1]
    assert payload["hits_total"] == 3
    # `entity_page` and today's `episodic` are no-ops (factor 1.0);
    # only the year-old `episodic` is counted in `hits_decayed`.
    assert payload["hits_decayed"] == 1
    assert 0.0 < payload["avg_decay_factor"] < 0.1
