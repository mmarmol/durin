"""Append-only session archive: the file cap retains instead of destroying.

``enforce_file_cap`` bounds the LIVE file (``save()`` rewrites it whole every
turn, and the full list lives in RAM), but the trimmed prefix used to be
deleted — surviving only as summaries. It now flows through ``archive_sink``
into per-session append-only segments that are never rewritten, so retention
costs disk only. These tests pin the sink wiring, segment rotation, the disk
cap, and the failure contract (a broken sink must never break the cap).
"""
from __future__ import annotations

import json

import pytest

import durin.session.manager as manager_module
from durin.session.manager import SessionManager


def _mk_messages(n: int, start: int = 0) -> list[dict]:
    out = []
    for i in range(start, start + n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({
            "role": role,
            "content": f"message number {i}",
            "timestamp": f"2026-07-22T10:{i % 60:02d}:00",
        })
    return out


def _archived_messages(sm: SessionManager, key: str) -> list[dict]:
    msgs: list[dict] = []
    for path in sm.archive_paths(key):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                msgs.append(json.loads(line))
    return msgs


def test_append_creates_a_segment_with_the_exact_messages(tmp_path):
    sm = SessionManager(tmp_path)
    batch = _mk_messages(6)

    sm.append_to_archive("cli:test", batch)

    paths = sm.archive_paths("cli:test")
    assert len(paths) == 1
    assert paths[0].name.endswith(".000001.jsonl")
    assert _archived_messages(sm, "cli:test") == batch


def test_appends_accumulate_and_never_rewrite(tmp_path):
    """Append-only is the economic contract: the segment is only ever grown."""
    sm = SessionManager(tmp_path)
    sm.append_to_archive("cli:test", _mk_messages(3))
    first_bytes = sm.archive_paths("cli:test")[0].read_bytes()

    sm.append_to_archive("cli:test", _mk_messages(3, start=3))

    path = sm.archive_paths("cli:test")[0]
    assert path.read_bytes().startswith(first_bytes), "existing bytes must be untouched"
    assert len(_archived_messages(sm, "cli:test")) == 6


def test_segment_rotation_by_size(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "ARCHIVE_SEGMENT_MAX_BYTES", 200)
    sm = SessionManager(tmp_path)

    sm.append_to_archive("cli:test", _mk_messages(4))          # > 200 bytes
    sm.append_to_archive("cli:test", _mk_messages(4, start=4))  # must rotate

    names = [p.name for p in sm.archive_paths("cli:test")]
    assert len(names) == 2
    assert names[0].endswith(".000001.jsonl")
    assert names[1].endswith(".000002.jsonl")
    # Chronological order is preserved across segments.
    all_msgs = _archived_messages(sm, "cli:test")
    assert [m["content"] for m in all_msgs] == [f"message number {i}" for i in range(8)]


def test_disk_cap_prunes_oldest_never_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "ARCHIVE_SEGMENT_MAX_BYTES", 200)
    monkeypatch.setattr(manager_module, "ARCHIVE_TOTAL_MAX_BYTES", 500)
    sm = SessionManager(tmp_path)

    for batch in range(5):
        sm.append_to_archive("cli:test", _mk_messages(4, start=batch * 4))

    paths = sm.archive_paths("cli:test")
    total = sum(p.stat().st_size for p in paths)
    assert total <= 500 + 200, "total stays near the cap"
    # The newest segment (just written) always survives.
    assert paths, "pruning must never empty the archive"
    newest = _archived_messages(sm, "cli:test")[-1]
    assert newest["content"] == "message number 19"


def test_zero_total_cap_disables_archiving(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_module, "ARCHIVE_TOTAL_MAX_BYTES", 0)
    sm = SessionManager(tmp_path)
    sm.append_to_archive("cli:test", _mk_messages(4))
    assert sm.archive_paths("cli:test") == []


def test_keys_do_not_collide_on_shared_prefixes(tmp_path):
    """`websocket:a` and `websocket:a2` must land in distinct segment files."""
    sm = SessionManager(tmp_path)
    sm.append_to_archive("websocket:a", _mk_messages(2))
    sm.append_to_archive("websocket:a2", _mk_messages(3, start=10))

    a = _archived_messages(sm, "websocket:a")
    a2 = _archived_messages(sm, "websocket:a2")
    assert len(a) == 2 and len(a2) == 3
    assert {m["content"] for m in a}.isdisjoint({m["content"] for m in a2})


def test_enforce_file_cap_routes_the_whole_prefix_to_the_sink(tmp_path):
    """Integration: the cap trims the live list AND the archive receives the
    full dropped prefix — not just the unconsolidated slice on_archive gets."""
    sm = SessionManager(tmp_path)
    session = sm.get_or_create("cli:test")
    session.messages = _mk_messages(30)
    session.last_consolidated = 28  # nearly everything consolidated

    raw_archived: list[list[dict]] = []
    session.enforce_file_cap(
        on_archive=raw_archived.append,
        limit=10,
        archive_sink=lambda msgs: sm.append_to_archive("cli:test", msgs),
    )

    kept = len(session.messages)
    assert kept <= 10
    dropped = 30 - kept
    archived = _archived_messages(sm, "cli:test")
    assert len(archived) == dropped, "the WHOLE dropped prefix is retained"
    assert [m["content"] for m in archived] == [
        f"message number {i}" for i in range(dropped)
    ]
    # The breadcrumb path is unchanged: only the unconsolidated slice.
    if raw_archived:
        assert len(raw_archived[0]) == dropped - 28


def test_failing_sink_never_breaks_the_cap(tmp_path):
    """Availability over retention: if the archive write fails, the trim still
    happens (matching the live file's own guarantees) and the failure is not
    swallowed into the message list."""
    sm = SessionManager(tmp_path)
    session = sm.get_or_create("cli:test")
    session.messages = _mk_messages(30)
    session.last_consolidated = 30

    def _boom(_msgs):
        raise OSError("disk full")

    session.enforce_file_cap(on_archive=None, limit=10, archive_sink=_boom)

    assert len(session.messages) <= 10, "trim must proceed despite the sink failing"


def test_archive_emits_telemetry(tmp_path, monkeypatch):
    events: list[tuple[str, dict]] = []

    class _Sink:
        def log(self, event_type: str, data: dict) -> None:
            events.append((event_type, dict(data)))

    monkeypatch.setattr(
        "durin.telemetry.logger.current_telemetry", lambda: _Sink(),
    )
    sm = SessionManager(tmp_path)
    sm.append_to_archive("cli:test", _mk_messages(4))

    assert [e[0] for e in events] == ["session.archived"]
    payload = events[0][1]
    assert payload["session_key"] == "cli:test"
    assert payload["messages"] == 4
    assert payload["bytes_written"] > 0
    assert payload["pruned_segments"] == 0
