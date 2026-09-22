"""Discovery resolves a proposal only to an entity that has a page.

The alias index also carries the raw ``entities:`` refs of session summaries
and other entries — display-name spellings the model wrote, such as
``person:Kojiro Kubo`` next to the real page ``person:kojiro-kubo``. The
resolver excludes the proposal's own ref and returned "the one other match":
that ghost. Writing to it without ``create`` raised, and the whole session's
extract failed — every night, since the cursor never advanced. Found on the
box on 2026-09-22 once the failing sessions were named.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.extract_dream import _resolve_existing_ref, discover_entities
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.session_summary_store import write_session_summary

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def _page(ws: Path, ref: str) -> EntityPage | None:
    type_, _, slug = ref.partition(":")
    return EntityPage.from_file(ws / "memory" / "entities" / type_ / f"{slug}.md")


def _workspace_with_a_page_and_a_ghost_alias(tmp_path: Path) -> tuple[Path, AliasIndex]:
    ws = tmp_path
    # The agent authored the page with a hyphenated slug, as memory_upsert_entity does.
    write_entity(
        ws, "person:kojiro-kubo",
        [FieldPatch(kind="attribute", key="email", value="kubo-k@macnica.co.jp",
                    author="agent", source_ref=None, at=NOW)],
        create=True, name="Kojiro Kubo",
    )
    # A session summary whose entities list carries the display-name ref.
    write_session_summary(
        ws, "slack:C1:t1", "- ticket 23145 filed by Kojiro Kubo",
        last_active=date(2026, 9, 14), entities=["person:Kojiro Kubo"],
    )
    index = AliasIndex(ws / "memory")
    index.build()
    # The trap: two refs for one name, only one of them a page.
    assert sorted(index.lookup("Kojiro Kubo")) == ["person:Kojiro Kubo", "person:kojiro-kubo"]
    return ws, index


def test_the_resolver_ignores_a_ref_that_has_no_page(tmp_path: Path) -> None:
    ws, index = _workspace_with_a_page_and_a_ghost_alias(tmp_path)
    assert _resolve_existing_ref(index, "person:Kojiro Kubo", "Kojiro Kubo", workspace=ws) == "person:kojiro-kubo"
    # The proposal IS the page: nothing else owns the name, so no redirect.
    assert _resolve_existing_ref(index, "person:kojiro-kubo", "Kojiro Kubo", workspace=ws) is None


def test_discovery_updates_the_page_instead_of_failing_on_the_ghost(tmp_path: Path) -> None:
    ws, index = _workspace_with_a_page_and_a_ghost_alias(tmp_path)
    proposals = json.dumps([{
        "ref": "person:kojiro-kubo", "name": "Kojiro Kubo",
        "attributes": {"role": "partner representative"},
    }])

    out = discover_entities(
        ws, "ASSISTANT: ticket 23145 was filed by Kojiro Kubo", existing_refs=[],
        llm_invoke=lambda *a, **k: _Resp(proposals), model="m", alias_index=index,
    )

    assert out == [{"ref": "person:kojiro-kubo", "committed": True}]
    assert _page(ws, "person:kojiro-kubo").attributes.get("role") == "partner representative"
    assert not (ws / "memory" / "entities" / "person" / "Kojiro Kubo.md").exists()


def test_a_display_name_proposal_lands_on_the_existing_page(tmp_path: Path) -> None:
    """The model may spell the ref as the name; the page still owns it."""
    ws, index = _workspace_with_a_page_and_a_ghost_alias(tmp_path)
    proposals = json.dumps([{
        "ref": "person:Kojiro Kubo", "name": "Kojiro Kubo",
        "attributes": {"company": "Macnica"},
    }])

    out = discover_entities(
        ws, "ASSISTANT: Kojiro Kubo of Macnica", existing_refs=[],
        llm_invoke=lambda *a, **k: _Resp(proposals), model="m", alias_index=index,
    )

    assert out == [{"ref": "person:kojiro-kubo", "committed": True}]
    assert _page(ws, "person:kojiro-kubo").attributes.get("company") == "Macnica"
    assert not (ws / "memory" / "entities" / "person" / "Kojiro Kubo.md").exists()
