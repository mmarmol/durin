"""F1 (audit third pass, 2026-05-28): doc 00 §189 promised
``memory.search.temporal_decay.class_half_life_overrides`` as the
config knob to tune per-class half-lives without a code patch. The
field never existed. This module ships it with default empty dict =
exact pre-F1 behaviour.

Use cases:
- Workspace very active → operator wants `episodic=30` (default 90 is
  too generous; old chatter clutters recall).
- Workspace long-running / multi-year → operator wants `episodic=365`
  (default 90 is too aggressive; the operator references older
  context).
- Per-class enable/disable while keeping the global toggle ON, e.g.
  `entity_page=null` already (no decay) — but an operator could now
  force `entity_page=180` if they actively dispute defunct facts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.memory.decay import (
    CLASS_HALF_LIFE_DEFAULTS,
    apply_class_decay,
    resolve_class_half_life,
)


_NOW = datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc)


def test_resolve_returns_default_when_no_override() -> None:
    assert resolve_class_half_life("episodic") == 90
    assert resolve_class_half_life("session_summary") == 120
    assert resolve_class_half_life("entity_page") is None


def test_resolve_respects_override_value() -> None:
    """Operator-supplied override for an existing class takes
    precedence over the default."""
    overrides = {"episodic": 30}
    assert resolve_class_half_life("episodic", overrides=overrides) == 30
    # Untouched classes still use the default.
    assert (
        resolve_class_half_life("session_summary", overrides=overrides)
        == 120
    )


def test_resolve_respects_override_null_disables_decay() -> None:
    """`None` in the override map means "this class no longer decays"
    even if the default was a number. Lets an operator opt out of
    decay for a specific class without disabling globally."""
    overrides = {"episodic": None}
    assert (
        resolve_class_half_life("episodic", overrides=overrides) is None
    )


def test_resolve_override_can_add_decay_to_a_default_no_op_class() -> None:
    """Operators can also flip the other way: take a no-decay default
    (entity_page) and impose a half-life."""
    overrides = {"entity_page": 180}
    assert (
        resolve_class_half_life("entity_page", overrides=overrides)
        == 180
    )


def test_resolve_unknown_class_with_override_still_returns_override() -> None:
    """Unknown class with no override returns None (safe); with an
    override returns the override (operator opt-in)."""
    overrides = {"custom_class": 42}
    assert (
        resolve_class_half_life("custom_class", overrides=overrides)
        == 42
    )
    # No override → still None (unchanged safety semantic).
    assert resolve_class_half_life("custom_class") is None


def test_apply_class_decay_threads_overrides() -> None:
    """`apply_class_decay` (the ranking-time consumer) honours
    overrides too. Without an override, episodic decays per the 90d
    default. With override of 30d at the same valid_from, the factor
    must be strictly smaller (steeper decay)."""
    # 60 days ago.
    valid_from = "2026-03-29"
    new_score_default, factor_default = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso=valid_from,
        now=_NOW,
    )
    new_score_override, factor_override = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso=valid_from,
        now=_NOW,
        overrides={"episodic": 30},
    )
    assert factor_override < factor_default
    assert new_score_override < new_score_default


def test_apply_class_decay_override_null_disables_decay() -> None:
    """When override sets a class to None, decay is a no-op (factor=1.0)."""
    _, factor = apply_class_decay(
        score=1.0,
        class_name="episodic",
        valid_from_iso="2026-01-01",
        now=_NOW,
        overrides={"episodic": None},
    )
    assert factor == 1.0


def test_config_field_exists_and_defaults_to_empty_dict() -> None:
    """The promise in doc 00 §189 is `memory.search.temporal_decay
    .class_half_life_overrides`. Default = empty dict so existing
    workspaces see zero behaviour change."""
    from durin.config.schema import MemoryTemporalDecayConfig

    cfg = MemoryTemporalDecayConfig()
    assert cfg.class_half_life_overrides == {}


def test_search_pipeline_threads_overrides_from_app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when `memory_search` builds its pipeline call, it
    must thread the operator-configured overrides into the decay step
    so that `memory.recall.decay` reflects the tuned half-lives."""
    from durin.config.schema import (
        Config,
        MemorySearchConfig,
        MemoryTemporalDecayConfig,
    )

    # Build a config that overrides episodic to a tight 1-day half-life.
    cfg = Config()
    cfg.memory.search = MemorySearchConfig(
        temporal_decay=MemoryTemporalDecayConfig(
            enabled=True,
            class_half_life_overrides={"episodic": 1},
        ),
    )

    # Capture the overrides arg passed into apply_class_decay.
    captured: dict = {}

    import durin.memory.search_pipeline as sp
    real_decay = sp._temporal_decay_step

    def _spy_decay(*args, **kwargs):
        captured["overrides"] = kwargs.get("overrides")
        return real_decay(*args, **kwargs)

    monkeypatch.setattr(sp, "_temporal_decay_step", _spy_decay)

    # Seed minimal workspace + run.
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index
    EntityPage(
        type="person", name="X", aliases=[], body="b",
    ).save(tmp_path / "memory" / "entities" / "person" / "x.md")
    rebuild_fts_index(tmp_path)

    from durin.agent.tools.memory_search import MemorySearchTool
    import asyncio
    tool = MemorySearchTool(workspace=tmp_path, app_config=cfg)
    asyncio.run(tool.execute(query="X"))

    assert captured.get("overrides") == {"episodic": 1}
