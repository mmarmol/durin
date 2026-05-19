"""Per-session metadata of significant events — foundation for Phase 2 memory.

One ``<workspace>/sessions/<safe_key>.meta.json`` file per session. Holds a
chronological list of lifecycle events that matter beyond the raw message
stream: plan submissions/approvals/closures today, and (extensible by ``type``)
future patterns like reviews, deliberations, or other actions worth tracing.

Why a single file per session (vs one sidecar per event):
- Memory subsystem (Phase 2) reads ONE file to know everything significant
  that happened in a session; no directory walking
- Editor / grep / jq friendly
- Event count is small (planes y similares son N pocos por session, no
  miles), no risk of unwieldy size
- Future event types just append a new entry with a different ``type``

What goes here:
- Events with a lifecycle (created, transitioned, closed)
- Events that need a stable identifier referenced from elsewhere (a plan
  file path, a tool that was invoked, etc.)
- Events that the memory subsystem will want to correlate against the
  raw session.messages (using ``msg_index``)

What does NOT go here:
- Per-turn telemetry (lives in `~/.cache/durin/telemetry/...`)
- The actual content of a plan (lives in its own .md file)
- Anything that already lives in ``session.json``
"""

from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def meta_path_for(session_key: str, sessions_dir: Path) -> Path:
    """Return the ``.meta.json`` path next to ``<session>.jsonl``."""
    import re

    safe = re.sub(r"[^\w\-]+", "_", session_key.replace(":", "_"))
    safe = safe.strip("_") or "default"
    return sessions_dir / f"{safe}.meta.json"


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def read_meta(meta_path: Path) -> dict[str, Any]:
    """Load the meta file, returning ``{"events": []}`` shape if missing.

    Tolerates a missing or malformed file — returns the empty default so
    callers can always treat the result uniformly.
    """
    if not meta_path.exists():
        return {"session_key": None, "events": []}
    try:
        text = meta_path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"session_key": None, "events": []}
        events = data.get("events")
        if not isinstance(events, list):
            data["events"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"session_key": None, "events": []}


def _atomic_write(meta_path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write: write to tmp, then rename. Never partial state."""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_path.with_suffix(meta_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# High-level API: events
# ---------------------------------------------------------------------------


def append_event(meta_path: Path, session_key: str, event: dict[str, Any]) -> None:
    """Append a new event (dict) to the meta file.

    The event dict should at minimum have ``type`` and ``id`` fields;
    additional fields are type-specific. ``recorded_at`` is auto-added
    if missing so the chronological order in the file matches wall time.
    """
    data = read_meta(meta_path)
    data["session_key"] = session_key  # backfill if missing
    if "recorded_at" not in event:
        event["recorded_at"] = _now_iso()
    data["events"].append(event)
    _atomic_write(meta_path, data)


def update_event(meta_path: Path, event_id: str, updates: dict[str, Any]) -> bool:
    """Apply ``updates`` to the event with matching ``id``. Returns True if found.

    Used for lifecycle transitions: e.g. when ``/build`` approves a plan,
    we update the plan event's ``approved_at``, ``msg_index.approved``,
    and ``outcome``.
    """
    data = read_meta(meta_path)
    for evt in data["events"]:
        if isinstance(evt, dict) and evt.get("id") == event_id:
            # Allow updating nested dicts (e.g. msg_index) with merge.
            for k, v in updates.items():
                if isinstance(v, dict) and isinstance(evt.get(k), dict):
                    evt[k].update(v)
                else:
                    evt[k] = v
            _atomic_write(meta_path, data)
            return True
    return False


def find_event(meta_path: Path, *, event_id: str | None = None,
               type: str | None = None, outcome: str | None = None) -> list[dict[str, Any]]:
    """Filter events from the meta file. All filters AND together; omit to skip."""
    data = read_meta(meta_path)
    out: list[dict[str, Any]] = []
    for evt in data["events"]:
        if not isinstance(evt, dict):
            continue
        if event_id is not None and evt.get("id") != event_id:
            continue
        if type is not None and evt.get("type") != type:
            continue
        if outcome is not None and evt.get("outcome") != outcome:
            continue
        out.append(evt)
    return out


def find_executing_plan(meta_path: Path) -> dict[str, Any] | None:
    """Convenience: return the single ``plan`` event with ``outcome=executing``,
    or ``None`` if none is in flight."""
    matches = find_event(meta_path, type="plan", outcome="executing")
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Title extraction (for plan-type events, but reusable)
# ---------------------------------------------------------------------------


def extract_markdown_title(text: str, *, fallback_len: int = 80) -> str:
    """Extract a one-line title from a markdown document.

    Strategy:
    1. First ``#``-prefixed heading (H1) — strip the leading ``#`` and surrounding whitespace
    2. First non-empty content line if no heading
    3. Empty string if the document is blank
    """
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Strip leading ``#`` chars + space
            return line.lstrip("#").strip()
        # First content line — use truncated to fallback_len
        return line[:fallback_len]
    return ""


# ---------------------------------------------------------------------------
# Plan-event helpers (type-specific convenience layer)
# ---------------------------------------------------------------------------


def make_plan_event(
    *, plan_id: str, plan_path: str, title: str = "",
    created_at_iso: str | None = None,
) -> dict[str, Any]:
    """Build a fresh ``type=plan`` event with ``outcome=pending``.

    Called by ``ExitPlanModeTool`` after writing the plan ``.md`` file.
    """
    return {
        "type": "plan",
        "id": plan_id,
        "title": title,
        "plan_path": plan_path,
        "created_at": created_at_iso or _now_iso(),
        "approved_at": None,
        "closed_at": None,
        "msg_index": {"approved": None, "closed": None},
        "outcome": "pending",
    }


def mark_plan_approved(meta_path: Path, plan_id: str, msg_index: int) -> bool:
    """Transition a plan event from ``pending`` → ``executing``.

    Called from ``cmd_build`` when the user approves a plan.
    """
    return update_event(meta_path, plan_id, {
        "approved_at": _now_iso(),
        "outcome": "executing",
        "msg_index": {"approved": msg_index},
    })


def mark_plan_superseded(meta_path: Path, plan_id: str, msg_index: int) -> bool:
    """Transition a plan event from ``executing`` → ``superseded``.

    Called from ``cmd_plan`` when a new plan supersedes a prior one.
    """
    return update_event(meta_path, plan_id, {
        "closed_at": _now_iso(),
        "outcome": "superseded",
        "msg_index": {"closed": msg_index},
    })


def mark_plan_cancelled(meta_path: Path, plan_id: str) -> bool:
    """Transition a plan event from ``pending`` → ``cancelled`` (no approval)."""
    return update_event(meta_path, plan_id, {
        "closed_at": _now_iso(),
        "outcome": "cancelled",
    })


# ---------------------------------------------------------------------------
# Tool-call-event helpers
# ---------------------------------------------------------------------------


def make_tool_call_event(
    *,
    tool_call_id: str,
    name: str,
    outcome: str,
    msg_index: int,
    duration_ms: float = 0.0,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a ``type=tool_call`` event for the session meta timeline.

    Captures a single tool invocation:
    - ``name``: tool name (e.g. ``read_file``)
    - ``outcome``: ``"ok"`` or ``"error"``
    - ``msg_index``: index in ``session.messages`` of the assistant message
      that emitted this call. Multiple events with the same ``msg_index``
      are expected when an assistant message issued parallel tool calls;
      they are distinguished by ``id`` (the LLM-assigned ``tool_call_id``).
    - ``duration_ms``: wall time spent inside ``tool.execute``.
    - ``error``: short error excerpt when ``outcome == "error"``.

    This is the cheap, lossy index — full args / result live in
    ``session.messages``, this is just the timeline pointer for the
    memory subsystem to walk.
    """
    event: dict[str, Any] = {
        "type": "tool_call",
        "id": tool_call_id,
        "name": name,
        "outcome": outcome,
        "msg_index": int(msg_index),
        "duration_ms": round(float(duration_ms), 1),
    }
    if error:
        event["error"] = str(error)[:200]
    return event


def append_events_batch(
    meta_path: Path, session_key: str, events: list[dict[str, Any]],
) -> None:
    """Append multiple events in one read-modify-write cycle.

    Same semantics as calling :func:`append_event` N times, but with a
    single JSON parse / write. Use when a single agent turn produced
    several events (e.g. parallel tool calls under one assistant message).
    """
    if not events:
        return
    data = read_meta(meta_path)
    data["session_key"] = session_key
    for event in events:
        if "recorded_at" not in event:
            event["recorded_at"] = _now_iso()
        data["events"].append(event)
    _atomic_write(meta_path, data)
