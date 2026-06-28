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

Observations (task-observer pattern): OPEN records in the observation queue are
the judge's evidence channel — they pull their skill into the delta even when
the body is unchanged, and the judge answers each shown record with a
disposition (applied/declined/keep). Observations on `manual` skills or
pristine builtins stay OPEN untouched: manual skills are the user's to edit,
and builtins join the evolving set only once forked by an edit. "new:*"
records are skill-extract input, never curation input.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from durin.agent import skill_observations as so
from durin.agent import skills_store as ss

DEFAULT_BUDGET = 50
logger = logging.getLogger(__name__)

_NO_OBS = {"applied": 0, "declined": 0, "kept": 0}


def curate_catalog(workspace, *, judge: Callable[[str], str],
                   usage: dict | None = None, budget: int = DEFAULT_BUDGET,
                   drift_check: Callable | None = None,
                   allowlist=None) -> dict:
    """One delta-curation pass.

    Returns {'reviewed', 'applied', 'deferred', 'observations'}.
    """
    workspace = Path(workspace)
    # Records APPLIED during the previous pass got their cycle of visibility —
    # move them to the archive before building this pass's evidence.
    so.archive_resolved(workspace)

    # Only the evolving WORKSPACE set: dream-created + forked skills. Pristine
    # builtins (source="builtin") are the stable seed — not re-curated/forked
    # until they're forked into the workspace by some other path.
    auto = [
        s["name"] for s in ss.list_skills_info(workspace)
        if s["mode"] == "auto" and s["source"] == "workspace"
    ]
    delta = [n for n in auto if ss.needs_curation(workspace, n)]
    # Observation-driven delta: an OPEN observation pulls its skill in even
    # when the body is unchanged. ("all"/"new:*" records carry no reviewable
    # skill; they ride along in the prompt / the skill-extract pass.)
    open_obs = so.open_observations(workspace)
    delta += sorted(n for n in {r.get("skill") for r in open_obs}
                    if n in auto and n not in delta)
    if not delta:
        return {"reviewed": 0, "applied": 0, "deferred": 0,
                "observations": {**_NO_OBS, "open": len(open_obs)},
                "principles": len(so.active_principles(workspace))}

    selected = delta[:budget]
    deferred = len(delta) - len(selected)
    if deferred:
        logger.info("skill curation: delta=%d > budget=%d; deferring %d",
                    len(delta), budget, deferred)

    catalog = {n: ss.read_skill_content(workspace, n) or "" for n in selected}

    import shutil as _shutil
    upstream: dict[str, str] = {}
    if drift_check is not None:
        for n in selected:
            try:
                rep = drift_check(workspace, n, allowlist=list(allowlist or []))
            except Exception:  # noqa: BLE001 — drift is best-effort; never break curation
                logger.exception("upstream drift check failed for %s", n)
                continue
            if rep is None:
                continue
            if rep.action == "allow":
                upstream[n] = rep.upstream_md  # safe → judge may incorporate via evolve
            else:
                logger.info("skill %s: upstream drift is %s (carries code / untrusted) — "
                            "not auto-incorporated, left for human review", n, rep.action)
            # always consume the fetched upstream copy
            _shutil.rmtree(rep.qdir, ignore_errors=True)

    # The judge sees OPEN observations for the skills under review (plus
    # cross-skill "all" records), and the compact DECLINED history so it
    # doesn't re-propose rejected changes.
    in_scope = set(selected) | {"all"}
    obs_shown = [r for r in open_obs if r.get("skill") in in_scope]
    declined_shown = [
        {"id": r.get("id"), "skill": r.get("skill"), "issue": r.get("issue")}
        for r in so.declined_observations(workspace)
        if r.get("skill") in in_scope
    ]

    principles = so.active_principles(workspace)

    prompt = _build_prompt(catalog, usage or {}, upstream, obs_shown,
                           declined_shown, principles)
    try:
        parsed = json.loads(judge(prompt)) or {}
    except (ValueError, TypeError):
        parsed = {}
    actions = parsed.get("actions", [])

    applied = 0
    for a in actions:
        t = a.get("type")
        if t == "fuse":
            if not set(a.get("sources", [])) <= set(selected):
                logger.warning("curation: skipping fuse with out-of-scope sources %s", a.get("sources"))
                continue
            r = ss.dream_fuse_skills(workspace, target=a["target"], content=a["content"],
                                     sources=a["sources"], rationale=a.get("rationale", "fuse"),
                                     attribution=ss.Attribution(actor="curation"))
            applied += 1 if r.get("ok") else 0
        elif t == "evolve":
            if a.get("name") not in selected:
                logger.warning("curation: skipping evolve of out-of-scope skill %s", a.get("name"))
                continue
            r = ss.apply_skill_edit(workspace, a["name"], old=a["old"], new=a["new"],
                                    rationale=a.get("rationale", "evolve"))
            applied += 1 if r.get("ok") else 0
        elif t == "retire":
            # Remove a fully-obsolete skill outright (git-recoverable via
            # remove_skill, which refuses builtins). The body-change/empty-body
            # path can only `evolve` toward an empty SKILL.md, leaving clutter.
            if a.get("name") not in selected:
                logger.warning("curation: skipping retire of out-of-scope skill %s", a.get("name"))
                continue
            r = ss.remove_skill(workspace, a["name"])
            applied += 1 if r.get("ok") else 0
        elif t == "principle":
            r = so.add_principle(workspace, str(a.get("text", "")),
                                 rationale=str(a.get("rationale", "")))
            if r.get("ok"):
                applied += 1
            else:
                logger.warning("curation: principle action rejected: %s", r.get("error"))
        elif t == "retire_principle":
            r = so.retire_principle(workspace, a.get("id", 0))
            if r.get("ok"):
                applied += 1
            else:
                logger.warning("curation: retire_principle rejected: %s", r.get("error"))

    # Per-observation dispositions — only for records the judge actually saw.
    shown_ids = {r.get("id") for r in obs_shown}
    dispositions = [d for d in parsed.get("observations", [])
                    if d.get("id") in shown_ids]
    obs_res = (so.apply_dispositions(workspace, dispositions)
               if dispositions else dict(_NO_OBS))

    for n in selected:
        if ss.read_skill_content(workspace, n) is not None:
            ss.mark_curated(workspace, n)
    return {"reviewed": len(selected), "applied": applied, "deferred": deferred,
            "observations": {**{k: obs_res.get(k, 0) for k in _NO_OBS},
                             "open": len(so.open_observations(workspace))},
            "principles": len(so.active_principles(workspace))}


def suggest_manual_skills(workspace, *, judge: Callable[[str], str],
                          usage: dict | None = None,
                          budget: int = DEFAULT_BUDGET) -> dict:
    """Curation for MANUAL skills: run the same judge, but ENQUEUE its actions as
    suggestions for user review instead of applying them. The auto path
    (curate_catalog) is untouched. Conclusions covered by a live rejection
    tombstone are suppressed. Evaluation state is tracked in a sidecar cursor so
    manual skill files are never written."""
    from durin.agent import skill_suggestions as sg

    workspace = Path(workspace)
    manual = [
        s["name"] for s in ss.list_skills_info(workspace)
        if s["mode"] == "manual" and s["source"] == "workspace"
    ]
    delta = [n for n in manual if sg.needs_suggestion(workspace, n)]
    if not delta:
        return {"reviewed": 0, "suggested": 0, "suppressed": 0}

    selected = delta[:budget]
    catalog = {n: ss.read_skill_content(workspace, n) or "" for n in selected}
    prompt = _build_prompt(catalog, usage or {}, None, [], [],
                           so.active_principles(workspace))
    try:
        parsed = json.loads(judge(prompt)) or {}
    except (ValueError, TypeError):
        parsed = {}
    actions = parsed.get("actions", [])

    suggested = 0
    suppressed = 0
    for a in actions:
        t = a.get("type")
        if t not in ("evolve", "retire", "fuse"):
            continue
        if t == "fuse":
            if not set(a.get("sources", [])) <= set(selected):
                continue
        elif a.get("name") not in selected:
            continue
        fp = sg.fingerprint(a)
        if sg.is_tombstoned(workspace, fp):
            suppressed += 1
            continue
        sg.add_suggestion(workspace, a)
        suggested += 1

    for n in selected:
        sg.mark_suggested(workspace, n)

    return {"reviewed": len(selected), "suggested": suggested,
            "suppressed": suppressed}


def _build_prompt(catalog: dict, usage: dict, upstream: dict | None = None,
                  observations: list[dict] | None = None,
                  declined: list[dict] | None = None,
                  principles: list[dict] | None = None) -> str:
    from durin.utils.prompt_templates import render_template
    return render_template("agent/skill_curation.md", strip=True,
                           catalog_json=json.dumps(catalog, ensure_ascii=False),
                           usage_json=json.dumps(usage, ensure_ascii=False),
                           upstream_json=json.dumps(upstream or {}, ensure_ascii=False),
                           observations_json=json.dumps(observations or [], ensure_ascii=False),
                           declined_json=json.dumps(declined or [], ensure_ascii=False),
                           principles_json=json.dumps(
                               [{"id": p.get("id"), "text": p.get("text")}
                                for p in principles or []], ensure_ascii=False))
