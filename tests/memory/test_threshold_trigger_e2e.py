"""End-to-end tests for the P7 threshold trigger.

These exercise the FULL pipeline:

1. ``MemoryStoreTool.execute()`` writes an entity-tagged entry.
2. The shared ``maybe_dispatch_threshold_dream`` counts post-cursor +
   corpus entries for the entity, finds the count >= threshold.
3. A daemon thread is spawned that constructs a real ``DreamRunner``.
4. ``DreamRunner.run(trigger=...)`` acquires the lock and invokes a
   real ``DreamConsolidator`` with our stub LLM (canned response).
5. The consolidator writes a canonical entity page to
   ``memory/entities/<type>/<slug>.md`` and refreshes the alias index.

We assert on the **observable side-effects on disk and in telemetry**
— this is the contract daily-driver depends on. The LLM is the only
seam we stub (real LLM calls are too slow/flaky for unit-level CI).
"""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from durin.agent.tools.memory_store import MemoryStoreTool
from durin.memory.dream_runner import DreamRunner as _RealRunner
from durin.memory.store import store_memory

# Default `agent_created` scope opened by `tests/conftest.py` (autouse).


def _stub_llm_for_alice():
    """Stub LLM that produces a well-formed dream response for Alice."""
    response = (
        "===PAGE===\n"
        "---\n"
        "type: person\n"
        "name: Alice\n"
        "aliases: [alice]\n"
        "dream_processed_through: 2026-05-23T00:00:00\n"
        "---\n"
        "\n"
        "# Alice\n"
        "\n## Current State\nConsolidated from 5 observations.\n"
        "===COMMIT===\n"
        "Consolidate person:alice (rev 1)\n"
        "\nE2E threshold trigger pass.\n"
        "\nSources: e1\nEntities-touched: person:alice\n"
        "Cursor-after: 2026-05-23T00:00:00\n"
        "===END===\n"
    )

    def stub(prompt: str, *, model: str) -> str:
        return response

    return stub


def _make_dream_config(threshold: int = 5) -> Any:
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


def _wait_for_path(path: Path, *, timeout_s: float = 8.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# E2E: store-path threshold fires real Dream that writes canonical page
# ---------------------------------------------------------------------------


def test_e2e_store_threshold_writes_canonical_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5th write crosses threshold=5 → DreamRunner spawned → canonical
    page exists on disk after the daemon thread completes."""

    # Inject stub LLM into the DreamConsolidator that DreamRunner will
    # construct internally. We can't pass llm_invoke through the
    # threshold helper, so we monkeypatch DreamRunner's constructor
    # to inject the stub before delegating to the real init.
    stub_llm = _stub_llm_for_alice()
    original_init = _RealRunner.__init__

    def patched_init(self: Any, **kwargs: Any) -> None:
        kwargs.setdefault("llm_invoke", stub_llm)
        original_init(self, **kwargs)

    monkeypatch.setattr(_RealRunner, "__init__", patched_init)

    # Pre-seed 4 entries — below threshold.
    for i in range(4):
        store_memory(
            tmp_path,
            content=f"alice observation {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )

    canonical = tmp_path / "memory" / "entities" / "person" / "alice.md"
    assert not canonical.exists(), "precondition: no canonical yet"

    # 5th write via the tool → crosses threshold=5 → dispatches.
    tool = MemoryStoreTool(
        workspace=tmp_path,
        embedding_model=None,
        dream_config=_make_dream_config(threshold=5),
    )
    asyncio.run(tool.execute(
        content="alice observation 4",
        class_name="episodic",
        entities=["person:alice"],
    ))

    # Wait for daemon thread → DreamRunner → consolidator → disk write.
    assert _wait_for_path(canonical), (
        f"canonical page never appeared at {canonical}; "
        f"existing entities dir: "
        f"{list((tmp_path / 'memory' / 'entities').rglob('*'))}"
    )

    content = canonical.read_text()
    assert "type: person" in content
    assert "name: Alice" in content
    assert "Consolidated from 5 observations" in content


def test_e2e_below_threshold_does_not_consolidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4 writes with threshold=5 → no Dream → no canonical page."""

    stub_llm = _stub_llm_for_alice()
    original_init = _RealRunner.__init__

    def patched_init(self: Any, **kwargs: Any) -> None:
        kwargs.setdefault("llm_invoke", stub_llm)
        original_init(self, **kwargs)

    monkeypatch.setattr(_RealRunner, "__init__", patched_init)

    tool = MemoryStoreTool(
        workspace=tmp_path,
        embedding_model=None,
        dream_config=_make_dream_config(threshold=5),
    )
    for i in range(4):
        asyncio.run(tool.execute(
            content=f"alice obs {i}",
            class_name="episodic",
            entities=["person:alice"],
        ))

    # Give the daemon thread a fair chance to run.
    time.sleep(0.3)

    canonical = tmp_path / "memory" / "entities" / "person" / "alice.md"
    assert not canonical.exists(), (
        "below-threshold writes must not consolidate"
    )


def test_e2e_corpus_entries_count_toward_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpus entries count even though Dream itself only consolidates
    episodic — verifies the trigger semantics (signal vs source)."""

    stub_llm = _stub_llm_for_alice()
    original_init = _RealRunner.__init__

    def patched_init(self: Any, **kwargs: Any) -> None:
        kwargs.setdefault("llm_invoke", stub_llm)
        original_init(self, **kwargs)

    monkeypatch.setattr(_RealRunner, "__init__", patched_init)

    # 3 episodic + 2 corpus = 5 total signal, crosses threshold=5
    # on the 5th write (2nd corpus).
    for i in range(3):
        store_memory(
            tmp_path, content=f"alice ep {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )
    store_memory(
        tmp_path, content="alice corpus 0",
        class_name="corpus",
        entities=["person:alice"],
    )

    canonical = tmp_path / "memory" / "entities" / "person" / "alice.md"
    assert not canonical.exists(), "after 4 entries, should not fire yet"

    # 5th entry (via the tool, signal counts: 3 ep + 1 existing corpus
    # + 1 new corpus = 5) crosses threshold.
    tool = MemoryStoreTool(
        workspace=tmp_path,
        embedding_model=None,
        dream_config=_make_dream_config(threshold=5),
    )
    asyncio.run(tool.execute(
        content="alice corpus 1",
        class_name="corpus",
        entities=["person:alice"],
    ))

    assert _wait_for_path(canonical), (
        "corpus-driven threshold should fire dream"
    )


# ---------------------------------------------------------------------------
# E2E: telemetry confirms the trigger label
# ---------------------------------------------------------------------------


def test_e2e_telemetry_records_trigger_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dream fires from the store path, telemetry tags the event
    with ``trigger="threshold"`` (backward-compat label preserved)."""

    stub_llm = _stub_llm_for_alice()
    original_init = _RealRunner.__init__
    events: list[tuple[str, dict]] = []

    def patched_init(self: Any, **kwargs: Any) -> None:
        kwargs.setdefault("llm_invoke", stub_llm)
        original_init(self, **kwargs)

    monkeypatch.setattr(_RealRunner, "__init__", patched_init)

    # Capture telemetry by stubbing the emit function the dream runner
    # uses. dream_runner.emit_tool_event is the same shared free function.
    from durin.agent.tools import _telemetry as _tel_mod

    real_emit = _tel_mod.emit_tool_event

    def capture(event_type: str, data: dict) -> None:
        events.append((event_type, dict(data)))
        real_emit(event_type, data)

    monkeypatch.setattr(_tel_mod, "emit_tool_event", capture)
    # The dream_runner module imports the function by name at import
    # time; rebind it there too so the dream code sees the stub.
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event", capture,
    )

    # Seed 4, then cross threshold via the tool.
    for i in range(4):
        store_memory(
            tmp_path,
            content=f"alice obs {i}",
            entities=["person:alice"],
            valid_from=datetime.date(2026, 5, 23),
        )

    tool = MemoryStoreTool(
        workspace=tmp_path,
        embedding_model=None,
        dream_config=_make_dream_config(threshold=5),
    )
    asyncio.run(tool.execute(
        content="alice obs 4",
        class_name="episodic",
        entities=["person:alice"],
    ))

    # Wait for dream to start AND emit telemetry.
    deadline = time.time() + 8.0
    while time.time() < deadline and not any(
        ev[0] == "memory.dream.start" for ev in events
    ):
        time.sleep(0.05)

    start_events = [ev for ev in events if ev[0] == "memory.dream.start"]
    assert start_events, f"no memory.dream.start emitted; saw: {[e[0] for e in events]}"
    # Trigger label preserved for backward compatibility with the
    # original store-path threshold (label is "threshold", not
    # "post_store_threshold").
    assert start_events[0][1].get("trigger") == "threshold", (
        f"trigger label changed: {start_events[0][1]}"
    )
