"""Automation run manifests: <workspace>/automations-runs/<automation>/<run_id>.json.

Each run file has a single owning writer (the runtime that fired it), so a
full-file atomic rewrite needs no lock — same model as loops' and workflows'
run logs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from durin.utils.atomic_write import atomic_write_text

SCHEMA = 1

ACTIVE_STATUSES = ("running", "paused")
TERMINAL_STATUSES = ("achieved", "completed", "failed", "rejected", "interrupted")

# A run that never produced a result (interrupted), a message that reached a
# human instead of running (rejected), or a run currently held for operator
# attention (paused) is not evidence about whether the automation itself is
# working. The streak counter means "this automation keeps missing its
# goal", so these must neither break a genuine streak nor extend it.
STREAK_TRANSPARENT = ("interrupted", "rejected", "paused")


def runs_root(workspace: str | Path) -> Path:
    return Path(workspace) / "automations-runs"


def _dir(ws, automation: str) -> Path:
    return runs_root(ws) / automation


def _path(ws, automation: str, run_id: str) -> Path:
    return _dir(ws, automation) / f"{run_id}.json"


def _write(ws, automation: str, run_id: str, record: dict) -> dict:
    d = _dir(ws, automation)
    d.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.time()
    atomic_write_text(_path(ws, automation, run_id), json.dumps(record, indent=2))
    return record


def start_run(ws, automation: str, run_id: str, *, cause: dict, origin: dict | None = None) -> dict:
    from durin.utils.process_tree import process_identity

    return _write(ws, automation, run_id, {
        "schema": SCHEMA, "run_id": run_id, "automation": automation, "status": "running",
        "cause": {
            "kind": cause.get("kind"),
            "excerpt": (cause.get("excerpt") or "")[:500],
            "trigger_index": cause.get("trigger_index"),
        },
        "origin": origin, "workflow_run_id": None,
        "detail": None, "final_route_label": None,
        "started_at": time.time(), "finished_at": None,
        "delivery": None, "approval": None,
        # Which process is executing this run — the crash sweep flips any
        # "running" manifest whose owner is no longer alive.
        "owner": process_identity(),
    })


def update_run(ws, automation: str, run_id: str, **fields) -> dict:
    record = read_run(ws, automation, run_id) or {"schema": SCHEMA, "run_id": run_id, "automation": automation}
    record.update(fields)
    return _write(ws, automation, run_id, record)


def finalize_run(ws, automation: str, run_id: str, *, status: str,
                 detail: str | None = None, workflow_run_id: str | None = None,
                 final_route_label: str | None = None) -> dict:
    record = read_run(ws, automation, run_id) or {"schema": SCHEMA, "run_id": run_id, "automation": automation}
    record.update({
        "status": status, "finished_at": time.time(),
        "workflow_run_id": workflow_run_id or record.get("workflow_run_id"),
        # None keeps the prior value; "" explicitly clears it — distinct
        # from "not provided" so a caller can clear a stale detail on purpose.
        "detail": detail[:2000] if detail is not None else record.get("detail"),
        "final_route_label": final_route_label if final_route_label is not None else record.get("final_route_label"),
    })
    return _write(ws, automation, run_id, record)


def record_delivery(ws, automation: str, run_id: str, *, channel: str, to: str, result: str, at_ms: int) -> dict:
    record = read_run(ws, automation, run_id) or {"schema": SCHEMA, "run_id": run_id, "automation": automation}
    record["delivery"] = {"channel": channel, "to": to, "result": result, "at_ms": at_ms}
    return _write(ws, automation, run_id, record)


def record_approval(ws, automation: str, run_id: str, *, action: str, by: str, at_ms: int) -> dict:
    record = read_run(ws, automation, run_id) or {"schema": SCHEMA, "run_id": run_id, "automation": automation}
    record["approval"] = {"action": action, "by": by, "at_ms": at_ms}
    return _write(ws, automation, run_id, record)


# The boot migration renames the loops-era runs directory into place without
# converting the records inside it, so a workspace routinely holds run files
# in the OLD loops schema: `loop` instead of `automation`, a bare
# `source`/`task` pair instead of a `cause`, the old status vocabulary, and
# none of the newer fields. Normalizing at READ time — never rewriting disk —
# keeps every consumer (the API, the webui, the orphan sweep) on one schema,
# and also covers records restored from old backups.
_LEGACY_STATUS = {
    # done/no_goal both ENDED on their own terms (no_goal = ran, judged
    # nothing left to pursue) — completed. error was an honest failure.
    # needs_operator/waiting_info/escalated were parked on a system that no
    # longer exists to resume them; calling them "paused" would surface dead
    # runs as answerable in the inbox, so they finalize as interrupted.
    "done": "completed",
    "no_goal": "completed",
    "error": "failed",
    "needs_operator": "interrupted",
    "waiting_info": "interrupted",
    "escalated": "interrupted",
}


def _normalize_legacy(record: dict) -> dict:
    # `loop` is the one unambiguous marker of the old schema — the loops-era
    # writer always stamped it, and no modern writer ever does. Records
    # without it (including degenerate modern stubs) pass through untouched.
    if "loop" not in record:
        return record
    if "automation" not in record:
        record["automation"] = record["loop"]
    if "cause" not in record:
        record["cause"] = {
            "kind": record.get("source") or "channel",
            "excerpt": (record.get("task") or "")[:300],
            "trigger_index": None,
        }
    record["status"] = _LEGACY_STATUS.get(record.get("status"), record.get("status"))
    for field in ("delivery", "approval", "final_route_label"):
        record.setdefault(field, None)
    return record


def read_run(ws, automation: str, run_id: str) -> dict | None:
    p = _path(ws, automation, run_id)
    if not p.exists():
        return None
    try:
        return _normalize_legacy(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return None


def _load_dir(d: Path) -> list[dict]:
    out = []
    for p in d.glob("*.json"):
        try:
            out.append(_normalize_legacy(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    out.sort(key=lambda m: (m.get("started_at") or 0, m.get("run_id") or ""), reverse=True)
    return out


def _cap_exempting_paused(runs: list[dict], limit: int) -> list[dict]:
    """Cap a newest-first run list at ``limit``, except a ``paused`` run is
    never dropped for it. A paused run is parked on an operator/counterpart
    answer (the only status AutomationsRuntime._park ever sets) — it is an
    actionable resume point, not history, and the "Needs you" tray must never
    lose one to a run-count cap. Mirrors durin.workflow.run_log.list_all_runs's
    identical ``needs_input`` exemption: paused entries are set aside first,
    the cap applies to everything else, then the two groups are merged back
    into one newest-first list."""
    paused = [m for m in runs if m.get("status") == "paused"]
    rest = [m for m in runs if m.get("status") != "paused"][:limit]
    out = paused + rest
    out.sort(key=lambda m: (m.get("started_at") or 0, m.get("run_id") or ""), reverse=True)
    return out


def list_runs(ws, automation: str, limit: int | None = 50) -> list[dict]:
    d = _dir(ws, automation)
    if not d.is_dir():
        return []
    runs = _load_dir(d)
    return runs if limit is None else _cap_exempting_paused(runs, limit)


def list_all_runs(ws, limit: int = 100) -> list[dict]:
    root = runs_root(ws)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in root.iterdir():
        if d.is_dir():
            out.extend(_load_dir(d))
    return _cap_exempting_paused(out, limit)


def active_runs(ws, automation: str) -> list[dict]:
    return [m for m in list_runs(ws, automation, limit=None) if m.get("status") in ACTIVE_STATUSES]


def consecutive_unachieved(ws, automation: str) -> int:
    n = 0
    for m in list_runs(ws, automation, limit=None):
        status = m.get("status")
        if status in ACTIVE_STATUSES or status in STREAK_TRANSPARENT:
            continue
        if status == "achieved":
            break
        if status in TERMINAL_STATUSES:
            n += 1
            continue
        break
    return n


def find_orphans(ws, now: float | None = None, max_age_s: float = 6 * 3600) -> list[dict]:
    """Report ``running`` manifests whose owner is gone, without writing.

    An owned manifest is orphaned as soon as its owner process is dead; a run
    owned by another LIVE process is never reported. Ownerless legacy
    manifests fall back to the ``started_at`` age cutoff. A malformed
    manifest is skipped, never fatal.

    Detection is deliberately separate from the write: deciding what an
    orphan means — relaunch it, or finalize it and tell somebody — needs the
    automation's definition and a delivery path, neither of which a
    file-level sweep has.
    """
    from durin.utils.process_tree import process_alive

    root = runs_root(ws)
    if not root.is_dir():
        return []
    if now is None:
        now = time.time()
    cutoff = now - max_age_s
    orphans: list[dict] = []
    for automation_dir in root.iterdir():
        if not automation_dir.is_dir():
            continue
        for p in automation_dir.glob("*.json"):
            try:
                rec = _normalize_legacy(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
            if rec.get("status") != "running":
                continue
            owner = rec.get("owner")
            if owner is not None:
                orphaned = not process_alive(owner)
            else:
                orphaned = (rec.get("started_at") or 0.0) < cutoff
            if orphaned:
                rec.setdefault("automation", automation_dir.name)
                orphans.append(rec)
    return orphans


def prune_runs(ws, automation: str, keep: int) -> None:
    runs = list_runs(ws, automation, limit=None)
    keepers = set()
    kept = 0
    for m in runs:
        status = m.get("status")
        if status == "paused" or kept < keep:
            keepers.add(m["run_id"])
            if status != "paused":
                kept += 1
    for m in runs:
        if m["run_id"] not in keepers:
            _path(ws, automation, m["run_id"]).unlink(missing_ok=True)
