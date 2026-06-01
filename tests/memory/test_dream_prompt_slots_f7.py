"""F7 (audit third pass, 2026-05-28): wire the three Dream prompt
slots that shipped as empty placeholders.

Pre-F7, `dream.py::_build_prompt` passed:
- `existing_attribute_keys=()`
- `existing_relation_types=()`
- `recent_history=""`

These render into the template as "(none)" / "(no recent history)"
even when the entity page has meaningful attributes, relations, and
git history. The LLM had to infer schema coherence with NO context
about what keys were already in use → schema drift bug (e.g. LLM
emits `email` when the page has `e-mail`, or invents a new relation
type when an equivalent one exists).

F7 wires:
- `existing_attribute_keys` from the parsed EntityPage's attributes.
- `existing_relation_types` from the parsed EntityPage's relations.
- `recent_history` from `format_recent_history(workspace, entity)`.

`existing_uris` (the fourth empty slot from E33) is deferred — the
producer (walk + sort by mtime + cap at 100) is a larger change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def _make_consolidator(tmp_path: Path):
    """Build a minimal Consolidator with disk roots wired to tmp_path
    so prompt building exercises the real producer code paths."""
    from durin.agent.memory import Consolidator

    return Consolidator.__new__(Consolidator).__class__.__init__.__func__


def test_existing_attribute_keys_populated_from_page(tmp_path: Path) -> None:
    """When the canonical page has `attributes: {email: ..., role: ...}`,
    the `existing_attribute_keys` slot lists the keys so the LLM
    avoids drifting to synonyms (`e-mail`)."""
    from durin.memory.dream import DreamConsolidator
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type="person", name="Marcelo", aliases=[],
        attributes={"e_mail_canonical": "x@y.com", "role_label": "founder"},
        relations=[],
        body="body",
    )
    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    page.save(page_path)

    c = DreamConsolidator(workspace=tmp_path)
    prompt = c._build_prompt(
        entity_ref="person:marcelo",
        current_page=page_path.read_text(encoding="utf-8"),
        entries=[],
    )
    # The slot rendering is a comma-separated sorted list. Pre-F7 the
    # slot rendered as `(none)`; post-F7 it carries the real keys.
    assert "attributes: e_mail_canonical, role_label" in prompt
    # Negative: empty placeholder must not be present for this slot.
    assert "attributes: (none)" not in prompt


def test_existing_relation_types_populated_from_page(tmp_path: Path) -> None:
    from durin.memory.dream import DreamConsolidator
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type="person", name="Marcelo", aliases=[],
        attributes={},
        relations=[
            {"to": "person:susana", "type": "spousal_partner"},
            {"to": "project:durin", "type": "maintains_canonical"},
        ],
        body="body",
    )
    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    page.save(page_path)

    c = DreamConsolidator(workspace=tmp_path)
    prompt = c._build_prompt(
        entity_ref="person:marcelo",
        current_page=page_path.read_text(encoding="utf-8"),
        entries=[],
    )
    # The slot rendering is a comma-separated sorted list.
    assert "relation types: maintains_canonical, spousal_partner" in prompt
    assert "relation types: (none)" not in prompt


def test_recent_history_calls_format_recent_history(
    tmp_path: Path,
) -> None:
    """The prompt builder must invoke `format_recent_history` for
    the entity so the LLM sees its own recent decisions."""
    from durin.memory.dream import DreamConsolidator
    from durin.memory.entity_page import EntityPage

    page = EntityPage(
        type="person", name="Marcelo", aliases=[],
        body="b",
    )
    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    page.save(page_path)

    c = DreamConsolidator(workspace=tmp_path)
    with patch(
        "durin.memory.dream.format_recent_history",
        return_value="- 2026-05-26 (Dream): Updated marker f7sentinel-unique",
    ) as mock_history:
        prompt = c._build_prompt(
            entity_ref="person:marcelo",
            current_page=page_path.read_text(encoding="utf-8"),
            entries=[],
        )
    mock_history.assert_called_once_with(tmp_path, "person:marcelo")
    assert "f7sentinel-unique" in prompt


def test_first_consolidation_existing_slots_empty(tmp_path: Path) -> None:
    """When the entity has no existing page (first consolidation),
    all three slots gracefully render as their empty placeholders —
    not a crash."""
    from durin.memory.dream import DreamConsolidator

    c = DreamConsolidator(workspace=tmp_path)
    prompt = c._build_prompt(
        entity_ref="person:newperson",
        current_page=None,
        entries=[],
    )
    # No crash; placeholders render.
    assert "person:newperson" in prompt
    # No attributes/relations because no page existed yet.
    assert "attributes: (none)" in prompt
    assert "relation types: (none)" in prompt
