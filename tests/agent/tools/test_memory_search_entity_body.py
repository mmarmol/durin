"""``memory_search`` entity hits carry substance: name, attributes and a
body excerpt at warm level; the full body at cold level.

Mirrors the harness of
``tests/memory/test_fragment_canonical_contract.py::test_memory_search_tool_emits_sectioned_rendered``.
"""

from __future__ import annotations

import asyncio
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
    page = EntityPage(type="person", name="Ana", aliases=["ana"],
                      attributes={"role": "baker"}, body=_BODY,
                      derived_from=["reference:ana-bakery-license"])
    page.save(tmp_path / "memory" / "entities" / "person" / "ana.md")


def test_warm_entity_hit_shows_name_attributes_and_an_excerpt(tmp_path: Path) -> None:
    _page(tmp_path)
    out = asyncio.run(MemorySearchTool(workspace=tmp_path).execute(query="Ana bakery", scope="dreamed", level="warm"))
    rendered = out["sectioned_rendered"]
    assert "=== CANONICAL: memory/entity_page/person:ana" in rendered
    assert "Ana" in rendered and "role is baker" in rendered
    assert "Ana runs the bakery on Main Street." in rendered
    assert "opens at dawn" not in rendered            # past the excerpt
    # Canonical markers join the completeness qualifier with the existing
    # "canonical entity page"/"consolidated <ts>" one inside one trailing
    # parenthesis (see `_compose_qualifiers`) — not a bare "(preview N/M)".
    assert ", preview " in rendered
    # `_attach_derived_from` reads the page's `derived_from` from disk and
    # the renderer turns it into a `Sources:` line pointing at the document.
    assert "Sources: reference:ana-bakery-license." in rendered


def test_cold_entity_hit_shows_the_whole_body(tmp_path: Path) -> None:
    _page(tmp_path)
    out = asyncio.run(MemorySearchTool(workspace=tmp_path).execute(query="Ana bakery", scope="dreamed", level="cold"))
    rendered = out["sectioned_rendered"]
    assert "opens at dawn and closes at noon" in rendered
    # Same joined-qualifier shape as the warm case (see comment above).
    assert ", complete)" in rendered
    assert "Sources: reference:ana-bakery-license." in rendered
