"""Live skill-observation queue (task-observer pattern).

In-session feedback about skills — user corrections, coverage gaps, candidate
improvements, pruning signals — is logged here at the moment it occurs and
consumed later by the daily curation pass. Log, don't act: nothing in this
module mutates a skill.

Store: ``<workspace>/skills/.observations.jsonl`` inside the skills GitStore
subtree, so every append/status change is committed with the same provenance
machinery as the skills themselves. One JSON record per line. Pure functions
over a workspace Path — unit-testable with tmp_path.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path

from durin.agent.skills_store import _safe_name, _skills_dir, _store_init

logger = logging.getLogger(__name__)


def _emit(event: str, **data) -> None:
    """Best-effort skill-loop telemetry."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # noqa: BLE001 — telemetry must never break observation logging
        pass

KINDS = ("correction", "gap", "improvement", "simplify")
PRINCIPLES_CAP = 12

_ACTIVE = ".observations.jsonl"
_ARCHIVE = ".observations.archive.jsonl"
_PRINCIPLES = ".principles.jsonl"


def _active_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / _ACTIVE


def _archive_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / _ARCHIVE


def _principles_path(workspace: Path) -> Path:
    return _skills_dir(workspace) / _PRINCIPLES


def _today() -> str:
    return _dt.date.today().isoformat()


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            logger.warning("skipping corrupt observation line in %s", path)
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_text(text, encoding="utf-8")


def _valid_skill_ref(skill: str) -> bool:
    """A skill ref is an existing/plain skill name, "all", or "new:<name>"."""
    if skill == "all":
        return True
    if skill.startswith("new:"):
        return _safe_name(skill[4:])
    return _safe_name(skill)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


_SIMILARITY_THRESHOLD = 0.5


def _same_issue(a: str, b: str) -> bool:
    """Whether two issue texts describe the same problem.

    LLMs rephrase the same issue on every recurrence ("Step 2 says X" vs
    "Step 2 STILL says X — second time"), so exact/containment matching alone
    under-counts recurrence. Containment catches short rewordings; Jaccard
    word overlap catches paraphrases.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    wa = set(re.findall(r"[\w/-]+", na))
    wb = set(re.findall(r"[\w/-]+", nb))
    if not wa or not wb:
        return False
    overlap = len(wa & wb) / len(wa | wb)
    return overlap >= _SIMILARITY_THRESHOLD


def _next_id(workspace: Path) -> int:
    ids = [int(r.get("id", 0)) for r in
           _read_records(_active_path(workspace)) + _read_records(_archive_path(workspace))]
    return max(ids, default=0) + 1


def log_observation(workspace: Path, *, skill: str, kind: str, issue: str,
                    improvement: str, principle: str | None = None,
                    session: str | None = None) -> dict:
    """Append one observation, or bump the matching OPEN one (dedup).

    Dedup: same skill + near-same issue (normalized containment) → bump
    ``count``/``last_seen`` instead of a new record. ``count >= 2`` is the
    recurrence signal curation looks for.
    """
    workspace = Path(workspace)
    if kind not in KINDS:
        return {"error": f"kind must be one of {', '.join(KINDS)}"}
    if not _valid_skill_ref(str(skill or "")):
        return {"error": "invalid skill name"}
    if not issue or not issue.strip():
        return {"error": "issue is required"}
    if not improvement or not improvement.strip():
        return {"error": "improvement is required"}

    _skills_dir(workspace).mkdir(parents=True, exist_ok=True)
    store = _store_init(workspace)
    records = _read_records(_active_path(workspace))

    for rec in records:
        if rec.get("skill") != skill or rec.get("status") != "OPEN":
            continue
        if _same_issue(str(rec.get("issue", "")), issue):
            rec["count"] = int(rec.get("count", 1)) + 1
            rec["last_seen"] = _today()
            if session and session not in rec.get("sessions", []):
                rec.setdefault("sessions", []).append(session)
            _write_records(_active_path(workspace), records)
            sha = store.auto_commit(
                f"observation(#{rec['id']} {skill}): recurred x{rec['count']}")
            _emit("skill.observation_logged", skill=skill, kind=kind,
                 dedup_bumped=True, count=rec["count"])
            return {"ok": True, "id": rec["id"], "count": rec["count"], "commit": sha}

    rec = {
        "id": _next_id(workspace),
        "skill": skill,
        "kind": kind,
        "issue": issue.strip(),
        "improvement": improvement.strip(),
        "principle": (principle.strip() if principle and principle.strip() else None),
        "status": "OPEN",
        "count": 1,
        "first_seen": _today(),
        "last_seen": _today(),
        "sessions": [session] if session else [],
    }
    records.append(rec)
    _write_records(_active_path(workspace), records)
    sha = store.auto_commit(f"observation(#{rec['id']} {skill}): {kind} logged")
    _emit("skill.observation_logged", skill=skill, kind=kind,
         dedup_bumped=False, count=1)
    return {"ok": True, "id": rec["id"], "count": 1, "commit": sha}


def open_observations(workspace: Path, skill: str | None = None) -> list[dict]:
    """All OPEN observations, optionally filtered to one skill ref."""
    recs = [r for r in _read_records(_active_path(Path(workspace)))
            if r.get("status") == "OPEN"]
    if skill is not None:
        recs = [r for r in recs if r.get("skill") == skill]
    return recs


def resolve_observation(workspace: Path, oid: int, disposition: str) -> dict:
    """Resolve one OPEN observation by hand (webui/API path).

    ``applied`` marks the underlying issue as handled (the record is archived
    at the start of the next curation pass); ``declined`` keeps the record in
    the active file as memory against the judge re-proposing the same change.
    """
    workspace = Path(workspace)
    if disposition not in ("applied", "declined"):
        return {"error": "disposition must be 'applied' or 'declined'"}
    records = _read_records(_active_path(workspace))
    for rec in records:
        if int(rec.get("id", 0)) == int(oid) and rec.get("status") == "OPEN":
            rec["status"] = "APPLIED" if disposition == "applied" else "DECLINED"
            rec["resolved_at"] = _today()
            _write_records(_active_path(workspace), records)
            store = _store_init(workspace)
            sha = store.auto_commit(
                f"observation(#{rec['id']} {rec.get('skill', '')}): "
                f"{disposition} by user")
            _emit("skill.observation_resolved", skill=rec.get("skill", ""),
                 kind=rec.get("kind", ""), disposition=disposition)
            return {"ok": True, "id": int(oid), "disposition": disposition,
                    "commit": sha}
    return {"error": f"no open observation with id {oid}"}


def declined_observations(workspace: Path) -> list[dict]:
    """DECLINED observations — kept active as memory against re-proposing."""
    return [r for r in _read_records(_active_path(Path(workspace)))
            if r.get("status") == "DECLINED"]


def apply_dispositions(workspace: Path, dispositions: list[dict]) -> dict:
    """Bulk status update from the curation judge's per-observation verdicts.

    Each entry is ``{"id": N, "disposition": "applied"|"declined"|"keep"}``.
    ``keep`` leaves the record OPEN; unknown ids are ignored (logged). One
    commit for the whole batch.
    """
    workspace = Path(workspace)
    records = _read_records(_active_path(workspace))
    by_id = {int(r.get("id", 0)): r for r in records}
    counts = {"applied": 0, "declined": 0, "kept": 0}
    for d in dispositions:
        rec = by_id.get(int(d.get("id", 0)))
        disp = d.get("disposition")
        if rec is None:
            logger.warning("disposition for unknown observation id %s ignored", d.get("id"))
            continue
        if disp == "applied":
            rec["status"] = "APPLIED"
            rec["resolved_at"] = _today()
            counts["applied"] += 1
        elif disp == "declined":
            rec["status"] = "DECLINED"
            rec["resolved_at"] = _today()
            counts["declined"] += 1
        elif disp == "keep":
            counts["kept"] += 1
        else:
            logger.warning("unknown disposition %r for observation %s", disp, d.get("id"))
    sha = None
    if counts["applied"] or counts["declined"]:
        _write_records(_active_path(workspace), records)
        store = _store_init(workspace)
        sha = store.auto_commit(
            f"observations: {counts['applied']} applied, {counts['declined']} declined")
    return {**counts, "commit": sha}


def active_principles(workspace: Path) -> list[dict]:
    """Cross-cutting principles currently in force (checklist for prompts)."""
    return [p for p in _read_records(_principles_path(Path(workspace)))
            if p.get("status") == "active"]


def add_principle(workspace: Path, text: str, rationale: str = "") -> dict:
    """Promote a generalizable lesson to a cross-cutting principle.

    Capped at ``PRINCIPLES_CAP`` active principles to bound the per-prompt
    cost of the compliance checklist — retire one to make room.
    """
    workspace = Path(workspace)
    if not text or not text.strip():
        return {"error": "text is required"}
    records = _read_records(_principles_path(workspace))
    active = [p for p in records if p.get("status") == "active"]
    norm = _norm(text)
    if any(_norm(str(p.get("text", ""))) == norm for p in active):
        return {"error": "principle already exists"}
    if len(active) >= PRINCIPLES_CAP:
        return {"error": f"principle cap reached ({PRINCIPLES_CAP}); retire one first"}
    pid = max((int(p.get("id", 0)) for p in records), default=0) + 1
    records.append({"id": pid, "text": text.strip(), "status": "active",
                    "added": _today(),
                    "rationale": rationale.strip() if rationale else ""})
    _skills_dir(workspace).mkdir(parents=True, exist_ok=True)
    store = _store_init(workspace)
    _write_records(_principles_path(workspace), records)
    sha = store.auto_commit(f"principle(#{pid}): added")
    return {"ok": True, "id": pid, "commit": sha}


def retire_principle(workspace: Path, pid: int) -> dict:
    """Retire an active principle (kept in the file for history)."""
    workspace = Path(workspace)
    records = _read_records(_principles_path(workspace))
    for p in records:
        if int(p.get("id", 0)) == int(pid) and p.get("status") == "active":
            p["status"] = "retired"
            p["retired"] = _today()
            _write_records(_principles_path(workspace), records)
            store = _store_init(workspace)
            sha = store.auto_commit(f"principle(#{pid}): retired")
            return {"ok": True, "id": int(pid), "commit": sha}
    return {"error": f"no active principle with id {pid}"}


def archive_resolved(workspace: Path) -> int:
    """Move APPLIED records to the archive file; returns how many moved.

    Called at the START of a curation pass, so records applied during the
    previous pass get one full cycle of visibility before leaving the active
    file. DECLINED records never move — they are the judge's memory against
    re-proposing rejected changes.
    """
    workspace = Path(workspace)
    records = _read_records(_active_path(workspace))
    moved = [r for r in records if r.get("status") == "APPLIED"]
    if not moved:
        return 0
    kept = [r for r in records if r.get("status") != "APPLIED"]
    archive = _read_records(_archive_path(workspace)) + moved
    _write_records(_archive_path(workspace), archive)
    _write_records(_active_path(workspace), kept)
    store = _store_init(workspace)
    store.auto_commit(f"observations: archived {len(moved)} applied")
    return len(moved)
