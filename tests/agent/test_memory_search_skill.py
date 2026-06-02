"""Task 2.4 — memory_search returns/enriches/drills skill results.

A skill authored under ``skills/<name>/SKILL.md`` and indexed must:

- surface from ``memory_search`` with ``kind == "skill"`` and a
  *drillable* ``uri`` (``skills/<name>/SKILL.md``, NOT the internal
  ``skill/<slug>`` vector/FTS id),
- carry a non-empty body when ``level="cold"`` (cold-tier enrichment
  reads the on-disk SKILL.md rather than mis-parsing the skill uri as a
  ``memory/<class>/<id>`` triplet),
- be filterable via the ``kinds`` param (``"skill"`` keeps only skills,
  ``"fact"`` drops them, default ``"all"`` includes them),
- resolve cleanly through ``memory_drill`` on the drillable uri.

The fixture mirrors ``tests/memory/test_memory_search_limit_param.py``:
``MemorySearchTool(workspace=tmp_path)`` runs the grep/FTS path (no
vector — ``app_config=None``), and ``rebuild_fts_index`` indexes the
tree. We additionally seed a plain memory fact so the ``kinds`` filter
has something to exclude.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from durin.agent.tools.memory_drill import MemoryDrillTool
from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.indexer import rebuild_fts_index
from durin.memory.store import store_memory

SKILL_NAME = "git-rebase-helper"
SKILL_NEEDLE = "uniqueskilltoken"
# A token shared by both the skill and the fact so a single query
# surfaces a skill AND a fact — this makes the `kinds=fact` filter
# non-vacuous (it has a skill to exclude).
SHARED_NEEDLE = "sharedrebasetoken"


def _seed(workspace: Path) -> None:
    """Author one skill on disk + one memory fact, then index both.

    Both carry ``SHARED_NEEDLE`` so a query for it returns both kinds;
    the skill additionally carries the skill-only ``SKILL_NEEDLE``.
    """
    skill_dir = workspace / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        f"description: how to {SKILL_NEEDLE} an interactive rebase\n"
        "---\n"
        f"Step 1: run git rebase -i to {SKILL_NEEDLE} {SHARED_NEEDLE}.\n"
        "Step 2: reorder the commits.\n",
        encoding="utf-8",
    )
    store_memory(
        workspace,
        content=f"the user prefers {SHARED_NEEDLE} in their workflow",
        class_name="episodic",
        headline=f"{SHARED_NEEDLE} preference",
    )
    rebuild_fts_index(workspace)


def _skill_results(out: dict) -> list[dict]:
    return [r for r in out["results"] if r.get("kind") == "skill"]


def test_skill_result_has_skill_kind_and_drillable_uri(tmp_path: Path) -> None:
    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query=SKILL_NEEDLE))

    skills = _skill_results(out)
    assert skills, f"no skill hit in {out['results']!r}"
    assert all(
        r["uri"] == f"skills/{SKILL_NAME}/SKILL.md" for r in skills
    ), f"skill uri not drillable: {[r['uri'] for r in skills]!r}"


def test_kinds_skill_only_returns_skills(tmp_path: Path) -> None:
    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query=SKILL_NEEDLE, kinds="skill"),
    )
    assert out["results"], "kinds='skill' returned nothing"
    assert all(r.get("kind") == "skill" for r in out["results"])


def test_kinds_fact_excludes_skills(tmp_path: Path) -> None:
    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)

    # Sanity: the shared token surfaces BOTH a skill and a fact by
    # default, so the filter below has something to exclude (guards
    # against a vacuous pass on an empty result set).
    baseline = asyncio.run(tool.execute(query=SHARED_NEEDLE))
    kinds_seen = {r.get("kind") for r in baseline["results"]}
    assert "skill" in kinds_seen and "fragment" in kinds_seen, (
        f"fixture did not surface both kinds: {baseline['results']!r}"
    )

    out = asyncio.run(tool.execute(query=SHARED_NEEDLE, kinds="fact"))
    assert out["results"], "kinds='fact' dropped everything"
    assert all(r.get("kind") != "skill" for r in out["results"]), (
        f"kinds='fact' leaked a skill: {out['results']!r}"
    )


def test_kinds_default_all_includes_skill(tmp_path: Path) -> None:
    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query=SKILL_NEEDLE))  # default kinds
    assert _skill_results(out), "default kinds dropped the skill"


def test_skill_cold_body_is_enriched(tmp_path: Path) -> None:
    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query=SKILL_NEEDLE, level="cold"))
    skills = _skill_results(out)
    assert skills, "no skill hit at cold tier"
    body = skills[0].get("body") or ""
    assert SKILL_NEEDLE in body, (
        f"cold-tier body not enriched from SKILL.md: {skills[0]!r}"
    )


def test_drill_resolves_skill_uri(tmp_path: Path) -> None:
    _seed(tmp_path)
    drill = MemoryDrillTool(workspace=tmp_path)
    out = asyncio.run(
        drill.execute(uri=f"skills/{SKILL_NAME}/SKILL.md"),
    )
    assert "error" not in out, f"drill errored: {out!r}"
    assert SKILL_NEEDLE in out["content"]
