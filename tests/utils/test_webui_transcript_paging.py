"""Byte-offset cursor pagination over the webui display transcript.

The transcript is append-only JSONL. A page is a byte window ending at the
cursor (file end when absent) whose start is expanded backward to a USER
message boundary so a turn and its tool events never split across pages.
Pure read path: these tests also pin that pagination never writes.

Fixture lines mirror the production writer (websocket channel): compact
JSON records dispatched on the "event" field — {"event":"user",...} user
turns, {"event":"message",...} assistant/trace frames (trace = message with
kind "tool_hint"/"progress"), reasoning_delta, and turn_end.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest

from durin.utils import webui_transcript as wt


def _dump(obj: dict) -> str:
    # Byte-identical to what append_transcript_object writes (compact JSON),
    # so the tests exercise _is_user_line's production branch.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _write_transcript(tmp_path: Path, monkeypatch, turns: int) -> Path:
    """Real-shaped transcript: 5 writer-format lines per turn, each stamped
    with a sequential turn marker ``i`` for coverage assertions."""
    monkeypatch.setattr(wt, "get_webui_dir", lambda: tmp_path)
    path = wt.webui_transcript_path("websocket:pagetest")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(turns):
            f.write(_dump({"event": "user", "chat_id": "pagetest", "text": f"question {i}", "i": i}) + "\n")
            f.write(_dump({"event": "reasoning_delta", "chat_id": "pagetest", "text": f"thinking {i}", "i": i}) + "\n")
            f.write(_dump({"event": "message", "chat_id": "pagetest", "kind": "tool_hint", "text": f"exec(step {i})", "i": i}) + "\n")
            f.write(_dump({"event": "message", "chat_id": "pagetest", "id": f"msg-{i}", "text": f"answer {i} " + "x" * 200, "i": i}) + "\n")
            f.write(_dump({"event": "turn_end", "chat_id": "pagetest", "latency_ms": 42, "i": i}) + "\n")
    return path


def _write_with_oversized_line(tmp_path: Path, monkeypatch) -> tuple[int, int, list[int]]:
    """Transcript with one 50 KB assistant line between normal turns.

    Scaled-down analogue of a multi-MB single line (the writer allows lines
    up to the 8 MB file cap): with target_bytes=4_000 and a monkeypatched
    _BOUNDARY_SCAN_LIMIT of 16_000 the initial 20_000-byte window fits
    entirely inside the oversized line, forcing the widening-retry path.

    Returns (total_lines, oversized_line_index, line_start_offsets); every
    line carries a sequential ``n`` for exactly-once coverage checks.
    """
    monkeypatch.setattr(wt, "get_webui_dir", lambda: tmp_path)
    monkeypatch.setattr(wt, "_BOUNDARY_SCAN_LIMIT", 16_000)
    path = wt.webui_transcript_path("websocket:pagetest")
    starts: list[int] = []
    n = 0
    with open(path, "wb") as f:

        def emit(obj: dict) -> None:
            nonlocal n
            starts.append(f.tell())
            f.write((_dump({**obj, "n": n}) + "\n").encode("utf-8"))
            n += 1

        def turn(i: int) -> None:
            emit({"event": "user", "chat_id": "pagetest", "text": f"question {i}"})
            emit({"event": "message", "chat_id": "pagetest", "kind": "tool_hint", "text": f"exec(step {i})"})
            emit({"event": "message", "chat_id": "pagetest", "text": f"answer {i}"})
            emit({"event": "turn_end", "chat_id": "pagetest"})

        for i in range(10):
            turn(i)
        emit({"event": "user", "chat_id": "pagetest", "text": "big question"})
        giant_idx = n
        emit({"event": "message", "chat_id": "pagetest", "text": "y" * 50_000})
        emit({"event": "turn_end", "chat_id": "pagetest"})
        for i in range(10, 20):
            turn(i)
    return n, giant_idx, starts


def _chain_backwards(before: int | None, target_bytes: int) -> tuple[list[int], int | None]:
    seen: list[int] = []
    cursor = before
    for _ in range(1000):
        lines, cursor = wt.read_transcript_page(
            "websocket:pagetest", before=cursor, target_bytes=target_bytes
        )
        seen = [ln["n"] for ln in lines] + seen
        if cursor is None:
            break
    return seen, cursor


def test_last_page_returns_tail_and_cursor(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=300)
    lines, cursor = wt.read_transcript_page("websocket:pagetest", target_bytes=10_000)
    assert lines, "tail page must not be empty"
    assert lines[0]["event"] == "user", "page must start at a user-message boundary"
    assert lines[-1]["i"] == 299, "tail page must end at the newest line"
    assert isinstance(cursor, int) and cursor > 0


def test_pages_chain_backwards_to_none_and_cover_everything(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=300)
    seen: list[int] = []
    cursor: int | None = None
    for _ in range(1000):
        lines, cursor = wt.read_transcript_page(
            "websocket:pagetest", before=cursor, target_bytes=10_000
        )
        seen = [ln["i"] for ln in lines if ln["event"] == "user"] + seen
        if cursor is None:
            break
    assert cursor is None, "chain must terminate"
    assert seen == list(range(300)), "no gaps, no duplicates, correct order"


def test_small_file_single_page(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=3)
    lines, cursor = wt.read_transcript_page("websocket:pagetest")
    assert len(lines) == 15
    assert cursor is None


def test_before_zero_or_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "get_webui_dir", lambda: tmp_path)
    lines, cursor = wt.read_transcript_page("websocket:nofile")
    assert lines == [] and cursor is None


def test_pagination_never_writes(tmp_path, monkeypatch):
    path = _write_transcript(tmp_path, monkeypatch, turns=50)
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    wt.read_transcript_page("websocket:pagetest", target_bytes=2_000)
    lines, cursor = wt.read_transcript_page("websocket:pagetest", before=5_000, target_bytes=2_000)
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before


def test_build_response_carries_prev_cursor(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=300)
    payload = wt.build_webui_thread_response("websocket:pagetest")
    assert payload is not None
    assert "prevCursor" in payload
    older = wt.build_webui_thread_response(
        "websocket:pagetest", before=payload["prevCursor"]
    )
    assert older is not None and older["messages"]


def test_replay_ids_unique_and_deterministic_across_pages(tmp_path, monkeypatch):
    """Reproduces the live-verification bug: replay mints fallback ids
    (``u-0``, ``as-1``, ...) per line index *within a page*, so two pages of
    the same session independently produced the identical id sequence. The
    webui's older-page splice dedupes by id, so that collision silently
    dropped the entire older page ("Beginning of conversation" despite
    unread history). ``build_webui_thread_response`` must namespace fallback
    ids by the page's own byte offset so pages never collide, while a
    refetch of the same page still yields identical ids (idempotent splice)."""
    _write_transcript(tmp_path, monkeypatch, turns=300)
    # Shrink the page-size default so this modest fixture spans several pages
    # instead of requiring megabytes of test data (same technique as
    # tests/api/test_webui_thread_endpoint.py).
    monkeypatch.setattr(
        wt, "read_transcript_page", functools.partial(wt.read_transcript_page, target_bytes=10_000)
    )

    page1 = wt.build_webui_thread_response("websocket:pagetest")
    assert page1 is not None and page1["messages"]
    assert page1["prevCursor"] is not None, "fixture must span multiple pages"

    page2 = wt.build_webui_thread_response("websocket:pagetest", before=page1["prevCursor"])
    assert page2 is not None and page2["messages"]

    ids1 = {m["id"] for m in page1["messages"]}
    ids2 = {m["id"] for m in page2["messages"]}
    assert ids1 & ids2 == set(), "page1 and page2 must never share a replay id"

    # Refetching page2 (same `before`) must be deterministic: a legitimate
    # round-trip dedupe on the frontend still needs identical ids.
    page2_again = wt.build_webui_thread_response("websocket:pagetest", before=page1["prevCursor"])
    assert page2_again is not None
    ids2_again = [m["id"] for m in page2_again["messages"]]
    assert [m["id"] for m in page2["messages"]] == ids2_again


def test_server_stamped_ids_bypass_page_namespacing(tmp_path, monkeypatch):
    """A record carrying its own persisted ``id`` (server-stamped, e.g. a
    command output) must survive replay byte-identical regardless of which
    page it lands on — that id is also the id of the live-streamed frame,
    and a later refetch must merge onto it rather than duplicate it."""
    monkeypatch.setattr(wt, "get_webui_dir", lambda: tmp_path)
    path = wt.webui_transcript_path("websocket:pagetest")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump({"event": "user", "chat_id": "pagetest", "text": "hi"}) + "\n")
        f.write(_dump({"event": "message", "chat_id": "pagetest", "id": "msg-stable", "text": "/status"}) + "\n")
        f.write(_dump({"event": "turn_end", "chat_id": "pagetest"}) + "\n")

    payload = wt.build_webui_thread_response("websocket:pagetest")
    assert payload is not None
    ids = [m["id"] for m in payload["messages"]]
    assert "msg-stable" in ids


def test_oversized_line_never_breaks_the_chain(tmp_path, monkeypatch):
    """A single line larger than the whole scan window must neither be
    skipped nor make the chain falsely report history exhausted: the
    widening retry re-reads with a doubled lookback until a complete
    line (or offset 0) is reached."""
    total, _giant_idx, _starts = _write_with_oversized_line(tmp_path, monkeypatch)
    seen, cursor = _chain_backwards(before=None, target_bytes=4_000)
    assert cursor is None, "chain must terminate only at offset 0"
    assert seen == list(range(total)), "every line exactly once, in order"


def test_chain_from_misaligned_offsets_no_gaps_no_dups(tmp_path, monkeypatch):
    """Chaining from an arbitrary mid-line ``before`` must cover every line
    that lies fully below the cut exactly once — including across the
    oversized line — and terminate with None only at offset 0. The cut
    line itself is unreadable by construction (its tail was excluded)."""
    total, giant_idx, starts = _write_with_oversized_line(tmp_path, monkeypatch)
    cases = (
        (giant_idx, 12_345),   # cut inside the oversized line
        (giant_idx + 4, 1),    # cut just after a later line's start
        (total - 3, 5),        # cut inside a normal line near the tail
    )
    for cut_line, wobble in cases:
        before = starts[cut_line] + wobble
        assert cut_line == len(starts) - 1 or before < starts[cut_line + 1]
        seen, cursor = _chain_backwards(before=before, target_bytes=4_000)
        assert cursor is None, f"cut at line {cut_line}+{wobble}: chain must terminate"
        assert seen == list(range(cut_line)), (
            f"cut at line {cut_line}+{wobble}: no gaps, no duplicates"
        )
