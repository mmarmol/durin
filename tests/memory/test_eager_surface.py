"""Tests for the frozen per-session eager memory surface: the snapshot type,
its round trip through session metadata, and the staleness rule.

This module only covers the foundation (the type + rule); building and
consuming snapshots during a real prompt build is exercised elsewhere once
that wiring lands.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from durin.memory.eager_surface import SNAPSHOT_KEY, EagerSnapshot, snapshot_is_stale


def _snapshot(**overrides) -> EagerSnapshot:
    fields = dict(
        pinned="pinned block text",
        hot="hot layer text",
        refs=frozenset({"person:marcelo", "topic:durin"}),
        turn=4,
        frozen_at="2026-09-08T12:00:00+00:00",
    )
    fields.update(overrides)
    return EagerSnapshot(**fields)


def test_snapshot_key_is_the_documented_metadata_key():
    assert SNAPSHOT_KEY == "_eager_surface"


# ---------------------------------------------------------------------------
# to_metadata / from_metadata round trip
# ---------------------------------------------------------------------------


def test_to_metadata_round_trips_through_from_metadata():
    snap = _snapshot()
    restored = EagerSnapshot.from_metadata(snap.to_metadata())
    assert restored == snap


def test_to_metadata_is_json_safe_and_survives_a_json_round_trip():
    # session.metadata is persisted as JSON via the sidecar — refs (a
    # frozenset) must come out as something json.dumps can carry.
    snap = _snapshot()
    encoded = json.dumps(snap.to_metadata())
    restored = EagerSnapshot.from_metadata(json.loads(encoded))
    assert restored == snap


# ---------------------------------------------------------------------------
# from_metadata: None on any shape error
# ---------------------------------------------------------------------------


def test_from_metadata_returns_none_for_non_dict_input():
    assert EagerSnapshot.from_metadata(None) is None
    assert EagerSnapshot.from_metadata("garbage") is None
    assert EagerSnapshot.from_metadata([1, 2, 3]) is None


def test_from_metadata_returns_none_for_missing_keys():
    assert EagerSnapshot.from_metadata({}) is None
    assert EagerSnapshot.from_metadata({"pinned": "p", "hot": "h"}) is None


def test_from_metadata_returns_none_for_wrong_field_types():
    base = _snapshot().to_metadata()
    assert EagerSnapshot.from_metadata(dict(base, pinned=123)) is None
    assert EagerSnapshot.from_metadata(dict(base, hot=None)) is None
    assert EagerSnapshot.from_metadata(dict(base, refs="not-a-list")) is None
    assert EagerSnapshot.from_metadata(dict(base, refs=[1, 2])) is None
    assert EagerSnapshot.from_metadata(dict(base, turn="4")) is None
    assert EagerSnapshot.from_metadata(dict(base, frozen_at=None)) is None


def test_from_metadata_returns_none_for_unparseable_frozen_at():
    # A hand-edited sidecar can carry a frozen_at that is a string but not a
    # valid ISO-8601 timestamp. from_metadata must discard it like any other
    # shape error rather than constructing a snapshot that later blows up
    # snapshot_is_stale's datetime.fromisoformat call.
    base = _snapshot().to_metadata()
    assert EagerSnapshot.from_metadata(dict(base, frozen_at="not-a-timestamp")) is None


# ---------------------------------------------------------------------------
# snapshot_is_stale
# ---------------------------------------------------------------------------


def test_snapshot_is_stale_false_with_refresh_after_min_zero_at_any_age():
    ancient = _snapshot(frozen_at="2000-01-01T00:00:00+00:00")
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    assert snapshot_is_stale(ancient, refresh_after_min=0, now=now) is False


def test_snapshot_is_stale_false_within_the_refresh_window():
    frozen_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    snap = _snapshot(frozen_at=frozen_at.isoformat())
    now = frozen_at + timedelta(minutes=5)
    assert snapshot_is_stale(snap, refresh_after_min=10, now=now) is False


def test_snapshot_is_stale_true_past_the_refresh_window():
    frozen_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    snap = _snapshot(frozen_at=frozen_at.isoformat())
    now = frozen_at + timedelta(minutes=15)
    assert snapshot_is_stale(snap, refresh_after_min=10, now=now) is True


def test_snapshot_is_stale_true_for_unparseable_frozen_at():
    # from_metadata rejects a garbage frozen_at before a snapshot is ever
    # built (see test_from_metadata_returns_none_for_unparseable_frozen_at),
    # but snapshot_is_stale must not trust that as its only guard: given a
    # snapshot built some other way with a bad frozen_at, it must still
    # never raise — it treats the parse failure as stale so the caller
    # falls back to a live render instead of the prompt build crashing.
    snap = _snapshot(frozen_at="not-a-timestamp")
    assert snapshot_is_stale(snap, refresh_after_min=5) is True


def test_snapshot_is_stale_true_for_a_naive_frozen_at():
    # A timestamp with no offset parses fine but cannot be compared against
    # the aware "now" the rule measures age from. The prompt build calls this
    # on every turn once a refresh window is configured, so a hand-edited or
    # foreign-written sidecar must degrade to a live render, not take the
    # turn down with a TypeError.
    snap = _snapshot(frozen_at="2026-09-08T12:00:00")
    assert snapshot_is_stale(snap, refresh_after_min=5) is True
