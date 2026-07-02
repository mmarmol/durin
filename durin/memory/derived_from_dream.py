"""Derived-from dream (catch/repair) — link entities to their source documents.

``memory_upsert_entity(derived_from=...)`` (P3) is the primary path; the agent
links an entity to the document it was distilled from at write time. This pass
is the catch/repair: it reads a session, finds entities the agent upserted whose
``derived_from`` is still empty, the references ingested in that session, and
asks the LLM which document(s) each entity was built from — reasoning over the
conversation, not temporal adjacency. Links are applied as field author
``dream`` via ``memory_writer`` (so a user/agent link is never clobbered).

Idempotent: an entity that already carries a ``derived_from`` link is skipped,
so re-running a session is a no-op (and costs no LLM call) once linked.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from json_repair import repair_json

from durin.memory.entity_page import EntityPage
from durin.memory.extract_runner import entity_refs_in_messages, load_session
from durin.memory.field_patch import FieldPatch
from durin.memory.llm_invoke import LLMResponse, default_llm_invoke, emit_parse_failure
from durin.memory.memory_writer import write_entity

__all__ = [
    "reference_refs_in_session",
    "entities_missing_derived_from",
    "build_link_prompt",
    "parse_links",
    "link_derived_from_for_session",
]

LLMInvoke = Callable[..., Any]

_REF_RE = re.compile(r"reference:[a-z0-9][a-z0-9-]*")

_LINK_PROMPT = """You are durin's source-linker. Each entity below was authored \
during the conversation. Decide which SOURCE DOCUMENT(S) — if any — each entity \
was distilled from, using ONLY the conversation as evidence.

Rules:
- Link an entity to a document ONLY when the conversation shows the entity's \
content came from that document (it was ingested, then summarised into the \
entity). When unsure, leave it unlinked.
- Use only the reference ids listed in AVAILABLE DOCUMENTS. Never invent ids.
- Output ONLY a JSON object mapping entity_ref -> list of reference ids. Omit \
entities with no source. No markdown, no commentary.

AVAILABLE DOCUMENTS:
{documents}

ENTITIES (ref — body excerpt):
{entities}

CONVERSATION:
{turns}

JSON:"""


def _ref_title(workspace: Path, ref: str) -> str:
    """Best-effort title from a reference's frontmatter; falls back to slug."""
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    path = Path(workspace) / "memory" / "references" / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return slug
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                import yaml

                fm = yaml.safe_load(text[3:end])
            except Exception:  # noqa: BLE001
                fm = None
            if isinstance(fm, dict) and isinstance(fm.get("title"), str):
                t = fm["title"].strip()
                if t:
                    return t
    return slug


def reference_refs_in_session(
    workspace: Path, messages: list[dict],
) -> list[str]:
    """References ingested/mentioned in this session that EXIST on disk.

    With C1 the ``memory_ingest`` result emits ``reference:<slug>`` near the
    front (survives the 16 KB head-truncation), so scanning the session text
    reliably harvests them. Each candidate is confirmed against
    ``memory/references/<slug>.md`` to drop typos / hallucinated ids.
    """
    root = Path(workspace) / "memory" / "references"
    found: list[str] = []
    seen: set[str] = set()
    for m in messages:
        blob = json.dumps(m)  # covers content + tool_calls + tool results
        for ref in _REF_RE.findall(blob):
            if ref in seen:
                continue
            slug = ref.split(":", 1)[1]
            if (root / f"{slug}.md").exists():
                seen.add(ref)
                found.append(ref)
    return found


def entities_missing_derived_from(
    workspace: Path, refs: list[str],
) -> list[tuple[str, EntityPage]]:
    """``(ref, page)`` for ``refs`` that exist on disk and lack ``derived_from``.

    The ``ref`` is the authoritative on-disk reference (the agent's upsert ref =
    the filename slug). We deliberately do NOT use ``EntityPage.entity_ref``,
    which re-derives the slug from the *name* and can diverge from the filename
    (e.g. a renamed page) — writing back to that ref would miss the real file.
    """
    out: list[tuple[str, EntityPage]] = []
    root = Path(workspace) / "memory"
    for ref in refs:
        type_, _, slug = ref.partition(":")
        page_path = root / "entities" / type_ / f"{slug}.md"
        if not page_path.exists():
            continue
        try:
            page = EntityPage.from_file(page_path)
        except Exception:  # noqa: BLE001
            continue
        if page is not None and not page.derived_from:
            out.append((ref, page))
    return out


def build_link_prompt(
    workspace: Path,
    pages: list[tuple[str, EntityPage]],
    references: list[str],
    turns: str,
) -> str:
    documents = "\n".join(
        f"- {ref} — {_ref_title(workspace, ref)}" for ref in references
    )
    entities = "\n".join(
        f"- {ref} — {(page.body or '').strip()[:300]}" for ref, page in pages
    )
    return _LINK_PROMPT.format(
        documents=documents, entities=entities, turns=turns[:12000],
    )


def parse_links(
    raw: str, *, valid_refs: set[str], valid_entities: set[str],
) -> dict[str, list[str]] | None:
    """Tolerant parse of the LLM's ``{entity_ref: [reference_ref, ...]}`` map.

    Keeps only known entity refs mapped to known reference refs — the LLM can
    neither invent a document nor link an entity we didn't ask about.
    Returns ``None`` when the output cannot be parsed at all (unloadable
    JSON or wrong top-level type) — distinct from ``{}`` for a valid
    object with no usable links.
    """
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    out: dict[str, list[str]] = {}
    for ent, docs in obj.items():
        if ent not in valid_entities:
            continue
        if isinstance(docs, str):
            docs = [docs]
        if not isinstance(docs, list):
            continue
        kept = [d for d in docs if isinstance(d, str) and d in valid_refs]
        if kept:
            out[ent] = kept
    return out


def link_derived_from_for_session(
    workspace: Path,
    jsonl_path: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
) -> dict:
    """Link entities authored in this session to the document(s) they came from."""
    llm_invoke = llm_invoke or default_llm_invoke
    jsonl_path = Path(jsonl_path)
    _meta, msgs = load_session(jsonl_path)
    refs = entity_refs_in_messages(msgs)
    if not refs:
        return {"session": jsonl_path.stem, "skipped": "no_entities"}
    references = reference_refs_in_session(workspace, msgs)
    if not references:
        return {"session": jsonl_path.stem, "skipped": "no_references"}
    pages = entities_missing_derived_from(workspace, refs)
    if not pages:
        return {"session": jsonl_path.stem, "skipped": "all_linked"}

    turns = "\n".join(
        f"{str(m.get('role') or '?').upper()}: {m.get('content')}"
        for m in msgs if m.get("content")
    )
    prompt = build_link_prompt(workspace, pages, references, turns)
    resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
    raw = resp.text if isinstance(resp, LLMResponse) else str(resp)
    links = parse_links(
        raw,
        valid_refs=set(references),
        valid_entities={ref for ref, _page in pages},
    )
    if links is None:
        emit_parse_failure("derived_from", source=jsonl_path.stem, raw=raw)
        links = {}
    if not links:
        return {"session": jsonl_path.stem, "linked": []}

    now = datetime.now(timezone.utc)
    src = f"[[sessions/{jsonl_path.stem}.md#turn-{len(msgs)}]]"
    linked: list[dict] = []
    for entity_ref, doc_refs in links.items():
        patches = [
            FieldPatch(kind="derived_from", value=d, author="dream",
                       source_ref=src, at=now)
            for d in doc_refs
        ]
        result = write_entity(workspace, entity_ref, patches, create=False)
        linked.append({
            "ref": entity_ref,
            "derived_from": doc_refs,
            "committed": result.committed,
        })
    return {"session": jsonl_path.stem, "linked": linked}
