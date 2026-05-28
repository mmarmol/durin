"""Tests for the shared threshold-trigger helper (P7, doc 20).

These exercise the helper directly (workspace + entities + dream_config
contract). The store-path wrapper tests live in
``test_dream_triggers_beta2.py`` (backward compat).
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from durin.memory.store import store_memory
from durin.memory.threshold_trigger import (
    count_pending_for_trigger,
    maybe_dispatch_threshold_dream,
)

# `agent_created` scope is opened by `tests/conftest.py::_test_default_author_scope`
# (autouse). These tests model agent-observed writes; no local override needed.


def _make_dream_config(threshold: int = 3) -> Any:
    return SimpleNamespace(
        enabled=True,
        threshold_entries=threshold,
        min_seconds_between_runs=0,
        model_override=None,
        auto_absorb=SimpleNamespace(
            enabled=False,
            confidence_threshold=95,
            min_age_hours=24,
            judge_model=None,
        ),
    )


# ---------------------------------------------------------------------------
# count_pending_for_trigger — episodic + corpus contributions
# ---------------------------------------------------------------------------


def test_count_counts_episodic_post_cursor_entries(tmp_path: Path) -> None:
    for i in range(4):
        store_memory(
            tmp_path,
            content=f"observation {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    counts = count_pending_for_trigger(tmp_path)
    assert counts.get("person:alice") == 4


def test_count_includes_corpus_entries(tmp_path: Path) -> None:
    # 1 episodic + 2 corpus on the same entity → counts to 3.
    store_memory(
        tmp_path, content="observation",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 23),
    )
    for i in range(2):
        store_memory(
            tmp_path,
            content=f"ingested doc body {i}",
            class_name="corpus",
            entities=["person:alice"],
        )
    counts = count_pending_for_trigger(tmp_path)
    assert counts.get("person:alice") == 3


def test_count_filters_by_entity_when_requested(tmp_path: Path) -> None:
    store_memory(
        tmp_path, content="alice obs", entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 23),
    )
    store_memory(
        tmp_path, content="bob obs", entities=["person:bob"],
        valid_from=datetime.date(2026, 5, 23),
    )
    counts = count_pending_for_trigger(tmp_path, entity_filter="person:alice")
    assert counts.get("person:alice") == 1
    assert "person:bob" not in counts


def test_count_empty_workspace_returns_empty(tmp_path: Path) -> None:
    counts = count_pending_for_trigger(tmp_path)
    assert counts == {}


# ---------------------------------------------------------------------------
# maybe_dispatch_threshold_dream — fires daemon thread on crossing
# ---------------------------------------------------------------------------


def _stub_runner_factory(spawns: list[tuple[str, str]]):
    class _StubRunner:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(
            self,
            *,
            trigger: str,
            entity_filter: str | None = None,
            on_progress: Any = None,
        ) -> SimpleNamespace:
            spawns.append((trigger, entity_filter or ""))
            return SimpleNamespace(
                ran=True, reason="ok",
                entities_consolidated=1, entities_failed=0,
                duration_s=0.0,
            )

    return _StubRunner


def _wait_for_spawn(spawns: list, expected_count: int = 1, timeout_s: float = 1.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and len(spawns) < expected_count:
        time.sleep(0.02)


def test_dispatch_fires_at_or_above_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    for i in range(3):
        store_memory(
            tmp_path, content=f"obs {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=["person:alice"],
        dream_config=_make_dream_config(threshold=3),
        vector_index=None,
        source_trigger="test_trigger",
    )
    _wait_for_spawn(spawns)
    assert spawns == [("test_trigger", "person:alice")]


def test_dispatch_no_fire_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    store_memory(
        tmp_path, content="single obs",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 23),
    )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=["person:alice"],
        dream_config=_make_dream_config(threshold=5),
        vector_index=None,
        source_trigger="test_trigger",
    )
    time.sleep(0.1)
    assert spawns == []


def test_dispatch_no_fire_when_dream_config_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    for i in range(5):
        store_memory(
            tmp_path, content=f"obs {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=["person:alice"],
        dream_config=None,
        vector_index=None,
        source_trigger="test_trigger",
    )
    time.sleep(0.1)
    assert spawns == []


def test_dispatch_no_fire_when_threshold_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    for i in range(5):
        store_memory(
            tmp_path, content=f"obs {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=["person:alice"],
        dream_config=_make_dream_config(threshold=0),
        vector_index=None,
        source_trigger="test_trigger",
    )
    time.sleep(0.1)
    assert spawns == []


def test_dispatch_corpus_counts_toward_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    # 1 episodic + 2 corpus → 3 entries total, crosses threshold=3.
    store_memory(
        tmp_path, content="ep",
        entities=["person:alice"],
        valid_from=datetime.date(2026, 5, 23),
    )
    for i in range(2):
        store_memory(
            tmp_path, content=f"corpus body {i}",
            class_name="corpus",
            entities=["person:alice"],
        )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=["person:alice"],
        dream_config=_make_dream_config(threshold=3),
        vector_index=None,
        source_trigger="post_ingest_threshold",
    )
    _wait_for_spawn(spawns)
    assert spawns == [("post_ingest_threshold", "person:alice")]


def test_dispatch_empty_entities_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner",
        _stub_runner_factory(spawns),
    )
    maybe_dispatch_threshold_dream(
        workspace=tmp_path,
        entities=[],
        dream_config=_make_dream_config(threshold=1),
        vector_index=None,
        source_trigger="test_trigger",
    )
    time.sleep(0.05)
    assert spawns == []


def test_concurrent_dispatches_serialize_via_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Burst protection: 5 simultaneous dispatches → DreamRunner's
    lock+throttle absorb; we don't add a process-level dedup window.

    The helper itself doesn't enforce serialisation — it just spawns
    daemon threads. ``DreamRunner.run`` is where the lock lives. We
    stub DreamRunner with a stub that increments a counter; the
    assertion is that all spawned threads complete (no crash, no
    duplicated event loops, no shared-mutable-state corruption).
    """
    spawns: list[tuple[str, str]] = []
    lock_count = {"n": 0}

    class _StubRunner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, *, trigger: str, entity_filter: str | None = None,
                on_progress: Any = None) -> SimpleNamespace:
            # Simulate the lock — only one allowed per snapshot.
            lock_count["n"] += 1
            spawns.append((trigger, entity_filter or ""))
            time.sleep(0.02)
            lock_count["n"] -= 1
            return SimpleNamespace(ran=True, reason="ok",
                                   entities_consolidated=1, entities_failed=0,
                                   duration_s=0.02)

    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner", _StubRunner,
    )
    # Seed enough entries to cross threshold for the entity.
    for i in range(5):
        store_memory(
            tmp_path, content=f"obs {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    # Five concurrent dispatch calls for the same entity.
    for _ in range(5):
        maybe_dispatch_threshold_dream(
            workspace=tmp_path,
            entities=["person:alice"],
            dream_config=_make_dream_config(threshold=3),
            vector_index=None,
            source_trigger="burst_test",
        )
    # Wait for daemon threads to finish (each sleeps 0.02s inside the
    # stubbed runner).
    deadline = time.time() + 2.0
    while time.time() < deadline and (
        len(spawns) < 5 or lock_count["n"] != 0
    ):
        time.sleep(0.02)
    # All 5 dispatches reached the (stubbed) runner; the real lock in
    # DreamRunner is exercised in integration tests, not here.
    assert len(spawns) == 5
    # No corruption: counter went back to 0 after all threads released.
    assert lock_count["n"] == 0
