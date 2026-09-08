"""Dream pass: a session summary for every conversation that went idle.

The compactor writes ``memory/session_summary/<key>.md`` when a session
compacts, and ``/new`` writes it when the user closes a session. A
conversation that ends by silence — a webui chat abandoned, a Slack thread
that stops — leaves neither. This pass runs in the nightly dream: for each
conversation idle for ``idle_hours``, it summarizes the messages since the
last summary (or since the compactor's ``last_consolidated``) with the same
archive prompt the compactor uses, appends the block to the same store, and
advances a per-session cursor so the pass is idempotent.

Only conversations qualify. Workflow, subagent, cron, automation and bench
sessions are skipped by file stem, and any session whose line-0 metadata
carries an ``origin_type`` marker is skipped too.

The cursor is a top-level ``summary_cursor`` key in ``<stem>.meta.json`` —
the same file and lock the extract cursor uses — because
``SessionManager.save()`` replaces only the ``derived`` block and so cannot
erase it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.memory.consolidator_tags import parse_consolidator_response
from durin.memory.extract_runner import _meta_path, load_session
from durin.memory.session_summary_store import append_session_summary_block
from durin.session.manager import is_workflow_session_file
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.utils.prompt_templates import render_template

__all__ = [
    "get_summary_cursor",
    "run_session_summary_pass",
    "set_summary_cursor",
    "summarize_session",
]

LLMInvoke = Callable[..., Any]

_CURSOR_KEY = "summary_cursor"
_MAX_SPAN_CHARS = 48_000
_SKIP_STEM_PREFIXES = ("workflow_", "subagent_", "cron_", "automation_", "bench_")


def get_summary_cursor(jsonl_path: Path) -> int:
    """Number of messages already summarized for this session (0 when unset)."""
    mp = _meta_path(Path(jsonl_path))
    if not mp.exists():
        return 0
    try:
        return int(json.loads(mp.read_text(encoding="utf-8")).get(_CURSOR_KEY) or 0)
    except Exception:  # noqa: BLE001 — a corrupt sidecar means "start over"
        return 0


def set_summary_cursor(jsonl_path: Path, n: int) -> None:
    """Persist the cursor as a top-level key under the session's lock."""
    jsonl_path = Path(jsonl_path)
    mp = _meta_path(jsonl_path)
    with cross_process_lock(jsonl_path):
        data: dict = {}
        if mp.exists():
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        data[_CURSOR_KEY] = int(n)
        atomic_write_text(mp, json.dumps(data, indent=2))


def _emit(event: str, **data: Any) -> None:
    """Best-effort dream telemetry."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(event, data)
    except Exception:  # pragma: no cover — telemetry must never break the dream
        pass


def _format_turns(messages: list[dict]) -> str:
    """The compactor's transcript shape: one line per message with a
    timestamp prefix and the turn's tool names, so the archive prompt sees
    the same input either way."""
    lines = []
    for m in messages:
        if not m.get("content"):
            continue
        tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
        lines.append(
            f"[{str(m.get('timestamp', '?'))[:16]}] "
            f"{str(m.get('role', '?')).upper()}{tools}: {m['content']}"
        )
    return "\n".join(lines)


def _skip_reason(meta: dict, jsonl_path: Path, *, idle_hours: int, now: datetime) -> str | None:
    if any(jsonl_path.stem.startswith(p) for p in _SKIP_STEM_PREFIXES):
        return "non_interactive"
    if (meta.get("metadata") or {}).get("origin_type"):
        return "non_interactive"
    raw = meta.get("updated_at")
    try:
        updated = datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        updated = None
    if updated is None:
        return "no_timestamp"
    if updated.tzinfo is not None:
        # ``now`` is naive local time, so convert before dropping the offset —
        # a bare replace() would read a UTC stamp as local and shift the
        # idle window by the machine's offset.
        updated = updated.astimezone().replace(tzinfo=None)
    if now - updated < timedelta(hours=idle_hours):
        return "active"
    return None


def summarize_session(
    workspace: Path,
    jsonl_path: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    idle_hours: int = 6,
    min_new_messages: int = 4,
    now: datetime | None = None,
) -> dict:
    """Summarize one session's unsummarized span; returns a small result dict."""
    from durin.memory.llm_invoke import default_llm_invoke

    llm_invoke = llm_invoke or default_llm_invoke
    now = now or datetime.now()
    jsonl_path = Path(jsonl_path)
    meta, msgs = load_session(jsonl_path)
    key = str(meta.get("key") or jsonl_path.stem)
    reason = _skip_reason(meta, jsonl_path, idle_hours=idle_hours, now=now)
    if reason:
        return {"session": key, "skipped": reason}

    start = max(get_summary_cursor(jsonl_path), int(meta.get("last_consolidated") or 0))
    if start > len(msgs):
        # The file shrank since the cursor was written (/new emptied it or the
        # file cap trimmed it): what is there now is a new conversation.
        start = int(meta.get("last_consolidated") or 0)
    span = [
        m for m in msgs[start:]
        if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("_command")
    ]
    if len(span) < min_new_messages:
        return {"session": key, "skipped": "too_short", "new_messages": len(span)}

    text = _format_turns(msgs[start:])
    if len(text) > _MAX_SPAN_CHARS:
        text = "(earlier turns omitted)\n" + text[-_MAX_SPAN_CHARS:]
    prompt = render_template("agent/consolidator_archive.md", strip=True) + "\n\n" + text
    resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
    raw = resp.text if hasattr(resp, "text") else str(resp)
    summary, tags = parse_consolidator_response(raw)

    total = len(msgs)
    if not summary or summary.strip() == "(nothing)":
        set_summary_cursor(jsonl_path, total)
        return {"session": key, "skipped": "nothing", "cursor": total}
    # The store declines a block it already holds as the newest one (a
    # degraded-LLM repeat) with no new tags, so "written" is what it
    # reports, not what we asked.
    path = append_session_summary_block(
        workspace, key, summary, last_active=meta.get("updated_at"),
        entities=tags["entities"], topics=tags["topics"],
    )
    set_summary_cursor(jsonl_path, total)
    return {
        "session": key, "written": path is not None,
        "cursor": total, "new_messages": len(span),
    }


def run_session_summary_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    max_seconds: int = 0,
    idle_hours: int = 6,
    min_new_messages: int = 4,
) -> dict:
    """Walk ``sessions/*.jsonl`` and summarize every idle conversation with new turns."""
    t0 = time.perf_counter()
    _emit("memory.dream.start", kind="session_summary")
    out: dict[str, Any] = {"sessions": 0, "written": 0, "skipped": 0, "errors": [], "yielded": False}
    sdir = Path(workspace) / "sessions"
    if sdir.is_dir():
        for jsonl_path in sorted(sdir.glob("*.jsonl")):
            if max_seconds and (time.perf_counter() - t0) >= max_seconds:
                out["yielded"] = True
                break
            if is_workflow_session_file(jsonl_path):
                continue
            try:
                result = summarize_session(
                    workspace, jsonl_path, llm_invoke=llm_invoke, model=model,
                    idle_hours=idle_hours, min_new_messages=min_new_messages,
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not stop the pass
                logger.warning("session summary pass: {} failed: {}", jsonl_path.stem, exc)
                out["errors"].append(f"{jsonl_path.stem}: {exc}")
                continue
            out["sessions"] += 1
            if result.get("written"):
                out["written"] += 1
            else:
                out["skipped"] += 1
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    _emit("memory.dream.end", kind="session_summary", sessions=out["sessions"],
          written=out["written"], skipped=out["skipped"], errors=len(out["errors"]),
          yielded=out["yielded"], duration_ms=out["duration_ms"])
    return out
