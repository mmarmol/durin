"""The dream pass that improves workflows.

For each workflow in ``manual`` mode with new runs since the last pass, it computes the
diagnostic signal (which nodes loop, which gates fail, recurring across runs), shows a
model the workflow definition + that diagnostic + the recent change history (so it does
not re-propose reverted edits), and records ONE proposed edit — to a node's prompt or a
gate's criterion — as a recommendation for the user to review. Manual mode never applies
the edit; it only recommends (the human applies).

The edit scope is enforced in code (prompt / criteria only); structural proposals are
rejected. The model is injectable so the pass is fully testable without a live LLM. The
auto-mode seam (apply instead of recommend, gated by a validation signal) plugs in where
noted.
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


def _manual_workflows(workspace):
    """(name, Workflow) for each parseable manual-mode workflow definition."""
    out = []
    for f in sorted(workflows_dir(workspace).glob("*.json")):
        try:
            wf = load_workflow(workspace, f.stem)
        except (WorkflowError, OSError, ValueError):
            continue   # a malformed definition must not break the pass
        if wf.improvement_mode == "manual":
            out.append((f.stem, wf))
        elif wf.improvement_mode == "auto":
            logger.info(
                "workflow {!r} is improvement_mode=auto; auto-apply not yet implemented, skipping",
                f.stem,
            )
    return out


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
    """Observe manual-mode workflows and record improvement recommendations.

    Returns a summary dict for the cron log. Best-effort per workflow: one failing
    workflow does not abort the pass.
    """
    from durin.workflow import workflow_recommendations as wr

    if llm_invoke is None:
        from durin.memory.llm_invoke import default_llm_invoke
        llm_invoke = default_llm_invoke

    processed = 0
    proposals = 0
    for name, wf in _manual_workflows(workspace):
        try:
            cursor = run_log.read_cursor(workspace, name)
            records = run_log.read_runs_since(workspace, name, cursor)
            if not records:
                continue
            newest_ts = max(r.get("ts", 0.0) for r in records)
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
            valid = _valid_proposal(_parse_proposal(content) or {}, wf)
            if valid:
                target, field, proposed = valid
                current = getattr(wf.nodes[target], field, "")
                reason = (_parse_proposal(content) or {}).get("reason", "")
                wr.log_recommendation(
                    workspace, name, target_id=target, field=field,
                    current=current, proposed=proposed, reason=reason,
                    run_ids=[r.get("run_id") for r in records],
                )
                proposals += 1
            run_log.advance_cursor(workspace, name, newest_ts)
        except Exception:  # noqa: BLE001 - one bad workflow must not abort the pass
            logger.exception("workflow improve pass failed for {}", name)
    return {"workflows": processed, "proposals": proposals}
