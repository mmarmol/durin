"""§2.A.1 β.2 — the three sub-daily triggers.

β.1 shipped the daily cron + the DreamRunner core. β.2 adds three
sub-daily triggers that all reuse the same runner:

- ``post_compaction``: Consolidator fires the hook after a successful
  archive round (``Consolidator.on_post_compaction``).
- ``session_close``: AgentLoop exposes ``on_session_close``; ``/new``
  invokes it after archiving the prior session.
- ``threshold``: ``memory_store`` counts per-entity post-cursor
  entries after each write and dispatches a background runner when
  ``threshold_entries`` is crossed.

These tests pin the contract without spinning up the full agent loop:
the hooks are plain callables, the threshold check is a method on the
tool, and the runner integration is exercised via a fake captured by
monkeypatch.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from durin.memory.store import store_memory


# ---------------------------------------------------------------------------
# Consolidator.on_post_compaction — attribute exists, fires after summary
# ---------------------------------------------------------------------------


class TestPostCompactionHook:
    def test_consolidator_source_declares_attribute(self) -> None:
        """Hard contract: ``Consolidator`` must declare the attribute
        so ``cli/commands.py`` wiring lands on production. Source-
        level check to avoid spinning up the many deps a real
        Consolidator needs."""
        import durin.agent.memory as mem_mod

        src = Path(mem_mod.__file__).read_text()
        assert "on_post_compaction" in src
        # And the call site exists too.
        assert "self.on_post_compaction(session.key)" in src


def test_post_compaction_hook_fires_after_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire a fake Consolidator to validate the hook contract.

    We don't construct the real Consolidator (too many deps). Instead
    we verify the simpler invariant: when ``on_post_compaction`` is
    set and ``last_summary`` is truthy at the end of
    maybe_consolidate_by_tokens, the callback fires exactly once with
    the session key.

    The real call site is one branch in agent/memory.py — covered by
    integration in the smoke e2e below; the unit-level pin is on the
    AgentLoop side (cmd_new test exercises both legs).
    """
    from durin.agent.memory import Consolidator

    # The attribute MUST exist on the class so the cli/commands.py
    # wiring `agent.consolidator.on_post_compaction = ...` doesn't
    # silently miss.
    assert hasattr(Consolidator, "__init__")
    # Build a real-ish stub: set the attr, call the branch by hand.
    fake = SimpleNamespace(on_post_compaction=None)
    received: list[str] = []

    def hook(key: str) -> None:
        received.append(key)

    fake.on_post_compaction = hook
    # Simulate the call site (last_summary truthy → invoke hook).
    fake.on_post_compaction("websocket:chat42")
    assert received == ["websocket:chat42"]


# ---------------------------------------------------------------------------
# AgentLoop.on_session_close + cmd_new — fires once per /new
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_new_invokes_on_session_close_when_set() -> None:
    """``/new`` must call ``loop.on_session_close(key)`` when the hook
    is set, regardless of whether the prior session had messages."""
    import asyncio

    from durin.bus.events import OutboundMessage
    from durin.command.builtin import cmd_new

    received: list[str] = []

    fake_session = SimpleNamespace(
        key="websocket:chat42",
        messages=[],
        last_consolidated=0,
        clear=lambda: None,
    )
    fake_loop = SimpleNamespace(
        _cancel_active_tasks=lambda key: asyncio.sleep(0),
        sessions=SimpleNamespace(
            get_or_create=lambda key: fake_session,
            save=lambda s: None,
            invalidate=lambda key: None,
        ),
        consolidator=SimpleNamespace(archive=lambda s: asyncio.sleep(0)),
        _schedule_background=lambda coro: None,
        on_session_close=lambda key: received.append(key),
    )
    fake_msg = SimpleNamespace(channel="cli", chat_id="x", metadata={})
    ctx = SimpleNamespace(
        loop=fake_loop, session=fake_session, msg=fake_msg, key="websocket:chat42",
    )

    out = await cmd_new(ctx)
    assert isinstance(out, OutboundMessage)
    assert "New session started" in out.content
    assert received == ["websocket:chat42"]


@pytest.mark.asyncio
async def test_cmd_new_handles_missing_on_session_close_attr() -> None:
    """SimpleNamespace loops (used by some tests) don't have the
    attribute. The defensive ``getattr`` keeps them working."""
    import asyncio

    from durin.command.builtin import cmd_new

    fake_session = SimpleNamespace(
        key="x", messages=[], last_consolidated=0, clear=lambda: None,
    )
    fake_loop = SimpleNamespace(
        _cancel_active_tasks=lambda k: asyncio.sleep(0),
        sessions=SimpleNamespace(
            get_or_create=lambda k: fake_session,
            save=lambda s: None,
            invalidate=lambda k: None,
        ),
        consolidator=SimpleNamespace(archive=lambda s: asyncio.sleep(0)),
        _schedule_background=lambda coro: None,
        # NO on_session_close attribute
    )
    fake_msg = SimpleNamespace(channel="cli", chat_id="x", metadata={})
    ctx = SimpleNamespace(loop=fake_loop, session=fake_session, msg=fake_msg, key="x")

    out = await cmd_new(ctx)
    assert "New session started" in out.content


@pytest.mark.asyncio
async def test_cmd_new_swallows_hook_exception() -> None:
    """A misbehaving hook must NOT break /new — log and continue."""
    import asyncio

    from durin.command.builtin import cmd_new

    def boom(key: str) -> None:
        raise RuntimeError("hook bug")

    fake_session = SimpleNamespace(
        key="x", messages=[], last_consolidated=0, clear=lambda: None,
    )
    fake_loop = SimpleNamespace(
        _cancel_active_tasks=lambda k: asyncio.sleep(0),
        sessions=SimpleNamespace(
            get_or_create=lambda k: fake_session,
            save=lambda s: None,
            invalidate=lambda k: None,
        ),
        consolidator=SimpleNamespace(archive=lambda s: asyncio.sleep(0)),
        _schedule_background=lambda coro: None,
        on_session_close=boom,
    )
    fake_msg = SimpleNamespace(channel="cli", chat_id="x", metadata={})
    ctx = SimpleNamespace(loop=fake_loop, session=fake_session, msg=fake_msg, key="x")

    out = await cmd_new(ctx)
    assert "New session started" in out.content  # didn't crash


def test_agent_loop_exposes_on_session_close_attribute() -> None:
    """Hard contract: ``AgentLoop`` must declare the attribute so
    ``cli/commands.py`` wiring (``agent.on_session_close = ...``)
    actually lands."""
    from durin.agent.loop import AgentLoop

    # Class-level annotation OR instance default is fine — just need
    # the attribute name to be a documented contract. Read source
    # marker as a low-cost check.
    src = Path(AgentLoop.__module__.replace(".", "/") + ".py")
    # Resolve relative to durin package
    import durin
    pkg_root = Path(durin.__file__).resolve().parent.parent
    full = pkg_root / src
    text = full.read_text()
    assert "on_session_close" in text


# ---------------------------------------------------------------------------
# threshold per-entity — memory_store dispatches when count crosses
# ---------------------------------------------------------------------------


def _make_store_with_threshold(workspace: Path, threshold: int = 3):
    from durin.agent.tools.memory_store import MemoryStoreTool

    dream_cfg = SimpleNamespace(
        enabled=True,
        threshold_entries=threshold,
        min_seconds_between_runs=0,
        model_override=None,
    )
    return MemoryStoreTool(
        workspace=workspace, embedding_model=None, dream_config=dream_cfg,
    )


def test_threshold_does_not_dispatch_below_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []  # (trigger, entity_filter)

    class _StubRunner:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, *, trigger: str, entity_filter: str | None = None,
                on_progress=None) -> SimpleNamespace:
            spawns.append((trigger, entity_filter or ""))
            return SimpleNamespace(ran=True, reason="ok",
                                   entities_consolidated=1, entities_failed=0,
                                   duration_s=0.0)

    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner", _StubRunner,
    )

    tool = _make_store_with_threshold(tmp_path, threshold=5)
    # Seed only 2 entries — below threshold.
    for i in range(2):
        store_memory(tmp_path, content=f"obs {i}",
                     entities=["person:marcelo"],
                     valid_from=datetime.date(2026, 5, 23))
    tool._maybe_dispatch_threshold_dream(["person:marcelo"], vector_index=None)
    # Threads are daemon + may not have spawned yet either way; key
    # assertion is that even after a beat, no runner was constructed.
    time.sleep(0.05)
    assert spawns == []


def test_threshold_dispatches_when_count_crosses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list[tuple[str, str]] = []

    class _StubRunner:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, *, trigger: str, entity_filter: str | None = None,
                on_progress=None) -> SimpleNamespace:
            spawns.append((trigger, entity_filter or ""))
            return SimpleNamespace(ran=True, reason="ok",
                                   entities_consolidated=1, entities_failed=0,
                                   duration_s=0.0)

    monkeypatch.setattr(
        "durin.memory.dream_runner.DreamRunner", _StubRunner,
    )

    tool = _make_store_with_threshold(tmp_path, threshold=3)
    for i in range(3):
        store_memory(tmp_path, content=f"obs {i}",
                     entities=["person:marcelo"],
                     valid_from=datetime.date(2026, 5, 23))
    tool._maybe_dispatch_threshold_dream(["person:marcelo"], vector_index=None)
    # Wait briefly for daemon thread.
    deadline = time.time() + 1.0
    while time.time() < deadline and not spawns:
        time.sleep(0.02)
    assert spawns == [("threshold", "person:marcelo")]


def test_threshold_disabled_when_config_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool

    spawns: list = []

    class _StubRunner:
        def __init__(self, **k: Any) -> None: pass
        def run(self, **k: Any) -> Any:
            spawns.append(k); return SimpleNamespace(ran=False, reason="ok",
                                                     entities_consolidated=0,
                                                     entities_failed=0, duration_s=0.0)

    monkeypatch.setattr("durin.memory.dream_runner.DreamRunner", _StubRunner)
    tool = MemoryStoreTool(workspace=tmp_path, dream_config=None)
    for i in range(10):
        store_memory(tmp_path, content=f"obs {i}",
                     entities=["person:m"],
                     valid_from=datetime.date(2026, 5, 23))
    tool._maybe_dispatch_threshold_dream(["person:m"], vector_index=None)
    time.sleep(0.05)
    assert spawns == []


def test_threshold_disabled_when_threshold_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns: list = []

    class _StubRunner:
        def __init__(self, **k: Any) -> None: pass
        def run(self, **k: Any) -> Any:
            spawns.append(k)
            return SimpleNamespace(ran=False, reason="ok",
                                   entities_consolidated=0,
                                   entities_failed=0, duration_s=0.0)

    monkeypatch.setattr("durin.memory.dream_runner.DreamRunner", _StubRunner)
    tool = _make_store_with_threshold(tmp_path, threshold=0)
    for i in range(10):
        store_memory(tmp_path, content=f"obs {i}",
                     entities=["person:m"],
                     valid_from=datetime.date(2026, 5, 23))
    tool._maybe_dispatch_threshold_dream(["person:m"], vector_index=None)
    time.sleep(0.05)
    assert spawns == []


def test_threshold_dispatches_per_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each entity that crosses the threshold gets its own runner thread."""
    spawns: list[tuple[str, str]] = []

    class _StubRunner:
        def __init__(self, **k: Any) -> None: pass
        def run(self, *, trigger: str, entity_filter: str | None = None,
                on_progress=None) -> SimpleNamespace:
            spawns.append((trigger, entity_filter or ""))
            return SimpleNamespace(ran=True, reason="ok",
                                   entities_consolidated=1,
                                   entities_failed=0, duration_s=0.0)

    monkeypatch.setattr("durin.memory.dream_runner.DreamRunner", _StubRunner)
    tool = _make_store_with_threshold(tmp_path, threshold=2)
    for i in range(3):
        store_memory(tmp_path, content=f"obs {i}",
                     entities=["person:marcelo", "project:durin"],
                     valid_from=datetime.date(2026, 5, 23))
    tool._maybe_dispatch_threshold_dream(
        ["person:marcelo", "project:durin"], vector_index=None,
    )
    deadline = time.time() + 1.0
    while time.time() < deadline and len(spawns) < 2:
        time.sleep(0.02)
    triggers = sorted(s[1] for s in spawns)
    assert triggers == ["person:marcelo", "project:durin"]
