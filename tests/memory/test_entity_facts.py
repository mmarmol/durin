"""The walks every prompt build makes parse a page again only when it changes.

A new session's first prompt walked every entity page and parsed its YAML
four times — the always_on list and the hot layer's recency order, twice
each — which took ~30 s on a workspace of 2,164 pages, on the event loop.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from durin.memory import entity_page as entity_page_mod
from durin.memory.entity_page import EntityPage
from durin.memory.hot_layer import read_hot_layer
from durin.memory.principal import list_always_on, mark_always_on


def _page(ws: Path, slug: str, *, updated: str) -> Path:
    page = EntityPage(type="topic", name=slug.title(), aliases=[], body=f"About {slug}.",
                      updated_at=datetime.fromisoformat(updated))
    path = ws / "memory" / "entities" / "topic" / f"{slug}.md"
    page.save(path)
    return path


@pytest.fixture
def parses(monkeypatch) -> dict[str, int]:
    """How many times a page's text is parsed."""
    count = {"n": 0}
    real = EntityPage.from_text.__func__

    def counting(cls, text):
        count["n"] += 1
        return real(cls, text)

    monkeypatch.setattr(EntityPage, "from_text", classmethod(counting))
    return count


def test_the_always_on_walk_parses_a_page_again_only_when_it_changes(tmp_path, parses) -> None:
    for i in range(5):
        _page(tmp_path, f"t{i}", updated=f"2026-09-0{i + 1}T10:00:00")

    assert list_always_on(tmp_path) == []
    assert parses["n"] == 5
    assert list_always_on(tmp_path) == []
    assert parses["n"] == 5                     # nothing changed, nothing parsed

    mark_always_on(tmp_path, "topic:t3")
    parses["n"] = 0
    assert list_always_on(tmp_path) == ["topic:t3"]
    assert parses["n"] == 1                     # only the page that changed


def test_the_hot_layer_parses_in_full_only_the_pages_it_renders(tmp_path, parses) -> None:
    for i in range(20):
        _page(tmp_path, f"t{i:02d}", updated=f"2026-09-{i + 1:02d}T10:00:00")

    first = read_hot_layer(tmp_path).render()
    parses["n"] = 0
    second = read_hot_layer(tmp_path).render()

    assert second == first
    assert parses["n"] == 12                    # the canonical block's pages only
    assert "topic:t19" in second and "topic:t07" not in second   # the 12 newest


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="PyYAML built without libyaml")
def test_a_page_reads_the_same_with_the_fast_yaml_parser(monkeypatch) -> None:
    """Frontmatter is parsed with libyaml when PyYAML has it: the same page,
    several times faster."""
    text = (
        "---\n"
        "type: person\nname: José Ñúñez\naliases: [Pepe, \"J. Ñ\"]\n"
        "updated_at: 2026-09-28T05:52:00\n"
        "always_on: true\n"
        "identifiers:\n  email: [jose@example.com]\n"
        "summary: |\n  Line one.\n  Line two: with a colon.\n"
        "---\n\nBody with *markdown* and 'quotes'.\n"
    )
    fast = EntityPage.from_text(text)
    monkeypatch.setattr(entity_page_mod, "_YAML_LOADER", yaml.SafeLoader)
    slow = EntityPage.from_text(text)
    assert fast is not None and fast == slow
