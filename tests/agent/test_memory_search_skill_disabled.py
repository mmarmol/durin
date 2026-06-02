"""M1: ``memory.index_skills=False`` must hide ALREADY-indexed skills.

The write-side gates (FTS rebuild, vector rebuild, drift) stop NEW skills
from being indexed when the flag is off. But a skill indexed earlier —
while the flag was True — leaves an FTS (and vector) row behind. Flipping
the flag off does not evict those rows, so the lexical/vector search arms
still read them and ``memory_search`` keeps surfacing the skill.

This test indexes a skill WHILE enabled (control: it IS found), then flips
``index_skills=False`` and asserts ``memory_search`` returns no skill — in
the ``results`` payload AND in ``sectioned_rendered`` — proving the
tool-boundary gate evicts lingering rows.

Flag flipping reuses the toggle test's pattern verbatim: monkeypatch
``durin.config.loader.load_config`` (the import ``skills_indexing_enabled``
performs internally) to return a real ``Config`` with the field pinned.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from durin.agent.skills_store import dream_create_skill
from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.indexer import rebuild_fts_index

_SKILL_MD = (
    "---\n"
    "name: deploy-flow\n"
    "description: deploy the service via the deploy-flow playbook\n"
    "---\n"
    "Step 1: run the deploy-flow playbook to deploy the service.\n"
)


def _force_flag(monkeypatch, value: bool) -> None:
    """Pin ``memory.index_skills`` to *value* for every ``load_config``."""
    from durin.config.schema import Config

    cfg = Config()
    cfg.memory.index_skills = value
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: cfg,
    )


def test_disabled_flag_hides_previously_indexed_skill(
    tmp_path: Path, monkeypatch,
) -> None:
    ws = tmp_path

    # 1. index a skill while ENABLED (default Config().memory.index_skills
    #    is True). dream_create_skill writes skills/<name>/SKILL.md; the
    #    FTS rebuild then gives lexical search a real skill row.
    dream_create_skill(ws, "deploy-flow", _SKILL_MD, rationale="seed")
    rebuild_fts_index(ws)

    # control: enabled → the skill IS found (kinds='skill' isolates it).
    on = asyncio.run(
        MemorySearchTool(workspace=ws).execute(query="deploy", kinds="skill"),
    )
    assert any(
        "deploy-flow" in str(r) for r in on["results"]
    ), f"control failed: skill not found while enabled: {on['results']!r}"

    # 2. DISABLE the flag and re-query. The FTS row still exists on disk,
    #    so without the tool-boundary gate the skill leaks through.
    _force_flag(monkeypatch, False)

    off = asyncio.run(
        MemorySearchTool(workspace=ws).execute(query="deploy"),
    )
    assert not any(
        "deploy-flow" in str(r) for r in off["results"]
    ), f"M1 leak: disabled skill still in results: {off['results']!r}"
    assert "deploy-flow" not in off.get("sectioned_rendered", ""), (
        "M1 leak: disabled skill still in sectioned_rendered: "
        f"{off.get('sectioned_rendered', '')!r}"
    )

    off_skill = asyncio.run(
        MemorySearchTool(workspace=ws).execute(query="deploy", kinds="skill"),
    )
    assert off_skill["results"] == [], (
        "M1 leak: kinds='skill' still returns the disabled skill: "
        f"{off_skill['results']!r}"
    )
