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
erase it. It names the message the pass ended on, not only its position:
``/new`` and the file cap renumber a session's messages without touching it
(``summarized_count``).
"""

from __future__ import annotations

import hashlib
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
from durin.utils.runtime import (
    runs_that_fit,
    summary_token_count,
    truncate_to_tokens,
    without_failure_placeholders,
)

__all__ = [
    "memory_input_budget",
    "run_session_summary_pass",
    "set_summary_cursor",
    "summarize_session",
    "summarized_count",
]

LLMInvoke = Callable[..., Any]

_CURSOR_KEY = "summary_cursor"
_SKIP_STEM_PREFIXES = ("workflow_", "subagent_", "cron_", "automation_", "bench_")


def _content_hash(message: dict) -> str:
    """A short digest of a message's content, the same in every process and
    across the session file's JSON round trip."""
    raw = json.dumps(message.get("content"), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def summarized_count(jsonl_path: Path, messages: list[dict]) -> int:
    """How many of *messages*, from the first, the nightly pass summarized.

    The cursor names the message the pass ended on (its timestamp, role and
    a hash of its content) and the position it held then. That message is
    found in *messages* as they are now, and the covered part ends right
    after it: ``/new`` empties a session and the file cap drops its head,
    both without touching the cursor, and a position alone would then cover
    messages no call ever summarized. A message that is gone covers nothing.
    One held more than once resolves to the last match at or before the
    recorded position: a message only moves toward the head, so a match past
    that position is a later copy the pass never saw. A cursor written as a
    bare position, before it named its message, covers nothing either: one
    more summary of the same turns, never a loss."""
    mp = _meta_path(Path(jsonl_path))
    if not mp.exists():
        return 0
    try:
        record = json.loads(mp.read_text(encoding="utf-8")).get(_CURSOR_KEY)
        position = int(record["position"]) if isinstance(record, dict) else 0
    except Exception:  # noqa: BLE001 — a corrupt sidecar means "start over"
        return 0
    for i in range(min(position, len(messages)) - 1, -1, -1):
        message = messages[i]
        if (
            message.get("timestamp") == record.get("timestamp")
            and message.get("role") == record.get("role")
            and _content_hash(message) == record.get("hash")
        ):
            return i + 1
    return 0


def set_summary_cursor(jsonl_path: Path, messages: list[dict], n: int) -> None:
    """Record, under the session's lock, that the pass summarized
    ``messages[:n]``: the position and the message it ended on."""
    jsonl_path = Path(jsonl_path)
    mp = _meta_path(jsonl_path)
    with cross_process_lock(jsonl_path):
        data: dict = {}
        if mp.exists():
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        if n > 0:
            last = messages[n - 1]
            data[_CURSOR_KEY] = {
                "position": int(n),
                "timestamp": last.get("timestamp"),
                "role": last.get("role"),
                "hash": _content_hash(last),
            }
        else:
            data.pop(_CURSOR_KEY, None)
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


def memory_input_budget(config: Any) -> int:
    """The input budget of the model the dream summarizes with (the memory
    preset, as the dream's own calls resolve it), sized as compaction sizes
    its calls: the window less the output ceiling and compaction's safety
    buffer."""
    from durin.agent.memory import Consolidator
    from durin.memory.model_resolve import resolve_aux_preset

    preset = config.resolve_preset_limits(resolve_aux_preset(config, purpose="memory"))
    return max(1, preset.context_window_tokens - preset.max_tokens - Consolidator._SAFETY_BUFFER)


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
    budget_tokens: int | None = None,
) -> dict:
    """Summarize one session's unsummarized span; returns a small result dict.

    The span goes to the summarizer whole, cut at message boundaries as
    compaction cuts it, into pieces that each fit one call of
    *budget_tokens* (the summarizing model's input budget; the memory
    model's, ``memory_input_budget``, when not given): one call per piece,
    in order, each summary stored as a block of its own. A single message
    larger than the budget is a piece of its own, cut to it as compaction
    cuts one — sent whole, it would fail its call every night on a model
    that cannot take it, and the session would never be summarized again.
    The cursor moves past each piece once its call answered, so a call that
    raises, or a ``deadline`` (``time.perf_counter()``) passed before the
    next piece, leaves the rest for the next pass instead of skipping it."""
    from durin.memory.llm_invoke import default_llm_invoke

    llm_invoke = llm_invoke or default_llm_invoke
    now = now or datetime.now()
    jsonl_path = Path(jsonl_path)
    meta, msgs = load_session(jsonl_path)
    key = str(meta.get("key") or jsonl_path.stem)
    reason = _skip_reason(meta, jsonl_path, idle_hours=idle_hours, now=now)
    if reason:
        return {"session": key, "skipped": reason}

    start = max(summarized_count(jsonl_path, msgs), int(meta.get("last_consolidated") or 0))
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
    if budget_tokens is None:
        from durin.config.loader import load_config

        budget_tokens = memory_input_budget(load_config())
    pieces = runs_that_fit(
        new, budget_tokens, line=lambda m: _format_turns([m]), count=summary_token_count,
    )
    summarized = written = False
    cursor = start
    for n, piece in enumerate(pieces):
        if n and deadline is not None and time.perf_counter() >= deadline:
            return {
                "session": key, "written": written, "cursor": cursor,
                "new_messages": len(span), "yielded": True,
            }
        text = truncate_to_tokens(_format_turns(piece), budget_tokens)
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
        set_summary_cursor(jsonl_path, msgs, cursor)
    # What follows the last piece is failure placeholders, left out.
    total = len(msgs)
    set_summary_cursor(jsonl_path, msgs, total)
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
    budget_tokens: int | None = None,
) -> dict:
    """Walk ``sessions/*.jsonl`` and summarize every idle conversation with new
    turns, in calls of at most *budget_tokens* of span (``summarize_session``)."""
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
                    budget_tokens=budget_tokens,
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
