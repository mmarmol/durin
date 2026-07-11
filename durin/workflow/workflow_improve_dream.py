"""The dream pass that improves workflows.

For each workflow with new terminal runs since the last pass, it computes the
diagnostic signal (which nodes loop, which gates fail, which script nodes crash,
recurring across runs), shows a model the workflow definition + that diagnostic +
the recent change history (so it does not re-propose reverted edits), and produces
ONE proposed edit. ``improvement_mode`` decides the disposition:

- ``manual`` — record a recommendation for the user to review (never applied).
- ``auto`` — apply it through the shared editing engine (graph re-validated,
  version commit with actor="dream"), then hold it *pending validation*: if the
  edited node's trouble rate worsened over the workflow's NEXT runs, the edit is
  auto-reverted and marked so it is never re-proposed.

A proposal is one of three shapes:
- a node's ``prompt`` (work nodes and routing nodes both use ``prompt``) — the
  original lane, unrestricted.
- a script node's inline ``command`` — only for a node whose recurring
  ``node_failed`` runs meet the evidence floor (``diagnostics.script_failures``).
- a ``script_file``'s full content — only for a file backing such a node.

The anti-Goodhart anchor (spec: gate-fail counts never justify a script edit;
routing targets never auto-apply) is enforced in code, not prompt trust: a
script edit on a node that *routes* (or a file referenced by one) is always
``manual_only`` — recorded as a recommendation even in ``improvement_mode: auto``.
An ``ok`` script proposal also runs the deterministic pre-apply gate
(``script_precheck.precheck_script_edit`` — syntax, security scan, smoke run)
before it is ever queued; a failing check escalates it as a structural
suggestion carrying the check's own output, never silently dropped.

``workflows/scripts/`` is a namespace shared by every workflow in the
workspace, not scoped to one — a ``script_file`` proposal is auto-appliable
ONLY when every node across ALL parseable workflows that references that file
lives in the PROPOSING workflow and none of them routes. A reference from a
DIFFERENT workflow (routing or not — its damage would be invisible to this
workflow's own pending-validation window) or a routing reference anywhere
forces ``manual_only``, exactly like the same-workflow gate case above.

A proposal OUTSIDE all three shapes (restructure, rewire, other fields, a
script edit with no recurring evidence) is never silently dropped either: it
lands in the recommendations queue as an annotated ``structural`` suggestion
(proposal + why the scope refused it + the diagnostic evidence) for the user to
treat in a session; it has no auto-apply in any mode. The model is injectable
so the pass is fully testable without a live LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from durin.workflow import run_log
from durin.workflow.diagnostics import RECURRENCE_FLOOR, compute_diagnostics
from durin.workflow.loader import WorkflowError, load_workflow, workflows_dir
from durin.workflow.script_precheck import precheck_script_edit
from durin.workflow.spec import ScriptNode, WorkNode
from durin.workflow.version_store import history_for_dream

# How much of a script file's content is embedded in the model's prompt.
_SCRIPT_CONTENT_CAP = 4000

_SYSTEM = (
    "You improve a user's agent workflow. You are shown the workflow definition (JSON), "
    "a diagnostic of where it recurringly struggles across recent runs, and the recent "
    "change history with the reasons prior edits were made. Propose exactly ONE small "
    "edit that would reduce the recurring trouble, in one of three shapes:\n"
    "1. A node's 'prompt' (work nodes and routing nodes both use 'prompt' as their "
    "editable field): {\"target_id\": \"<node id>\", \"field\": \"prompt\", "
    "\"current\": \"<current text>\", \"proposed\": \"<new text>\", \"reason\": \"<why>\"}.\n"
    "2. A script node's inline 'command' — ONLY for a node listed below under 'Failing "
    "scripts' (its failures recur across runs, with the stderr/exit evidence attached): "
    "{\"target_id\": \"<node id>\", \"field\": \"command\", \"proposed\": \"<new command>\", "
    "\"reason\": \"<why>\"}.\n"
    "3. A script file's full new content — ONLY for a file backing a node listed below "
    "under 'Failing scripts': {\"field\": \"script_file\", \"script\": \"<filename>\", "
    "\"proposed\": \"<full new file content>\", \"reason\": \"<why>\"}.\n"
    "Rules for the script shapes: a script node that routes (on_pass/on_fail or cases) is "
    "a quality gate — NEVER weaken a gate's check to make it pass. If a gate fails a lot, "
    "the trouble is almost always in whatever it is checking, so prefer improving the "
    "PRODUCER node's prompt instead of the gate. A gate edit is still possible, but it "
    "will always be held for a person to review before it takes effect — it is never "
    "applied automatically, no matter the workflow's mode. Only propose a script edit for "
    "a node explicitly listed under 'Failing scripts' below; a healthy script must never "
    "be touched speculatively. "
    "Do NOT add or remove nodes or edges. Do NOT re-propose an edit the history shows was "
    "already tried. Reply with ONLY the JSON object for the ONE shape you are proposing."
)


def _improvable_workflows(workspace):
    """(name, Workflow) for each parseable workflow definition, both modes.
    ``improvement_mode`` decides a proposal's DISPOSITION (auto → apply,
    manual → recommend), not whether the workflow is observed."""
    out = []
    for f in sorted(workflows_dir(workspace).glob("*.json")):
        try:
            wf = load_workflow(workspace, f.stem)
        except (WorkflowError, OSError, ValueError):
            continue   # a malformed definition must not break the pass
        out.append((f.stem, wf))
    return out


def _script_referenced_outside(workspace, name: str, script: str) -> bool:
    """True when a node in some workflow OTHER than ``name`` references ``script``.

    ``workflows/scripts/`` is a namespace shared across every workflow in the
    workspace: two unrelated workflows can point a script node at the same
    file. A proposal classified against only the PROPOSING workflow's nodes
    cannot see damage a same-named-file edit would do to a sibling workflow,
    so any reference from outside forces the proposal to ``manual_only``
    regardless of whether that sibling reference itself routes. Reuses
    ``_improvable_workflows`` so a malformed sibling definition is tolerated
    (skipped), never raises out of this scan."""
    for other_name, other_wf in _improvable_workflows(workspace):
        if other_name == name:
            continue
        if any(isinstance(n, ScriptNode) and n.script == script for n in other_wf.nodes.values()):
            return True
    return False


def _emit(event: str, **data) -> None:
    """Best-effort improve-pass telemetry — never breaks the pass."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover
        pass


def _pending_path(workspace, name: str):
    from durin.workflow.run_log import runs_root
    return runs_root(workspace) / name / ".pending_validation.json"


def _node_trouble_rate(diag, node_id: str) -> float:
    """The edited node's trouble per run: loop-backs + gate fails + script
    failures, normalized. Script failures are 0 for a node type that cannot
    raise one, so this is a strict extension of the pre-script-repair metric."""
    total = max(diag.total_runs, 1)
    return (diag.loop_backs.get(node_id, 0) + diag.gate_fails.get(node_id, 0)
            + diag.script_failures.get(node_id, 0)) / total


def _script_file_trouble_rate(wf, diag, script: str) -> float:
    """A script file's trouble rate is the MAX across every node that runs it —
    the file is one artifact shared by however many nodes reference it."""
    referencing = [n.id for n in wf.nodes.values()
                   if isinstance(n, ScriptNode) and n.script == script]
    if not referencing:
        return 0.0
    return max(_node_trouble_rate(diag, nid) for nid in referencing)


def _write_pending(workspace, name: str, *, rec_id: str, kind: str,
                   baseline_rate: float, target_id: str | None = None,
                   previous: str | None = None, script: str | None = None,
                   previous_content: str | None = None) -> None:
    p = _pending_path(workspace, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    record: dict = {"rec_id": rec_id, "kind": kind, "baseline_rate": baseline_rate}
    if target_id is not None:
        record["target_id"] = target_id
    if previous is not None:
        record["previous"] = previous
    if script is not None:
        record["script"] = script
    if previous_content is not None:
        record["previous_content"] = previous_content
    p.write_text(json.dumps(record), encoding="utf-8")


def _read_pending(workspace, name: str) -> dict | None:
    p = _pending_path(workspace, name)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _clear_pending(workspace, name: str) -> None:
    _pending_path(workspace, name).unlink(missing_ok=True)


def _revert_definition_field(workspace, name: str, pending: dict, new_rate: float, field: str) -> None:
    """Restore a node's prompt/command through the shared editing engine (the
    same path apply_recommendation used to write it)."""
    from durin.workflow.editing import save_workflow_definition

    wf_path = workflows_dir(workspace) / f"{name}.json"
    data = json.loads(wf_path.read_text(encoding="utf-8"))
    node = next((n for n in data.get("nodes", []) if n.get("id") == pending["target_id"]), None)
    if node is not None:
        node[field] = pending["previous"]
        save_workflow_definition(
            workspace, name, data,
            reason=(f"auto-revert {pending['rec_id']}: edited node "
                    f"{pending['target_id']!r} worsened "
                    f"({pending.get('baseline_rate', 0.0):.2f} -> {new_rate:.2f} trouble/run)"),
            actor="dream", must_exist=True)


def _revert_script_file(workspace, name: str, pending: dict, new_rate: float) -> None:
    """Restore a script file's previous bytes atomically + a best-effort version
    snapshot — mirrors apply_recommendation's script_file branch, including
    serializing on the shared version lock so a concurrent editor save (or a
    concurrent apply) never races the revert write."""
    from durin.utils.atomic_write import atomic_write_text
    from durin.utils.file_lock import cross_process_lock
    from durin.workflow.version_store import WorkflowVersionStore, version_lock_target

    script_path = workflows_dir(workspace) / "scripts" / pending["script"]
    with cross_process_lock(version_lock_target(workflows_dir(workspace))):
        atomic_write_text(script_path, pending["previous_content"])
        try:
            WorkflowVersionStore(workflows_dir(workspace)).snapshot(
                f"auto-revert {pending['rec_id']}: script {pending['script']!r} worsened "
                f"({pending.get('baseline_rate', 0.0):.2f} -> {new_rate:.2f} trouble/run)"
            )
        except Exception:  # noqa: BLE001 - versioning must not block the revert
            pass


def _maybe_auto_revert(workspace, name: str, records: list[dict]) -> bool:
    """The auto-apply safety net: an applied edit stays *pending validation*
    until the workflow's NEXT terminal runs. If the edited target's trouble rate
    worsened vs the pre-edit baseline, restore the previous state (a forward
    revert — prompt/command through the definition path, a script file through
    an atomic file restore + snapshot), mark the recommendation reverted (its
    dedup id pins any repeat proposal), and report. Returns True when a revert
    happened (the pass then skips proposing for this workflow this round)."""
    from durin.workflow import workflow_recommendations as wr

    pending = _read_pending(workspace, name)
    if pending is None or not records:
        return False
    diag = compute_diagnostics(records)
    # Pre-script-repair pending records have no 'kind' — they are always a prompt edit.
    kind = pending.get("kind", "prompt")
    if kind == "script_file":
        try:
            wf = load_workflow(workspace, name)
            new_rate = _script_file_trouble_rate(wf, diag, pending["script"])
        except (WorkflowError, OSError, ValueError):
            new_rate = 0.0
    else:
        new_rate = _node_trouble_rate(diag, pending["target_id"])
    if new_rate <= pending.get("baseline_rate", 0.0):
        _clear_pending(workspace, name)   # validated: no worse than before
        return False
    try:
        if kind == "script_file":
            _revert_script_file(workspace, name, pending, new_rate)
        else:
            _revert_definition_field(workspace, name, pending, new_rate, kind)
        wr.mark_reverted(workspace, name, pending["rec_id"],
                         note=f"diagnostic worsened: {pending.get('baseline_rate', 0.0):.2f} -> {new_rate:.2f}")
        # Only one of target_id/script applies per kind — omit the other rather
        # than emitting it as an explicit None (the schema declares both NotRequired).
        extra = {}
        if pending.get("target_id") is not None:
            extra["target_id"] = pending["target_id"]
        if pending.get("script") is not None:
            extra["script"] = pending["script"]
        _emit("workflow.improve.reverted", workflow=name, kind=kind,
              rec_id=pending["rec_id"], baseline_rate=pending.get("baseline_rate", 0.0),
              new_rate=new_rate, **extra)
    finally:
        _clear_pending(workspace, name)
    return True


@dataclass
class _Proposal:
    """A classified, in-scope proposal ready to be logged/applied."""
    kind: str                    # "prompt" | "command" | "script_file"
    proposed: str
    manual_only: bool
    target_id: str | None = None   # node id (prompt/command); None for script_file
    script: str | None = None      # filename (script_file only)


def _failing_script_nodes(wf, diag) -> list[str]:
    """ScriptNode ids whose recurring node_failed evidence meets the floor — the
    ONLY nodes ever offered on the script lane (a gate-fail count alone never
    qualifies; only recurring script_failures does)."""
    return sorted(
        nid for nid, node in wf.nodes.items()
        if isinstance(node, ScriptNode) and diag.script_failures.get(nid, 0) >= RECURRENCE_FLOOR
    )


def _classify_proposal(proposal: dict | None, wf, diag, script_exists: Callable[[str], bool],
                       script_referenced_elsewhere: Callable[[str], bool]):
    """('ok', _Proposal) — in scope and actionable;
    ('structural', why) — outside scope, or script-shaped but missing evidence;
    ('skip', None) — unparseable / empty / in-scope no-op.

    ``script_referenced_elsewhere(script)`` must answer whether some OTHER
    workflow's node references ``script`` (see ``_script_referenced_outside``)
    — the script_file lane's ``manual_only`` decision needs this, since
    ``workflows/scripts/`` is shared across the whole workspace, not scoped
    to ``wf`` alone."""
    if not isinstance(proposal, dict) or not proposal:
        return "skip", None
    field = proposal.get("field")
    proposed = proposal.get("proposed")
    target = proposal.get("target_id") or proposal.get("node_id") or proposal.get("gate_id")

    if field == "prompt" and target and target in wf.nodes and isinstance(wf.nodes[target], WorkNode):
        node = wf.nodes[target]
        if not proposed or not str(proposed).strip():
            return "skip", None
        if proposed.strip() == (node.prompt or "").strip():
            return "skip", None            # no-op: nothing to escalate
        return "ok", _Proposal(kind="prompt", target_id=target, proposed=proposed, manual_only=False)

    if field == "command" and target and target in wf.nodes and isinstance(wf.nodes[target], ScriptNode) \
            and wf.nodes[target].command.strip():
        node = wf.nodes[target]
        if not proposed or not str(proposed).strip():
            return "skip", None
        if proposed.strip() == (node.command or "").strip():
            return "skip", None            # no-op: nothing to escalate
        if diag.script_failures.get(target, 0) < RECURRENCE_FLOOR:
            return "structural", "no recurring script-failure evidence"
        return "ok", _Proposal(kind="command", target_id=target, proposed=proposed, manual_only=node.routes)

    if field == "script_file":
        script = proposal.get("script")
        if script and script_exists(script):
            if not proposed or not str(proposed).strip():
                return "skip", None
            referencing = [n for n in wf.nodes.values()
                           if isinstance(n, ScriptNode) and n.script == script]
            if not referencing:
                return "structural", f"script {script!r} exists but is not referenced by any node in this workflow"
            has_evidence = any(diag.script_failures.get(n.id, 0) >= RECURRENCE_FLOOR for n in referencing)
            if not has_evidence:
                return "structural", "no recurring script-failure evidence"
            # manual_only if a reference in THIS workflow routes, or if ANY other
            # workflow references the same shared file at all (see module docstring).
            manual_only = any(n.routes for n in referencing) or script_referenced_elsewhere(script)
            return "ok", _Proposal(kind="script_file", script=script, proposed=proposed, manual_only=manual_only)

    # Generic scope fallback — same shape as the original prompt-only classifier.
    if not proposed or not str(proposed).strip():
        return "skip", None
    if field == "script_file":
        why = f"script {proposal.get('script')!r} does not exist under workflows/scripts/"
    elif target and target in wf.nodes:
        why = (f"field {field!r} is outside the editable scope "
               f"(prompt; command/script_file with recurring failure evidence)")
    else:
        why = f"target {target!r} is not an editable node of this workflow"
    return "structural", why


def _parse_proposal(text: str) -> dict | None:
    """Tolerant parse of the model's JSON proposal (strips prose around the object)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _truncate(text: str, cap: int = _SCRIPT_CONTENT_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n...[truncated, {len(text) - cap} more chars]"


def _script_home_text(node: ScriptNode, workspace) -> str:
    """The editable content of a script node's home, for the model to see."""
    if node.command.strip():
        return f"inline command:\n{node.command}"
    script_path = workflows_dir(workspace) / "scripts" / node.script
    try:
        content = script_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"script file {node.script!r} (could not be read: {exc})"
    except UnicodeDecodeError:
        return f"script file {node.script!r} (unreadable content)"
    return f"script file {node.script!r}:\n{_truncate(content)}"


def _build_prompt(name, wf_json: str, diag, history, wf, workspace) -> str:
    lines = [
        f"Workflow '{name}' definition:",
        wf_json,
        "",
        "Diagnostic (recurring across runs):",
        f"  nodes that loop back: {diag.loop_backs}",
        f"  gates that fail: {diag.gate_fails}",
        f"  scripts that fail: {diag.script_failures}",
        f"  runs that gave up (exhausted): {diag.max_visits_aborts} / {diag.total_runs}",
        f"  candidates to improve: {sorted(diag.candidates())}",
        "",
    ]
    failing_scripts = _failing_script_nodes(wf, diag)
    if failing_scripts:
        lines.append("Failing scripts (script-edit evidence — a 'command' or 'script_file' "
                     "proposal is only eligible for one of these):")
        for nid in failing_scripts:
            node = wf.nodes[nid]
            gate_note = " [ROUTES — a gate; edit will be held for review, never auto-applied]" if node.routes else ""
            lines.append(f"  node {nid!r} ({diag.script_failures.get(nid, 0)} failing runs){gate_note}:")
            lines.append(f"    {_script_home_text(node, workspace)}")
            samples = diag.failure_samples.get(nid, [])
            if samples:
                lines.append("    recent failure samples:")
                for s in samples:
                    lines.append(f"      - {s}")
        lines.append("")
    lines.append("Recent change history (newest first) — do not re-propose what was tried:")
    for h in history:
        lines.append(f"  [{h['sha']}] {h['reason']}")
    return "\n".join(lines)


def _diag_text(diag) -> str:
    return (f"loop-backs {diag.loop_backs}, gate fails {diag.gate_fails}, "
            f"script failures {diag.script_failures}, "
            f"exhausted {diag.max_visits_aborts}/{diag.total_runs}")


def _script_file_exists(workspace, name: str) -> bool:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    return (workflows_dir(workspace) / "scripts" / name).is_file()


def run_workflow_improve_pass(workspace, *, llm_invoke=None, model=None) -> dict:
    """Observe workflows with new runs; recommend (manual), apply (auto), or
    escalate structural ideas to the user. Best-effort per workflow: one failing
    workflow does not abort the pass."""
    from durin.workflow import workflow_recommendations as wr

    if llm_invoke is None:
        from durin.memory.llm_invoke import default_llm_invoke
        llm_invoke = default_llm_invoke

    processed = 0
    proposals = 0
    applied = 0
    structural = 0
    reverted = 0
    for name, wf in _improvable_workflows(workspace):
        try:
            cursor = run_log.read_cursor(workspace, name)
            records = run_log.read_runs_since(workspace, name, cursor)
            # Only terminal runs are diagnostic; skip in-flight ('running') and stale
            # ('crashed') records, and don't advance the cursor past them so they are
            # reprocessed once finalized (finalize advances their ts past started_at).
            records = [r for r in records if r.get("status") not in ("running", "crashed")]
            if not records:
                continue
            newest_ts = max(r.get("ts", 0.0) for r in records)
            # A previous auto-applied edit is judged FIRST, on these new runs:
            # worsened → revert and consume the window (no new proposal this round).
            if _maybe_auto_revert(workspace, name, records):
                reverted += 1
                run_log.advance_cursor(workspace, name, newest_ts)
                continue
            diag = compute_diagnostics(records)
            if not diag.candidates():
                run_log.advance_cursor(workspace, name, newest_ts)   # consumed, nothing to propose
                continue
            processed += 1
            wf_json = (workflows_dir(workspace) / f"{name}.json").read_text(encoding="utf-8")
            history = history_for_dream(workspace, name)
            prompt = f"{_SYSTEM}\n\n{_build_prompt(name, wf_json, diag, history, wf, workspace)}"
            resp = llm_invoke(prompt, model=model)
            content = getattr(resp, "content", resp if isinstance(resp, str) else "")
            proposal = _parse_proposal(content) or {}
            verdict, payload = _classify_proposal(
                proposal, wf, diag, lambda n: _script_file_exists(workspace, n),
                lambda s: _script_referenced_outside(workspace, name, s))
            run_ids = [r.get("run_id") for r in records]
            if verdict == "ok":
                reason = proposal.get("reason", "")
                if payload.kind == "script_file":
                    script_path = workflows_dir(workspace) / "scripts" / payload.script
                    try:
                        current = script_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        logger.warning("cannot read script {} for {}: {}", payload.script, name, exc)
                        run_log.advance_cursor(workspace, name, newest_ts)
                        continue
                    ok, detail = precheck_script_edit("script_file", payload.proposed, filename=payload.script)
                    if not ok:
                        rid = wr.log_structural_suggestion(
                            workspace, name, proposal=proposal, why_rejected=detail,
                            diagnostic=_diag_text(diag), run_ids=run_ids)
                        structural += 1
                        _emit("workflow.improve.structural", workflow=name, rec_id=rid,
                              why_rejected=detail, runs=len(records), kind="script_file")
                        run_log.advance_cursor(workspace, name, newest_ts)
                        continue
                    rid = wr.log_script_file_recommendation(
                        workspace, name, script=payload.script, current=current,
                        proposed=payload.proposed, reason=reason, run_ids=run_ids,
                        manual_only=payload.manual_only)
                    proposals += 1
                    if wf.improvement_mode == "auto" and not payload.manual_only:
                        r = wr.apply_recommendation(workspace, name, rid, actor="dream")
                        if r.get("ok"):
                            applied += 1
                            # Use apply_recommendation's own pre-write read, not this
                            # function's earlier 'current' — a concurrent editor save
                            # during the multi-second precheck above would otherwise
                            # leave the revert baseline stale and misapply on revert.
                            _write_pending(workspace, name, rec_id=rid, kind="script_file",
                                           script=payload.script,
                                           previous_content=r.get("previous_content", current),
                                           baseline_rate=_script_file_trouble_rate(wf, diag, payload.script))
                            _emit("workflow.improve.applied", workflow=name, script=payload.script,
                                  rec_id=rid, reason=reason, runs=len(records), kind="script_file")
                        else:
                            logger.warning("auto-apply failed for {} ({}): {}", name, rid, r.get("error"))
                    else:
                        _emit("workflow.improve.recommended", workflow=name, script=payload.script,
                              rec_id=rid, reason=reason, runs=len(records), kind="script_file",
                              manual_only=payload.manual_only)
                else:
                    target, field = payload.target_id, payload.kind
                    current = getattr(wf.nodes[target], field, "")
                    if field == "command":
                        ok, detail = precheck_script_edit("command", payload.proposed)
                        if not ok:
                            rid = wr.log_structural_suggestion(
                                workspace, name, proposal=proposal, why_rejected=detail,
                                diagnostic=_diag_text(diag), run_ids=run_ids)
                            structural += 1
                            _emit("workflow.improve.structural", workflow=name, rec_id=rid,
                                  why_rejected=detail, runs=len(records), kind="command")
                            run_log.advance_cursor(workspace, name, newest_ts)
                            continue
                    rid = wr.log_recommendation(
                        workspace, name, target_id=target, field=field,
                        current=current, proposed=payload.proposed, reason=reason,
                        run_ids=run_ids, manual_only=payload.manual_only,
                    )
                    proposals += 1
                    if wf.improvement_mode == "auto" and not payload.manual_only:
                        r = wr.apply_recommendation(workspace, name, rid, actor="dream")
                        if r.get("ok"):
                            applied += 1
                            # Same reasoning as the script_file branch above: prefer
                            # apply_recommendation's own pre-write read over this
                            # function's earlier 'current' as the revert baseline.
                            _write_pending(workspace, name, rec_id=rid, kind=field, target_id=target,
                                           previous=r.get("previous", current),
                                           baseline_rate=_node_trouble_rate(diag, target))
                            _emit("workflow.improve.applied", workflow=name,
                                  target_id=target, rec_id=rid, reason=reason,
                                  runs=len(records), kind=field)
                        else:
                            logger.warning("auto-apply failed for {} ({}): {}", name, rid, r.get("error"))
                    else:
                        _emit("workflow.improve.recommended", workflow=name,
                              target_id=target, rec_id=rid, reason=reason,
                              runs=len(records), kind=field, manual_only=payload.manual_only)
            elif verdict == "structural":
                rid = wr.log_structural_suggestion(
                    workspace, name, proposal=proposal, why_rejected=payload,
                    diagnostic=_diag_text(diag), run_ids=run_ids)
                structural += 1
                field = proposal.get("field")
                kind = field if field in ("prompt", "command", "script_file") else None
                _emit("workflow.improve.structural", workflow=name, rec_id=rid,
                      why_rejected=payload, runs=len(records), kind=kind)
            run_log.advance_cursor(workspace, name, newest_ts)
        except Exception:  # noqa: BLE001 - one bad workflow must not abort the pass
            logger.exception("workflow improve pass failed for {}", name)
    return {"workflows": processed, "proposals": proposals, "applied": applied,
            "structural": structural, "reverted": reverted}
