"""Tests for append-only WebUI transcript replay."""

from __future__ import annotations

from durin.utils.webui_transcript import (
    WEBUI_TRANSCRIPT_SCHEMA_VERSION,
    append_transcript_object,
    read_transcript_page,
    replay_transcript_to_ui_messages,
)


def test_append_and_read_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t1"
    append_transcript_object(key, {"event": "user", "chat_id": "t1", "text": "hello"})
    lines, prev_cursor = read_transcript_page(key)
    assert len(lines) == 1
    assert lines[0]["text"] == "hello"
    assert prev_cursor is None


def test_replay_delta_and_turn_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t2"
    for ev in (
        {"event": "user", "chat_id": "t2", "text": "q"},
        {"event": "reasoning_delta", "chat_id": "t2", "text": "think"},
        {"event": "reasoning_end", "chat_id": "t2"},
        {"event": "delta", "chat_id": "t2", "text": "a"},
        {"event": "stream_end", "chat_id": "t2"},
        {"event": "turn_end", "chat_id": "t2", "latency_ms": 42},
    ):
        append_transcript_object(key, ev)
    lines, _ = read_transcript_page(key)
    msgs = replay_transcript_to_ui_messages(lines)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "q"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "a"
    assert msgs[1]["reasoning"] == "think"
    assert msgs[1]["latencyMs"] == 42


def test_build_response_schema(monkeypatch, tmp_path) -> None:
    from durin.utils.webui_transcript import build_webui_thread_response

    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    key = "websocket:t3"
    append_transcript_object(key, {"event": "user", "chat_id": "t3", "text": "x"})
    out = build_webui_thread_response(key, augment_user_media=None)
    assert out is not None
    assert out["schemaVersion"] == WEBUI_TRANSCRIPT_SCHEMA_VERSION
    assert out["sessionKey"] == key
    assert len(out["messages"]) == 1


def test_replay_preserves_tool_events() -> None:
    """tool_hint records keep structured tool_events (merged by call_id) so
    the webui renders the same rich blocks live and after reload."""
    lines = [
        {"event": "message", "kind": "tool_hint", "text": "exec(...)",
         "tool_events": [{"version": 1, "phase": "start", "call_id": "c1",
                          "name": "exec", "arguments": {"command": "ls"}}]},
        {"event": "message", "kind": "tool_hint", "text": "",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "c1",
                          "name": "exec", "result": "out.txt"}]},
        {"event": "turn_end"},
    ]
    messages = replay_transcript_to_ui_messages(lines)
    traces = [m for m in messages if m.get("kind") == "trace"]
    assert len(traces) == 1
    events = traces[0]["toolEvents"]
    assert len(events) == 1                                  # merged by call_id
    assert events[0]["phase"] == "end"
    assert events[0]["arguments"] == {"command": "ls"}       # start args survive
    assert events[0]["result"] == "out.txt"


def test_replay_keeps_eventful_record_without_trace_lines() -> None:
    """A tool_hint frame whose events carry no start phase (no formatted
    trace line) must still survive replay — dropping it loses the payload."""
    lines = [
        {"event": "message", "kind": "tool_hint", "text": "",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "q1",
                          "name": "ask_user_question",
                          "arguments": {"question": "Color?", "options": ["red"]}}]},
        {"event": "turn_end"},
    ]
    messages = replay_transcript_to_ui_messages(lines)
    traces = [m for m in messages if m.get("kind") == "trace"]
    assert len(traces) == 1
    assert traces[0]["toolEvents"][0]["arguments"]["question"] == "Color?"


def test_replay_merges_blocking_tool_end_after_user_answer() -> None:
    """Blocking ask_user: a user answer recorded between the start and end
    tool_hint frames must not split the question into two rows on replay."""
    lines = [
        {"event": "message", "kind": "tool_hint", "text": "ask_user_question(...)",
         "tool_events": [{"version": 1, "phase": "start", "call_id": "q1",
                          "name": "ask_user_question",
                          "arguments": {"question": "¿Color?", "options": ["Rojo"]}}]},
        {"event": "user", "text": "Rojo"},
        {"event": "message", "kind": "tool_hint", "text": "",
         "tool_events": [{"version": 1, "phase": "end", "call_id": "q1",
                          "name": "ask_user_question", "result": "ok"}]},
        {"event": "turn_end"},
    ]
    messages = replay_transcript_to_ui_messages(lines)
    traces = [m for m in messages if m.get("kind") == "trace"]
    assert len(traces) == 1                                  # merged, not duplicated
    events = traces[0]["toolEvents"]
    assert len(events) == 1
    assert events[0]["phase"] == "end"
    assert events[0]["arguments"]["question"] == "¿Color?"   # start args survive
    # The answer bubble is still its own message.
    users = [m for m in messages if m.get("role") == "user"]
    assert any(m.get("content") == "Rojo" for m in users)


def test_replay_infers_attachment_kind_from_name() -> None:
    """Assistant attachment kinds come from the filename: an ``.html`` mockup
    must replay as ``html`` (rendered sandboxed), not as a broken image."""
    lines = [
        {"event": "user", "chat_id": "t9", "text": "mockup please"},
        {
            "event": "message",
            "chat_id": "t9",
            "text": "here you go",
            "media_urls": [
                {"url": "/api/media/sig/page.html", "name": "page.html"},
                {"url": "/api/media/sig/shot.png", "name": "shot.png"},
                {"url": "/api/media/sig/clip.mp4", "name": "clip.mp4"},
                {"url": "/api/media/sig/notes.pdf", "name": "notes.pdf"},
                {"url": "/api/media/sig/opaque", "name": ""},
            ],
        },
        {"event": "turn_end", "chat_id": "t9"},
    ]
    msgs = replay_transcript_to_ui_messages(lines)
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    kinds = [m["kind"] for m in assistant["media"]]
    assert kinds == ["html", "image", "video", "file", "image"]


# ---------------------------------------------------------------------------
# TranscriptWriter — coalesced off-loop appends (one fsync per batch)
# ---------------------------------------------------------------------------

import asyncio
import json
import os

import pytest

from durin.utils import webui_transcript as wt


@pytest.fixture()
def writer_env(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    wt.reset_transcript_writer_for_tests()
    yield tmp_path
    wt.reset_transcript_writer_for_tests()


def _read_events(key):
    path = wt.webui_transcript_path(key)
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_enqueue_preserves_order_across_flushes(writer_env):
    async def run():
        w = wt.get_transcript_writer()
        for i in range(250):
            w.enqueue("websocket:s1", {"event": "delta", "i": i})
        await w.flush("websocket:s1")
        w.enqueue("websocket:s1", {"event": "turn_end"})
        await w.flush("websocket:s1")

    asyncio.run(run())
    events = _read_events("websocket:s1")
    assert [e.get("i") for e in events[:250]] == list(range(250))
    assert events[-1]["event"] == "turn_end"


def test_single_fsync_per_batch(writer_env, monkeypatch):
    counts = {"fsync": 0}
    real_fsync = os.fsync

    def counting_fsync(fd):
        counts["fsync"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(wt.os, "fsync", counting_fsync)

    async def run():
        w = wt.get_transcript_writer()
        for i in range(100):
            w.enqueue("websocket:s1", {"event": "delta", "i": i})
        await w.flush("websocket:s1")

    asyncio.run(run())
    assert counts["fsync"] == 1
    assert len(_read_events("websocket:s1")) == 100


def test_flush_is_read_barrier(writer_env):
    async def run():
        w = wt.get_transcript_writer()
        w.enqueue("websocket:s1", {"event": "delta", "i": 0})
        await w.flush("websocket:s1")
        assert wt.webui_transcript_path("websocket:s1").exists()

    asyncio.run(run())


def test_oversized_object_dropped_not_raised(writer_env):
    async def run():
        w = wt.get_transcript_writer()
        w.enqueue("websocket:s1", {"event": "delta", "text": "x" * (9 * 1024 * 1024)})
        w.enqueue("websocket:s1", {"event": "delta", "i": 1})
        await w.flush("websocket:s1")

    asyncio.run(run())
    events = _read_events("websocket:s1")
    assert len(events) == 1 and events[0]["i"] == 1


def test_interval_flush_without_explicit_barrier(writer_env):
    async def run():
        w = wt.get_transcript_writer()
        w.enqueue("websocket:s1", {"event": "delta", "i": 0})
        await asyncio.sleep(0.4)  # > flush interval, no barrier

    asyncio.run(run())
    assert wt.webui_transcript_path("websocket:s1").exists()


def test_aclose_drains_pending(writer_env):
    async def run():
        w = wt.get_transcript_writer()
        w.enqueue("websocket:s1", {"event": "delta", "i": 7})
        await w.aclose()

    asyncio.run(run())
    events = _read_events("websocket:s1")
    assert events and events[0]["i"] == 7
