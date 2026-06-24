"""Extract dream (frequent) — the experience → knowledge bridge.

Reads raw conversation turns about an entity and extracts STRUCTURED
ATTRIBUTES, applying them as field author ``dream`` via ``memory_writer``
(dream owns the attribute schema; the agent owns name/aliases/relations/body).
Per-field precedence (user > dream > agent) means a user-set attribute is
never overwritten by extraction.

This is the CORE extractor: ``extract_entity(workspace, ref, turns)``. The
discovery/orchestration (which sessions, which entities, the per-session
cursor) is a thin follow-on layer.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from json_repair import repair_json

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.llm_invoke import default_llm_invoke
from durin.memory.memory_writer import WriteResult, write_entity

__all__ = [
    "build_extract_prompt", "parse_attributes", "extract_entity",
    "build_discover_prompt", "parse_discoveries", "discover_entities",
]

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
    # The extract dream respects a delete tombstone — it never re-creates
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
    raw = resp.text if hasattr(resp, "text") else str(resp)
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
    result = write_entity(workspace, entity_ref, patches, create=True)
    # Dream telemetry: reuse the legacy event so existing dashboards keep
    # counting consolidations. Best-effort.
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event("memory.dream.patch_applied", {
            "entity_ref": entity_ref,
            "ops_applied": len(attrs),
            "trigger": "extract",
            "committed": result.committed,
            "source_ref": src,
        })
    except Exception:  # pragma: no cover
        pass
    return result


_DISCOVER_PROMPT = """You are durin's memory discovery pass. From the conversation \
turns below, identify entities (people, organizations, places, projects, topics) that \
carry a DURABLE fact worth remembering long-term, and output them as structured proposals.

Rules:
- Include ONLY durable, identity-defining facts: who/what an entity is, stable roles or
  relationships, lasting preferences, commitments and deadlines, life events.
- EXCLUDE ephemeral task details, transient state, speculation, and small talk.
- Only facts explicitly stated in the turns. Do not invent or infer.
- Each entity is an object with:
  - "ref": "<type>:<slug>" — lowercase ascii slug; type one of
    person/place/project/topic/organization/event/artifact/stance/practice
  - "name": the display name
  - "aliases": optional array of OTHER names/spellings for this entity that appear
    in the turns (e.g. the conversation used both "Torrent" and "Torrente"). Do
    not invent names that are not present.
  - "relations": optional array of {{"to": "<type>:<slug>", "type": "<relation>"}}
    linking this entity to ANOTHER entity mentioned in the turns
    (e.g. {{"to": "place:valencia", "type": "located_in"}}).
  - "significance": optional ONE sentence on WHY this entity matters to the user /
    their relationship to it (e.g. "a place the user tracks the weather for").
    Omit it unless the turns state such a reason. Do NOT restate the attributes.
  - "turn": the turn number (the [turn-N] tag) where this entity's durable fact
    appears.
  - "attributes": a JSON object of scalar or short-list values — NO prose, NO nested objects
- Output ONLY a JSON array of these objects. If nothing durable is stated, output [].

CONVERSATION TURNS:
{turns}

JSON:"""


def build_discover_prompt(turns: str) -> str:
    return _DISCOVER_PROMPT.format(turns=turns[:12000])


def parse_discoveries(raw: str) -> list[dict[str, Any]]:
    """Tolerant parse of the discovery LLM's JSON array of entity proposals.

    Each item needs a well-formed ``ref`` (``<type>:<slug>``) and a non-empty
    ``name``; ``attributes`` are filtered through :func:`parse_attributes`
    (scalars / lists of scalars only). Optional fields ``aliases``, ``relations``,
    ``significance``, and ``turn`` are included with malformed sub-values dropped.
    Malformed items are dropped, not raised.
    """
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except Exception:
        return []
    if not isinstance(obj, list):
        return []
    out: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        name = str(item.get("name") or "").strip()
        if not ref or ":" not in ref or not name:
            continue
        attrs_raw = item.get("attributes")
        attrs = (
            parse_attributes(json.dumps(attrs_raw))
            if isinstance(attrs_raw, dict) else {}
        )
        aliases = [a.strip() for a in (item.get("aliases") or [])
                   if isinstance(a, str) and a.strip()]
        relations = [
            {"to": r["to"].strip(), "type": str(r.get("type") or "").strip()}
            for r in (item.get("relations") or [])
            if isinstance(r, dict) and isinstance(r.get("to"), str)
            and ":" in r.get("to", "") and str(r.get("type") or "").strip()
        ]
        sig_raw = item.get("significance")
        significance = sig_raw.strip() if isinstance(sig_raw, str) and sig_raw.strip() else None
        turn_raw = item.get("turn")
        turn = turn_raw if isinstance(turn_raw, int) and turn_raw > 0 else None
        out.append({"ref": ref, "name": name, "attributes": attrs,
                    "aliases": aliases, "relations": relations,
                    "significance": significance, "turn": turn})
    return out


def _resolve_semantic_ref(
    workspace: Path, vector_index: object, proposed_ref: str, name: str,
    attributes: dict, *, llm_invoke, model, confidence_threshold: int,
    distance_threshold: float,
) -> str | None:
    """An embedding-near existing same-type entity the judge confirms is the
    same identity as this proposal — else None. Catches a variant-name
    duplicate at birth when lexical name matching missed it. Best-effort:
    any vector/judge failure falls back to None (create).

    The index is the run's start-of-pass snapshot, so an entity created
    earlier in the SAME run isn't visible here — same-run cross-session
    variants are caught by the nightly refine pass instead."""
    from durin.memory.absorb_judge import JudgeError, judge_pair
    from durin.memory.deletion import is_deleted
    from durin.memory.vector_index import VectorIndex
    type_ = proposed_ref.split(":", 1)[0]
    query = VectorIndex._compose_entity_page_text(
        name=name, aliases=[], body="", attributes=attributes, relations=[])
    try:
        # top_k=5: the nearest few neighbours; type-filtered below. A few extra
        # covers the case where closer neighbours are a different type.
        rows = vector_index.search(query, top_k=5)
    except Exception:  # noqa: BLE001
        return None
    for row in rows:
        ref = row.get("id")
        if (not isinstance(ref, str) or ref == proposed_ref
                or row.get("class_name") != "entity_page"
                or ref.split(":", 1)[0] != type_):
            continue
        if float(row.get("_distance", 1.0)) > distance_threshold:
            return None  # nearest same-type neighbour already too far
        if is_deleted(workspace, ref):
            continue
        ctype, _, cslug = ref.partition(":")
        candidate = EntityPage.from_file(
            Path(workspace) / "memory" / "entities" / ctype / f"{cslug}.md")
        if candidate is None:
            continue
        transient = EntityPage(type=type_, name=name, attributes=dict(attributes))
        try:
            judged = judge_pair(
                candidate, transient, [], llm_invoke=llm_invoke, model=model,
                canonical_ref=ref, absorbed_ref=proposed_ref)
        except JudgeError:
            return None
        return ref if (judged.verdict == "same"
                       and judged.confidence >= confidence_threshold) else None
    return None


def _resolve_existing_ref(index, proposed_ref: str, name: str) -> str | None:
    """The single existing same-type entity that already owns this name/slug,
    or None. None when there is no match OR when the match is ambiguous (>1
    existing entity shares the name) — ambiguity defers to refine + the judge,
    preserving deliberate same-name disambiguation (person:marcelo_marmol vs
    person:marcelo_diaz)."""
    type_, _, slug = proposed_ref.partition(":")
    matches: set[str] = set()
    for key in (name, slug):
        if not key:
            continue
        for ref in index.lookup(key):
            if ref != proposed_ref and ref.split(":", 1)[0] == type_:
                matches.add(ref)
    return next(iter(matches)) if len(matches) == 1 else None


def discover_entities(
    workspace: Path,
    turns: str,
    *,
    existing_refs: Iterable[str] = (),
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    source_ref: str | None = None,
    alias_index: "AliasIndex | None" = None,
    vector_index: object | None = None,
    confidence_threshold: int = 95,
    semantic_distance_threshold: float = 0.20,
) -> list[dict[str, Any]]:
    """Discover entities mentioned in ``turns`` that the agent did NOT upsert and
    write them as dream-authored pages.

    Skips refs already handled by the precise extract stage (``existing_refs``)
    and refs under a delete tombstone (a user-deleted entity is never re-created).
    Discovered ``name`` is set via ``write_entity(name=...)`` (last-writer-wins,
    so a later agent/user correction simply overwrites it); attributes are
    ``author="dream"`` so user/agent values keep precedence.
    """
    from durin.memory.deletion import is_deleted
    llm_invoke = llm_invoke or default_llm_invoke
    if not turns.strip():
        return []
    skip = set(existing_refs)
    prompt = build_discover_prompt(turns)
    resp = llm_invoke(prompt, model=model) if model else llm_invoke(prompt)
    raw = resp.text if hasattr(resp, "text") else str(resp)
    proposals = parse_discoveries(raw)

    index = alias_index
    if index is None:
        index = AliasIndex(Path(workspace) / "memory")
        index.build()

    now = datetime.now(timezone.utc)
    src = source_ref or "discover_dream"
    out: list[dict[str, Any]] = []
    for prop in proposals:
        ref = prop["ref"]
        if ref in skip or is_deleted(workspace, ref):
            continue
        patches = [
            FieldPatch(kind="attribute", key=k, value=v, author="dream",
                       source_ref=src, at=now)
            for k, v in prop["attributes"].items()
        ]
        for al in prop.get("aliases", []):
            patches.append(FieldPatch(kind="alias", value=al, author="dream",
                                      source_ref=src, at=now))
        for rel in prop.get("relations", []):
            patches.append(FieldPatch(kind="relation", value=rel, author="dream",
                                      source_ref=src, at=now))
        sig = prop.get("significance")
        if sig:
            patches.append(FieldPatch(kind="body_replace", value=sig, author="dream",
                                      source_ref=src, at=now))
        target = _resolve_existing_ref(index, ref, prop["name"])
        if target is None and vector_index is not None:
            target = _resolve_semantic_ref(
                workspace, vector_index, ref, prop["name"], prop["attributes"],
                llm_invoke=llm_invoke, model=model,
                confidence_threshold=confidence_threshold,
                distance_threshold=semantic_distance_threshold)
        if target is not None:
            # Existing same-type entity already owns this name — update it in
            # place instead of minting a duplicate slug. Leave its name alone.
            result = write_entity(workspace, target, patches, create=False)
            written = target
        else:
            result = write_entity(
                workspace, ref, patches, create=True, name=prop["name"])
            written = ref
        # Best-effort: keep the index current so a later proposal in this same
        # pass resolves against what we just wrote. A failed re-read is harmless
        # — the next call's build() rebuilds the index from disk.
        type_, _, slug = written.partition(":")
        try:
            page = EntityPage.from_file(
                Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md")
        except OSError:
            page = None
        if page is not None:
            index.refresh_for(page, slug)
        out.append({"ref": written, "committed": result.committed})

    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event("memory.dream.discover", {
            "proposed": len(proposals),
            "written": sum(1 for r in out if r["committed"]),
            "skipped": len(proposals) - len(out),
            "refs": [r["ref"] for r in out],
        })
    except Exception:  # pragma: no cover
        pass
    return out
