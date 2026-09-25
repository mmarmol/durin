"""Durable store for approval requests.

One JSON file per request under ``<workspace>/.approvals/<id>.json``. A request
records exactly what would run (``payload``), a hash binding it to the state
that was reviewed (``change_hash``), and its lifecycle. Every status change goes
through ``transition``, a compare-and-set under a cross-process lock, so a
request runs at most once even when an in-chat click and a later approval race.

Records from the earlier per-subsystem layout (``.approvals/<subsystem>/<id>.json``)
are listed with ``legacy: True``. They carry no payload, so they can be
discarded but never approved.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

KINDS: tuple[str, ...] = (
    "skill_install", "skill_edit", "skill_deps", "mcp_change", "exec_command",
)
TERMINAL: tuple[str, ...] = ("rejected", "applied", "failed", "stale", "expired")
PENDING_TTL = timedelta(days=14)
RESOLVED_RETENTION = timedelta(days=30)
# How long a record may stay ``approved`` (decided, its run under way) before
# it is taken for interrupted. Executors take minutes at most, so a record
# still ``approved`` an hour after its decision was left by a process killed
# mid-run, and nothing else would ever move it on.
APPROVED_RUN_BOUND = timedelta(hours=1)

_DIR = ".approvals"


def _root(workspace: Path | str) -> Path:
    return Path(workspace) / _DIR


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _path(workspace: Path | str, approval_id: str) -> Path:
    return _root(workspace) / f"{approval_id}.json"


def _write(workspace: Path | str, record: dict) -> None:
    root = _root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_path(workspace, record["id"]),
                      json.dumps(record, indent=2, ensure_ascii=False))


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def create(
    workspace: Path | str, *, kind: str, summary: str, detail: dict[str, Any],
    payload: dict[str, Any], change_hash: str, session_key: str | None,
    context: str, status: str = "pending", decided_by: dict | None = None,
) -> dict:
    """Write a new record and return it. ``status`` may start at ``approved``
    when the decision was already taken (the judge cleared it)."""
    if kind not in KINDS:
        raise ValueError(f"unknown approval kind: {kind!r}")
    now = _now()
    record = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "summary": summary,
        "detail": detail,
        "payload": payload,
        "change_hash": change_hash,
        "requested_by_session": session_key,
        "context": context,
        "status": status,
        "decided_by": decided_by,
        "decided_at": now.isoformat() if decided_by else None,
        "result": None,
        "requested_at": now.isoformat(),
        "expires_at": (now + PENDING_TTL).isoformat(),
    }
    with cross_process_lock(_root(workspace)):
        _write(workspace, record)
    return record


def get(workspace: Path | str, approval_id: str) -> dict | None:
    rec = _read(_path(workspace, approval_id))
    if rec is not None:
        return rec
    for legacy in _legacy_paths(workspace):
        if legacy.stem == approval_id:
            return _legacy_view(legacy)
    return None


def _legacy_paths(workspace: Path | str) -> list[Path]:
    root = _root(workspace)
    if not root.is_dir():
        return []
    return sorted(p for sub in root.iterdir() if sub.is_dir() for p in sub.glob("*.json"))


def _legacy_view(path: Path) -> dict | None:
    raw = _read(path)
    if raw is None:
        return None
    return {
        "id": raw.get("id") or path.stem,
        "kind": f"legacy:{raw.get('subsystem') or path.parent.name}",
        "summary": raw.get("summary") or "",
        "detail": raw.get("detail") or {},
        "payload": None,
        "change_hash": None,
        "requested_by_session": raw.get("session_key"),
        "context": "autonomous",
        "status": raw.get("status") or "pending",
        "decided_by": None,
        "decided_at": None,
        "result": None,
        "requested_at": raw.get("requested_at") or "",
        "expires_at": None,
        "legacy": True,
        "_path": str(path),
    }


def list_records(workspace: Path | str, *, status: str | None = None,
                 include_legacy: bool = True) -> list[dict]:
    """All records, oldest first; filtered by ``status`` when given."""
    out: list[dict] = []
    root = _root(workspace)
    if root.is_dir():
        for path in root.glob("*.json"):
            rec = _read(path)
            if rec is not None:
                out.append(rec)
    if include_legacy:
        out.extend(r for r in (_legacy_view(p) for p in _legacy_paths(workspace)) if r)
    if status is not None:
        out = [r for r in out if r.get("status") == status]
    out.sort(key=lambda r: r.get("requested_at") or "")
    return out


def find_pending(workspace: Path | str, *, session_key: str | None, kind: str,
                 change_hash: str) -> dict | None:
    """The pending record for the same request, so a replayed turn does not
    file a duplicate."""
    for rec in list_records(workspace, status="pending", include_legacy=False):
        if (rec.get("requested_by_session") == session_key and rec.get("kind") == kind
                and rec.get("change_hash") == change_hash):
            return rec
    return None


def transition(workspace: Path | str, approval_id: str, *, expect: tuple[str, ...],
               to: str, **fields: Any) -> dict | None:
    """Move a record from one of ``expect`` to ``to``; None when it was in any
    other state (someone else decided first) or does not exist."""
    with cross_process_lock(_root(workspace)):
        rec = _read(_path(workspace, approval_id))
        if rec is None or rec.get("status") not in expect:
            return None
        rec["status"] = to
        if "decided_by" in fields or to == "expired":
            # An expiry is stamped like a decision, so the retention window of
            # an expired record counts from when it expired.
            rec["decided_at"] = _now().isoformat()
        rec.update(fields)
        _write(workspace, rec)
        return rec


def expire_and_prune(workspace: Path | str, *, now: datetime | None = None) -> dict[str, int]:
    """Expire pending records past their TTL; close as interrupted a record
    left ``approved`` past ``APPROVED_RUN_BOUND``; delete terminal records
    older than the retention window."""
    now = now or _now()
    counts = {"expired": 0, "interrupted": 0, "pruned": 0}
    for rec in list_records(workspace, include_legacy=False):
        status = rec.get("status")
        if status == "pending":
            expires = rec.get("expires_at")
            if expires and datetime.fromisoformat(expires) <= now:
                if transition(workspace, rec["id"], expect=("pending",), to="expired",
                              decided_at=now.isoformat()):
                    counts["expired"] += 1
        elif status == "approved":
            decided = rec.get("decided_at")
            if decided and datetime.fromisoformat(decided) + APPROVED_RUN_BOUND <= now:
                # Compare-and-set: a run that finishes first keeps its result.
                if transition(workspace, rec["id"], expect=("approved",), to="failed",
                              result={"error": "interrupted"}):
                    counts["interrupted"] += 1
        elif status in TERMINAL:
            stamp = rec.get("decided_at") or rec.get("requested_at")
            if stamp and datetime.fromisoformat(stamp) + RESOLVED_RETENTION <= now:
                if discard(workspace, rec["id"]):
                    counts["pruned"] += 1
    return counts


def discard(workspace: Path | str, approval_id: str) -> bool:
    """Delete a record (current or legacy layout); False when absent."""
    path = _path(workspace, approval_id)
    try:
        path.unlink()
        return True
    except OSError:
        pass
    for legacy in _legacy_paths(workspace):
        if legacy.stem == approval_id:
            try:
                legacy.unlink()
                return True
            except OSError:
                return False
    return False
