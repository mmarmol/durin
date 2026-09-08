"""End-to-end wiring of the pinned-page dedup in ``memory_search``.

The prompt's pinned block renders the principal's page and the ``always_on``
guidance whole, and the hot layer's canonical block excludes them. This test
drives the real tool over a real index to prove the third leg holds: a search
that hits a pinned page collapses it to a pointer line instead of paying for
the same text twice in the turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.principal import mark_always_on


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    _clear_all()
    yield
    _clear_all()


def test_pinned_page_collapses_to_a_pointer_line(tmp_path: Path) -> None:
    page = EntityPage(
        type="practice", name="Always Spanish",
        body="Answer in Spanish unless asked otherwise.",
        relations=[{"to": "person:marcelo", "type": "requested_by"}],
    )
    page.save(tmp_path / "memory" / "entities" / "practice" / "always-spanish.md")
    mark_always_on(tmp_path, "practice:always-spanish")

    # `create()` turns the dedup on for the core scope; the direct constructor
    # defaults it off, so the wiring under test has to be asked for explicitly.
    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)
    out = asyncio.run(tool.execute(query="Always Spanish", scope="dreamed", level="warm"))

    assert out["already_in_context"] == ["memory/entity_page/practice:always-spanish"]
    assert "## Matches shown in your Memory sections" in out["sectioned_rendered"]
    # The pointer replaces the body — the pinned block already carries it.
    assert "Answer in Spanish unless asked otherwise." not in out["sectioned_rendered"]
