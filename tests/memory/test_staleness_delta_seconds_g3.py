"""G3 (audit fourth pass, 2026-05-28): `memory.index.staleness_detected`
emits `delta_seconds` when `reason='mtime_lag'`.

F11 dropped this field from doc 07 §9.3 claiming "the delta is
implicit in the join with `memory.index.write` posterior". That
justification was technically wrong: the recovery latency
(write_time - detect_time) is a different metric from the
staleness magnitude (file_mtime - indexed_mtime). Without the
magnitude, an operator can only count staleness events but not
graph p50/p95 of how far behind the watcher fell.

G3 ships the field as `NotRequired[float]` — set only on
`mtime_lag` events where the delta is meaningful (missing_row and
row_for_missing_file have no indexed_mtime to compare).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def _make_entity_page(workspace: Path, type_: str, slug: str) -> Path:
    """Use an entity_page rather than a memory entry — the indexer's
    URI scheme for entity pages (`<type>:<slug>`) matches what
    `detect_index_staleness` derives from disk. Memory entries have a
    pre-existing URI mismatch (indexer stores `memory/<class>/<id>`
    but the cron walker computes `<id>` alone) that's out of scope
    for G3; tracked separately."""
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type=type_, name=slug.replace("_", " ").title(),
        aliases=[], body="b",
    )
    path = workspace / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    import durin.memory.indexer as idx_mod
    real_emit = idx_mod.emit_tool_event if hasattr(
        idx_mod, "emit_tool_event",
    ) else None
    # The indexer imports emit_tool_event lazily inside _emit_*; patch
    # the source module so the lazy import binds to our spy.
    import durin.agent.tools._telemetry as tel
    monkeypatch.setattr(
        tel, "emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_mtime_lag_emits_delta_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose disk mtime is N seconds ahead of its indexed
    row's mtime emits `delta_seconds` ≈ N on the mtime_lag event."""
    from durin.memory.indexer import (
        detect_index_staleness, rebuild_fts_index,
    )

    md = _make_entity_page(tmp_path, "person", "g3_subject")
    rebuild_fts_index(tmp_path)

    # Move the file's mtime forward 120 seconds; the indexed row stays
    # at its original mtime so the cron sees a real gap.
    future = time.time() + 120
    os.utime(md, (future, future))

    events = _capture(monkeypatch)
    detect_index_staleness(tmp_path)

    lags = [
        d for t, d in events
        if t == "memory.index.staleness_detected"
        and d.get("reason") == "mtime_lag"
    ]
    assert len(lags) == 1, lags
    payload = lags[0]
    assert "delta_seconds" in payload
    # Allow some slack — the indexer's recorded mtime is close to
    # 'now', the file's mtime is 'now + 120'.
    assert 100.0 <= payload["delta_seconds"] <= 200.0


def test_missing_row_omits_delta_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file present on disk but absent from the FTS index has no
    indexed_mtime to compare against — `delta_seconds` is omitted."""
    from durin.memory.indexer import (
        detect_index_staleness, rebuild_fts_index,
    )

    # Rebuild on an empty workspace so the FTS table exists but has
    # no rows.
    (tmp_path / "memory").mkdir()
    rebuild_fts_index(tmp_path)

    # Now add a file AFTER the rebuild → missing_row case.
    _make_entity_page(tmp_path, "person", "g3_late_arrival")

    events = _capture(monkeypatch)
    detect_index_staleness(tmp_path)

    missing = [
        d for t, d in events
        if t == "memory.index.staleness_detected"
        and d.get("reason") == "missing_row"
    ]
    assert len(missing) == 1
    assert "delta_seconds" not in missing[0]


def test_row_for_missing_file_omits_delta_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose .md was deleted from disk has nothing to compare
    against — `delta_seconds` is omitted."""
    from durin.memory.indexer import (
        detect_index_staleness, rebuild_fts_index,
    )

    md = _make_entity_page(tmp_path, "person", "g3_doomed")
    rebuild_fts_index(tmp_path)
    md.unlink()

    events = _capture(monkeypatch)
    detect_index_staleness(tmp_path)

    orphans = [
        d for t, d in events
        if t == "memory.index.staleness_detected"
        and d.get("reason") == "row_for_missing_file"
    ]
    assert len(orphans) == 1
    assert "delta_seconds" not in orphans[0]


def test_typeddict_declares_delta_seconds_not_required() -> None:
    """The schema TypedDict must mark `delta_seconds` as
    `NotRequired[float]` so dashboards know it only fires on
    mtime_lag without breaking schema validation for the other
    two reasons."""
    from durin.telemetry.schema import (
        MemoryIndexStalenessDetectedEvent,
    )

    annotations = MemoryIndexStalenessDetectedEvent.__annotations__
    assert "delta_seconds" in annotations
    # `NotRequired[float]` shows up as `typing.NotRequired[float]`;
    # checking by repr keeps the test resilient to typing module
    # internals.
    raw = repr(annotations["delta_seconds"])
    assert "NotRequired" in raw
    assert "float" in raw
