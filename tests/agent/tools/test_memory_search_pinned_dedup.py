"""End-to-end wiring of the pinned-page dedup in ``memory_search``.

The prompt's pinned block renders the principal's page and the ``always_on``
guidance whole, and the hot layer's canonical block excludes them. This test
drives the real tool over a real index to prove the third leg holds: a search
that hits a pinned page collapses it to a pointer line instead of paying for
the same text twice in the turn.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.aliases_cache import _clear_all
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.principal import (
    _PRINCIPAL_BODY_CHARS,
    ANONYMOUS,
    build_pinned_context,
    ensure_owner,
    mark_always_on,
)


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
    # The pointer replaces the block entirely — no canonical section for it.
    # (Canonical markers carry the full `memory/entity_page/<ref>` uri, per
    # the `already_in_context` assertion above.)
    assert "=== CANONICAL: memory/entity_page/practice:" not in out["sectioned_rendered"]


def test_a_hit_the_turns_prefetch_already_showed_collapses_to_a_pointer(
    tmp_path: Path,
) -> None:
    """The automatic per-turn prefetch fences its hits into the user message.
    A search the model makes in the same turn must not render them again: the
    loop hands the tool the turn's refs and they dedup like a whole-rendered
    pinned page.

    The entry is untagged on purpose — an entry that tags no entity never
    surfaces as a hot-layer fragment, so nothing but the turn's refs can
    collapse this hit.
    """
    entries = tmp_path / "memory" / "episodic"
    entries.mkdir(parents=True)
    (entries / "bakery.md").write_text(
        "---\nid: bakery\nheadline: Ana opened a bakery\n"
        "summary: Ana opened a bakery on Main St in March.\nentities: []\n---\n\n"
        "Ana opened a bakery on Main St in March.\n",
        encoding="utf-8",
    )

    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)
    plain = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    # Premise: with no refs handed over, the hit renders whole.
    assert "=== FRAGMENT: memory/episodic/bakery " in plain["sectioned_rendered"]
    assert "already_in_context" not in plain

    tool.set_turn_prefetch_refs({"memory/episodic/bakery"})
    out = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))

    assert out["already_in_context"] == ["memory/episodic/bakery"]
    assert "## Matches shown in your Memory sections" in out["sectioned_rendered"]
    assert "=== FRAGMENT: memory/episodic/bakery " not in out["sectioned_rendered"]

    # The refs belong to one turn only: cleared, the hit renders whole again.
    tool.clear_turn_prefetch_refs()
    again = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    assert "already_in_context" not in again
    assert "=== FRAGMENT: memory/episodic/bakery " in again["sectioned_rendered"]


def test_principal_page_hit_survives_past_the_body_cap(tmp_path: Path) -> None:
    """Unlike the always_on pins, the principal's page is rendered body-capped,
    so a hit on it can match text the prompt never showed. It must be judged by
    containment against the hot layer — which excludes the pinned page, so no
    block matches — and stay a real result instead of collapsing to a pointer.
    No config owner here, so the tool resolves the anonymous principal.
    """
    ensure_owner(tmp_path, ANONYMOUS, name="Anonymous")
    filler = " ".join(f"fact{i}" for i in range(_PRINCIPAL_BODY_CHARS // 4))
    assert len(filler) > _PRINCIPAL_BODY_CHARS  # the match sits past the cap
    write_entity(
        tmp_path, ANONYMOUS,
        [FieldPatch(kind="body_append",
                    value=f"{filler} Sails a catamaran every winter.",
                    author="agent", source_ref="s",
                    at=datetime.now(timezone.utc))],
    )

    # The premise: the prompt's pinned block really does cut the match away.
    assert "catamaran" not in build_pinned_context(tmp_path, ANONYMOUS)

    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)
    out = asyncio.run(tool.execute(query="catamaran", scope="dreamed", level="warm"))

    assert f"memory/entity_page/{ANONYMOUS}" not in out.get("already_in_context", [])
    assert out["total"] == 1
    assert f"=== CANONICAL: memory/entity_page/{ANONYMOUS}" in out["sectioned_rendered"]
    assert "## Matches shown in your Memory sections" not in out["sectioned_rendered"]
