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


def _emit(event: str, **data) -> None:
    """Best-effort curation telemetry."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # noqa: BLE001 — telemetry must never break curation
        pass


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
    skills_info = ss.list_skills_info(workspace)
    auto = [s["name"] for s in skills_info if s["mode"] == "auto" and s["source"] == "workspace"]

    # Repair before selection: a skill missing its frontmatter description is
    # invisible to the agent (the prompt-summary fallback is just its name).
    # This only touches frontmatter, not the body, so it does NOT change the
    # body hash `needs_curation` compares — the repaired skill is added to the
    # delta explicitly below (same pattern as the observation-driven pull-in).
    backfilled_names: set[str] = set()
    for s in skills_info:
        if s["name"] in auto and not s["description"].strip():
            if ss.backfill_surface_frontmatter(workspace, s["name"]):
                backfilled_names.add(s["name"])
                _emit("skill.curation_action", action="backfill",
                     skill=s["name"], applied=True)
    backfilled = len(backfilled_names)

    delta = [n for n in auto if ss.needs_curation(workspace, n)]
    delta += sorted(n for n in backfilled_names if n not in delta)
    # Observation-driven delta: an OPEN observation pulls its skill in even
    # when the body is unchanged. ("all"/"new:*" records carry no reviewable
    # skill; they ride along in the prompt / the skill-extract pass.)
    open_obs = so.open_observations(workspace)
    delta += sorted(n for n in {r.get("skill") for r in open_obs}
                    if n in auto and n not in delta)
    if not delta:
        _emit("skill.curation_run", reviewed=0, applied=0, deferred=0,
             backfilled=backfilled)
        return {"reviewed": 0, "applied": 0, "deferred": 0, "backfilled": backfilled,
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

    # User hand-edits since the last curation: dream must treat these as
    # intentional — evolve only for a concrete reason, never revert silently.
    user_edits = {
        n: ev for n in selected
        if (ev := ss.user_edits_since_curation(workspace, n))
    }

    prompt = _build_prompt(catalog, usage or {}, upstream, obs_shown,
                           declined_shown, principles, user_edits)
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
            ok = bool(r.get("ok"))
            applied += 1 if ok else 0
            _emit("skill.curation_action", action="fuse", skill=a["target"], applied=ok)
        elif t == "evolve":
            if a.get("name") not in selected:
                logger.warning("curation: skipping evolve of out-of-scope skill %s", a.get("name"))
                continue
            r = ss.apply_skill_edit(workspace, a["name"], old=a["old"], new=a["new"],
                                    rationale=a.get("rationale", "evolve"))
            ok = bool(r.get("ok"))
            applied += 1 if ok else 0
            _emit("skill.curation_action", action="evolve", skill=a["name"], applied=ok)
        elif t == "retire":
            # Remove a fully-obsolete skill outright (git-recoverable via
            # remove_skill, which refuses builtins). The body-change/empty-body
            # path can only `evolve` toward an empty SKILL.md, leaving clutter.
            if a.get("name") not in selected:
                logger.warning("curation: skipping retire of out-of-scope skill %s", a.get("name"))
                continue
            r = ss.remove_skill(workspace, a["name"])
            ok = bool(r.get("ok"))
            applied += 1 if ok else 0
            _emit("skill.curation_action", action="retire", skill=a["name"], applied=ok)
        elif t == "principle":
            r = so.add_principle(workspace, str(a.get("text", "")),
                                 rationale=str(a.get("rationale", "")))
            ok = bool(r.get("ok"))
            if ok:
                applied += 1
            else:
                logger.warning("curation: principle action rejected: %s", r.get("error"))
            _emit("skill.curation_action", action="principle", applied=ok)
        elif t == "retire_principle":
            r = so.retire_principle(workspace, a.get("id", 0))
            ok = bool(r.get("ok"))
            if ok:
                applied += 1
            else:
                logger.warning("curation: retire_principle rejected: %s", r.get("error"))
            _emit("skill.curation_action", action="retire_principle", applied=ok)

    # Per-observation dispositions — only for records the judge actually saw.
    shown_ids = {r.get("id") for r in obs_shown}
    dispositions = [d for d in parsed.get("observations", [])
                    if d.get("id") in shown_ids]
    obs_res = (so.apply_dispositions(workspace, dispositions)
               if dispositions else dict(_NO_OBS))

    for n in selected:
        if ss.read_skill_content(workspace, n) is not None:
            ss.mark_curated(workspace, n)
    _emit("skill.curation_run", reviewed=len(selected), applied=applied,
         deferred=deferred, backfilled=backfilled)
    return {"reviewed": len(selected), "applied": applied, "deferred": deferred,
            "backfilled": backfilled,
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
    prompt = _build_suggestion_prompt(catalog)
    try:
        parsed = json.loads(judge(prompt)) or {}
    except (ValueError, TypeError):
        parsed = {}
    actions = parsed.get("actions", [])

    suggested = 0
    suppressed = 0
    for a in actions:
        t = a.get("type")
        if t not in ("evolve", "retire"):
            # The suggestion pass only proposes evolve/retire for manual skills
            # (fuse refuses manual sources; principles are cross-cutting). Log so
            # an unexpected judge action type isn't dropped without a trace.
            logger.debug("skill suggestions: dropping unsupported action type %r", t)
            continue
        if a.get("name") not in selected:
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


def _build_suggestion_prompt(catalog: dict) -> str:
    from durin.utils.prompt_templates import render_template
    return render_template("agent/skill_suggestions.md", strip=True,
                           catalog_json=json.dumps(catalog, ensure_ascii=False))


def _build_prompt(catalog: dict, usage: dict, upstream: dict | None = None,
                  observations: list[dict] | None = None,
                  declined: list[dict] | None = None,
                  principles: list[dict] | None = None,
                  user_edits: dict | None = None) -> str:
    from durin.utils.prompt_templates import render_template
    return render_template("agent/skill_curation.md", strip=True,
                           catalog_json=json.dumps(catalog, ensure_ascii=False),
                           usage_json=json.dumps(usage, ensure_ascii=False),
                           upstream_json=json.dumps(upstream or {}, ensure_ascii=False),
                           observations_json=json.dumps(observations or [], ensure_ascii=False),
                           declined_json=json.dumps(declined or [], ensure_ascii=False),
                           principles_json=json.dumps(
                               [{"id": p.get("id"), "text": p.get("text")}
                                for p in principles or []], ensure_ascii=False),
                           user_edits_json=json.dumps(
                               {n: [{"subject": e.get("subject"),
                                     "diff": e.get("diff", "")} for e in ev]
                                for n, ev in (user_edits or {}).items()},
                               ensure_ascii=False))
