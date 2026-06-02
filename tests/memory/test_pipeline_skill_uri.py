"""B1 (skills): a skill vector row must produce the SAME FUSION uri the
FTS indexer writes — the bare ``skill/<slug>`` (``_payload_for_skill``),
NOT ``skills/<slug>/SKILL.md`` and NOT the generic ``memory/skill/<id>``.
Otherwise RRF treats the vector hit and the FTS hit for the same skill as
different URIs, splits their scores, and drops well-matched skills below
top-K (and duplicates the skill in results). The drillable
``skills/<slug>/SKILL.md`` display path is carried separately in the
row's ``path`` field and resolved at the result layer.

These tests exercise ``_safe_vector_search`` directly with a stub vector
index, mirroring the row shape the production ``VectorIndex.search()``
emits (``id`` / ``class_name`` / ``path``), the same way
``test_search_pipeline.test_vector_index_native_row_shape_is_accepted``
does.
"""

from __future__ import annotations

from durin.memory.search_pipeline import _resolve_meta, _safe_vector_search


class _SkillIndex:
    """Stub vector index returning a single skill row in the native shape."""

    def __init__(self, rows):
        self._rows = rows

    def search(self, query, top_k=50):
        return list(self._rows)


def _recovery() -> dict:
    return {"sources": set(), "ms": 0.0}


def test_skill_vector_row_uses_bare_skill_fusion_uri() -> None:
    """The vector FUSION uri is the bare ``skill/<slug>`` (== FTS uri),
    NOT ``skills/<slug>/SKILL.md`` and NOT ``memory/skill/...``. The
    drillable display path stays in the row's ``path`` field."""
    index = _SkillIndex([
        {
            "id": "skill/git-helper",
            "class_name": "skill",
            "summary": "Helps with git operations",
            "path": "skills/git-helper/SKILL.md",
            "_distance": 12.0,
        },
    ])
    rows = _safe_vector_search(index, "git", recovery=_recovery())
    assert len(rows) == 1
    assert rows[0]["uri"] == "skill/git-helper"
    # The display path rides along for the result layer to drill.
    assert rows[0]["path"] == "skills/git-helper/SKILL.md"
    # type passes through as "skill" (class_name → type fallback).
    assert rows[0]["type"] == "skill"


def test_skill_vector_row_without_path_still_uses_bare_id() -> None:
    """When the row has no stored ``path``, the FUSION uri is still the
    bare ``skill/<slug>`` id (the result layer reconstructs the display
    path from it via ``_skill_uri_to_path``)."""
    index = _SkillIndex([
        {
            "id": "skill/git-helper",
            "class_name": "skill",
            "summary": "Helps with git operations",
            "_distance": 12.0,
        },
    ])
    rows = _safe_vector_search(index, "git", recovery=_recovery())
    assert len(rows) == 1
    assert rows[0]["uri"] == "skill/git-helper"


def test_resolve_meta_leaves_skill_type_untouched() -> None:
    """``_resolve_meta`` rewrites only ``entity_page → entity``; a
    ``skill`` type must pass through unchanged."""
    uri = "skill/git-helper"
    vector_meta = {
        uri: {"type": "skill", "path": "skills/git-helper/SKILL.md"},
    }
    meta = _resolve_meta(uri, vector_meta, {})
    assert meta["type"] == "skill"
    # The display path is carried through for the result layer.
    assert meta["path"] == "skills/git-helper/SKILL.md"
