"""Regression: the Dream consolidation pass MUST NEVER touch skills.

Skills are a pseudo-class owned by ``workspace/skills/`` — they live
*outside* ``memory/`` and are not one of :data:`MEMORY_CLASSES`. The dream
only walks the canonical memory classes + entity pages, so it must never
index, consolidate, rewrite, archive, or delete a skill.

This test asserts already-true behaviour. It seeds a real skill *and* real
episodic memory the dream legitimately consolidates, runs the real
``DreamRunner`` (the same stub-LLM harness the other dream tests use), and
then proves the skill is byte-identical, unmoved, and still retrievable —
while the dream demonstrably did work (it produced an entity page).

If this test ever fails, the dream has started sweeping skills — that is a
real finding, not a test to relax.
"""

from __future__ import annotations

import asyncio
import datetime
import json as _json
from pathlib import Path

from durin.agent.skills_store import dream_create_skill
from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.dream_runner import DreamRunner
from durin.memory.paths import walk_skills
from durin.memory.store import store_memory

# A skill whose body/description contain a keyword the dream would notice if
# it ever (wrongly) swept skills into its query/consolidation flow.
SKILL_MD = (
    "---\n"
    "name: deploy-flow\n"
    "description: deploy flow rollout and rollback steps\n"
    "---\n"
    "Run the deploy script, then verify the rollout before promoting.\n"
)


def _stub_llm(slug: str = "marcelo"):
    """The v2 Dream LLM stub used by tests/memory/test_dream_runner.py.

    Emits a well-formed PATCH/BODY_DELTA/COMMIT block so the consolidator
    produces a real entity page — i.e. the dream actually *does work*.
    """
    ops = [
        {"op": "add", "path": "/aliases/-", "value": slug,
         "provenance": "episodic/e1.md"},
        {"op": "add", "path": "/attributes/note", "value": "observed",
         "provenance": "episodic/e1.md"},
    ]
    response = (
        "===PATCH===\n"
        + _json.dumps(ops, indent=2) + "\n"
        + "===BODY_DELTA===\n"
        + "Observed.\n"
        + "===COMMIT===\n"
        + f"Consolidate person:{slug} (rev 1)\n"
        + "\nInitial pass.\n"
        + f"\nSources: episodic/e1.md\nEntities-touched: person:{slug}\n"
        + "Cursor-after: 2026-05-23T00:00:00\n"
        + "===END===\n"
    )

    def stub(prompt, *, model):
        return response

    return stub


def test_dream_pass_does_not_touch_skills(tmp_path: Path) -> None:
    ws = tmp_path

    # --- seed a skill (writes skills/deploy-flow/SKILL.md, commits, indexes).
    created = dream_create_skill(ws, "deploy-flow", SKILL_MD, rationale="seed")
    assert created.get("ok"), created
    skill_md = ws / "skills" / "deploy-flow" / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")

    # --- seed NORMAL memory the dream legitimately consolidates. The autouse
    # fixture in tests/conftest.py opens author_scope("agent_created"), so
    # these are agent-observed pending entries the dream picks up. Two distinct
    # entries for one entity guarantee the dream has real work to do.
    store_memory(
        ws, content="marcelo observation one",
        entities=["person:marcelo"], valid_from=datetime.date(2026, 5, 23),
    )
    store_memory(
        ws, content="marcelo observation two",
        entities=["person:marcelo"], valid_from=datetime.date(2026, 5, 23),
    )

    # --- run the real consolidation/dream pass (same harness as
    # test_dream_runner.py: DreamRunner + stub LLM, throttle disabled).
    result = DreamRunner(
        workspace=ws, llm_invoke=_stub_llm(), min_seconds_between_runs=0,
    ).run(trigger="cron_daily")

    # The dream RAN and did work: it consolidated the episodic entries into an
    # entity page. This proves we are testing a dream that exercised its real
    # walk — yet (below) left the skill untouched.
    assert result.ran and result.reason == "ok"
    assert result.entities_consolidated == 1
    entity_page = ws / "memory" / "entities" / "person" / "marcelo.md"
    assert entity_page.exists()

    # INVARIANT 1: the skill file on disk is byte-identical — not archived,
    # rewritten, re-stamped, or deleted by the dream.
    assert skill_md.exists()
    assert skill_md.read_text(encoding="utf-8") == before

    # INVARIANT 2: no skill was swept into an entity page or an archive.
    # walk_skills still yields exactly the one skill, unmoved; the dream
    # produced no entity/archive artifact derived from the skill.
    assert [p for p in walk_skills(ws)] == [skill_md]
    assert not (ws / "memory" / "archive").exists()
    # The dream's only entity output is the legitimate person page — nothing
    # named after / derived from the skill leaked into entities/.
    entity_dirs = sorted(
        p.name for p in (ws / "memory" / "entities").rglob("*.md")
    )
    assert entity_dirs == ["marcelo.md"]
    assert not any("deploy" in name for name in entity_dirs)

    # INVARIANT 3: the skill is still retrievable — the dream did not drop or
    # invalidate its index row.
    out = asyncio.run(
        MemorySearchTool(workspace=ws).execute(query="deploy", kinds="skill")
    )
    assert any("deploy-flow" in str(r) for r in out["results"])
