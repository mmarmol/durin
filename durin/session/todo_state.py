"""Session metadata helpers for the agent's todo list (``todo_write`` tool).

The model maintains a short flat list of tasks for the current chat thread.
The tool stores the full list under ``metadata[TODOS_KEY]``. Reads convert
the blob into a normalized ``list[dict]`` shape so callers do not have to
re-validate.

Schema (each item):
    {
        "content":    str  — imperative description ("Run the tests")
        "status":     "pending" | "in_progress" | "completed"
        "activeForm": str  — present-continuous form ("Running the tests"),
                              used by the CLI / UI to show what the agent
                              is currently doing
    }

The list is intentionally flat (no IDs, no parents, no due-dates). The
contract the model follows is the same as Claude Code's ``TodoWrite`` —
each tool call REPLACES the entire list, exactly one entry is
``in_progress`` while work is active, and items move to ``completed``
immediately as they finish. We do not try to enforce any of those rules
in code — the prompt does — but ``todos_runtime_lines`` echoes the
current state back to the model on every turn so it cannot silently
drift.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

TODOS_KEY = "todos"

_ALLOWED_STATUSES = frozenset({"pending", "in_progress", "completed"})
_MAX_ITEMS = 50
_MAX_CONTENT = 400
_MAX_ACTIVE = 400
_MAX_LINES_IN_RUNTIME = 50


def todos_raw(metadata: Mapping[str, Any] | None) -> Any:
    """Return the raw todos blob under :data:`TODOS_KEY` (or ``None``)."""
    if not metadata:
        return None
    return metadata.get(TODOS_KEY)


def parse_todos(blob: Any) -> list[dict[str, str]] | None:
    """Validate *blob* into a normalized ``list[dict]`` or return ``None``.

    Accepts the value as either a list already or a JSON string. Drops
    any entry that does not have the three required fields with a valid
    status. Truncates content / activeForm to keep the runtime line
    block bounded.
    """
    if blob is None:
        return None
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return None
    if not isinstance(blob, list):
        return None
    out: list[dict[str, str]] = []
    for entry in blob[:_MAX_ITEMS]:
        if not isinstance(entry, Mapping):
            continue
        content = str(entry.get("content") or "").strip()
        status = str(entry.get("status") or "").strip()
        active = str(entry.get("activeForm") or "").strip()
        if not content or status not in _ALLOWED_STATUSES:
            continue
        out.append({
            "content": content[:_MAX_CONTENT],
            "status": status,
            "activeForm": (active or content)[:_MAX_ACTIVE],
        })
    return out


def todos_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Lines appended inside the Runtime Context block when todos exist.

    Mirrors :func:`durin.session.goal_state.goal_state_runtime_lines` —
    the model sees a compact, deterministic restatement of the current
    todo list on every turn. Without this echo, compaction or long
    intervening tool output can hide the list and the model regresses
    to "I forgot the plan" behavior.
    """
    todos = parse_todos(todos_raw(metadata))
    if not todos:
        return []
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for t in todos:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    header = (
        f"Todos: {len(todos)} total — "
        f"{counts['in_progress']} in_progress, "
        f"{counts['pending']} pending, "
        f"{counts['completed']} completed."
    )
    out = [header]
    for t in todos[:_MAX_LINES_IN_RUNTIME]:
        marker = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}[t["status"]]
        # Use the active form for in_progress items so the model has the
        # exact phrase it should be using when narrating what it's doing.
        text = t["activeForm"] if t["status"] == "in_progress" else t["content"]
        out.append(f"  {marker} {text}")
    if len(todos) > _MAX_LINES_IN_RUNTIME:
        out.append(f"  … (+{len(todos) - _MAX_LINES_IN_RUNTIME} more, truncated)")
    return out


def todos_ws_blob(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """JSON-safe snapshot for WebSocket ``todos_state`` events."""
    todos = parse_todos(todos_raw(metadata)) or []
    return {"items": todos}


def render_todos_markdown(todos: list[dict[str, str]] | None) -> str:
    """Render the list as a markdown checklist for user-facing display."""
    if not todos:
        return "_(no todos)_"
    lines: list[str] = []
    for t in todos:
        if t["status"] == "completed":
            box = "[x]"
        elif t["status"] == "in_progress":
            box = "[~]"
        else:
            box = "[ ]"
        text = t["activeForm"] if t["status"] == "in_progress" else t["content"]
        lines.append(f"- {box} {text}")
    return "\n".join(lines)
