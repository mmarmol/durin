"""``memory_search`` parses an entity page's file at most once per search
call, no matter how many call sites inside the tool need its content.

Companion to ``test_memory_search_entity_body.py`` (which covers the
rendered warm/cold shapes). Before this fix, one entity hit drove three
separate ``EntityPage.from_file`` calls from inside
``durin/agent/tools/memory_search.py``: the sectioned-conversion step
(``_sectioned_to_result``), the body-length probe (``_entity_full_length``)
and the ``Sources:`` attachment step (``_attach_derived_from``). All three
now resolve through the same per-call cache (``_load_entity_page``), so the
file is read once.

The counting wrapper below scopes to calls whose direct caller lives in
``memory_search.py``: the same workspace also produces legitimate,
single-purpose ``EntityPage.from_file`` calls from the indexer (payload
build on ``MemorySearchTool.__init__``'s ``ensure_index_fresh``), the grep
fallback's own entity scan (``durin.memory.search._search_entity_pages``)
and the shared alias index build (``durin.memory.aliases_index.build``) —
none of those are what this task fixes, and a raw, unscoped call count
would conflate them with the redundant parsing under test.

This is also the first end-to-end coverage of ``_attach_derived_from``: no
existing test drove a ``Sources:`` line through a real ``execute()`` call.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage

_BODY = ("Ana runs the bakery on Main Street. " * 40) + "She opens at dawn and closes at noon."


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    _clear_all()
    yield
    _clear_all()


def _page(tmp_path: Path) -> None:
    page = EntityPage(
        type="person", name="Ana", aliases=["ana"],
        attributes={"role": "baker"}, body=_BODY,
        derived_from=["reference:ana-bakery-license"],
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "ana.md")


def _count_memory_search_parses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``EntityPage.from_file`` with a counting wrapper that still
    calls the real parser, recording only calls made directly from
    ``memory_search.py`` (see module docstring for why the count is
    scoped rather than global)."""
    calls: list[str] = []
    original = EntityPage.from_file.__func__

    def counting(cls, path):
        caller = sys._getframe(1)
        if caller.f_code.co_filename.endswith("memory_search.py"):
            calls.append(str(path))
        return original(cls, path)

    monkeypatch.setattr(EntityPage, "from_file", classmethod(counting))
    return calls


def test_an_entity_hit_parses_its_page_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page(tmp_path)
    calls = _count_memory_search_parses(monkeypatch)

    out = asyncio.run(
        MemorySearchTool(workspace=tmp_path).execute(
            query="Ana bakery", scope="dreamed", level="warm",
        )
    )
    rendered = out["sectioned_rendered"]

    assert len(calls) == 1, f"expected exactly one parse, got {calls!r}"
    # Content still renders correctly through the cached page.
    assert "Ana" in rendered and "role is baker" in rendered
    assert "Ana runs the bakery on Main Street." in rendered
    assert ", preview " in rendered
    # `_attach_derived_from`'s Sources: line — the missing end-to-end
    # coverage of that method.
    assert "Sources: reference:ana-bakery-license." in rendered


def test_cold_entity_hit_parses_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _page(tmp_path)
    calls = _count_memory_search_parses(monkeypatch)

    out = asyncio.run(
        MemorySearchTool(workspace=tmp_path).execute(
            query="Ana bakery", scope="dreamed", level="cold",
        )
    )
    rendered = out["sectioned_rendered"]

    assert len(calls) == 1, f"expected exactly one parse, got {calls!r}"
    assert "opens at dawn and closes at noon" in rendered
    assert ", complete)" in rendered
    assert "Sources: reference:ana-bakery-license." in rendered
