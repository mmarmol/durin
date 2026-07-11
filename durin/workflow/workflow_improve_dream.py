"""The dream pass that improves workflows.

For each workflow with new terminal runs since the last pass, it computes the
diagnostic signal (which nodes loop, which gates fail, recurring across runs),
shows a model the workflow definition + that diagnostic + the recent change
history (so it does not re-propose reverted edits), and produces ONE proposed
edit to a node's prompt. ``improvement_mode`` decides the disposition:

- ``manual`` — record a recommendation for the user to review (never applied).
- ``auto`` — apply it through the shared editing engine (graph re-validated,
  version commit with actor="dream"), then hold it *pending validation*: if the
  edited node's trouble rate worsened over the workflow's NEXT runs, the edit is
  auto-reverted and marked so it is never re-proposed.

The edit scope is enforced in code (node prompts only). A proposal OUTSIDE that
scope — restructure, rewire, other fields — is never silently dropped: it lands
in the recommendations queue as an annotated ``structural`` suggestion (proposal
+ why the scope refused it + the diagnostic evidence) for the user to treat in a
session; it has no auto-apply in any mode. The model is injectable so the pass
is fully testable without a live LLM.
"""

from __future__ import annotations

import json

from loguru import logger

from durin.workflow import run_log
from durin.workflow.diagnostics import compute_diagnostics
from durin.workflow.loader import WorkflowError, load_workflow, workflows_dir
from durin.workflow.spec import WorkNode
from durin.workflow.version_store import history_for_dream

_SYSTEM = (
    "You improve a user's agent workflow. You are shown the workflow definition (JSON), "
    "a diagnostic of where it recurringly struggles across recent runs, and the recent "
    "change history with the reasons prior edits were made. Propose exactly ONE small "
    "edit that would reduce the recurring trouble: rewrite any node's 'prompt' (both "
    "work nodes and routing nodes use 'prompt' as their editable field). Do NOT add or "
    "remove nodes or edges. Do NOT re-propose an edit the history shows was already tried. "
    "Reply with ONLY a JSON object: {\"target_id\": \"<node id>\", \"field\": \"prompt\", "
    "\"current\": \"<current text>\", \"proposed\": \"<new text>\", \"reason\": \"<why>\"}."
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
    """The edited node's trouble per run: loop-backs + gate fails, normalized."""
    total = max(diag.total_runs, 1)
    return (diag.loop_backs.get(node_id, 0) + diag.gate_fails.get(node_id, 0)) / total


def _write_pending(workspace, name: str, *, rec_id: str, target_id: str,
                   previous: str, baseline_rate: float) -> None:
    p = _pending_path(workspace, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "rec_id": rec_id, "target_id": target_id, "previous": previous,
        "baseline_rate": baseline_rate,
    }), encoding="utf-8")


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


def _maybe_auto_revert(workspace, name: str, records: list[dict]) -> bool:
    """The auto-apply safety net: an applied edit stays *pending validation*
    until the workflow's NEXT terminal runs. If the edited node's trouble rate
    worsened vs the pre-edit baseline, restore the previous prompt (a forward
    revert commit — git history stays the audit trail), mark the recommendation
    reverted (its dedup id pins any repeat proposal), and report. Returns True
    when a revert happened (the pass then skips proposing for this workflow
    this round)."""
    from durin.workflow import workflow_recommendations as wr
    from durin.workflow.editing import save_workflow_definition

    pending = _read_pending(workspace, name)
    if pending is None or not records:
        return False
    diag = compute_diagnostics(records)
    new_rate = _node_trouble_rate(diag, pending["target_id"])
    if new_rate <= pending.get("baseline_rate", 0.0):
        _clear_pending(workspace, name)   # validated: no worse than before
        return False
    try:
        wf_path = workflows_dir(workspace) / f"{name}.json"
        data = json.loads(wf_path.read_text(encoding="utf-8"))
        node = next((n for n in data.get("nodes", []) if n.get("id") == pending["target_id"]), None)
        if node is not None:
            node["prompt"] = pending["previous"]
            save_workflow_definition(
                workspace, name, data,
                reason=(f"auto-revert {pending['rec_id']}: edited node "
                        f"{pending['target_id']!r} worsened "
                        f"({pending.get('baseline_rate', 0.0):.2f} -> {new_rate:.2f} trouble/run)"),
                actor="dream", must_exist=True)
        wr.mark_reverted(workspace, name, pending["rec_id"],
                         note=f"diagnostic worsened: {pending.get('baseline_rate', 0.0):.2f} -> {new_rate:.2f}")
        _emit("workflow.improve.reverted", workflow=name,
              target_id=pending["target_id"], rec_id=pending["rec_id"],
              baseline_rate=pending.get("baseline_rate", 0.0), new_rate=new_rate)
    finally:
        _clear_pending(workspace, name)
    return True


def _classify_proposal(proposal: dict | None, wf):
    """('ok', (target, field, proposed)) — in scope and actionable;
    ('structural', why) — outside the prompt-only scope, worth the user's eyes;
    ('skip', None) — unparseable / empty / in-scope no-op."""
    if not isinstance(proposal, dict) or not proposal:
        return "skip", None
    valid = _valid_proposal(proposal, wf)
    if valid:
        return "ok", valid
    target = proposal.get("target_id") or proposal.get("node_id") or proposal.get("gate_id")
    field = proposal.get("field")
    proposed = proposal.get("proposed")
    if not proposed or not str(proposed).strip():
        return "skip", None
    if target and target in wf.nodes and field == "prompt" and isinstance(wf.nodes[target], WorkNode):
        return "skip", None            # in-scope no-op (same text) — nothing to escalate
    why = (f"field {field!r} is outside the prompt-only scope" if target and target in wf.nodes
           else f"target {target!r} is not an editable node of this workflow")
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


def _valid_proposal(proposal: dict, wf) -> tuple[str, str, str] | None:
    """Enforce the S1 edit scope. Returns (target_id, field, proposed) or None."""
    if not isinstance(proposal, dict):
        return None
    target = proposal.get("target_id") or proposal.get("node_id") or proposal.get("gate_id")
    field = proposal.get("field")
    proposed = proposal.get("proposed")
    if not target or target not in wf.nodes or not proposed:
        return None
    node = wf.nodes[target]
    ok = field == "prompt" and isinstance(node, WorkNode)
    if not ok:
        return None   # structural / mismatched field / unknown target → rejected
    if proposed.strip() == (getattr(node, field, "") or "").strip():
        return None   # no-op: the proposed text equals the current text → don't queue it
    return target, field, proposed


def _build_prompt(name, wf_json: str, diag, history) -> str:
    lines = [
        f"Workflow '{name}' definition:",
        wf_json,
        "",
        "Diagnostic (recurring across runs):",
        f"  nodes that loop back: {diag.loop_backs}",
        f"  gates that fail: {diag.gate_fails}",
        f"  runs that gave up (exhausted): {diag.max_visits_aborts} / {diag.total_runs}",
        f"  candidates to improve: {sorted(diag.candidates())}",
        "",
        "Recent change history (newest first) — do not re-propose what was tried:",
    ]
    for h in history:
        lines.append(f"  [{h['sha']}] {h['reason']}")
    return "\n".join(lines)


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
            prompt = f"{_SYSTEM}\n\n{_build_prompt(name, wf_json, diag, history)}"
            resp = llm_invoke(prompt, model=model)
            content = getattr(resp, "content", resp if isinstance(resp, str) else "")
            proposal = _parse_proposal(content) or {}
            verdict, payload = _classify_proposal(proposal, wf)
            run_ids = [r.get("run_id") for r in records]
            if verdict == "ok":
                target, field, proposed = payload
                current = getattr(wf.nodes[target], field, "")
                reason = proposal.get("reason", "")
                rid = wr.log_recommendation(
                    workspace, name, target_id=target, field=field,
                    current=current, proposed=proposed, reason=reason,
                    run_ids=run_ids,
                )
                proposals += 1
                if wf.improvement_mode == "auto":
                    r = wr.apply_recommendation(workspace, name, rid, actor="dream")
                    if r.get("ok"):
                        applied += 1
                        _write_pending(workspace, name, rec_id=rid, target_id=target,
                                       previous=current,
                                       baseline_rate=_node_trouble_rate(diag, target))
                        _emit("workflow.improve.applied", workflow=name,
                              target_id=target, rec_id=rid, reason=reason,
                              runs=len(records))
                    else:
                        logger.warning("auto-apply failed for {} ({}): {}",
                                       name, rid, r.get("error"))
                else:
                    _emit("workflow.improve.recommended", workflow=name,
                          target_id=target, rec_id=rid, reason=reason,
                          runs=len(records))
            elif verdict == "structural":
                diag_text = (f"loop-backs {diag.loop_backs}, gate fails {diag.gate_fails}, "
                             f"exhausted {diag.max_visits_aborts}/{diag.total_runs}")
                rid = wr.log_structural_suggestion(
                    workspace, name, proposal=proposal, why_rejected=payload,
                    diagnostic=diag_text, run_ids=run_ids)
                structural += 1
                _emit("workflow.improve.structural", workflow=name, rec_id=rid,
                      why_rejected=payload, runs=len(records))
            run_log.advance_cursor(workspace, name, newest_ts)
        except Exception:  # noqa: BLE001 - one bad workflow must not abort the pass
            logger.exception("workflow improve pass failed for {}", name)
    return {"workflows": processed, "proposals": proposals, "applied": applied,
            "structural": structural, "reverted": reverted}
