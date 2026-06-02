"""Daily content-driven skill curation (E2 Part B).

Reviews the workspace `auto` set (the evolving catalog — dream-created and forked
skills), never pristine builtins. Cut-off = CHANGE, not "review everything":
only the **delta** — `auto` skills that are new or whose BODY changed since last
curated (via
`skills_store.needs_curation`). Stable skills are skipped with no LLM call, so
the pass never scales with catalog size. `budget` caps the per-day delta; the
rest carries over (un-cursored → a later day), logged.

Judges by CONTENT (Hermes rule: not usage counts). Judge is injected so the core
is unit-testable without a provider. The day's usage is light context only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from durin.agent import skills_store as ss

DEFAULT_BUDGET = 50
logger = logging.getLogger(__name__)


def curate_catalog(workspace, *, judge: Callable[[str], str],
                   usage: dict | None = None, budget: int = DEFAULT_BUDGET) -> dict:
    """One delta-curation pass. Returns {'reviewed', 'applied', 'deferred'}."""
    workspace = Path(workspace)
    # Only the evolving WORKSPACE set: dream-created + forked skills. Pristine
    # builtins (source="builtin") are the stable seed — not re-curated/forked
    # until they're forked into the workspace by some other path.
    auto = [
        s["name"] for s in ss.list_skills_info(workspace)
        if s["mode"] == "auto" and s["source"] == "workspace"
    ]
    delta = [n for n in auto if ss.needs_curation(workspace, n)]
    if not delta:
        return {"reviewed": 0, "applied": 0, "deferred": 0}

    selected = delta[:budget]
    deferred = len(delta) - len(selected)
    if deferred:
        logger.info("skill curation: delta=%d > budget=%d; deferring %d",
                    len(delta), budget, deferred)

    catalog = {n: ss.read_skill_content(workspace, n) or "" for n in selected}
    prompt = _build_prompt(catalog, usage or {})
    try:
        actions = (json.loads(judge(prompt)) or {}).get("actions", [])
    except (ValueError, TypeError):
        actions = []

    applied = 0
    for a in actions:
        t = a.get("type")
        if t == "fuse":
            r = ss.dream_fuse_skills(workspace, target=a["target"], content=a["content"],
                                     sources=a["sources"], rationale=a.get("rationale", "fuse"))
            applied += 1 if r.get("ok") else 0
        elif t == "evolve":
            r = ss.apply_skill_edit(workspace, a["name"], old=a["old"], new=a["new"],
                                    rationale=a.get("rationale", "evolve"))
            applied += 1 if r.get("ok") else 0

    for n in selected:
        if ss.read_skill_content(workspace, n) is not None:
            ss.mark_curated(workspace, n)
    return {"reviewed": len(selected), "applied": applied, "deferred": deferred}


def _build_prompt(catalog: dict, usage: dict) -> str:
    from durin.utils.prompt_templates import render_template
    return render_template("agent/skill_curation.md", strip=True,
                           catalog_json=json.dumps(catalog, ensure_ascii=False),
                           usage_json=json.dumps(usage, ensure_ascii=False))
