"""Tests for the auto-trigger runner (doc 25 §2.A.1).

The runner wraps :class:`DreamConsolidator` with lock + throttle +
telemetry. These tests pin the behaviours the auto-triggers depend on:

- ``no_pending`` → return without acquiring the lock.
- ``concurrent_lock`` → respect another process's lock.
- ``throttle`` → cool-down between runs.
- ``ok`` → consolidator runs, success counters updated, telemetry fired.
- Stale lock (>10 min old) → cleaned up and overwritten.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path

import pytest

from durin.memory.dream_runner import DreamRunner, _LOCK_FILENAME
from durin.memory.entity_page import EntityPage
from durin.memory.store import store_memory


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_stub_llm(slug: str = "marcelo"):
    """Return an LLM stub that produces a well-formed dream response."""
    response = (
        "===PAGE===\n"
        "---\n"
        "type: person\n"
        f"name: {slug.title()}\n"
        f"aliases: [{slug}]\n"
        "dream_processed_through: 2026-05-23T00:00:00\n"
        "---\n"
        "\n"
        f"# {slug.title()}\n"
        "\n## Current State\nObserved.\n"
        "===COMMIT===\n"
        f"Consolidate person:{slug} (rev 1)\n"
        "\nInitial pass.\n"
        f"\nSources: e1\nEntities-touched: person:{slug}\n"
        "Cursor-after: 2026-05-23T00:00:00\n"
        "===END===\n"
    )

    def stub(prompt, *, model):
        return response

    return stub


def _seed_pending_entry(workspace: Path, slug: str = "marcelo") -> None:
    # Doc memory §4.6.1: `user_authored` entries are protected from
    # Dream consumption. The tests model agent-observed entries —
    # wrap the store call in agent_created scope so the resulting
    # episodic is picked up by `_discover_pending_consolidations`.
    from durin.memory.provenance import author_scope
    with author_scope("agent_created"):
        store_memory(
            workspace, content=f"{slug} observation",
            entities=[f"person:{slug}"],
            valid_from=datetime.date(2026, 5, 23),
        )


# ---------------------------------------------------------------------------
# no_pending: empty workspace shortcuts before lock acquisition
# ---------------------------------------------------------------------------


def test_cold_workspace_returns_no_pending(tmp_path: Path) -> None:
    runner = DreamRunner(workspace=tmp_path)
    result = runner.run(trigger="cron_daily")
    assert not result.ran
    assert result.reason == "no_pending"
    # No lock created — we shortcut before acquiring.
    assert not (tmp_path / "memory" / _LOCK_FILENAME).exists()


def test_no_tagged_entries_returns_no_pending(tmp_path: Path) -> None:
    """Entries without entity tags shouldn't trigger a dream."""
    store_memory(tmp_path, content="untagged", entities=[])
    runner = DreamRunner(workspace=tmp_path, llm_invoke=_make_stub_llm())
    result = runner.run(trigger="cron_daily")
    assert result.reason == "no_pending"


# ---------------------------------------------------------------------------
# ok: full pass
# ---------------------------------------------------------------------------


def test_successful_run_returns_ok(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    result = runner.run(trigger="cron_daily")
    assert result.ran
    assert result.reason == "ok"
    assert result.entities_consolidated == 1
    assert result.entities_failed == 0
    assert result.duration_s >= 0
    # Page landed on disk.
    page = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    assert page.exists()


def test_lock_is_released_after_run(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")
    assert not (tmp_path / "memory" / _LOCK_FILENAME).exists()


def test_last_run_marker_touched_after_success(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")
    marker = tmp_path / "memory" / ".dream.last_run"
    assert marker.exists()


# ---------------------------------------------------------------------------
# concurrent_lock: respect another process's lock
# ---------------------------------------------------------------------------


def test_existing_fresh_lock_blocks_run(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    # Pre-create a fresh lock (simulate another process).
    memory_root = tmp_path / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    lock_path = memory_root / _LOCK_FILENAME
    lock_path.write_text(json.dumps({
        "pid": 999999, "started_at": time.time(), "trigger": "manual",
    }))

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    result = runner.run(trigger="cron_daily")
    assert not result.ran
    assert result.reason == "concurrent_lock"
    # The pre-existing lock is left intact — we don't own it.
    assert lock_path.exists()


def test_stale_lock_is_removed_and_run_proceeds(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    memory_root = tmp_path / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    lock_path = memory_root / _LOCK_FILENAME
    lock_path.write_text("{}")
    # Age the lock beyond _STALE_LOCK_SECONDS (10 min).
    stale_mtime = time.time() - 700
    os.utime(lock_path, (stale_mtime, stale_mtime))

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    result = runner.run(trigger="cron_daily")
    assert result.ran
    assert result.reason == "ok"


# ---------------------------------------------------------------------------
# throttle: cooldown between runs
# ---------------------------------------------------------------------------


def test_throttle_blocks_immediate_rerun(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    stub = _make_stub_llm()
    # First run with no throttle so it completes and touches last_run.
    DreamRunner(
        workspace=tmp_path, llm_invoke=stub, min_seconds_between_runs=0,
    ).run(trigger="cron_daily")

    # Seed a second entity so there'd be work if not throttled.
    _seed_pending_entry(tmp_path, slug="other")

    # Second runner with throttle that cannot have elapsed.
    result = DreamRunner(
        workspace=tmp_path, llm_invoke=stub, min_seconds_between_runs=600,
    ).run(trigger="cron_daily")
    assert not result.ran
    assert result.reason == "throttle"


def test_zero_throttle_never_blocks(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path)
    stub = _make_stub_llm()
    DreamRunner(
        workspace=tmp_path, llm_invoke=stub, min_seconds_between_runs=0,
    ).run(trigger="cron_daily")
    _seed_pending_entry(tmp_path, slug="another")
    result = DreamRunner(
        workspace=tmp_path, llm_invoke=_make_stub_llm("another"),
        min_seconds_between_runs=0,
    ).run(trigger="cron_daily")
    assert result.ran  # throttle disabled


# ---------------------------------------------------------------------------
# entity_filter narrows the pass
# ---------------------------------------------------------------------------


def test_entity_filter_only_processes_requested(tmp_path: Path) -> None:
    _seed_pending_entry(tmp_path, slug="marcelo")
    _seed_pending_entry(tmp_path, slug="durin")

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm("marcelo"),
        min_seconds_between_runs=0,
    )
    result = runner.run(trigger="threshold", entity_filter="person:marcelo")
    assert result.ran
    assert result.entities_consolidated == 1  # only marcelo, not durin
    assert (tmp_path / "memory" / "entities" / "person" / "marcelo.md").exists()
    assert not (tmp_path / "memory" / "entities" / "person" / "durin.md").exists()


# ---------------------------------------------------------------------------
# telemetry: start/end/skipped events fire with the trigger label
# ---------------------------------------------------------------------------


def test_telemetry_emits_start_and_end_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending_entry(tmp_path)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")

    types = [t for t, _ in events]
    assert "memory.dream.start" in types
    assert "memory.dream.end" in types
    end_payload = next(d for t, d in events if t == "memory.dream.end")
    assert end_payload["trigger"] == "cron_daily"
    assert end_payload["entities_consolidated"] == 1


def test_telemetry_emits_skipped_on_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending_entry(tmp_path)
    DreamRunner(
        workspace=tmp_path, llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    ).run(trigger="cron_daily")

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    DreamRunner(
        workspace=tmp_path, llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=600,
    ).run(trigger="threshold")

    skipped = [d for t, d in events if t == "memory.dream.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "throttle"
    assert skipped[0]["trigger"] == "threshold"


def test_telemetry_emits_skipped_on_concurrent_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_pending_entry(tmp_path)
    memory_root = tmp_path / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / _LOCK_FILENAME).write_text(
        json.dumps({"pid": 1, "started_at": time.time(), "trigger": "manual"})
    )

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    DreamRunner(
        workspace=tmp_path, llm_invoke=_make_stub_llm(),
        min_seconds_between_runs=0,
    ).run(trigger="post_compaction")

    skipped = [d for t, d in events if t == "memory.dream.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "concurrent_lock"


def test_telemetry_no_start_event_when_no_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold workspace must NOT emit start/end — only `skipped(no_pending)`."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    DreamRunner(workspace=tmp_path).run(trigger="cron_daily")

    types = [t for t, _ in events]
    assert "memory.dream.start" not in types
    assert "memory.dream.end" not in types
    assert "memory.dream.skipped" in types
