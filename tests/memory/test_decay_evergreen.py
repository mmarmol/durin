"""Decay / evergreen frontmatter on `MemoryEntry`.

Per `docs/memory/03_search_pipeline.md` §10:
- `decay_half_life: <int|null>` overrides the per-class default.
- `evergreen: true` forces no decay regardless of class or override.
- Evergreen wins over `decay_half_life`.
- Without either field, the per-class default applies (resolved at
  ranking time, not at schema time).

Phase 0 scope (per `docs/memory/09_implementation_roadmap.md` §3 d4):
**fields supported** — the ranking-time consumer arrives in a later
phase, but storage round-trip and `half_life_for` helper land here.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from durin.memory.schema import MemoryEntry
from durin.memory.decay import half_life_for, CLASS_HALF_LIFE_DEFAULTS
from durin.memory.storage import load_entry, save_entry


# ---------------------------------------------------------------------------
# MemoryEntry schema accepts the two new optional fields
# ---------------------------------------------------------------------------


class TestSchema:
    def test_defaults_are_none(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert entry.decay_half_life is None
        assert entry.evergreen is False

    def test_decay_half_life_int_accepted(self) -> None:
        entry = MemoryEntry(id="x", headline="h", decay_half_life=180)
        assert entry.decay_half_life == 180

    def test_decay_half_life_null_accepted(self) -> None:
        """`null` is the spec's signal for 'never decay this entry'."""
        entry = MemoryEntry(id="x", headline="h", decay_half_life=None)
        assert entry.decay_half_life is None

    def test_decay_half_life_negative_rejected(self) -> None:
        with pytest.raises(Exception):  # ValidationError
            MemoryEntry(id="x", headline="h", decay_half_life=-1)

    def test_evergreen_true_accepted(self) -> None:
        entry = MemoryEntry(id="x", headline="h", evergreen=True)
        assert entry.evergreen is True


# ---------------------------------------------------------------------------
# Round-trip through storage
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_preserves_decay_half_life(self, tmp_path: Path) -> None:
        entry = MemoryEntry(
            id="2010-marcelo-wedding", headline="wedding",
            valid_from=date(2010, 5, 15),
            decay_half_life=None,  # explicit "permanent fact"
        )
        path = tmp_path / "e.md"
        save_entry(entry, path)
        loaded = load_entry(path)
        # `decay_half_life` field set explicitly should survive — even
        # when its value is `null`, which is meaningful (permanent).
        assert loaded.decay_half_life is None
        # ... but how do we tell "field absent" from "field is null"?
        # The discriminator is in the FRONTMATTER text, not the loaded
        # object. Verify the literal frontmatter contains the key.
        assert "decay_half_life:" in path.read_text(encoding="utf-8")

    def test_round_trip_preserves_evergreen_true(self, tmp_path: Path) -> None:
        entry = MemoryEntry(id="x", headline="h", evergreen=True)
        path = tmp_path / "e.md"
        save_entry(entry, path)
        loaded = load_entry(path)
        assert loaded.evergreen is True

    def test_evergreen_false_default_omitted_from_frontmatter(
        self, tmp_path: Path,
    ) -> None:
        """Don't pollute v1 entries with `evergreen: false` lines."""
        entry = MemoryEntry(id="x", headline="h")
        path = tmp_path / "e.md"
        save_entry(entry, path)
        text = path.read_text(encoding="utf-8")
        assert "evergreen" not in text

    def test_decay_half_life_unset_omitted_from_frontmatter(
        self, tmp_path: Path,
    ) -> None:
        """If the field was never set, we don't emit a `decay_half_life:`
        line. Defaults are resolved at ranking time from the class."""
        entry = MemoryEntry(id="x", headline="h")
        path = tmp_path / "e.md"
        save_entry(entry, path)
        text = path.read_text(encoding="utf-8")
        assert "decay_half_life" not in text


# ---------------------------------------------------------------------------
# half_life_for resolver — implements doc 03 §10.5 logic
# ---------------------------------------------------------------------------


class TestHalfLifeFor:
    def test_evergreen_wins_over_everything(self) -> None:
        entry = MemoryEntry(
            id="x", headline="h", evergreen=True, decay_half_life=30,
        )
        assert half_life_for(entry, class_name="episodic") is None

    def test_per_entry_null_overrides_class_default(self) -> None:
        entry = MemoryEntry(id="x", headline="h", decay_half_life=None)
        # episodic class default is 90 days, but null override wins.
        # However: how to discriminate "never set" (use default) from
        # "explicitly null" (override)? The schema needs a sentinel.
        # We test the *unset* path here; the explicit-null path uses
        # a sentinel ("__unset__") so we check via model_fields_set.
        # For ergonomic API, pass `explicit_decay_half_life=True`.
        # Simplest: half_life_for accepts an additional kwarg.
        assert half_life_for(entry, class_name="episodic", decay_field_set=True) is None

    def test_default_episodic_is_90(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="episodic") == 90

    def test_default_session_summary_is_120(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="session_summary") == 120

    def test_default_entity_is_none(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="entity") is None

    def test_default_stable_is_none(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="stable") is None

    def test_default_corpus_is_none(self) -> None:
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="corpus") is None

    def test_per_entry_int_override_applies(self) -> None:
        entry = MemoryEntry(id="x", headline="h", decay_half_life=30)
        assert half_life_for(
            entry, class_name="episodic", decay_field_set=True,
        ) == 30

    def test_unknown_class_returns_none(self) -> None:
        """Unknown class = no decay rule defined → safe default = no decay."""
        entry = MemoryEntry(id="x", headline="h")
        assert half_life_for(entry, class_name="zzz_unknown") is None


# ---------------------------------------------------------------------------
# Class-default table
# ---------------------------------------------------------------------------


class TestClassDefaults:
    def test_table_matches_spec(self) -> None:
        # Spec values from doc 03 §10.2.
        assert CLASS_HALF_LIFE_DEFAULTS["episodic"] == 90
        assert CLASS_HALF_LIFE_DEFAULTS["session_summary"] == 120
        assert CLASS_HALF_LIFE_DEFAULTS["entity"] is None
        assert CLASS_HALF_LIFE_DEFAULTS["stable"] is None
        assert CLASS_HALF_LIFE_DEFAULTS["corpus"] is None
