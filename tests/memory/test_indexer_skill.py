"""Skill indexing into the FTS index (Task 1.2).

Mirrors the memory-file indexing path: skills under ``skills/<name>/SKILL.md``
get one FTS row each (``type='skill'``, ``uri='skill/<slug>'``). Covers the
incremental write path (:func:`reindex_one_skill`), eviction of a deleted
skill, and inclusion in the bulk :func:`rebuild_fts_index` pass.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import (
    _payload_for_skill,
    rebuild_fts_index,
    reindex_one_skill,
)
from durin.memory.paths import skill_uri


def _mk(ws, name, desc="rebase flow", body="run git rebase -i"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n",
        encoding="utf-8",
    )


def test_reindex_one_skill_upserts_and_searches(tmp_path: Path) -> None:
    _mk(tmp_path, "git-helper", body="run git rebase interactive")
    reindex_one_skill(tmp_path, tmp_path / "skills" / "git-helper" / "SKILL.md")
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("rebase")
    assert any(h.uri == "skill/git-helper" and h.type == "skill" for h in hits)


def test_reindex_one_skill_evicts_deleted(tmp_path: Path) -> None:
    _mk(tmp_path, "x")
    md = tmp_path / "skills" / "x" / "SKILL.md"
    reindex_one_skill(tmp_path, md)
    import shutil

    shutil.rmtree(tmp_path / "skills" / "x")
    reindex_one_skill(tmp_path, md)  # file gone -> delete from index
    with FTSIndex.open(tmp_path) as idx:
        assert all(h.uri != "skill/x" for h in idx.search("does"))


def test_rebuild_fts_includes_skills(tmp_path: Path) -> None:
    _mk(tmp_path, "deploy", body="kubectl apply uniquetokenzz")
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        assert any(
            h.uri == "skill/deploy"
            for h in idx.search("uniquetokenzz")
        )


def test_payload_uri_matches_skill_uri(tmp_path: Path) -> None:
    """The payload uri must equal ``skill_uri(slug)`` so the rebuild and
    incremental paths produce identical (dedup-able) rows."""
    _mk(tmp_path, "deploy")
    md = tmp_path / "skills" / "deploy" / "SKILL.md"
    payload = _payload_for_skill(tmp_path, md)
    assert payload is not None
    assert payload["uri"] == skill_uri("deploy")
    assert payload["type_"] == "skill"
    assert payload["entity_type"] is None
