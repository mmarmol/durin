"""Extract dream orchestration — run the extractor over a session's new turns.

Per-session cursor (stored in the session's ``.meta.json`` ``derived`` block):
process only turns after the cursor, discover the entities the agent authored
in those turns (``memory_upsert_entity`` tool calls), extract attributes for
each, and advance the cursor to the last turn. Re-running is safe — the
extractor's writes are idempotent under per-field precedence (design §2.6, 3A-1).

The discovery is precise (the agent's explicit upsert refs in the new turns);
mention-based discovery for entities the agent did not upsert is a refinement.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from durin.memory.extract_dream import discover_entities, extract_entity
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

__all__ = [
    "load_session",
    "entity_refs_in_messages",
    "get_extract_cursor",
    "set_extract_cursor",
    "run_extract_for_session",
]

LLMInvoke = Callable[..., Any]


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
    ``.meta.json`` sidecar (see docs/architecture/concurrency.md #15).
    Falls back to the legacy ``derived.extract_cursor`` location so that
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
    session's sidecar, preventing lost-update races (hazard #15-B).
    See docs/architecture/concurrency.md.
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
) -> dict:
    """Distil this session's new turns into entities (and skill signals).

    Stage 1 (extract): for entities the agent upserted, pull structured
    attributes. Stage 2 (discover, when ``discover``): find durable facts about
    entities the agent did NOT upsert and create/update them as dream-authored
    pages — the experience→knowledge bridge for non-declared entities.
    Stage 3 (skill_signals, when ``skill_signals``): detect skill corrections/gaps
    from the same turns and log them as observations for the curation pass.
    """
    jsonl_path = Path(jsonl_path)
    _meta, msgs = load_session(jsonl_path)
    cursor = get_extract_cursor(jsonl_path)
    total = len(msgs)
    if total <= cursor:
        return {"session": jsonl_path.stem, "skipped": "no_new_turns", "cursor": cursor}

    new_msgs = msgs[cursor:]                       # turns cursor+1 .. total
    text = "\n".join(
        f"{str(m.get('role') or '?').upper()}: {m.get('content')}"
        for m in new_msgs if m.get("content")
    )
    refs = entity_refs_in_messages(new_msgs)
    src = f"[[sessions/{jsonl_path.stem}.md#turn-{total}]]"
    extracted: list[dict] = []
    for ref in refs:
        r = extract_entity(
            workspace, ref, text,
            llm_invoke=llm_invoke, model=model, source_ref=src,
        )
        extracted.append({"ref": ref, "committed": r.committed})
    discovered: list[dict] = []
    if discover:
        discovered = discover_entities(
            workspace, text, existing_refs=refs,
            llm_invoke=llm_invoke, model=model, source_ref=src,
        )
    signals: list[dict] = []
    if skill_signals:
        from durin.agent.skill_signals import discover_skill_signals
        from durin.agent.skill_usage import extract_skill_calls
        signals = discover_skill_signals(
            workspace, text, skill_loads=extract_skill_calls(new_msgs),
            llm_invoke=llm_invoke, model=model, session=jsonl_path.stem,
        )
    set_extract_cursor(jsonl_path, total)          # advance per-batch
    return {
        "session": jsonl_path.stem,
        "extracted": extracted,
        "discovered": discovered,
        "skill_signals": signals,
        "cursor": total,
        "new_turns": len(new_msgs),
    }
