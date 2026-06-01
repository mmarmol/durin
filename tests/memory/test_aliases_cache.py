"""Tests for the process-wide shared :class:`AliasIndex` cache.

Verifies the §2.C contract from
``docs/25_post_t1_state_and_t2_horizon.md``: a single ``durin agent``
run that hits multiple memory consumers (memory_search,
DreamConsolidator, EntityAbsorption) builds the AliasIndex once,
and writes by one consumer become visible to the others without
explicit invalidation.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from durin.memory.aliases_cache import (
    _cache_size,
    _clear_all,
    get_shared_alias_index,
    invalidate_alias_index,
)
from durin.memory.entity_page import EntityPage


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Each test starts with an empty cache so cross-test state doesn't leak."""
    _clear_all()
    yield
    _clear_all()


def _write_page(memory_root: Path, type_: str, slug: str, aliases: list[str]) -> EntityPage:
    page = EntityPage(type=type_, name=slug.title(), aliases=aliases)
    page.save(memory_root / "entities" / type_ / f"{slug}.md")
    return page


# ---------------------------------------------------------------------------
# basic sharing
# ---------------------------------------------------------------------------


def test_first_call_builds_and_caches(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    assert _cache_size() == 0
    idx = get_shared_alias_index(mem)
    assert idx is not None
    assert _cache_size() == 1


def test_repeat_call_same_root_returns_same_instance(tmp_path: Path) -> None:
    mem = tmp_path / "memory"
    a = get_shared_alias_index(mem)
    b = get_shared_alias_index(mem)
    assert a is b
    assert _cache_size() == 1


def test_different_workspaces_get_independent_instances(tmp_path: Path) -> None:
    ws1 = tmp_path / "ws1" / "memory"
    ws2 = tmp_path / "ws2" / "memory"
    a = get_shared_alias_index(ws1)
    b = get_shared_alias_index(ws2)
    assert a is not b
    assert _cache_size() == 2


def test_cold_workspace_returns_empty_index_still_usable(tmp_path: Path) -> None:
    """Workspace with no entities/ subdir → empty index, not None."""
    mem = tmp_path / "memory"
    idx = get_shared_alias_index(mem)
    assert idx is not None
    assert idx.size() == 0


# ---------------------------------------------------------------------------
# in-place mutation propagation (the core §2.C value proposition)
# ---------------------------------------------------------------------------


def test_refresh_propagates_across_consumers(tmp_path: Path) -> None:
    """When one consumer calls refresh_for, others see the change immediately."""
    mem = tmp_path / "memory"

    # Consumer A (e.g. memory_search) builds the shared index first.
    idx_a = get_shared_alias_index(mem)
    assert idx_a.size() == 0

    # Consumer B (e.g. DreamConsolidator) writes a page + refreshes.
    page = _write_page(mem, "person", "marcelo", ["Marcelo", "mmarmol"])
    idx_b = get_shared_alias_index(mem)
    idx_b.refresh_for(page, slug="marcelo")

    # Consumer A's reference now reflects B's write (same instance).
    assert idx_a is idx_b
    assert idx_a.size() == 2  # "marcelo" + "mmarmol"
    assert idx_a.lookup("Marcelo") == ["person:marcelo"]


def test_remove_propagates_across_consumers(tmp_path: Path) -> None:
    """Mirror of refresh propagation, for absorption's removal step."""
    mem = tmp_path / "memory"

    page = _write_page(mem, "person", "marcelo", ["Marcelo"])
    idx = get_shared_alias_index(mem)
    idx.refresh_for(page, slug="marcelo")
    assert idx.size() == 1

    # Another consumer removes (e.g. EntityAbsorption archives a page).
    idx2 = get_shared_alias_index(mem)
    idx2.remove("person:marcelo")

    assert idx is idx2
    assert idx.size() == 0


# ---------------------------------------------------------------------------
# explicit invalidation (defensive path for out-of-band edits)
# ---------------------------------------------------------------------------


def test_invalidate_forces_rebuild(tmp_path: Path) -> None:
    """After invalidate, the next call returns a fresh instance built from disk."""
    mem = tmp_path / "memory"

    # Build, mutate in memory.
    page = _write_page(mem, "person", "marcelo", ["Marcelo"])
    idx_old = get_shared_alias_index(mem)
    idx_old.refresh_for(page, slug="marcelo")
    assert idx_old.size() == 1

    invalidate_alias_index(mem)
    assert _cache_size() == 0

    # Next call rebuilds from disk; new instance, same on-disk content.
    idx_new = get_shared_alias_index(mem)
    assert idx_new is not idx_old
    assert idx_new.size() == 1  # picked up the persisted page


def test_invalidate_is_per_workspace(tmp_path: Path) -> None:
    ws1 = tmp_path / "ws1" / "memory"
    ws2 = tmp_path / "ws2" / "memory"
    a1 = get_shared_alias_index(ws1)
    b1 = get_shared_alias_index(ws2)
    assert _cache_size() == 2

    invalidate_alias_index(ws1)
    assert _cache_size() == 1

    # ws2's instance unchanged; ws1 rebuilds.
    a2 = get_shared_alias_index(ws1)
    b2 = get_shared_alias_index(ws2)
    assert a2 is not a1
    assert b2 is b1


def test_invalidate_unknown_root_is_noop(tmp_path: Path) -> None:
    """Calling invalidate before any build must not raise."""
    invalidate_alias_index(tmp_path / "nope")
    assert _cache_size() == 0


# ---------------------------------------------------------------------------
# concurrency — only one build runs even under contention
# ---------------------------------------------------------------------------


def test_concurrent_first_call_builds_once(tmp_path: Path) -> None:
    """Race two threads on the same cold workspace; expect one shared instance."""
    mem = tmp_path / "memory"
    _write_page(mem, "person", "marcelo", ["Marcelo"])

    barrier = threading.Barrier(8)
    results: list = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        idx = get_shared_alias_index(mem)
        with results_lock:
            results.append(idx)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All threads see the same instance; cache holds one entry.
    assert len(results) == 8
    first = results[0]
    assert all(r is first for r in results)
    assert _cache_size() == 1


def test_concurrent_read_and_mutate_on_shared_instance(tmp_path: Path) -> None:
    """Readers and writers hammer the SAME cached instance — no crash.

    `AliasIndex` itself is unit-tested for thread safety in
    test_aliases_index.py. This is the integration-level guard for the
    §2.C contract: the instance handed out by ``get_shared_alias_index``
    is the shared, mutation-visible one, so a search thread calling
    ``lookup``/``all_entities`` while a Dream/absorb thread calls
    ``refresh_for``/``remove`` on the cache handle must not raise (the
    P0 race fixed in `4e2c55d`). A future cache change that handed out
    per-thread copies would still pass the unit test but fail here.
    """
    mem = tmp_path / "memory"
    for i in range(50):
        _write_page(mem, "person", f"p{i}", [f"alias{i}", f"name {i}"])
    # Prime the cache so every thread gets the same built instance.
    get_shared_alias_index(mem)

    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                idx = get_shared_alias_index(mem)
                idx.all_entities()
                idx.lookup("alias5")
        except BaseException as exc:  # noqa: BLE001 - capture for assert
            errors.append(exc)

    def writer() -> None:
        try:
            for i in range(1500):
                idx = get_shared_alias_index(mem)
                idx.refresh_for(
                    EntityPage(type="person", name=f"P{i}", aliases=[f"alias{i}"]),
                    slug=f"p{i}",
                )
                idx.remove(f"person:p{i % 50}")
        except BaseException as exc:  # noqa: BLE001 - capture for assert
            errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer) for _ in range(2)]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors, f"shared-instance thread-safety violation: {errors[:3]}"


# ---------------------------------------------------------------------------
# consumer wiring smoke — the 3 real consumers all hit the shared instance
# ---------------------------------------------------------------------------


def test_memory_search_uses_shared_cache(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    mem = tmp_path / "memory"
    _write_page(mem, "person", "marcelo", ["Marcelo"])

    tool = MemorySearchTool(workspace=tmp_path)
    tool_idx = tool._get_alias_index()
    cached = get_shared_alias_index(mem)
    assert tool_idx is cached


def test_dream_consolidator_uses_shared_cache(tmp_path: Path) -> None:
    from durin.memory.dream import DreamConsolidator

    mem = tmp_path / "memory"
    _write_page(mem, "person", "marcelo", ["Marcelo"])

    dream = DreamConsolidator(workspace=tmp_path, llm_invoke=lambda *a, **kw: "")
    dream_idx = dream._get_alias_index()
    cached = get_shared_alias_index(mem)
    assert dream_idx is cached


def test_entity_absorption_uses_shared_cache(tmp_path: Path) -> None:
    from durin.memory.absorption import EntityAbsorption

    mem = tmp_path / "memory"
    _write_page(mem, "person", "marcelo", ["Marcelo"])

    absorber = EntityAbsorption(workspace=tmp_path)
    abs_idx = absorber._get_alias_index()
    cached = get_shared_alias_index(mem)
    assert abs_idx is cached


def test_three_consumers_share_one_instance(tmp_path: Path) -> None:
    """End-to-end §2.C: the three real consumers all hit the same instance."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.absorption import EntityAbsorption
    from durin.memory.dream import DreamConsolidator

    mem = tmp_path / "memory"
    _write_page(mem, "person", "marcelo", ["Marcelo"])

    search = MemorySearchTool(workspace=tmp_path)
    dream = DreamConsolidator(workspace=tmp_path, llm_invoke=lambda *a, **kw: "")
    absorber = EntityAbsorption(workspace=tmp_path)

    a = search._get_alias_index()
    b = dream._get_alias_index()
    c = absorber._get_alias_index()

    assert a is b is c
    assert _cache_size() == 1


def test_injected_index_bypasses_shared_cache(tmp_path: Path) -> None:
    """Tests that inject their own AliasIndex must not be force-shared."""
    from durin.memory.absorption import EntityAbsorption
    from durin.memory.aliases_index import AliasIndex
    from durin.memory.dream import DreamConsolidator

    mem = tmp_path / "memory"
    injected = AliasIndex(mem)

    dream = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=lambda *a, **kw: "",
        alias_index=injected,
    )
    absorber = EntityAbsorption(workspace=tmp_path, alias_index=injected)

    assert dream._get_alias_index() is injected
    assert absorber._get_alias_index() is injected
    # Shared cache untouched.
    assert _cache_size() == 0
