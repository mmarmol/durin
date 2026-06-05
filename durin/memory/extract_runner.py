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

from durin.memory.extract_dream import extract_entity

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
    mp = _meta_path(jsonl_path)
    if not mp.exists():
        return 0
    try:
        d = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return int((d.get("derived") or {}).get("extract_cursor") or 0)


def set_extract_cursor(jsonl_path: Path, n: int) -> None:
    mp = _meta_path(jsonl_path)
    d: dict = {}
    if mp.exists():
        try:
            d = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    d.setdefault("derived", {})["extract_cursor"] = n
    mp.write_text(json.dumps(d, indent=2), encoding="utf-8")


def run_extract_for_session(
    workspace: Path,
    jsonl_path: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
) -> dict:
    """Extract attributes for entities authored in this session's new turns."""
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
    set_extract_cursor(jsonl_path, total)          # advance per-batch
    return {
        "session": jsonl_path.stem,
        "extracted": extracted,
        "cursor": total,
        "new_turns": len(new_msgs),
    }
