"""``memory_search`` entity hits carry substance: name, attributes and a
body excerpt at warm level; the full body at cold level.

Mirrors the harness of
``tests/memory/test_fragment_canonical_contract.py::test_memory_search_tool_emits_sectioned_rendered``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool, _entity_composition
from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.hot_layer import _render_attributes_line

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


# ---------------------------------------------------------------------------
# `_entity_composition` bounds attributes + body TOGETHER at warm level.
#
# Before the fix, only the body was cut to `excerpt_chars`; the attributes
# line was appended whole. A page with many attributes (a real 42-attribute
# page measured at 1623 chars for that line alone) could render a warm
# summary far past the per-hit budget, which then blew the prefetch's
# response budget and dropped later hits entirely. See the module docstring
# of `tests/agent/tools/test_memory_search_warm_budget.py` for the sibling
# bug this class of budget bypass belongs to.
# ---------------------------------------------------------------------------


def _many_attributes(n: int) -> dict[str, str]:
    return {
        f"attr_{i:02d}": f"a fairly long descriptive value for attribute number {i}"
        for i in range(n)
    }


def test_entity_composition_bounds_attrs_and_body_together() -> None:
    page = EntityPage(
        type="person", name="Zorak", aliases=[],
        attributes=_many_attributes(40),
        body="Zorak keeps detailed logs of every shift. " * 100,
    )
    excerpt_chars = 600
    full_attrs = _render_attributes_line(page.attributes)
    assert len(full_attrs) > excerpt_chars  # sanity: attrs alone overflow the budget

    result = _entity_composition(page, excerpt_chars=excerpt_chars)
    name_line = page.name
    assert len(result) <= excerpt_chars

    lines = result.split("\n")
    assert lines[0] == name_line
    attrs_line = lines[1]
    assert attrs_line.startswith("Attributes: ")
    # The cut lands on a whole attribute: what follows it in the uncut
    # line is either nothing or a fresh "; " boundary, never a partial word.
    assert full_attrs.startswith(attrs_line)
    tail = full_attrs[len(attrs_line):]
    assert tail == "" or tail.startswith("; ")

    real_body = page.body.strip()
    if len(lines) > 2:
        assert real_body.startswith(lines[2])
    # else: the attributes line alone consumed the whole budget — the
    # body is absent, which the rule explicitly allows.


def test_entity_composition_counts_a_long_name_line_against_the_budget() -> None:
    """A page whose name and aliases line is long spends budget too: the
    name line renders whole and the attributes and body fit in what it
    leaves, so the whole composition stays within `excerpt_chars`."""
    page = EntityPage(
        type="project", name="El Ojo en el Abismo — campaña homebrew del Norte",
        aliases=["Ojo en el Abismo", "El Ojo", "campaña del Abismo", "OEA", "Abismo"],
        attributes=_many_attributes(40),
        body="Campaña de ocho misiones en los Reinos Olvidados del Norte. " * 50,
    )
    excerpt_chars = 600
    result = _entity_composition(page, excerpt_chars=excerpt_chars)
    lines = result.split("\n")
    assert lines[0].startswith("El Ojo en el Abismo") and "(aliases: " in lines[0]
    assert len(lines[0]) > 100
    assert len(result) <= excerpt_chars
    assert lines[1].startswith("Attributes: ")
    full_attrs = _render_attributes_line(page.attributes)
    assert full_attrs.startswith(lines[1])


def test_entity_composition_cuts_the_attrs_line_plainly_when_even_the_first_attribute_overflows() -> None:
    page = EntityPage(
        type="person", name="Zorak", aliases=[],
        attributes={"very_long_attribute_key": "a value long enough that even one entry overflows a tiny budget"},
        body="irrelevant",
    )
    excerpt_chars = 10
    result = _entity_composition(page, excerpt_chars=excerpt_chars)
    lines = result.split("\n")
    full_attrs = _render_attributes_line(page.attributes)
    attrs_budget = excerpt_chars - len("Zorak") - 1  # the name line spends budget too
    assert lines[1] == full_attrs[:attrs_budget]
    assert len(lines) == 2  # body gets nothing
    assert len(result) <= excerpt_chars


def test_entity_composition_gives_body_the_remainder_when_attrs_fit() -> None:
    """When the attributes line is short, the body gets what's left of the
    budget — and when the whole thing (name + attrs + body) fits under
    `excerpt_chars`, the bounded and unbounded compositions are identical."""
    page = EntityPage(
        type="person", name="Bo", aliases=[],
        attributes={"role": "baker", "city": "Lima"},
        body="A short body that easily fits under the budget.",
    )
    bounded = _entity_composition(page, excerpt_chars=600)
    unbounded = _entity_composition(page, excerpt_chars=None)
    assert bounded == unbounded


def test_entity_composition_excerpt_chars_none_is_the_full_unbounded_composition() -> None:
    page = EntityPage(
        type="person", name="Zorak", aliases=["z"],
        attributes=_many_attributes(5),
        body="Zorak's full body text goes here in full, uncut.",
    )
    result = _entity_composition(page, excerpt_chars=None)
    name_line = f"{page.name} (aliases: z)."
    attrs = _render_attributes_line(page.attributes)
    body = page.body.strip()
    assert result == "\n".join([name_line, attrs, body])


def test_warm_entity_hit_with_many_attributes_bounds_the_whole_composition(tmp_path: Path) -> None:
    """End-to-end through `MemorySearchTool`: the rendered canonical block
    for a many-attribute page stays within the same total bound
    `_entity_composition` enforces, and its `preview N/M` qualifier still
    reports the true full-composition length."""
    body = "Zorak keeps detailed logs of every shift at the workshop. " * 60
    page_path = tmp_path / "memory" / "entities" / "person" / "zorak.md"
    EntityPage(
        type="person", name="Zorak", aliases=[],
        attributes=_many_attributes(40), body=body,
    ).save(page_path)

    out = asyncio.run(
        MemorySearchTool(workspace=tmp_path).execute(
            query="Zorak workshop", scope="dreamed", level="warm",
        )
    )
    rendered = out["sectioned_rendered"]

    loaded = EntityPage.from_file(page_path)
    assert loaded is not None
    expected_summary = _entity_composition(loaded, excerpt_chars=600).strip()
    expected_full_len = len(_entity_composition(loaded, excerpt_chars=None))

    assert expected_summary in rendered
    assert len(expected_summary) <= 600
    assert f", preview {len(expected_summary)}/{expected_full_len})" in rendered
