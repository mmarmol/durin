"""Extract dream (frequent) — the experience → knowledge bridge.

Reads raw conversation turns about an entity and extracts STRUCTURED
ATTRIBUTES, applying them as field author ``dream`` via ``memory_writer``
(design §2.6/§2.7, decision b: dream owns the attribute schema; the agent
owns name/aliases/relations/body). Per-field precedence (user > dream >
agent) means a user-set attribute is never overwritten by extraction.

This is the CORE extractor: ``extract_entity(workspace, ref, turns)``. The
discovery/orchestration (which sessions, which entities, the per-session
cursor) is a thin follow-on layer.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from json_repair import repair_json

from durin.memory.dream import LLMResponse, default_llm_invoke
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import WriteResult, write_entity

__all__ = ["build_extract_prompt", "parse_attributes", "extract_entity"]

LLMInvoke = Callable[..., Any]

_EXTRACT_PROMPT = """You are durin's memory extractor. From the conversation turns \
below, extract STRUCTURED ATTRIBUTES about the entity {ref} ({name}).

Rules:
- Only include facts explicitly stated in the turns. Do not invent or infer.
- Reuse an existing attribute key when the meaning matches (see EXISTING).
- Values are scalars or short lists of scalars — NO prose, NO nested objects.
- Output ONLY a JSON object mapping attribute_key -> value. No markdown, no commentary.

EXISTING ATTRIBUTE KEYS: {existing}

ENTITY BODY (prose the agent wrote — extract structure FROM it too):
{body}

CONVERSATION TURNS:
{turns}

JSON:"""


def build_extract_prompt(page: EntityPage, turns: str) -> str:
    return _EXTRACT_PROMPT.format(
        ref=page.entity_ref,
        name=page.name,
        existing=", ".join(sorted(page.attributes.keys())) or "(none)",
        body=(page.body or "(empty)")[:4000],
        turns=turns[:12000],
    )


def parse_attributes(raw: str) -> dict[str, Any]:
    """Tolerant parse of the LLM's JSON attribute object.

    Strips code fences, repairs small-model JSON quirks, and keeps only
    scalar / list-of-scalar values (drops prose blobs and nested dicts).
    """
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in obj.items():
        if isinstance(v, (str, int, float, bool)):
            out[str(k)] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            out[str(k)] = v
    return out


def extract_entity(
    workspace: Path,
    entity_ref: str,
    turns: str,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    source_ref: str | None = None,
) -> WriteResult:
    """Extract attributes for ``entity_ref`` from ``turns`` and apply as dream."""
    llm_invoke = llm_invoke or default_llm_invoke
    # §2.13: the extract dream respects a delete tombstone — it never re-creates
    # an entity the user deleted (the user overrides by explicitly re-authoring).
    from durin.memory.deletion import is_deleted
    if is_deleted(workspace, entity_ref):
        return WriteResult(entity_ref, committed=False, retries=0)
    root = Path(workspace) / "memory"
    type_, _, slug = entity_ref.partition(":")
    page_path = root / "entities" / type_ / f"{slug}.md"
    page = (
        EntityPage.from_file(page_path)
        if page_path.exists()
        else EntityPage(type=type_, name=slug)
    )

    prompt = build_extract_prompt(page, turns)
    resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
    raw = resp.text if isinstance(resp, LLMResponse) else str(resp)
    attrs = parse_attributes(raw)
    if not attrs:
        return WriteResult(entity_ref, committed=False, retries=0)

    now = datetime.now(timezone.utc)
    src = source_ref or "extract_dream"
    patches = [
        FieldPatch(kind="attribute", key=k, value=v, author="dream",
                   source_ref=src, at=now)
        for k, v in attrs.items()
    ]
    return write_entity(workspace, entity_ref, patches, create=True)
