"""MemoryService.dream_digest — unit tests.

Calls the service directly (no HTTP).  Telemetry reads are monkeypatched
so the tests never touch disk and remain deterministic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from durin.service.memory import (
    DreamDigest,
    DreamDigestQuery,
    DreamEvent,
    MemoryService,
)
from durin.service.principal import Principal
from durin.service.types import ForbiddenError

LOCAL = Principal.local()


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(workspace_resolver=lambda: tmp_path)


def _jsonl_line(event_type: str, data: dict, ts: float | None = None) -> str:
    entry: dict = {"ts": ts or time.time(), "type": event_type}
    if data:
        entry["data"] = data
    return json.dumps(entry)


# ---------------------------------------------------------------------------
# Helpers: seed a fake telemetry JSONL file
# ---------------------------------------------------------------------------


def _seed_telemetry(telemetry_dir: Path, lines: list[str]) -> None:
    """Write lines into a single JSONL segment the reader can scan."""
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    seg = telemetry_dir / "test-session_2026-06-24.jsonl"
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# scope guard
# ---------------------------------------------------------------------------


async def test_dream_digest_requires_memory_read(tmp_path: Path):
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await _service(tmp_path).dream_digest(DreamDigestQuery(), principal)


# ---------------------------------------------------------------------------
# empty telemetry -> empty digest
# ---------------------------------------------------------------------------


async def test_dream_digest_empty_telemetry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "durin.service.memory._telemetry_dir",
        lambda: tmp_path / "telemetry",
    )
    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)
    assert isinstance(result, DreamDigest)
    assert result.events == []
    assert result.last_run_at_ms is None


# ---------------------------------------------------------------------------
# absorb.auto_merged -> kind="merged"
# ---------------------------------------------------------------------------


async def test_dream_digest_maps_auto_merged(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts = 1_700_000_000.0
    lines = [
        _jsonl_line("memory.absorb.auto_merged", {
            "canonical": "person:alice",
            "absorbed": "person:alice-v2",
            "confidence": 92,
            "sha": "abc123",
        }, ts=ts),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert len(result.events) == 1
    ev = result.events[0]
    assert ev.kind == "merged"
    assert ev.ref == "person:alice"
    assert ev.ref_kind == "entity"
    assert ev.at_ms == int(ts * 1000)
    assert result.last_run_at_ms == int(ts * 1000)


# ---------------------------------------------------------------------------
# dream.discover -> kind="created" per entity ref
# ---------------------------------------------------------------------------


async def test_dream_digest_maps_discover(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts = 1_700_000_100.0
    lines = [
        _jsonl_line("memory.dream.discover", {
            "proposed": 3,
            "written": 2,
            "skipped": 1,
            "refs": ["project:durin", "person:bob"],
        }, ts=ts),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert len(result.events) == 2
    kinds = {ev.kind for ev in result.events}
    assert kinds == {"created"}
    refs = {ev.ref for ev in result.events}
    assert refs == {"project:durin", "person:bob"}
    for ev in result.events:
        assert ev.ref_kind == "entity"
        assert ev.at_ms == int(ts * 1000)


# ---------------------------------------------------------------------------
# dream.skill_extract -> kind="improved"
# ---------------------------------------------------------------------------


async def test_dream_digest_maps_skill_extract(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts = 1_700_000_200.0
    lines = [
        _jsonl_line("memory.dream.skill_extract", {
            "skills_touched": 1,
            "duration_ms": 250,
        }, ts=ts),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert len(result.events) == 1
    ev = result.events[0]
    assert ev.kind == "improved"
    assert ev.ref is None
    assert ev.ref_kind == "skill"
    assert ev.at_ms == int(ts * 1000)


# ---------------------------------------------------------------------------
# dream.learnings -> kind="created" per ref
# ---------------------------------------------------------------------------


async def test_dream_digest_maps_learnings(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts = 1_700_000_300.0
    lines = [
        _jsonl_line("memory.dream.learnings", {
            "proposed": 2,
            "written": 1,
            "refs": ["feedback:deploy-tip"],
        }, ts=ts),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert len(result.events) == 1
    ev = result.events[0]
    assert ev.kind == "created"
    assert ev.ref == "feedback:deploy-tip"
    assert ev.ref_kind == "entity"


# ---------------------------------------------------------------------------
# last_run_at_ms uses dream.end / dream.start if present
# ---------------------------------------------------------------------------


async def test_dream_digest_last_run_from_dream_end(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts_event = 1_700_000_000.0
    ts_end = 1_700_001_000.0
    lines = [
        _jsonl_line("memory.absorb.auto_merged", {
            "canonical": "person:alice",
            "absorbed": "person:x",
            "confidence": 90,
            "sha": "a",
        }, ts=ts_event),
        _jsonl_line("memory.dream.end", {
            "kind": "extract",
            "duration_ms": 5000,
        }, ts=ts_end),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert result.last_run_at_ms == int(ts_end * 1000)


# ---------------------------------------------------------------------------
# limit param
# ---------------------------------------------------------------------------


async def test_dream_digest_respects_limit(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts_base = 1_700_000_000.0
    # 5 auto_merged events
    lines = [
        _jsonl_line("memory.absorb.auto_merged", {
            "canonical": f"person:p{i}",
            "absorbed": f"person:p{i}-old",
            "confidence": 90,
            "sha": "x",
        }, ts=ts_base + i)
        for i in range(5)
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(limit=2), LOCAL)

    assert len(result.events) == 2


# ---------------------------------------------------------------------------
# newest-first sort
# ---------------------------------------------------------------------------


async def test_dream_digest_sorted_newest_first(tmp_path: Path, monkeypatch):
    tel_dir = tmp_path / "telemetry"
    ts_old = 1_700_000_000.0
    ts_new = 1_700_001_000.0
    lines = [
        _jsonl_line("memory.absorb.auto_merged", {
            "canonical": "person:old",
            "absorbed": "person:old-v2",
            "confidence": 90,
            "sha": "a",
        }, ts=ts_old),
        _jsonl_line("memory.absorb.auto_merged", {
            "canonical": "person:new",
            "absorbed": "person:new-v2",
            "confidence": 90,
            "sha": "b",
        }, ts=ts_new),
    ]
    _seed_telemetry(tel_dir, lines)
    monkeypatch.setattr("durin.service.memory._telemetry_dir", lambda: tel_dir)

    result = await _service(tmp_path).dream_digest(DreamDigestQuery(), LOCAL)

    assert len(result.events) == 2
    assert result.events[0].at_ms > result.events[1].at_ms
    assert result.events[0].ref == "person:new"
