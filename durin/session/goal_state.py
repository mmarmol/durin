"""Session metadata helpers for sustained goals (e.g. ``long_task`` / ``complete_goal``).

Tools set ``metadata[GOAL_STATE_KEY]``. Reads accept the legacy session key ``thread_goal``
for older sessions. Callers use ``goal_state_runtime_lines``, ``goal_state_ws_blob``, and
``runner_wall_llm_timeout_s`` without importing tool implementations.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, MutableMapping

from durin.session.manager import SessionManager

GOAL_STATE_KEY = "goal_state"
# Older builds stored the same JSON blob under this key.
_LEGACY_GOAL_STATE_SESSION_KEY = "thread_goal"
_MAX_OBJECTIVE_IN_RUNTIME = 4000
_MAX_OBJECTIVE_WS = 600
# A finished goal keeps only a short trace in the anchor — it is orientation,
# not the working objective, and it rides every turn.
_MAX_COMPLETED_GOAL_CHARS = 240


def _session_goal_raw(metadata: Mapping[str, Any] | None) -> Any:
    if not metadata:
        return None
    if GOAL_STATE_KEY in metadata:
        return metadata.get(GOAL_STATE_KEY)
    return metadata.get(_LEGACY_GOAL_STATE_SESSION_KEY)


def discard_legacy_goal_state_key(metadata: MutableMapping[str, Any]) -> None:
    """Remove legacy metadata key after migrating writes to :data:`GOAL_STATE_KEY`."""
    metadata.pop(_LEGACY_GOAL_STATE_SESSION_KEY, None)


def goal_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    """Return the session goal blob under :data:`GOAL_STATE_KEY` or the legacy key."""
    return _session_goal_raw(metadata)


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    """True when this session has an active sustained objective (``long_task`` bookkeeping)."""
    goal = parse_goal_state(goal_state_raw(metadata))
    return isinstance(goal, dict) and goal.get("status") == "active"


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def goal_headline(goal: Mapping[str, Any] | None) -> str:
    """Shortest faithful label for a goal: its summary, else the objective's
    first line. Shared so the runtime anchor and the decision-log breadcrumb
    cannot drift into two different descriptions of the same goal."""
    if not isinstance(goal, Mapping):
        return ""
    headline = str(goal.get("ui_summary") or "").strip()
    if not headline:
        objective = str(goal.get("objective") or "").strip()
        headline = objective.splitlines()[0] if objective else ""
    if len(headline) > _MAX_COMPLETED_GOAL_CHARS:
        headline = headline[:_MAX_COMPLETED_GOAL_CHARS].rstrip() + "…"
    return headline


def _completed_goal_lines(goal: Mapping[str, Any]) -> list[str]:
    """Compact trace of a finished goal: what it was and how it ended."""
    headline = goal_headline(goal)
    if not headline:
        return []
    out = [f"Goal (completed): {headline}"]
    recap = str(goal.get("recap") or "").strip()
    if recap:
        if len(recap) > _MAX_COMPLETED_GOAL_CHARS:
            recap = recap[:_MAX_COMPLETED_GOAL_CHARS].rstrip() + "…"
        out.append(f"Outcome: {recap}")
    return out


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Lines appended inside the Runtime Context block when a goal is active."""
    if not metadata:
        return []
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict):
        return []
    status = goal.get("status")
    if status == "completed":
        # A session rarely ends when its goal does — work continues, and the
        # objective is exactly the context compaction must not drop. Keep a
        # compact trace in the anchor instead of rendering nothing, so the
        # model can still tell what this session was for (and that it is done,
        # so it does not resume the finished work). Deliberately does NOT go
        # through ``sustained_goal_active``, which gates the runner's
        # wall-clock backstop and must stay false for a finished goal.
        return _completed_goal_lines(goal)
    if status != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return ["Goal: active (no objective text stored)."]
    if len(objective) > _MAX_OBJECTIVE_IN_RUNTIME:
        objective = objective[:_MAX_OBJECTIVE_IN_RUNTIME].rstrip() + "\n… (truncated)"
    out = ["Goal (active):", objective]
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    max_turns = goal.get("max_turns")
    if isinstance(max_turns, int) and max_turns > 0:
        used = int(goal.get("turns_used", 0))
        out.append(f"Turn budget: {used}/{max_turns}.")
        if used >= max_turns:
            out.append(
                "Turn budget exceeded — wrap up now: call complete_goal "
                "with an honest recap of where things stand, or ask the "
                "user whether to extend the budget."
            )
    return out


def increment_goal_turns(metadata: MutableMapping[str, Any] | None) -> None:
    """Per-turn bookkeeping: bump ``turns_used`` on an active budgeted goal.

    No-op when there is no active goal or the goal has no ``max_turns``
    (un-budgeted goals stay exactly as before). Writes the blob back as a
    dict under :data:`GOAL_STATE_KEY` (migrating legacy string blobs).
    """
    if not isinstance(metadata, MutableMapping):
        return
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return
    if "max_turns" not in goal:
        return
    goal = dict(goal)
    goal["turns_used"] = int(goal.get("turns_used", 0)) + 1
    metadata[GOAL_STATE_KEY] = goal


def goal_state_ws_blob(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """JSON-safe snapshot for WebSocket ``goal_state`` events (one chat_id per frame)."""
    goal = parse_goal_state(_session_goal_raw(metadata)) if metadata else None
    if isinstance(goal, dict) and goal.get("status") == "active":
        objective = str(goal.get("objective") or "").strip()
        if len(objective) > _MAX_OBJECTIVE_WS:
            objective = objective[:_MAX_OBJECTIVE_WS].rstrip() + "…"
        summary = str(goal.get("ui_summary") or "").strip()[:120]
        blob: dict[str, Any] = {"active": True}
        if summary:
            blob["ui_summary"] = summary
        if objective:
            blob["objective"] = objective
    else:
        blob = {"active": False}
    if metadata:
        # Non-default agent mode and a pending question ride this frame so
        # the webui composer can render badges/strips (Tasks C2/D4).
        from durin.agent.agent_mode import DEFAULT_MODE, SESSION_MODE_KEY

        mode = str(metadata.get(SESSION_MODE_KEY) or "")
        if mode and mode != DEFAULT_MODE:
            blob["mode"] = mode
        pq = metadata.get("pending_question")
        if isinstance(pq, Mapping) and pq.get("question"):
            blob["pending_question"] = {
                "question": str(pq.get("question") or ""),
                "options": [str(o) for o in (pq.get("options") or [])],
            }
    return blob


def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """Wall-clock cap for :class:`~durin.agent.runner.AgentRunner` when streaming an LLM.

    Returns ``0.0`` to disable ``asyncio.wait_for`` around the request when a sustained goal is
    active; ``None`` means use ``DURIN_LLM_TIMEOUT_S``. Pass in-memory ``metadata`` when the
    caller already holds :attr:`~durin.session.manager.Session.metadata` for this turn.
    """
    meta: Mapping[str, Any] | None = metadata
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_active(meta) else None
