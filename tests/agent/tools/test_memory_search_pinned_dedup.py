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

from durin.agent.tools.memory_search import (
    MemorySearchTool,
    bind_turn_eager_surface,
    bind_turn_prefetch_refs,
    reset_turn_eager_surface,
    reset_turn_prefetch_refs,
)
from durin.memory.aliases_cache import _clear_all
from durin.memory.eager_surface import EagerSnapshot
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.hot_layer import HotLayer
from durin.memory.memory_writer import write_entity
from durin.memory.principal import (
    _PRINCIPAL_BODY_CHARS,
    ANONYMOUS,
    build_pinned_context,
    ensure_owner,
    mark_always_on,
)
from durin.memory.section_markers import end_marker, fragment_marker


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

    token = bind_turn_prefetch_refs({"memory/episodic/bakery"})
    out = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))

    assert out["already_in_context"] == ["memory/episodic/bakery"]
    assert "## Matches shown in your Memory sections" in out["sectioned_rendered"]
    assert "=== FRAGMENT: memory/episodic/bakery " not in out["sectioned_rendered"]

    # The refs belong to one turn only: reset, the hit renders whole again.
    reset_turn_prefetch_refs(token)
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


# ---------------------------------------------------------------------------
# The dedup judges the frozen eager surface, not the live workspace
# ---------------------------------------------------------------------------


def _snapshot(hot: str, refs: frozenset[str] = frozenset()) -> EagerSnapshot:
    return EagerSnapshot(
        pinned="", hot=hot, refs=refs, turn=1,
        frozen_at=datetime.now(timezone.utc).isoformat(),
    )


def _frozen_hot(*fragments: str) -> str:
    """A hot layer rendered exactly the way the prompt renders it."""
    return HotLayer(
        identity="", canonical_blocks=[], fragment_blocks=list(fragments), headlines=[],
    ).render()


def _fragment(path: str, body: str) -> str:
    return "\n".join([fragment_marker(path, ts="2026-09-08"), body, end_marker("fragment")])


def _write_entry(workspace: Path, entities: str) -> None:
    entries = workspace / "memory" / "episodic"
    entries.mkdir(parents=True, exist_ok=True)
    (entries / "bakery.md").write_text(
        "---\nid: bakery\nheadline: Ana opened a bakery\n"
        f"summary: Ana opened a bakery on Main St in March.\nentities: {entities}\n---\n\n"
        "Ana opened a bakery on Main St in March.\n",
        encoding="utf-8",
    )


def test_a_hit_inside_the_frozen_hot_text_collapses_to_a_pointer(tmp_path: Path) -> None:
    """The entry is untagged, so it never reaches the live hot layer: the only
    thing that can collapse this hit is the frozen text the turn carries."""
    _write_entry(tmp_path, "[]")
    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)

    plain = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    assert "already_in_context" not in plain

    token = bind_turn_eager_surface(_snapshot(_frozen_hot(
        _fragment("memory/episodic/bakery.md", "Ana opened a bakery on Main St in March."),
    )))
    try:
        out = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    finally:
        reset_turn_eager_surface(token)

    assert out["already_in_context"] == ["memory/episodic/bakery"]
    assert "=== FRAGMENT: memory/episodic/bakery " not in out["sectioned_rendered"]


def test_an_entry_written_after_the_freeze_still_renders_whole(tmp_path: Path) -> None:
    """It is in the live hot layer but not in the text the model was shown at
    the freeze — collapsing it would hand the model a pointer to content it
    never received."""
    _write_entry(tmp_path, "[person:ana]")
    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)

    # Premise: judged against the live workspace, this hit collapses.
    live = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    assert live["already_in_context"] == ["memory/episodic/bakery"]

    token = bind_turn_eager_surface(_snapshot(_frozen_hot(
        _fragment("memory/episodic/other.md", "Something else entirely."),
    )))
    try:
        out = asyncio.run(tool.execute(query="bakery", scope="dreamed", level="warm"))
    finally:
        reset_turn_eager_surface(token)

    assert "already_in_context" not in out
    assert "=== FRAGMENT: memory/episodic/bakery " in out["sectioned_rendered"]


def test_a_page_pinned_after_the_freeze_still_renders_whole(tmp_path: Path) -> None:
    """The whole-rendered set comes off the snapshot too: a page marked
    always_on after the freeze is in the live pinned block, not in the frozen
    one the model holds."""
    page = EntityPage(
        type="practice", name="Always Spanish",
        body="Answer in Spanish unless asked otherwise.",
    )
    page.save(tmp_path / "memory" / "entities" / "practice" / "always-spanish.md")
    mark_always_on(tmp_path, "practice:always-spanish")
    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)

    # Premise: live, the pin collapses the hit (the pinned block renders it whole).
    live = asyncio.run(tool.execute(query="Always Spanish", scope="dreamed", level="warm"))
    assert live["already_in_context"] == ["memory/entity_page/practice:always-spanish"]

    token = bind_turn_eager_surface(_snapshot(_frozen_hot(), refs=frozenset()))
    try:
        out = asyncio.run(tool.execute(query="Always Spanish", scope="dreamed", level="warm"))
    finally:
        reset_turn_eager_surface(token)

    assert "already_in_context" not in out
    assert "=== CANONICAL: memory/entity_page/practice:always-spanish" in out["sectioned_rendered"]


def test_concurrent_sessions_never_see_each_others_snapshot(tmp_path: Path) -> None:
    """One tool instance serves every session; the surface is carried in a
    ContextVar so a search landing on session B while A's turn is open judges
    B's own frozen text."""
    _write_entry(tmp_path, "[]")
    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)
    contains = _snapshot(_frozen_hot(
        _fragment("memory/episodic/bakery.md", "Ana opened a bakery on Main St in March."),
    ))
    lacks = _snapshot(_frozen_hot(_fragment("memory/episodic/other.md", "Something else.")))

    async def _turn(snapshot: EagerSnapshot) -> dict:
        token = bind_turn_eager_surface(snapshot)
        try:
            await asyncio.sleep(0)  # let the other task bind before we search
            return await tool.execute(query="bakery", scope="dreamed", level="warm")
        finally:
            reset_turn_eager_surface(token)

    async def _both() -> tuple[dict, dict]:
        return await asyncio.gather(_turn(contains), _turn(lacks))

    deduped, whole = asyncio.run(_both())

    assert deduped["already_in_context"] == ["memory/episodic/bakery"]
    assert "already_in_context" not in whole


def test_the_snapshots_own_principal_is_excluded_from_whole_refs_not_the_live_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The principal's page belongs in ``pinned_refs`` but is deliberately kept
    OUT of ``whole_refs`` (its body is capped, so it is judged by containment
    like any other page — see ``test_principal_page_hit_survives_past_the_body_cap``).
    That exclusion has to use the principal the snapshot was frozen with. A
    dedup that resolves the principal live instead would exclude whichever ref
    the CURRENT config owner happens to be — the wrong one if the operator
    changed it mid-session — leaving the frozen principal's own ref sitting in
    ``whole_refs`` and auto-collapsing any hit on it regardless of containment.
    """
    write_entity(
        tmp_path, "person:a",
        [FieldPatch(kind="body_append", value="Sails a catamaran every winter.",
                    author="agent", source_ref="s", at=datetime.now(timezone.utc))],
        create=True, name="A",
    )
    import durin.memory.principal as principal_mod
    monkeypatch.setattr(principal_mod, "resolve_owner_principal", lambda *a, **k: "person:b")

    tool = MemorySearchTool(workspace=tmp_path, context_dedup=True)
    token = bind_turn_eager_surface(EagerSnapshot(
        pinned="", hot=_frozen_hot(), refs=frozenset({"person:a"}), turn=1,
        frozen_at=datetime.now(timezone.utc).isoformat(), principal="person:a",
    ))
    try:
        out = asyncio.run(tool.execute(query="catamaran", scope="dreamed", level="warm"))
    finally:
        reset_turn_eager_surface(token)

    assert "memory/entity_page/person:a" not in out.get("already_in_context", [])
    assert out["total"] == 1
    assert "=== CANONICAL: memory/entity_page/person:a" in out["sectioned_rendered"]
