"""Extract dream orchestration — run the extractor over a session's new turns.

Per-session cursor (a top-level key of the session's ``.meta.json``): only
the turns after it are read. That unread span is cut at message boundaries
into pieces that each fit one stage prompt, the stages run per piece, and the
cursor moves past a piece once the model has answered for it — never past a
message no prompt carried. Re-running is safe — the extractor's writes are
idempotent under per-field precedence.

The discovery is precise (the agent's explicit upsert refs in the new turns);
mention-based discovery for entities the agent did not upsert is a refinement.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from durin.memory.extract_dream import (
    PIECE_CHARS,
    ProviderError,
    discover_entities,
    extract_entity,
    mine_learnings,
)
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock
from durin.utils.runtime import runs_that_fit, without_failure_placeholders

__all__ = [
    "load_session",
    "entity_refs_in_messages",
    "get_extract_cursor",
    "set_extract_cursor",
    "run_extract_for_session",
]

LLMInvoke = Callable[..., Any]

_clock = time.perf_counter  # module-level so tests can move the budget clock


def load_session(jsonl_path: Path) -> tuple[dict, list[dict]]:
    """Return (metadata, messages). ``messages[i]`` is turn ``i + 1``."""
    meta: dict = {}
    msgs: list[dict] = []
    lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if i == 0 and rec.get("_type") == "metadata":
            meta = rec
            continue
        msgs.append(rec)
    return meta, msgs


def entity_refs_in_messages(messages: list[dict]) -> list[str]:
    """Entity refs the agent authored via ``memory_upsert_entity`` tool calls."""
    refs: list[str] = []
    seen: set[str] = set()
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") != "memory_upsert_entity":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            ref = str(args.get("ref") or "").strip()
            if ref and ":" in ref and ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def _meta_path(jsonl_path: Path) -> Path:
    return Path(jsonl_path).with_suffix(".meta.json")


def get_extract_cursor(jsonl_path: Path) -> int:
    """Return the per-session extract cursor (number of turns already processed).

    The cursor is stored as a top-level ``"extract_cursor"`` key in the
    ``.meta.json`` sidecar. Falls back to the legacy ``derived.extract_cursor``
    location so that
    pre-existing sessions are not re-processed from turn 0.
    """
    mp = _meta_path(jsonl_path)
    if not mp.exists():
        return 0
    try:
        d = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return 0
    # Top-level key takes precedence; fall back to legacy derived location.
    top = d.get("extract_cursor")
    if top is not None:
        return int(top)
    return int((d.get("derived") or {}).get("extract_cursor") or 0)


def set_extract_cursor(jsonl_path: Path, n: int) -> None:
    """Advance the per-session extract cursor to ``n`` (turns processed so far).

    Stores the value as a top-level ``"extract_cursor"`` key in the
    ``.meta.json`` sidecar, outside the ``derived`` block, so that
    ``SessionManager.save()`` / ``save_runtime_state()`` — which replace
    only ``data["derived"]`` — cannot erase it (hazard #15-A).

    The read-modify-write is serialized under the same
    ``cross_process_lock(jsonl_path)`` that ``SessionManager`` uses for that
    session's sidecar, preventing lost-update races.
    """
    mp = _meta_path(jsonl_path)
    # Use the same lock key that SessionManager uses: cross_process_lock
    # appends ".lock" to the path, yielding "<session>.jsonl.lock".
    with cross_process_lock(Path(jsonl_path)):
        d: dict = {}
        if mp.exists():
            try:
                d = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        d["extract_cursor"] = n
        atomic_write_text(mp, json.dumps(d, indent=2))


def run_extract_for_session(
    workspace: Path,
    jsonl_path: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    discover: bool = True,
    skill_signals: bool = True,
    learnings: bool = True,
    confidence_threshold: int = 95,
    semantic_distance_threshold: float = 0.30,
    alias_index: "Any | None" = None,
    vector_index: "Any | None" = None,
    deadline: float | None = None,
) -> dict:
    """Distil this session's new turns into entities (and skill signals).

    The unread span is rendered as the session-summary pass renders a
    conversation — one line per message that has content, turns that
    produced no answer left out — each line prefixed with its ``[turn-N]``
    tag, and cut at message boundaries into pieces of at most
    ``PIECE_CHARS``. A single message larger than that is a piece of its
    own, and the stage prompts keep its head. Per piece:

    Stage 1 (extract): for entities the agent upserted in the piece, pull
    structured attributes. Stage 2 (discover, when ``discover``): find durable
    facts about entities the agent did NOT upsert and create/update them as
    dream-authored pages — the experience→knowledge bridge for non-declared
    entities. Stage 4 (learnings, when ``learnings``): mine durable
    preferences and corrections into feedback/stance/practice pages.

    The cursor moves past a piece when its stages have answered; an answer
    that cannot be parsed counts, since the model read the piece. A model
    call that fails (``ProviderError``) stops the session there with the
    cursor before that piece (``stopped`` / ``error`` in the result), and a
    ``deadline`` (``time.perf_counter()``) passed before the next piece does
    the same (``yielded``): the next run resumes at that piece.

    Stage 3 (skill_signals, when ``skill_signals``) runs once, after the last
    piece, over what this run read: it detects skill corrections/gaps and
    logs them as observations for the curation pass.

    ``pieces`` / ``span_chars`` / ``sent_chars`` in the result measure
    coverage: the pieces the cursor moved past, their characters, and how
    many of those the stage prompts carried.
    """
    # Imported here: that module imports this one for the session loader.
    from durin.memory.session_summary_dream import _format_turns

    jsonl_path = Path(jsonl_path)
    _meta, msgs = load_session(jsonl_path)
    cursor = get_extract_cursor(jsonl_path)
    total = len(msgs)
    if cursor > total:
        # The file shrank since the cursor was written (/new emptied it or the
        # file cap trimmed it): what is there now is a new conversation.
        cursor = 0
    if total <= cursor:
        return {"session": jsonl_path.stem, "skipped": "no_new_turns", "cursor": cursor}

    new_msgs = msgs[cursor:]                       # turns cursor+1 .. total
    turn = {id(m): i + 1 for i, m in enumerate(msgs)}

    def line(message: dict) -> str:
        rendered = _format_turns([message])
        return f"[turn-{turn[id(message)]}] {rendered}" if rendered else ""

    pieces = runs_that_fit(
        without_failure_placeholders(new_msgs), PIECE_CHARS, line=line, count=len)
    out: dict[str, Any] = {
        "session": jsonl_path.stem, "extracted": [], "discovered": [],
        "skill_signals": [], "learnings": [], "cursor": cursor,
        "new_turns": len(new_msgs), "pieces": 0, "span_chars": 0, "sent_chars": 0,
    }
    read: list[str] = []
    for n, piece in enumerate(pieces):
        if n and deadline is not None and _clock() >= deadline:
            out["yielded"] = True
            return out
        text = "\n".join(filter(None, map(line, piece)))
        end = turn[id(piece[-1])]
        src = f"[[sessions/{jsonl_path.stem}.md#turn-{end}]]"
        refs = entity_refs_in_messages(piece)
        try:
            for ref in refs:
                r = extract_entity(
                    workspace, ref, text,
                    llm_invoke=llm_invoke, model=model, source_ref=src,
                )
                out["extracted"].append({"ref": ref, "committed": r.committed})
            if discover:
                out["discovered"].extend(discover_entities(
                    workspace, text, existing_refs=refs,
                    llm_invoke=llm_invoke, model=model, source_ref=src,
                    confidence_threshold=confidence_threshold,
                    semantic_distance_threshold=semantic_distance_threshold,
                    alias_index=alias_index, vector_index=vector_index,
                ))
            if learnings:
                out["learnings"].extend(mine_learnings(
                    workspace, text, llm_invoke=llm_invoke, model=model, source_ref=src,
                    alias_index=alias_index, vector_index=vector_index,
                    confidence_threshold=confidence_threshold,
                    semantic_distance_threshold=semantic_distance_threshold))
        except ProviderError as exc:
            out["stopped"] = "provider_error"
            out["error"] = str(exc)
            return out
        set_extract_cursor(jsonl_path, end)        # advance per piece
        out["cursor"] = end
        out["pieces"] += 1
        out["span_chars"] += len(text)
        out["sent_chars"] += min(len(text), PIECE_CHARS)
        read.append(text)
    if skill_signals:
        from durin.agent.skill_signals import discover_skill_signals
        from durin.agent.skill_usage import extract_skill_calls
        out["skill_signals"] = discover_skill_signals(
            workspace, "\n".join(read),
            skill_loads=extract_skill_calls(new_msgs, workspace=workspace),
            llm_invoke=llm_invoke, model=model, session=jsonl_path.stem,
        )
    # What follows the last piece is turns that produced no answer, left out.
    set_extract_cursor(jsonl_path, total)
    out["cursor"] = total
    return out
