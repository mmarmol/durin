"""Dream pass: a session summary for every conversation that went idle.

The compactor writes ``memory/session_summary/<key>.md`` when a session
compacts, and ``/new`` writes it when the user closes a session. A
conversation that ends by silence — a webui chat abandoned, a Slack thread
that stops — leaves neither. This pass runs in the nightly dream: for each
conversation idle for ``idle_hours``, it summarizes the messages since the
last summary (or since the compactor's ``last_consolidated``) with the same
archive prompt the compactor uses, in pieces that each fit one call, appends
a block per piece to the same store, and advances a per-session cursor past
each piece so the pass is idempotent.

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
from durin.utils.runtime import runs_that_fit, without_failure_placeholders

__all__ = [
    "get_summary_cursor",
    "run_session_summary_pass",
    "set_summary_cursor",
    "summarize_session",
]

LLMInvoke = Callable[..., Any]

_CURSOR_KEY = "summary_cursor"
# The most span text one summarizing call of this pass takes; a longer span
# is summarized in pieces of at most this size.
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
    deadline: float | None = None,
) -> dict:
    """Summarize one session's unsummarized span; returns a small result dict.

    The span goes to the summarizer whole, cut at message boundaries into
    pieces of at most ``_MAX_SPAN_CHARS`` (a single message larger than
    that is a piece of its own): one call per piece, in order, each summary
    stored as a block of its own. The cursor moves past each piece once its
    call answered, so a call that raises, or a ``deadline``
    (``time.perf_counter()``) passed before the next piece, leaves the rest
    for the next pass instead of skipping it."""
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
    # The placeholders of turns that produced no answer stay out, as they do
    # of what compaction summarizes into the same bounded store: they say
    # nothing worth a block that would evict an older, real one.
    new = without_failure_placeholders(msgs[start:])
    span = [
        m for m in new
        if m.get("role") in ("user", "assistant") and m.get("content") and not m.get("_command")
    ]
    if len(span) < min_new_messages:
        return {"session": key, "skipped": "too_short", "new_messages": len(span)}

    # Where each message sits in the file: the cursor indexes the file's
    # messages, placeholders included, and a piece ends at its last message.
    position = {id(m): i for i, m in enumerate(msgs)}
    instructions = render_template("agent/consolidator_archive.md", strip=True)
    pieces = runs_that_fit(new, _MAX_SPAN_CHARS, line=lambda m: _format_turns([m]), count=len)
    summarized = written = False
    cursor = start
    for n, piece in enumerate(pieces):
        if n and deadline is not None and time.perf_counter() >= deadline:
            return {
                "session": key, "written": written, "cursor": cursor,
                "new_messages": len(span), "yielded": True,
            }
        text = _format_turns(piece)
        if text:
            prompt = instructions + "\n\n" + text
            resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
            raw = resp.text if hasattr(resp, "text") else str(resp)
            summary, tags = parse_consolidator_response(raw)
            if summary and summary.strip() != "(nothing)":
                # The store declines a block it already holds as the newest
                # one (a degraded-LLM repeat) with no new tags, so "written"
                # is what it reports, not what we asked.
                path = append_session_summary_block(
                    workspace, key, summary, last_active=meta.get("updated_at"),
                    entities=tags["entities"], topics=tags["topics"],
                )
                summarized = True
                written = written or path is not None
        cursor = position[id(piece[-1])] + 1
        set_summary_cursor(jsonl_path, cursor)
    # What follows the last piece is failure placeholders, left out.
    total = len(msgs)
    set_summary_cursor(jsonl_path, total)
    if not summarized:
        return {"session": key, "skipped": "nothing", "cursor": total}
    return {
        "session": key, "written": written,
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
    deadline = t0 + max_seconds if max_seconds else None
    _emit("memory.dream.start", kind="session_summary")
    out: dict[str, Any] = {"sessions": 0, "written": 0, "skipped": 0, "errors": [], "yielded": False}
    sdir = Path(workspace) / "sessions"
    if sdir.is_dir():
        for jsonl_path in sorted(sdir.glob("*.jsonl")):
            if deadline is not None and time.perf_counter() >= deadline:
                out["yielded"] = True
                break
            if is_workflow_session_file(jsonl_path):
                continue
            try:
                result = summarize_session(
                    workspace, jsonl_path, llm_invoke=llm_invoke, model=model,
                    idle_hours=idle_hours, min_new_messages=min_new_messages, deadline=deadline,
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
            if result.get("yielded"):
                # Out of time inside this session: the rest of it, and the
                # sessions after it, wait for the next pass.
                out["yielded"] = True
                break
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    _emit("memory.dream.end", kind="session_summary", sessions=out["sessions"],
          written=out["written"], skipped=out["skipped"], errors=len(out["errors"]),
          yielded=out["yielded"], duration_ms=out["duration_ms"])
    return out
