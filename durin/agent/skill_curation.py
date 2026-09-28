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
import re
from pathlib import Path
from typing import Any, Callable

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


def _parse_judge_output(raw: object) -> dict | None:
    """Parse the judge's JSON object. Returns ``None`` when the output cannot
    be parsed at all (unloadable JSON or wrong top-level type) — distinct from
    a valid object with empty actions, which is a completed review. Same
    None-vs-empty contract (and fence-strip + repair tolerance) as the
    dream-pass parsers."""
    import re as _re

    from json_repair import repair_json

    s = str(raw or "").strip()
    m = _re.search(r"```(?:json)?\s*(.*?)```", s, _re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


_MIN_EVIDENCE_CHARS = 12
# How much of the bundled files the judge sees: enough to aim an edit at a
# script line, bounded per file, per skill, and for the whole review — every
# selected skill may carry scripts, and one batch can hold many skills.
_BUNDLE_FILE_CHARS = 6000
_BUNDLE_SKILL_CHARS = 12000
_BUNDLES_TOTAL_CHARS = 48000
_NOT_SHOWN = "[not shown this pass: the review's room for bundled files is used up]"


def _bundle_view(workspace: Path, name: str, budget: int = _BUNDLE_SKILL_CHARS) -> dict[str, str]:
    """A skill's bundled text files for the judge, each cut to a bounded
    head with a marker when longer, within ``budget`` for the skill."""
    skill_dir = ss._skills_dir(workspace) / name
    if not skill_dir.is_dir():
        return {}
    view: dict[str, str] = {}
    for rel, text in ss.read_bundle_files(skill_dir).items():
        if budget <= 0:
            view[rel] = _NOT_SHOWN
            continue
        cap = min(_BUNDLE_FILE_CHARS, budget)
        view[rel] = text if len(text) <= cap else (
            text[:cap] + f"\n[... cut: {len(text) - cap} more chars not shown ...]")
        budget -= len(view[rel])
    return view


def _bundle_views(workspace: Path, names: list[str]) -> dict[str, dict[str, str]]:
    """Bundled files for the skills under review, in ``names`` order, within
    one budget for the whole prompt; a skill past it shows its file names."""
    views: dict[str, dict[str, str]] = {}
    remaining = _BUNDLES_TOTAL_CHARS
    for name in names:
        view = _bundle_view(workspace, name, budget=min(_BUNDLE_SKILL_CHARS, remaining))
        if view:
            views[name] = view
            remaining -= sum(len(t) for t in view.values() if t != _NOT_SHOWN)
    return views


def _settle_decided_edits(workspace: Path) -> set[int]:
    """Settle the OPEN records whose last attempt is an edit that went to a
    person: applied makes them APPLIED, rejected DECLINED. Returns the ids
    still waiting for that decision, which this pass leaves alone — shown
    again, the judge would propose the same edit."""
    from durin.agent import approval_store

    waiting: set[int] = set()
    decided: list[dict] = []
    for rec in so.open_observations(workspace):
        attempts = rec.get("attempts") or []
        approval_id = attempts[-1].get("approval") if attempts else None
        if not approval_id:
            continue
        status = (approval_store.get(workspace, str(approval_id)) or {}).get("status")
        if status == "applied":
            decided.append({"id": rec.get("id"), "disposition": "applied"})
        elif status == "rejected":
            decided.append({"id": rec.get("id"), "disposition": "declined"})
        elif status in ("pending", "approved"):
            waiting.add(int(rec.get("id", 0)))
    if decided:
        so.apply_dispositions(workspace, decided)
    return waiting


def _applied_holds(workspace: Path, rec: dict, disposition: dict, landed: set[str]) -> bool:
    """Whether an `applied` verdict on ``rec`` is backed by something real: a
    change that landed on its skill this pass, or ``evidence`` — text quoted
    from the skill — that is really in the skill's files (the judge's claim
    that the fix was already there)."""
    skill = str(rec.get("skill") or "")
    if skill in landed:
        return True
    evidence = disposition.get("evidence")
    if not isinstance(evidence, str):
        return False
    needle = _squash(evidence)
    if len(needle) < _MIN_EVIDENCE_CHARS:
        return False
    if skill == "all":
        # A cross-skill lesson is in place when an active principle says it.
        return any(needle in _squash(str(p.get("text", "")))
                   for p in so.active_principles(workspace))
    skill_dir = ss._skills_dir(workspace) / skill
    if not skill_dir.is_dir():
        return False
    texts = [ss.read_skill_content(workspace, skill) or ""]
    texts += list(ss.read_bundle_files(skill_dir).values())
    return any(needle in _squash(t) for t in texts)


def _squash(text: str) -> str:
    """Whitespace-insensitive form, so a quote survives line re-wrapping."""
    return re.sub(r"\s+", " ", text).strip()


def _normalize_files(raw: object) -> dict[str, str]:
    """Accept the judge's bundled-file spec as either a {path: content} object or
    a [{path, content}] array, returning a {path: content} dict. Malformed
    entries are skipped."""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("path"):
                out[str(item["path"])] = str(item.get("content", ""))
    return out


def _emit_curation_parse_failure(stage: str, raw: object) -> None:
    """Surface an unparseable judge response the same way dream-pass parse
    failures surface (telemetry + Dream-feed warning). Best-effort."""
    try:
        from durin.memory.llm_invoke import emit_parse_failure
        emit_parse_failure(stage, raw=str(raw or ""))
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
    waiting = _settle_decided_edits(workspace)

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
        if s["name"] in auto and not str(s["description"] or "").strip():
            if ss.backfill_surface_frontmatter(workspace, s["name"]):
                backfilled_names.add(s["name"])
                _emit("skill.curation_action", action="backfill",
                     skill=s["name"], applied=True)
    backfilled = len(backfilled_names)

    delta = [n for n in auto if ss.needs_curation(workspace, n)]
    delta += sorted(n for n in backfilled_names if n not in delta)
    # Observation-driven delta: an OPEN observation pulls its skill in even
    # when the body is unchanged. ("all"/"new:*" records carry no reviewable
    # skill; they ride along in the prompt / the skill-extract pass.) A record
    # waiting for a person's decision on its edit, or one curation stopped
    # trying (stalled), stays out.
    open_obs = [r for r in so.open_observations(workspace)
                if int(r.get("id", 0)) not in waiting and not r.get("stalled_at")]
    delta += sorted(n for n in {r.get("skill") for r in open_obs}
                    if n in auto and n not in delta)
    if not delta:
        _emit("skill.curation_run", reviewed=0, applied=0, deferred=0,
             backfilled=backfilled)
        return {"reviewed": 0, "applied": 0, "deferred": 0, "backfilled": backfilled,
                "observations": {**_NO_OBS, "open": len(so.open_observations(workspace))},
                "principles": len(so.active_principles(workspace))}

    selected = delta[:budget]
    deferred = len(delta) - len(selected)
    if deferred:
        logger.info("skill curation: delta=%d > budget=%d; deferring %d",
                    len(delta), budget, deferred)

    catalog = {n: ss.read_skill_content(workspace, n) or "" for n in selected}
    # Skills with open observations first: their fix may be in a script.
    with_obs = {r.get("skill") for r in open_obs}
    bundles = _bundle_views(workspace, sorted(selected, key=lambda n: n not in with_obs))

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
    # cross-skill "all" records), and the compact history of records already
    # taken off the queue by hand — rejected, or routed to durin itself — so it
    # doesn't re-propose them.
    in_scope = set(selected) | {"all"}
    obs_shown = [r for r in open_obs if r.get("skill") in in_scope]
    declined_shown = [
        {"id": r.get("id"), "skill": r.get("skill"), "issue": r.get("issue")}
        for r in so.suppressed_observations(workspace)
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
                           declined_shown, principles, user_edits,
                           workspace=workspace, bundles=bundles)
    raw = judge(prompt)
    parsed = _parse_judge_output(raw)
    if parsed is None:
        # Unparseable judge output (degraded provider, prose instead of JSON).
        # Do NOT stamp the reviewed set — leaving it unstamped means the same
        # skills re-enter the next run's delta instead of the review being
        # silently consumed as a no-op. Observations stay OPEN for the same
        # reason. Backfills already applied above are deterministic and kept.
        _emit_curation_parse_failure("curation", raw)
        logger.warning(
            "curation: judge output unparseable; skipping stamps so the "
            "%d selected skill(s) re-enter the next run", len(selected))
        _emit("skill.curation_run", reviewed=len(selected), applied=0,
             deferred=deferred, backfilled=backfilled)
        return {"reviewed": len(selected), "applied": 0, "deferred": deferred,
                "backfilled": backfilled, "judge_parse_failed": True,
                "observations": {**dict(_NO_OBS),
                                 "open": len(so.open_observations(workspace))},
                "principles": len(so.active_principles(workspace))}
    actions = parsed.get("actions", [])

    applied = 0
    # Which skills a change actually landed on this pass, and what happened to
    # the attempts that did not: an observation is settled by a landed change,
    # not by the judge saying so.
    landed: set[str] = set()
    attempts: dict[str, list[str]] = {}
    # The approval an edit was filed as, by skill: its decision settles the
    # records later (_settle_decided_edits).
    approvals: dict[str, str] = {}

    def _attempt(skill: Any, what: str, res: dict) -> None:
        detail = res.get("error") or "no change was committed"
        if res.get("pending_approval"):
            detail = f"waiting for approval {res['pending_approval']}"
            approvals[str(skill)] = str(res["pending_approval"])
        attempts.setdefault(str(skill), []).append(f"{what}: {detail}")

    for a in actions:
        t = a.get("type")
        if t == "fuse":
            if not set(a.get("sources", [])) <= set(selected):
                logger.warning("curation: skipping fuse with out-of-scope sources %s", a.get("sources"))
                _emit("skill.curation_action", action="fuse", skill=a.get("target"), applied=False)
                continue
            # Same reason as the evolve guard below: a field the judge forgot is
            # one skipped action, not a KeyError that kills the rest of the pass.
            if not a.get("target") or not a.get("content") or not a.get("sources"):
                logger.warning("curation: skipping incomplete fuse of %s", a.get("target"))
                _emit("skill.curation_action", action="fuse", skill=a.get("target"), applied=False)
                continue
            r = ss.dream_fuse_skills(workspace, target=a["target"], content=a["content"],
                                     sources=a["sources"], rationale=a.get("rationale", "fuse"),
                                     files=_normalize_files(a.get("files")) or None,
                                     composition_judge=judge,
                                     attribution=ss.Attribution(actor="curation"))
            ok = bool(r.get("ok"))
            applied += 1 if ok else 0
            if ok:
                landed.update([a["target"], *a["sources"]])
            else:
                for name in a["sources"]:
                    _attempt(name, f"fuse into {a['target']}", r)
            _emit("skill.curation_action", action="fuse", skill=a["target"], applied=ok)
        elif t == "restructure":
            # The doctrine-repair verb. The judge only DECIDES ("restructure X
            # toward this intent"); an agentic sub-agent EXECUTES it in an isolated
            # staging copy using real tools (read the skill's files, bundle a
            # script, author a workflow to delegate to), the result is validated,
            # and only a validated, complete skill is applied to live — else it is
            # discarded, live untouched. The judge never emits whole artifacts
            # inline (that shape corrupted a skill when a completion truncated).
            if a.get("name") not in selected:
                logger.warning("curation: skipping restructure of out-of-scope skill %s", a.get("name"))
                _emit("skill.curation_action", action="restructure", skill=a.get("name"), applied=False)
                continue
            intent = str(a.get("intent") or a.get("rationale") or "").strip()
            if not intent:
                logger.warning("curation: skipping restructure of %s — no intent given", a.get("name"))
                _emit("skill.curation_action", action="restructure", skill=a.get("name"), applied=False)
                continue
            from durin.agent.skill_restructure import restructure_skill_agentic
            r = restructure_skill_agentic(workspace, a["name"], intent=intent)
            ok = bool(r.get("applied"))
            applied += 1 if ok else 0
            if ok:
                landed.add(a["name"])
            else:
                _attempt(a["name"], "restructure", r)
                logger.info("curation: restructure of %s not applied: %s",
                            a["name"], r.get("error"))
            _emit("skill.curation_action", action="restructure", skill=a["name"], applied=ok)
        elif t == "evolve":
            if a.get("name") not in selected:
                logger.warning("curation: skipping evolve of out-of-scope skill %s", a.get("name"))
                _emit("skill.curation_action", action="evolve", skill=a.get("name"), applied=False)
                continue
            # An evolve missing its old/new pair used to raise KeyError here and
            # abort the whole pass mid-flight — losing every remaining action AND
            # the review stamps, so the same delta came back the next night. A
            # malformed action is one skipped action, never a dead pass.
            if "old" not in a or "new" not in a:
                logger.warning("curation: skipping evolve of %s — action carries no %s",
                               a.get("name"),
                               "old text" if "old" not in a else "new text")
                _emit("skill.curation_action", action="evolve", skill=a.get("name"),
                     applied=False)
                continue
            r = ss.apply_skill_edit(workspace, a["name"], old=a["old"], new=a["new"],
                                    rationale=a.get("rationale", "evolve"),
                                    file=str(a.get("file") or "SKILL.md"))
            # An edit that committed nothing (a no-op) changed nothing.
            ok = bool(r.get("ok")) and bool(r.get("commit"))
            applied += 1 if ok else 0
            if ok:
                landed.add(a["name"])
            else:
                _attempt(a["name"], "evolve", r)
            _emit("skill.curation_action", action="evolve", skill=a["name"], applied=ok)
        elif t == "retire":
            # Remove a fully-obsolete skill outright (git-recoverable via
            # remove_skill, which refuses builtins). The body-change/empty-body
            # path can only `evolve` toward an empty SKILL.md, leaving clutter.
            if a.get("name") not in selected:
                logger.warning("curation: skipping retire of out-of-scope skill %s", a.get("name"))
                _emit("skill.curation_action", action="retire", skill=a.get("name"), applied=False)
                continue
            # A workflow names a skill by name: retiring one it references leaves
            # the node pointing at nothing. The guard sits here, at the autonomous
            # call site, rather than inside remove_skill — a user deleting their
            # own skill is entitled to, and is warned elsewhere.
            from durin.registry_graph import dependents_of, describe
            deps = dependents_of(workspace, skill=a["name"])
            if deps:
                logger.warning("curation: refusing to retire %s — referenced by %s",
                               a["name"], describe(deps))
                _emit("skill.curation_action", action="retire", skill=a["name"], applied=False)
                continue
            replaced_by = a.get("replaced_by")
            r = ss.remove_skill(workspace, a["name"], by="curation",
                                reason=str(a.get("rationale") or ""),
                                replaced_by=replaced_by if isinstance(replaced_by, str) else None)
            ok = bool(r.get("ok"))
            applied += 1 if ok else 0
            if ok:
                landed.add(a["name"])
            else:
                _attempt(a["name"], "retire", r)
            _emit("skill.curation_action", action="retire", skill=a["name"], applied=ok)
        elif t == "principle":
            r = so.add_principle(workspace, str(a.get("text", "")),
                                 rationale=str(a.get("rationale", "")))
            ok = bool(r.get("ok"))
            if ok:
                applied += 1
                landed.add("all")
            elif r.get("error") == "principle already exists":
                # The lesson is already in force: what an "all" record asks for
                # is in place.
                landed.add("all")
            else:
                _attempt("all", "principle", r)
                logger.warning("curation: principle action rejected: %s", r.get("error"))
            _emit("skill.curation_action", action="principle", applied=ok)
        elif t == "retire_principle":
            r = so.retire_principle(workspace, a.get("id", 0))
            ok = bool(r.get("ok"))
            if ok:
                applied += 1
                landed.add("all")
            else:
                _attempt("all", "retire_principle", r)
                logger.warning("curation: retire_principle rejected: %s", r.get("error"))
            _emit("skill.curation_action", action="retire_principle", applied=ok)

    # Per-observation dispositions — only for records the judge actually saw.
    # An `applied` stands only on a change that landed on that skill this pass,
    # or on quoted evidence found in the skill's files that it already holds
    # the fix; otherwise the record stays OPEN with a note of what was tried.
    shown = {r.get("id"): r for r in obs_shown}
    dispositions = []
    for d in parsed.get("observations", []):
        rec = shown.get(d.get("id"))
        if rec is None:
            continue
        skill = str(rec.get("skill") or "")
        if d.get("disposition") == "applied" and not _applied_holds(workspace, rec, d, landed):
            note = "; ".join(attempts.get(skill, [])) or (
                "marked applied, but no change landed on the skill and no evidence "
                "was quoted from it")
            d = {"id": d.get("id"), "disposition": "keep", "note": note}
        elif d.get("disposition") == "keep" and attempts.get(skill):
            d = {**d, "note": "; ".join(attempts[skill])}
        if d.get("disposition") == "keep" and skill in approvals:
            d = {**d, "approval": approvals[skill]}
        dispositions.append(d)
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
    raw = judge(prompt)
    parsed = _parse_judge_output(raw)
    if parsed is None:
        # Same guard as curate_catalog: an unparseable judge must not advance
        # the evaluation cursor, or the skill is never re-evaluated.
        _emit_curation_parse_failure("suggestions", raw)
        logger.warning("skill suggestions: judge output unparseable; cursor "
                       "not advanced for %d skill(s)", len(selected))
        return {"reviewed": len(selected), "suggested": 0, "suppressed": 0,
                "judge_parse_failed": True}
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
                  user_edits: dict | None = None,
                  workspace: Path | None = None,
                  bundles: dict | None = None) -> str:
    from durin.agent.skills_doctrine import composition_doctrine, workflow_catalog_text
    from durin.utils.prompt_templates import render_template
    return render_template("agent/skill_curation.md", strip=True,
                           doctrine=composition_doctrine() or "(doctrine unavailable)",
                           workflow_catalog=(workflow_catalog_text(workspace)
                                             if workspace else "(no workflows installed)"),
                           catalog_json=json.dumps(catalog, ensure_ascii=False),
                           bundles_json=json.dumps(bundles or {}, ensure_ascii=False),
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
