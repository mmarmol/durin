"""B1: one skill indexed in BOTH the vector store and FTS must fuse to a
SINGLE RRF hit, not split into 2-3 hits under different uri shapes.

The vector arm (``_safe_vector_search``), the FTS/lexical arm
(``indexer._payload_for_skill``), and the grep arm (``search.search_skills``)
historically emitted DIFFERENT uri strings for the same skill:

- vector + grep: ``skills/<slug>/SKILL.md``
- FTS/lexical:   ``skill/<slug>``

``fuse_rrf`` keys on the uri string, so the same skill surfaced as two
separate hits with split RRF scores (ranked far lower than it should and
duplicated in results). The fix unifies the FUSION uri to ``skill/<slug>``
across all three sources; the drillable display path
(``skills/<slug>/SKILL.md``) is resolved at the result layer.

This test drives the REAL ``run_search_pipeline`` with both arms hitting:
a duck-typed vector index returns the skill row (native ``VectorIndex``
shape), and a real FTS index is built containing the same skill.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.indexer import reindex_one_skill
from durin.memory.search_pipeline import run_search_pipeline


class _SkillVectorIndex:
    """Duck-typed vector index returning one skill row in the native
    ``VectorIndex.search`` shape (see ``vector_index._skill_record``):
    keyed on ``id`` / ``class_name`` / ``path``, no ``uri`` / ``type``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def search(self, query: str, top_k: int = 50) -> list[dict]:
        return list(self._rows)


def _write_skill(ws: Path, slug: str, *, desc: str, body: str) -> None:
    d = ws / "skills" / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {desc}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_skill_fuses_to_single_hit_across_vector_and_fts(tmp_path: Path) -> None:
    slug = "git-helper"
    query = "rebase"
    _write_skill(
        tmp_path, slug,
        desc="interactive rebase workflow",
        body="run git rebase interactive rebase",
    )
    # Real FTS arm: index the skill so lexical_search returns it.
    reindex_one_skill(tmp_path, tmp_path / "skills" / slug / "SKILL.md")

    # Vector arm: same skill, native row shape (id=skill/<slug>, no uri/type).
    vector_index = _SkillVectorIndex([
        {
            "id": f"skill/{slug}",
            "class_name": "skill",
            "summary": "interactive rebase workflow",
            "headline": slug,
            "body_length": 30,
            "path": f"skills/{slug}/SKILL.md",
            "valid_from": "",
            "entities": [],
            "_distance": 0.1,
        },
    ])

    result = run_search_pipeline(tmp_path, query, vector_index=vector_index)

    skill_hits = [h for h in result.hits if h.type == "skill"]
    # B1 repro: exactly ONE fused hit for the skill. Pre-fix this is 2
    # (vector emits `skills/git-helper/SKILL.md`, FTS emits `skill/git-helper`).
    assert len(skill_hits) == 1, (
        f"expected 1 fused skill hit, got {len(skill_hits)}: "
        f"{[h.uri for h in skill_hits]}"
    )
    # The single hit was found by BOTH sources, so its fused uri is the
    # canonical FTS/vector key `skill/<slug>` and it carries the display
    # `path` for the result layer to drill.
    hit = skill_hits[0]
    assert hit.uri == f"skill/{slug}"
    assert hit.path == f"skills/{slug}/SKILL.md"
