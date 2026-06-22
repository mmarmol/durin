"""`memory.index.write` payload must include `duration_ms` and `trigger`
so the `index_write_p95_ms` dashboard alert and FTS5 trigram size
monitoring are computable.

Trigger taxonomy reflects real callsites of `reindex_one_file`:
- `watcher`   — file_watcher detected a write under memory/
- `dream_apply` — emitted by dream.py after a consolidation lands
- `drift_repair` — health check detected staleness and repaired it
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import reindex_one_file


def _seed(tmp_path: Path) -> Path:
    p = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="content",
    ).save(p)
    return p


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    import durin.agent.tools._telemetry as _tel
    monkeypatch.setattr(
        _tel, "emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_index_write_includes_duration_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`duration_ms` powers the `index_write_p95_ms` alert (healthy range < 50ms per row)."""
    p = _seed(tmp_path)
    events = _capture(monkeypatch)

    reindex_one_file(tmp_path, p)

    writes = [e for e in events if e[0] == "memory.index.write"]
    assert len(writes) == 1
    payload = writes[0][1]
    assert "duration_ms" in payload
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0.0


def test_index_write_default_trigger_is_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher is the most common caller; default trigger keeps
    existing callsites (and tests like this one) working without
    explicit passing."""
    p = _seed(tmp_path)
    events = _capture(monkeypatch)

    reindex_one_file(tmp_path, p)

    payload = [e[1] for e in events if e[0] == "memory.index.write"][0]
    assert payload["trigger"] == "watcher"


def test_index_write_accepts_dream_apply_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _seed(tmp_path)
    events = _capture(monkeypatch)

    reindex_one_file(tmp_path, p, trigger="dream_apply")

    payload = [e[1] for e in events if e[0] == "memory.index.write"][0]
    assert payload["trigger"] == "dream_apply"


def test_index_write_accepts_drift_repair_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _seed(tmp_path)
    events = _capture(monkeypatch)

    reindex_one_file(tmp_path, p, trigger="drift_repair")

    payload = [e[1] for e in events if e[0] == "memory.index.write"][0]
    assert payload["trigger"] == "drift_repair"


def test_index_write_delete_op_includes_duration_and_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the file vanishes between watcher pickup and indexing,
    the row is deleted from FTS — the event must carry the same
    diagnostic fields as the upsert path."""
    p = _seed(tmp_path)
    # First index the file so a row exists to delete.
    reindex_one_file(tmp_path, p)
    p.unlink()

    events = _capture(monkeypatch)
    reindex_one_file(tmp_path, p, trigger="drift_repair")

    writes = [e for e in events if e[0] == "memory.index.write"]
    assert len(writes) == 1
    payload = writes[0][1]
    assert payload["op"] == "delete"
    assert payload["trigger"] == "drift_repair"
    assert "duration_ms" in payload
    assert isinstance(payload["duration_ms"], float)
