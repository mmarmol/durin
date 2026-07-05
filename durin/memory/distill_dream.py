"""Dream pass: distil ingested reference documents into a structured outline.

Cold-path "know the book" index. For each reference document, group its
structure-aware chunks by breadcrumb (its ``Chapter › Section`` heading path),
then a single LLM call produces a whole-document abstract plus a one-to-two
sentence summary per section. The result is written to a
``memory/references/<slug>.outline.json`` sidecar next to the ``.chunks.jsonl``.

Idempotent per document: the outline records the ``chunk_count`` it was built
from, so a document is distilled once and re-distilled only when it is
re-ingested (its chunk count changes). No LLM is spent on the hot path.

A second pass, :func:`run_seed_entities_pass`, reads each document's outline
and seeds candidate entity pages (with ``derived_from`` pointers back to the
document) — the bridge that carries document knowledge into the entity graph
and, distilled, into default recall. The refine pass dedups them against
existing entities.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from json_repair import repair_json

from durin.memory.entities import SUGGESTED_TYPES_ORDERED
from durin.memory.entity_manifest import build_entity_manifest
from durin.memory.extract_dream import parse_discoveries
from durin.memory.field_patch import FieldPatch
from durin.memory.llm_invoke import LLMInvoke, default_llm_invoke
from durin.memory.memory_writer import write_entity
from durin.memory.reference import reference_chunks
from durin.utils.atomic_write import atomic_write_text

__all__ = [
    "build_outline_prompt",
    "build_seed_prompt",
    "build_topics_prompt",
    "outline_path_for",
    "parse_outline",
    "parse_topics",
    "run_curate_topics_pass",
    "run_distill_reference_pass",
    "run_seed_entities_pass",
    "topics_path_for",
]

# Cap on the curated library topic index — coherent themes, not per-document
# granular topics, so the always-on "Covers:" map stays bounded and scannable.
_MAX_TOPICS = 24

# Max entities seeded per document — selective by design, so a single book does
# not flood the entity graph with hundreds of incidental mentions.
_MAX_ENTITIES_PER_DOC = 20

# Total document text handed to one outline call. Mirrors the discover pass's
# turn cap — keeps the prompt within a small model's context; a document larger
# than this is summarised from its head (noted in the return payload).
_MAX_PROMPT_CHARS = 12000
_PER_SECTION_CHARS = 1500


_OUTLINE_PROMPT = """You are durin's document outline pass. Summarise the \
document below into a compact structured outline a reader can scan to know what \
the document covers.

Return ONLY JSON of the form:
{{"abstract": "<1-2 sentence summary of the WHOLE document>",
  "sections": {{"<breadcrumb>": "<1-2 sentence summary of that section>"}}}}

Rules:
- Use the EXACT breadcrumb strings given as the keys of "sections".
- Summarise only what the text states; do not invent or add outside knowledge.
- Keep each summary to one or two sentences.

DOCUMENT: {title}

SECTIONS:
{sections}

JSON:"""


def outline_path_for(workspace: Path, slug: str) -> Path:
    return Path(workspace) / "memory" / "references" / f"{slug}.outline.json"


def _sections_from_chunks(chunks: list[dict]) -> list[tuple[str, list[int], str]]:
    """Group chunks by breadcrumb, preserving document order.

    Returns ``[(breadcrumb, [chunk_idx, ...], joined_text), ...]``. Chunks with
    an empty breadcrumb (document preamble before any heading) group under
    ``""``.
    """
    order: list[str] = []
    by_crumb: dict[str, list[dict]] = {}
    for rec in chunks:
        crumb = str(rec.get("breadcrumb") or "")
        if crumb not in by_crumb:
            by_crumb[crumb] = []
            order.append(crumb)
        by_crumb[crumb].append(rec)
    out: list[tuple[str, list[int], str]] = []
    for crumb in order:
        recs = by_crumb[crumb]
        idxs = [int(r["idx"]) for r in recs]
        text = "\n\n".join(str(r.get("text") or "") for r in recs)
        out.append((crumb, idxs, text))
    return out


def build_outline_prompt(
    title: str, sections: list[tuple[str, list[int], str]]
) -> str:
    blocks: list[str] = []
    used = 0
    for crumb, _idxs, text in sections:
        label = crumb or "(document preamble)"
        body = text[:_PER_SECTION_CHARS]
        block = f"### {label}\n{body}"
        used += len(block)
        if used > _MAX_PROMPT_CHARS:
            break
        blocks.append(block)
    return _OUTLINE_PROMPT.format(title=title or "(untitled)",
                                  sections="\n\n".join(blocks))


def parse_outline(raw: str) -> Optional[dict[str, Any]]:
    """Tolerant parse of the outline LLM's JSON object.

    Returns ``{"abstract": str, "sections": {breadcrumb: summary}}`` or ``None``
    when the output cannot be parsed at all (unloadable JSON / wrong shape).
    """
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    abstract = str(obj.get("abstract") or "").strip()
    sections_raw = obj.get("sections")
    sections: dict[str, str] = {}
    if isinstance(sections_raw, dict):
        for k, v in sections_raw.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                sections[k] = v.strip()
    if not abstract and not sections:
        return None
    return {"abstract": abstract, "sections": sections}


def _title_of(workspace: Path, slug: str) -> str:
    md = Path(workspace) / "memory" / "references" / f"{slug}.md"
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return slug
    m = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else slug


def run_distill_reference_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    max_seconds: int = 0,
) -> dict[str, Any]:
    """Distil each reference document into a ``<slug>.outline.json`` sidecar.

    Idempotent: a document whose outline already matches its current
    ``chunk_count`` is skipped. Returns a summary dict for the cron log.
    """
    invoke = llm_invoke or default_llm_invoke
    refs_dir = Path(workspace) / "memory" / "references"
    started = time.monotonic()
    outlined = 0
    skipped = 0
    errors: list[str] = []
    total = 0

    if refs_dir.is_dir():
        for md_path in sorted(refs_dir.glob("*.md")):
            total += 1
            if max_seconds and (time.monotonic() - started) > max_seconds:
                break
            slug = md_path.stem
            ref = f"reference:{slug}"
            chunks = reference_chunks(workspace, ref)
            if not chunks:
                skipped += 1
                continue
            out_path = outline_path_for(workspace, slug)
            if out_path.exists():
                try:
                    prev = json.loads(out_path.read_text(encoding="utf-8"))
                    if int(prev.get("chunk_count") or -1) == len(chunks):
                        skipped += 1
                        continue
                except Exception:
                    pass  # unreadable/stale outline → re-distil

            sections = _sections_from_chunks(chunks)
            title = _title_of(workspace, slug)
            prompt = build_outline_prompt(title, sections)
            try:
                resp = invoke(prompt, model=model)
                raw = resp.text if hasattr(resp, "text") else str(resp)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{ref}: {exc}")
                continue
            parsed = parse_outline(str(raw))
            if parsed is None:
                errors.append(f"{ref}: unparseable outline")
                continue

            summaries = parsed["sections"]
            outline = {
                "ref": ref,
                "title": title,
                "chunk_count": len(chunks),
                "abstract": parsed["abstract"],
                "sections": [
                    {
                        "breadcrumb": crumb,
                        "summary": summaries.get(
                            crumb or "(document preamble)", summaries.get(crumb, "")
                        ),
                        "chunk_indices": idxs,
                    }
                    for crumb, idxs, _text in sections
                ],
            }
            if model:
                outline["model"] = model
            try:
                atomic_write_text(out_path, json.dumps(outline, indent=2))
                outlined += 1
            except OSError as exc:
                errors.append(f"{ref}: write failed: {exc}")

    return {
        "references": total,
        "outlined": outlined,
        "skipped": skipped,
        "errors": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


_SEED_PROMPT = """You are durin's document knowledge pass. From the document below, \
extract the KEY entities it is ABOUT — the people, organisations, concepts, works, \
places, projects, or topics a reader would want durin to remember this document covers.

Rules:
- Be SELECTIVE: at most {cap} entities, the most CENTRAL ones. Skip incidental
  mentions, examples, and generic terms.
- Each entity is an object with:
  - "ref": "<type>:<slug>" — lowercase ascii slug; type one of {types}
  - "name": the display name
  - "aliases": optional array of other names/spellings for this entity present in the document
  - "relations": optional array of {{"to": "<type>:<slug>", "type": "<relation>"}} linking
    this entity to ANOTHER entity the document is about
  - "significance": ONE sentence on what this entity is and its role in the document
  - "attributes": optional JSON object of scalar or short-list values — NO prose, NO nested objects
- Only what the document states; do not invent or add outside knowledge.
- Output ONLY a JSON array. If nothing central, output [].

KNOWN ENTITIES — if the document is about one of these, reuse its EXACT ref:
{existing}

DOCUMENT: {title}
ABSTRACT: {abstract}

SECTIONS:
{sections}

JSON:"""


def build_seed_prompt(
    title: str,
    abstract: str,
    sections: list[tuple[str, str]],
    *,
    existing: str = "",
    cap: int = _MAX_ENTITIES_PER_DOC,
) -> str:
    sec = "\n".join(
        f"- {crumb or '(preamble)'}: {summary}" for crumb, summary in sections
    )
    return _SEED_PROMPT.format(
        cap=cap,
        types="/".join(SUGGESTED_TYPES_ORDERED),
        existing=existing.strip() or "(none yet)",
        title=title or "(untitled)",
        abstract=abstract or "",
        sections=sec[:8000],
    )


def run_seed_entities_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    max_seconds: int = 0,
) -> dict[str, Any]:
    """Seed candidate entity pages from each reference's distilled outline.

    Reads the ``<slug>.outline.json`` written by :func:`run_distill_reference_pass`,
    asks one selective LLM call for the KEY entities the document is about, and
    writes them as dream-authored pages stamped ``derived_from`` = the document.
    Idempotent per document via an ``entities_seeded_chunk_count`` marker on the
    outline; the refine pass dedups against existing entities.
    """
    from durin.memory.deletion import is_deleted

    invoke = llm_invoke or default_llm_invoke
    refs_dir = Path(workspace) / "memory" / "references"
    started = time.monotonic()
    seeded_docs = 0
    entities = 0
    skipped = 0
    errors: list[str] = []
    total = 0

    if refs_dir.is_dir():
        for md_path in sorted(refs_dir.glob("*.md")):
            slug = md_path.stem
            out_path = outline_path_for(workspace, slug)
            if not out_path.exists():
                continue  # not distilled yet — nothing to seed from
            total += 1
            if max_seconds and (time.monotonic() - started) > max_seconds:
                break
            try:
                outline = json.loads(out_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            chunk_count = int(outline.get("chunk_count") or 0)
            if int(outline.get("entities_seeded_chunk_count") or -1) == chunk_count:
                skipped += 1
                continue

            doc_ref = f"reference:{slug}"
            sections = [
                (str(s.get("breadcrumb") or ""), str(s.get("summary") or ""))
                for s in outline.get("sections", [])
            ]
            abstract = str(outline.get("abstract") or "")
            title = str(outline.get("title") or slug)
            manifest = build_entity_manifest(
                workspace, query=f"{title}\n{abstract}", limit=20)
            prompt = build_seed_prompt(
                title, abstract, sections, existing=manifest)
            try:
                resp = invoke(prompt, model=model)
                raw = resp.text if hasattr(resp, "text") else str(resp)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{doc_ref}: {exc}")
                continue
            proposals = parse_discoveries(str(raw)) or []

            now = datetime.now(timezone.utc)
            src = f"[[references/{slug}.md]]"
            for prop in proposals[:_MAX_ENTITIES_PER_DOC]:
                ref = prop["ref"]
                if is_deleted(workspace, ref):
                    continue
                patches = [
                    FieldPatch(kind="attribute", key=k, value=v, author="dream",
                               source_ref=src, at=now)
                    for k, v in prop["attributes"].items()
                ]
                patches += [
                    FieldPatch(kind="alias", value=al, author="dream",
                               source_ref=src, at=now)
                    for al in prop.get("aliases", [])
                ]
                patches += [
                    FieldPatch(kind="relation", value=rel, author="dream",
                               source_ref=src, at=now)
                    for rel in prop.get("relations", [])
                ]
                sig = prop.get("significance")
                if sig:
                    patches.append(FieldPatch(kind="body_replace", value=sig,
                                              author="dream", source_ref=src, at=now))
                patches.append(FieldPatch(kind="derived_from", value=doc_ref,
                                          author="dream", source_ref=src, at=now))
                try:
                    write_entity(workspace, ref, patches, create=True,
                                 name=prop["name"])
                    entities += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{ref}: write failed: {exc}")

            outline["entities_seeded_chunk_count"] = chunk_count
            try:
                atomic_write_text(out_path, json.dumps(outline, indent=2))
                seeded_docs += 1
            except OSError as exc:
                errors.append(f"{doc_ref}: marker write failed: {exc}")

    return {
        "references": total,
        "seeded_docs": seeded_docs,
        "entities": entities,
        "skipped": skipped,
        "errors": errors,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


_TOPICS_PROMPT = """You are durin's library topic-index pass. Curate a clean, \
STABLE index of the subjects the user's document library covers — the short map a \
reader scans to know what is in the library.

You are given every document (title + abstract) and the CURRENT index. Return the \
updated index.

Rules:
- Use coherent THEME labels, not granular sub-topics. Fold synonyms and \
translations into ONE label (e.g. "Paraprostatic cysts" and "Quistes \
paraprostaticos" are the same theme), and roll specific procedures or findings up \
under the theme they belong to.
- REUSE the current index's labels wherever they still fit — do NOT rename them or \
coin near-duplicates. Add a topic only for a genuinely new theme; drop a topic \
whose documents are all gone. Stability matters: the index must not drift run to run.
- Assign each document to 1-3 topics by its EXACT slug.
- Order topics broadest-first (most central to the library first).

Return ONLY JSON: {{"topics": [{{"label": "<theme>", "docs": ["<slug>", ...]}}]}}

CURRENT INDEX:
{current}

DOCUMENTS:
{documents}

JSON:"""


def topics_path_for(workspace: Path) -> Path:
    return Path(workspace) / "memory" / "references" / "_topics.json"


def build_topics_prompt(
    documents: list[tuple[str, str, str]],
    current: list[dict[str, Any]],
) -> str:
    """``documents`` = [(slug, title, abstract), ...]; ``current`` = the stored
    ``topics`` list, fed back so the LLM reuses labels instead of drifting."""
    docs_block = "\n".join(
        f"- {slug}: {title} — {abstract}" for slug, title, abstract in documents
    )
    cur_block = "\n".join(
        f"- {t.get('label')}: {', '.join(t.get('docs') or [])}" for t in current
    ) or "(none yet)"
    return _TOPICS_PROMPT.format(
        current=cur_block[:4000], documents=docs_block[:_MAX_PROMPT_CHARS],
    )


def parse_topics(
    raw: str, valid_slugs: set[str]
) -> Optional[list[dict[str, Any]]]:
    """Tolerant parse of the topic-index JSON.

    Returns ``[{"label": str, "docs": [slug, ...]}, ...]`` — deduped by label,
    docs filtered to ``valid_slugs``, topics with no valid doc dropped, capped at
    :data:`_MAX_TOPICS`. Returns ``None`` only when the output is unparseable or
    the wrong shape (an empty list is a valid result for an empty library)."""
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(repair_json(s))
    except Exception:
        return None
    topics_raw = obj.get("topics") if isinstance(obj, dict) else obj
    if not isinstance(topics_raw, list):
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in topics_raw:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()
        if not label or label.lower() in seen:
            continue
        docs = [str(d) for d in (t.get("docs") or []) if str(d) in valid_slugs]
        if not docs:
            continue
        seen.add(label.lower())
        out.append({"label": label, "docs": docs})
        if len(out) >= _MAX_TOPICS:
            break
    return out


def run_curate_topics_pass(
    workspace: Path,
    *,
    llm_invoke: LLMInvoke | None = None,
    model: str | None = None,
    max_seconds: int = 0,
) -> dict[str, Any]:
    """Curate the library's topic index (``memory/references/_topics.json``).

    One LLM call over the distilled documents' abstracts produces a clean, stable
    set of theme labels with the documents under each — the "map" the always-on
    Library awareness reads (:func:`principal.build_library_awareness`). The
    current index is fed back so the LLM reuses labels instead of regenerating
    (curation, not drift). Idempotent: skipped when the set of distilled
    documents is unchanged (signature = each doc's slug + chunk_count).
    """
    invoke = llm_invoke or default_llm_invoke
    refs_dir = Path(workspace) / "memory" / "references"
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if not refs_dir.is_dir():
        return {"topics": 0, "skipped": True, "duration_ms": _elapsed()}

    documents: list[tuple[str, str, str]] = []
    signature: list[str] = []
    for md_path in sorted(refs_dir.glob("*.md")):
        slug = md_path.stem
        out_path = outline_path_for(workspace, slug)
        if not out_path.exists():
            continue  # only distilled documents carry an abstract to theme
        try:
            outline = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        documents.append(
            (slug, str(outline.get("title") or slug),
             str(outline.get("abstract") or "")))
        signature.append(f"{slug}:{int(outline.get('chunk_count') or 0)}")

    if not documents:
        return {"topics": 0, "skipped": True, "duration_ms": _elapsed()}

    tpath = topics_path_for(workspace)
    current: list[dict[str, Any]] = []
    if tpath.exists():
        try:
            prev = json.loads(tpath.read_text(encoding="utf-8"))
            if prev.get("signature") == signature:
                return {"topics": len(prev.get("topics", [])),
                        "skipped": True, "duration_ms": _elapsed()}
            current = prev.get("topics", []) or []
        except Exception:
            pass  # unreadable/stale index → re-curate from scratch

    if max_seconds and _elapsed() > max_seconds * 1000:
        return {"topics": 0, "skipped": True, "duration_ms": _elapsed()}

    valid = {slug for slug, _t, _a in documents}
    prompt = build_topics_prompt(documents, current)
    try:
        resp = invoke(prompt, model=model)
        raw = resp.text if hasattr(resp, "text") else str(resp)
    except Exception as exc:  # noqa: BLE001
        return {"topics": 0, "errors": [str(exc)], "duration_ms": _elapsed()}

    topics = parse_topics(str(raw), valid)
    if topics is None:
        return {"topics": 0, "errors": ["unparseable topics"],
                "duration_ms": _elapsed()}

    payload: dict[str, Any] = {
        "topics": topics, "signature": signature, "doc_count": len(documents),
    }
    if model:
        payload["model"] = model
    try:
        atomic_write_text(tpath, json.dumps(payload, indent=2))
    except OSError as exc:
        return {"topics": 0, "errors": [f"write failed: {exc}"],
                "duration_ms": _elapsed()}
    return {"topics": len(topics), "documents": len(documents),
            "skipped": False, "duration_ms": _elapsed()}
