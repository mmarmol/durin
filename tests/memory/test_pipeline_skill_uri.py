"""H28 (skills): a skill vector row must reconstruct the SAME uri shape
the FTS indexer writes (``skills/<slug>/SKILL.md``), NOT the generic
``memory/skill/<id>``. Otherwise RRF treats the vector hit and the FTS
hit for the same skill as different URIs, splits their scores, and drops
well-matched skills below top-K.

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


def test_skill_vector_row_gets_skills_uri() -> None:
    """A skill row carrying its stored ``path`` reconstructs the
    ``skills/<slug>/SKILL.md`` uri (NOT ``memory/skill/...``)."""
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
    assert rows[0]["uri"] == "skills/git-helper/SKILL.md"
    # type passes through as "skill" (class_name → type fallback).
    assert rows[0]["type"] == "skill"


def test_skill_vector_row_without_path_reconstructs_from_uri() -> None:
    """When the row has no stored ``path``, the uri is reconstructed
    from the ``skill/<slug>`` id via ``skill_path_from_uri``."""
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
    assert rows[0]["uri"] == "skills/git-helper/SKILL.md"


def test_resolve_meta_leaves_skill_type_untouched() -> None:
    """``_resolve_meta`` rewrites only ``entity_page → entity``; a
    ``skill`` type must pass through unchanged."""
    uri = "skills/git-helper/SKILL.md"
    vector_meta = {
        uri: {"type": "skill", "path": uri},
    }
    meta = _resolve_meta(uri, vector_meta, {})
    assert meta["type"] == "skill"
