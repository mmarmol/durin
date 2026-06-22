"""Session metadata helpers for the agent's decision log (concern B / task-state anchor).

The model accumulates a short, flat list of key decisions and findings for the
current task. Two writers feed it: the ``note_decision`` tool (real time) and the
consolidator's auto-extraction at compaction (durin/agent/memory.py). Both go
through :func:`add_decision`, which dedups and caps so the log stays cheap to
re-inject every turn.

Mirrors durin/session/todo_state.py: stored under ``metadata[DECISION_LOG_KEY]``
(not a derived key, so it persists on line-0 and survives compaction), echoed
into Runtime Context every turn via :func:`decision_log_runtime_lines`.

Caps default to the module constants but callers pass the configured values
(``AgentDefaults.decision_log_max_entries`` / ``decision_log_max_chars``).

Schema (each entry):
    {"text": str, "ts": str (iso8601, may be ""), "source": "tool" | "auto"}

See docs/internals/loop.md.
"""
from __future__ import annotations

from typing import Any, Mapping

DECISION_LOG_KEY = "decision_log"

_DEFAULT_MAX_ENTRIES = 10
_DEFAULT_MAX_CHARS = 1500
_MAX_TEXT = 400
_ALLOWED_SOURCES = frozenset({"tool", "auto"})


def _normalize_key(text: str) -> str:
    return " ".join(text.lower().split())


def decision_log_raw(metadata: Mapping[str, Any] | None) -> Any:
    if not metadata:
        return None
    return metadata.get(DECISION_LOG_KEY)


def parse_decisions(blob: Any) -> list[dict[str, str]]:
    """Validate *blob* into a normalized ``list[dict]`` (empty list if invalid)."""
    if not isinstance(blob, list):
        return []
    out: list[dict[str, str]] = []
    for entry in blob:
        if not isinstance(entry, Mapping):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        source = str(entry.get("source") or "auto").strip()
        if source not in _ALLOWED_SOURCES:
            source = "auto"
        out.append({
            "text": text[:_MAX_TEXT],
            "ts": str(entry.get("ts") or ""),
            "source": source,
        })
    return out


def _enforce_cap(
    entries: list[dict[str, str]], max_entries: int, max_chars: int
) -> tuple[list[dict[str, str]], int]:
    """Drop oldest entries until both limits hold. Returns (entries, dropped)."""
    dropped = 0
    if len(entries) > max_entries:
        dropped += len(entries) - max_entries
        entries = entries[-max_entries:]
    while len(entries) > 1 and sum(len(e["text"]) for e in entries) > max_chars:
        entries.pop(0)
        dropped += 1
    return entries, dropped


def add_decision(
    metadata: dict[str, Any] | None,
    text: str,
    *,
    source: str,
    ts: str = "",
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> tuple[list[dict[str, str]], int]:
    """Append a decision (dedup + cap). Returns ``(entries, dropped)``.

    No-op (returns the current list, dropped=0) on blank text or a normalized
    duplicate of an existing entry.
    """
    if metadata is None:
        return [], 0
    text = str(text or "").strip()
    entries = parse_decisions(decision_log_raw(metadata))
    if not text:
        return entries, 0
    key = _normalize_key(text)
    if any(_normalize_key(e["text"]) == key for e in entries):
        return entries, 0
    if source not in _ALLOWED_SOURCES:
        source = "auto"
    entries.append({"text": text[:_MAX_TEXT], "ts": str(ts or ""), "source": source})
    entries, dropped = _enforce_cap(entries, max_entries, max_chars)
    metadata[DECISION_LOG_KEY] = entries
    return entries, dropped


def decision_log_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Lines for the 'Decisions & findings' section of the task-state anchor."""
    entries = parse_decisions(decision_log_raw(metadata))
    return [f"  - {e['text']}" for e in entries]
