"""Dream pass: distil ingested reference documents into a structured outline.

Cold-path "know the book" index. For each reference document, group its
structure-aware chunks by breadcrumb (its ``Chapter › Section`` heading path),
then a single LLM call produces a whole-document abstract plus a one-to-two
sentence summary per section. The result is written to a
``memory/references/<slug>.outline.json`` sidecar next to the ``.chunks.jsonl``.

Idempotent per document: the outline records the ``chunk_count`` it was built
from, so a document is distilled once and re-distilled only when it is
re-ingested (its chunk count changes). No LLM is spent on the hot path.

The complementary entity pass (seeding entity pages with ``derived_from``
pointers) is a separate follow-on; this pass owns the outline artifact.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from json_repair import repair_json

from durin.memory.llm_invoke import LLMInvoke, default_llm_invoke
from durin.memory.reference import reference_chunks
from durin.utils.atomic_write import atomic_write_text

__all__ = [
    "build_outline_prompt",
    "outline_path_for",
    "parse_outline",
    "run_distill_reference_pass",
]

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
