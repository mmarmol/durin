"""Byte-offset cursor pagination over the webui display transcript.

The transcript is append-only JSONL. A page is a byte window ending at the
cursor (file end when absent) whose start is expanded backward to a USER
message boundary so a turn and its tool events never split across pages.
Pure read path: these tests also pin that pagination never writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.utils import webui_transcript as wt


def _write_transcript(tmp_path: Path, monkeypatch, turns: int) -> Path:
    monkeypatch.setattr(wt, "get_webui_dir", lambda: tmp_path)
    path = wt.webui_transcript_path("websocket:pagetest")
    with open(path, "w", encoding="utf-8") as f:
        for i in range(turns):
            f.write(json.dumps({"kind": "user", "text": f"question {i}", "i": i, "event": "user"}) + "\n")
            f.write(json.dumps({"kind": "trace", "text": f"tool run {i}", "i": i}) + "\n")
            f.write(json.dumps({"kind": "assistant", "text": f"answer {i} " + "x" * 200, "i": i}) + "\n")
    return path


def test_last_page_returns_tail_and_cursor(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=300)
    lines, cursor = wt.read_transcript_page("websocket:pagetest", target_bytes=10_000)
    assert lines, "tail page must not be empty"
    assert lines[0]["kind"] == "user", "page must start at a user-message boundary"
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
        seen = [ln["i"] for ln in lines if ln["kind"] == "user"] + seen
        if cursor is None:
            break
    assert cursor is None, "chain must terminate"
    assert seen == list(range(300)), "no gaps, no duplicates, correct order"


def test_small_file_single_page(tmp_path, monkeypatch):
    _write_transcript(tmp_path, monkeypatch, turns=3)
    lines, cursor = wt.read_transcript_page("websocket:pagetest")
    assert len(lines) == 9
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
