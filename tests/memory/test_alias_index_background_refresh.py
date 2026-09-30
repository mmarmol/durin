"""The shared alias index rebuilds on a background thread.

A dream worker writes entity pages from its own process, so the gateway
rebuilds its shared index from disk after every dream (and builds it once at
startup). Walking every page takes seconds on a large workspace; searches must
keep the previous index meanwhile and see the new one after the swap. Only a
caller that finds no index at all waits, and only for the one build in flight.
Writes made in this process during a rebuild must survive the swap.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import durin.memory.aliases_index as aliases_index
from durin.memory.aliases_cache import (
    _clear_all,
    get_shared_alias_index,
    refresh_alias_index_in_background,
)
from durin.memory.entity_page import EntityPage


@pytest.fixture(autouse=True)
def _clean_cache():
    _clear_all()
    yield
    _clear_all()


def _save(memory_root: Path, slug: str, name: str) -> EntityPage:
    page = EntityPage(type="person", name=name)
    page.save(memory_root / "entities" / "person" / f"{slug}.md")
    return page


class _Gate:
    """Holds every entity-page parse an alias build makes until opened."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered = threading.Event()
        self.open = threading.Event()
        self.parses = 0
        gate = self

        class _GatedPage(EntityPage):
            @classmethod
            def from_file(cls, path):
                gate.parses += 1
                gate.entered.set()
                gate.open.wait(10)
                return EntityPage.from_file(path)

        monkeypatch.setattr(aliases_index, "EntityPage", _GatedPage)


def test_rebuild_serves_the_previous_index_until_the_swap(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    idx = get_shared_alias_index(root)
    assert idx.lookup("alice") == ["person:alice"]
    # A dream worker's write: another process, invisible to this index.
    _save(root, "bob", "Bob")

    gate = _Gate(monkeypatch)
    thread = refresh_alias_index_in_background(root)
    assert gate.entered.wait(5)

    # Mid-rebuild, a caller gets the previous index without waiting.
    assert get_shared_alias_index(root) is idx
    assert idx.lookup("alice") == ["person:alice"]
    assert idx.lookup("bob") == []

    gate.open.set()
    thread.join(5)
    assert not thread.is_alive()
    assert get_shared_alias_index(root) is idx
    assert idx.lookup("bob") == ["person:bob"]


def test_cold_start_callers_wait_for_the_one_build_in_flight(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    gate = _Gate(monkeypatch)
    thread = refresh_alias_index_in_background(root)
    assert gate.entered.wait(5)

    got: list = []
    callers = [
        threading.Thread(target=lambda: got.append(get_shared_alias_index(root)))
        for _ in range(3)
    ]
    for c in callers:
        c.start()
    for c in callers:
        c.join(0.2)
    assert got == []  # no index exists yet, so they wait for the build

    gate.open.set()
    thread.join(5)
    for c in callers:
        c.join(5)
    assert len(got) == 3
    assert all(g is got[0] for g in got)
    assert got[0].lookup("alice") == ["person:alice"]
    assert gate.parses == 1  # one build served every caller


def test_in_process_writes_during_a_rebuild_survive_the_swap(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    _save(root, "bob", "Bob")
    idx = get_shared_alias_index(root)

    gate = _Gate(monkeypatch)
    thread = refresh_alias_index_in_background(root)
    assert gate.entered.wait(5)

    # The rebuild already listed the pages: carol is new to it, and it
    # still reads bob's page as it was before this process removed him.
    carol = _save(root, "carol", "Carol")
    idx.refresh_for(carol, "carol")
    idx.remove("person:bob")

    gate.open.set()
    thread.join(5)
    assert idx.lookup("carol") == ["person:carol"]
    assert idx.lookup("bob") == []
    assert idx.lookup("alice") == ["person:alice"]


def test_entity_write_during_the_first_build_is_not_lost(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    from durin.memory.provenance import author_scope

    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    gate = _Gate(monkeypatch)
    thread = refresh_alias_index_in_background(root)
    assert gate.entered.wait(5)

    with author_scope("agent_created"):
        write_entity(
            tmp_path,
            "company:acme",
            [FieldPatch(kind="body_append", value="Seed.", author="agent",
                        source_ref="s", at=datetime(2026, 9, 1, tzinfo=timezone.utc))],
            create=True,
            name="Acme Corp",
        )

    gate.open.set()
    thread.join(5)
    assert get_shared_alias_index(root).lookup("acme corp") == ["company:acme"]


def test_a_request_during_a_rebuild_runs_one_more_pass(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    idx = get_shared_alias_index(root)

    gate = _Gate(monkeypatch)
    thread = refresh_alias_index_in_background(root)
    assert gate.entered.wait(5)

    # A second dream finishes while the first rebuild is still walking.
    _save(root, "dave", "Dave")
    assert refresh_alias_index_in_background(root) is None

    gate.open.set()
    thread.join(5)
    assert not thread.is_alive()
    assert idx.lookup("dave") == ["person:dave"]


def test_a_request_during_a_searchs_first_build_runs_one_more_pass(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    gate = _Gate(monkeypatch)
    got: list = []
    search = threading.Thread(target=lambda: got.append(get_shared_alias_index(root)))
    search.start()
    assert gate.entered.wait(5)

    # A dream finishes while a search's first build is still walking: that
    # walk already listed the pages, so it cannot read frank.
    _save(root, "frank", "Frank")
    thread = refresh_alias_index_in_background(root)
    assert thread is not None
    thread.join(0.2)
    assert thread.is_alive()  # the request waits for the build in flight

    gate.open.set()
    search.join(5)
    thread.join(5)
    assert not thread.is_alive()
    assert got[0] is get_shared_alias_index(root)
    assert got[0].lookup("frank") == ["person:frank"]


def test_a_failed_rebuild_keeps_serving_and_does_not_block_the_next(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _save(root, "alice", "Alice")
    idx = get_shared_alias_index(root)

    def _boom(self):
        raise OSError("disk went away")

    with monkeypatch.context() as m:
        m.setattr(aliases_index.AliasIndex, "build", _boom)
        thread = refresh_alias_index_in_background(root)
        thread.join(5)
    assert get_shared_alias_index(root) is idx
    assert idx.lookup("alice") == ["person:alice"]

    _save(root, "erin", "Erin")
    thread = refresh_alias_index_in_background(root)
    assert thread is not None
    thread.join(5)
    assert idx.lookup("erin") == ["person:erin"]
