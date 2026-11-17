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
import functools
import json
import logging
import re
from pathlib import Path

from durin.agent.skills_store import _safe_name, _skills_dir, _store, _store_init

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

# Dispositions a human may set on an OPEN record, and the status each writes.
_MANUAL_DISPOSITIONS = {
    "applied": "APPLIED",
    "declined": "DECLINED",
    "upstream": "UPSTREAM",
}

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


def _under_store_lock(fn):
    """Run ``fn`` holding the skills store's write lock. The gateway (the
    skill_observe tool, the webui) and the dream worker (signal pass,
    curation) both read-modify-write these files; unlocked, one process's
    update overwrites the other's. Reentrant, and the same lock the store's
    commits take."""
    @functools.wraps(fn)
    def wrapper(workspace, *args, **kwargs):
        with _store(Path(workspace)).write_lock():
            return fn(workspace, *args, **kwargs)
    return wrapper


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
# Attempt notes kept per record, and their length: enough to show the judge
# what already failed without the record growing every night.
_MAX_ATTEMPTS = 5
_NOTE_CHARS = 300
# Attempts that land nothing before a record stops pulling its skill into
# every curation pass and waits for a person (or a new report of the issue).
_STALL_ATTEMPTS = 3


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


def _route_new_skill_ref(workspace: Path, skill: str, kind: str,
                         issue: str) -> tuple[str, str, str]:
    """Where a ``new:<name>`` gap belongs.

    A gap named after a skill that exists is feedback on that skill: it
    becomes an ``improvement`` on it rather than a request for a duplicate. A
    gap for a retired skill that has a replacement is a request to extend
    that replacement: it becomes an ``improvement`` on it, noting the retired
    name, instead of a gap that would ask for the retired skill again.
    """
    if not skill.startswith("new:"):
        return skill, kind, issue
    from durin.agent.skill_retirements import name_key, retirement_for

    name = skill[4:]
    skills_dir = _skills_dir(workspace)
    existing = next((p.name for p in (skills_dir.iterdir() if skills_dir.is_dir() else [])
                     if name_key(p.name) == name_key(name) and (p / "SKILL.md").is_file()),
                    None)
    if existing is not None:
        return existing, "improvement", issue.strip() + " (proposed as a new skill; it already exists)"
    retired = retirement_for(workspace, name)
    target = retired.get("replaced_by") if retired else None
    if target and (_skills_dir(workspace) / target / "SKILL.md").is_file():
        note = f" (proposed as the new skill `{name}`, which is retired"
        if retired.get("reason"):
            note += f": {retired['reason']}"
        return target, "improvement", issue.strip() + note + ")"
    return skill, kind, issue


@_under_store_lock
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

    skill, kind, issue = _route_new_skill_ref(workspace, skill, kind, issue)

    _skills_dir(workspace).mkdir(parents=True, exist_ok=True)
    store = _store_init(workspace)
    records = _read_records(_active_path(workspace))

    closed = _closed_gap(records, skill)
    if closed is not None:
        # A gap that was turned down stays turned down: a later sighting is
        # noted on its record instead of opening it again.
        closed["seen_since_closed"] = int(closed.get("seen_since_closed", 0)) + 1
        closed["last_seen"] = _today()
        _write_records(_active_path(workspace), records)
        sha = store.auto_commit(
            f"observation(#{closed['id']} {closed['skill']}): seen again, stays closed")
        _emit("skill.observation_logged", skill=str(closed["skill"]), kind=kind,
              dedup_bumped=True, count=int(closed.get("count", 1)), closed=True)
        return {"ok": True, "id": closed["id"], "closed": True, "commit": sha}

    for rec in records:
        if rec.get("skill") != skill or rec.get("status") != "OPEN":
            continue
        if _same_issue(str(rec.get("issue", "")), issue):
            rec["count"] = int(rec.get("count", 1)) + 1
            rec["last_seen"] = _today()
            # A new report is new evidence: curation tries a stalled one again.
            rec.pop("stalled_at", None)
            if session and session not in rec.get("sessions", []):
                rec.setdefault("sessions", []).append(session)
            _write_records(_active_path(workspace), records)
            sha = store.auto_commit(
                f"observation(#{rec['id']} {skill}): recurred x{rec['count']}")
            _emit("skill.observation_logged", skill=skill, kind=kind,
                 dedup_bumped=True, count=rec["count"])
            return {"ok": True, "id": rec["id"], "count": rec["count"], "commit": sha}

    reopened = _reopen_applied(workspace, records, skill, issue, improvement, session)
    if reopened is not None:
        sha = store.auto_commit(
            f"observation(#{reopened['id']} {skill}): reopened, recurred x{reopened['count']}")
        _emit("skill.observation_logged", skill=skill, kind=kind,
             dedup_bumped=True, count=reopened["count"], reopened=True)
        return {"ok": True, "id": reopened["id"], "count": reopened["count"],
                "reopened": True, "commit": sha}

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


def _closed_gap(records: list[dict], skill: str) -> dict | None:
    """The turned-down record of the gap ``skill`` names, if there is one."""
    if not skill.startswith("new:"):
        return None
    from durin.agent.skill_retirements import name_key

    key = name_key(skill[4:])
    return next((r for r in records
                 if r.get("status") == "DECLINED"
                 and str(r.get("skill", "")).startswith("new:")
                 and name_key(str(r["skill"])[4:]) == key), None)


def closed_gaps(workspace: Path) -> list[dict]:
    """The gaps that were turned down, most recently seen first."""
    gaps = [r for r in _read_records(_active_path(Path(workspace)))
            if r.get("status") == "DECLINED" and str(r.get("skill", "")).startswith("new:")]
    gaps.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return gaps


# How many times the skill-extract pass is handed a gap, and leaves it
# unauthored, before the gap is closed.
GAP_HANDOVERS = 3


@_under_store_lock
def note_gap_handovers(workspace: Path, names: list[str]) -> int:
    """Record that the extractor was handed these gaps and authored none of
    them; close the ones it has now left ``GAP_HANDOVERS`` times.

    The extractor turns a gap down when a skill or a workflow already covers
    the procedure, and it would be handed the same gap again on every new
    sighting. A closed gap becomes one DECLINED record for its name — the
    newest, carrying every session — and the others are dropped, so later
    sightings have one place to land. Returns how many gaps were closed.
    """
    from durin.agent.skill_retirements import name_key

    workspace = Path(workspace)
    wanted = {name_key(n) for n in names}
    records = _read_records(_active_path(workspace))
    groups: dict[str, list[dict]] = {}
    for rec in records:
        ref = str(rec.get("skill", ""))
        if rec.get("status") == "OPEN" and ref.startswith("new:") and name_key(ref[4:]) in wanted:
            groups.setdefault(name_key(ref[4:]), []).append(rec)
    if not groups:
        return 0
    occasions = {name_key(str(g["skill"])[4:]): g["occasions"] for g in open_gaps(workspace)}
    dropped: set[int] = set()
    counted = closed = 0
    for key, group in groups.items():
        # The newest record is the one kept: ids stay the highest ever given.
        keep = group[-1]
        handed = max(int(r.get("handed", 0)) for r in group)
        seen = occasions.get(key, 1)
        # A rerun caused by something else — another gap, a new skill, a
        # changed prompt — is the same verdict again, not a new one: a
        # handover counts once per time the gap was seen again.
        if handed and seen <= max(int(r.get("handed_occasions", 0)) for r in group):
            continue
        counted += 1
        for rec in group:
            rec["handed"] = handed + 1
            rec["handed_occasions"] = seen
        if handed + 1 < GAP_HANDOVERS:
            continue
        sessions: list[str] = []
        for rec in group:
            sessions += [s for s in rec.get("sessions") or [] if s not in sessions]
        keep.update({
            "sessions": sessions, "count": sum(int(r.get("count", 1)) for r in group),
            "first_seen": min(str(r.get("first_seen") or "") for r in group),
            "status": "DECLINED", "resolved_at": _today(),
            "closed_reason": f"handed to skill-extract {handed + 1} times, never authored",
        })
        dropped.update(id(r) for r in group[:-1])
        closed += 1
        _emit("skill.observation_resolved", skill=str(keep.get("skill", "")),
              kind="gap", disposition="declined")
    if not counted:
        return 0
    _write_records(_active_path(workspace), [r for r in records if id(r) not in dropped])
    _store_init(workspace).auto_commit(
        f"observations: {counted} gap(s) left unauthored by skill-extract, {closed} closed")
    return closed


def _reopen_applied(workspace: Path, records: list[dict], skill: str, issue: str,
                    improvement: str, session: str | None) -> dict | None:
    """Reopen the APPLIED record this issue matches, if any; written, not committed.

    An issue marked fixed that comes back is a regression. Logged as a new
    record it would start again at count 1 — a one-off to curation — while
    the fix that did not hold sits in the archive. The matching record comes
    back OPEN instead (out of the archive when it was moved there), with its
    count bumped and the new report's improvement: the old one is the fix
    that did not hold. DECLINED records are left alone: they are the judge's
    memory of what not to do.
    """
    def _matches(rec: dict) -> bool:
        return (rec.get("skill") == skill and rec.get("status") == "APPLIED"
                and _same_issue(str(rec.get("issue", "")), issue))

    target = next((r for r in records if _matches(r)), None)
    archive: list[dict] | None = None
    if target is None:
        archive = _read_records(_archive_path(workspace))
        target = next((r for r in archive if _matches(r)), None)
        if target is None:
            return None
        records.append(target)
    target["status"] = "OPEN"
    target["count"] = int(target.get("count", 1)) + 1
    target["last_seen"] = _today()
    target["reopened_at"] = _today()
    target["improvement"] = improvement.strip()
    target.pop("resolved_at", None)
    if session and session not in target.get("sessions", []):
        target.setdefault("sessions", []).append(session)
    # Active file first: a failure before the archive write leaves the record
    # in both files, never in neither.
    _write_records(_active_path(workspace), records)
    if archive is not None:
        _write_records(_archive_path(workspace), [r for r in archive if r is not target])
    return target


def _session_ref(ref: str) -> str:
    """One form for a session reference: the signal pass records the session
    file's stem (``websocket_x``), the in-session tool its key (``websocket:x``)."""
    return str(ref).replace(":", "_")


def observations_from_sessions(workspace: Path, sessions: list[str]) -> list[dict]:
    """Records about existing skills logged from any of ``sessions``, active
    or archived, whatever their status — what those sessions taught about the
    skills they used."""
    wanted = {_session_ref(s) for s in sessions if s}
    if not wanted:
        return []
    workspace = Path(workspace)
    out = []
    for rec in _read_records(_active_path(workspace)) + _read_records(_archive_path(workspace)):
        if str(rec.get("skill", "")).startswith("new:"):
            continue
        if wanted & {_session_ref(s) for s in rec.get("sessions") or []}:
            out.append(rec)
    return out


def open_observations(workspace: Path, skill: str | None = None) -> list[dict]:
    """All OPEN observations, optionally filtered to one skill ref."""
    recs = [r for r in _read_records(_active_path(Path(workspace)))
            if r.get("status") == "OPEN"]
    if skill is not None:
        recs = [r for r in recs if r.get("skill") == skill]
    return recs


def open_gaps(workspace: Path) -> list[dict]:
    """The open coverage gaps, one per working name, most recently seen first.

    A procedure seen again is logged under the same working name, but its
    issue text is written afresh each time and seldom matches the earlier
    one, so one gap is usually several records. They are read as one here:
    ``sessions`` is every session it was seen in, ``days`` every day it was
    logged on, ``issue``/``improvement`` come from the newest record, and
    ``skill`` is the oldest record's ref so the name stays the same.

    ``occasions`` is how many separate times it was seen: the larger of the
    two counts, because a chat kept open for weeks is a single session and
    two slices of one conversation read on the same night are a single day.
    """
    from durin.agent.skill_retirements import name_key

    # A name with a turned-down record is closed, whatever else is open
    # under it: a person may have declined one record of several.
    closed = {name_key(str(r["skill"])[4:]) for r in closed_gaps(workspace)}
    groups: dict[str, dict] = {}
    for rec in open_observations(workspace):
        ref = str(rec.get("skill", ""))
        if not ref.startswith("new:") or name_key(ref[4:]) in closed:
            continue
        gap = groups.setdefault(name_key(ref[4:]), {
            "skill": ref, "sessions": [], "days": set(), "last_seen": "", "_newest": rec})
        seen = {_session_ref(s) for s in gap["sessions"]}
        for session in rec.get("sessions") or []:
            if session and _session_ref(str(session)) not in seen:
                gap["sessions"].append(str(session))
                seen.add(_session_ref(str(session)))
        gap["days"].update(str(d) for d in (rec.get("first_seen"), rec.get("last_seen")) if d)
        if str(rec.get("last_seen") or "") >= gap["last_seen"]:
            gap["last_seen"] = str(rec.get("last_seen") or "")
            gap["_newest"] = rec
    gaps = []
    for gap in groups.values():
        newest = gap.pop("_newest")
        gap["days"] = sorted(gap["days"])
        gap["issue"] = str(newest.get("issue") or "")
        gap["improvement"] = str(newest.get("improvement") or "")
        gap["occasions"] = max(len(gap["sessions"]), len(gap["days"]), 1)
        gaps.append(gap)
    gaps.sort(key=lambda g: g["last_seen"], reverse=True)
    return gaps


@_under_store_lock
def resolve_observation(workspace: Path, oid: int, disposition: str) -> dict:
    """Resolve one OPEN observation by hand (webui/API path).

    ``applied`` marks the underlying issue as handled (the record is archived
    at the start of the next curation pass); ``declined`` keeps the record in
    the active file as memory against the judge re-proposing the same change.

    ``upstream`` is the exit for a record curation can never act on: an
    observation about a skill durin *ships*. Applying it locally would mean
    forking the shipped skill, which shadows the packaged copy and cuts the
    install off from that skill's future releases — so the fix belongs in
    durin itself, and the record leaves the OPEN queue without claiming a
    local change that never happened. Like ``declined`` it stays in the active
    file, so nothing re-proposes it here.
    """
    workspace = Path(workspace)
    if disposition not in _MANUAL_DISPOSITIONS:
        return {"error": "disposition must be one of "
                         f"{', '.join(repr(d) for d in _MANUAL_DISPOSITIONS)}"}
    records = _read_records(_active_path(workspace))
    for rec in records:
        if int(rec.get("id", 0)) == int(oid) and rec.get("status") == "OPEN":
            rec["status"] = _MANUAL_DISPOSITIONS[disposition]
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


def suppressed_observations(workspace: Path) -> list[dict]:
    """Records kept active as memory against re-proposing a change to a
    skill: DECLINED (rejected here) and UPSTREAM (accepted, but the fix
    belongs in durin itself). Turned-down gaps are not among them — they are
    about no existing skill, and ``closed_gaps`` lists those."""
    return [r for r in _read_records(_active_path(Path(workspace)))
            if r.get("status") in ("DECLINED", "UPSTREAM")
            and not str(r.get("skill", "")).startswith("new:")]


@_under_store_lock
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
    noted = 0
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
            # What was tried and did not land stays on the record, so the next
            # pass sees it instead of trying the same thing blind.
            if d.get("note"):
                attempts = rec.setdefault("attempts", [])
                entry = {"at": _today(), "note": str(d["note"])[:_NOTE_CHARS]}
                if d.get("approval"):
                    # Went to a person; their decision settles the record.
                    entry["approval"] = str(d["approval"])
                attempts.append(entry)
                del attempts[:-_MAX_ATTEMPTS]
                noted += 1
                failed = [a for a in attempts if not a.get("approval")]
                if len(failed) >= _STALL_ATTEMPTS and not rec.get("stalled_at"):
                    rec["stalled_at"] = _today()
                    _emit("skill.observation_stalled", skill=rec.get("skill", ""),
                         kind=rec.get("kind", ""), attempts=len(failed))
        else:
            logger.warning("unknown disposition %r for observation %s", disp, d.get("id"))
    sha = None
    if counts["applied"] or counts["declined"] or noted:
        _write_records(_active_path(workspace), records)
        store = _store_init(workspace)
        sha = store.auto_commit(
            f"observations: {counts['applied']} applied, {counts['declined']} declined, "
            f"{noted} attempts noted")
    return {**counts, "commit": sha}


def active_principles(workspace: Path) -> list[dict]:
    """Cross-cutting principles currently in force (checklist for prompts)."""
    return [p for p in _read_records(_principles_path(Path(workspace)))
            if p.get("status") == "active"]


@_under_store_lock
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


@_under_store_lock
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


@_under_store_lock
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
