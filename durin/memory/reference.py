"""References — coherent ingested documents kept WHOLE.

A reference is stored intact (never synthesized by dream) under
``memory/references/<slug>.md`` with a REFERENCE marker. A token-aware chunk
index (<=512 tokens each — the e5-small embedder's max_seq) is written
alongside as a ``.chunks.jsonl`` sidecar, every chunk carrying a ``parent``
pointer back to the reference so a fragment hit can pull the whole document.

The whole doc is the FTS unit; the chunks are the vector unit.
Wiring the chunks into the live FTS/vector index is a follow-on; this module
owns the storage model + token-aware chunking.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from durin.utils.atomic_write import atomic_write_text
from durin.utils.helpers import estimate_text_tokens

__all__ = [
    "ReferenceResult",
    "chunk_by_tokens",
    "ingest_reference",
    "load_reference",
    "reference_chunks",
    "reference_marker",
]

_MAX_CHUNK_TOKENS = 512


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "untitled"


def chunk_by_tokens(text: str, max_tokens: int = _MAX_CHUNK_TOKENS) -> list[str]:
    """Greedy token-aware chunking: pack paragraphs until <=max_tokens; split
    an oversize paragraph by sentence, then by char window as a last resort."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""

    def flush() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for p in paras:
        if estimate_text_tokens(p) > max_tokens:
            flush()
            chunks.extend(_split_oversize(p, max_tokens))
            continue
        candidate = (cur + "\n\n" + p) if cur else p
        if estimate_text_tokens(candidate) > max_tokens:
            flush()
            cur = p
        else:
            cur = candidate
    flush()
    return chunks


def _split_oversize(p: str, max_tokens: int) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+", p)
    out: list[str] = []
    cur = ""
    for s in sents:
        cand = (cur + " " + s).strip() if cur else s
        if estimate_text_tokens(cand) > max_tokens:
            if cur:
                out.append(cur)
                cur = s
            else:                                  # single sentence too big
                out.extend(_char_windows(s, max_tokens))
                cur = ""
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _char_windows(s: str, max_tokens: int) -> list[str]:
    budget = max_tokens * 4                          # ~4 chars/token approx
    return [s[i:i + budget] for i in range(0, len(s), budget)] or [s]


@dataclass
class ReferenceResult:
    ref: str
    path: str
    chunk_count: int


def ingest_reference(workspace: Path, title: str, content: str,
                     *, source: str | None = None) -> ReferenceResult:
    """Store ``content`` whole as a REFERENCE + write the token-aware chunk index."""
    root = Path(workspace) / "memory" / "references"
    root.mkdir(parents=True, exist_ok=True)
    slug = _slug(title)
    ref = f"reference:{slug}"
    now = datetime.now(timezone.utc).isoformat()
    chunks = chunk_by_tokens(content)

    fm = {
        "type": "reference", "title": title, "source": source or "",
        "ingested_at": now, "chunk_count": len(chunks),
    }
    doc = f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n{content.strip()}\n"
    atomic_write_text(root / f"{slug}.md", doc)

    chunk_recs = [
        {"idx": i, "parent": ref, "tokens": estimate_text_tokens(c), "text": c}
        for i, c in enumerate(chunks)
    ]
    atomic_write_text(
        root / f"{slug}.chunks.jsonl",
        "\n".join(json.dumps(r) for r in chunk_recs))
    return ReferenceResult(ref=ref, path=str(root / f"{slug}.md"),
                           chunk_count=len(chunks))


def load_reference(workspace: Path, ref: str) -> str | None:
    """Return the whole reference document (with frontmatter), or None."""
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    p = Path(workspace) / "memory" / "references" / f"{slug}.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def reference_chunks(workspace: Path, ref: str) -> list[dict]:
    """Return the chunk index records (each with a parent pointer)."""
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    p = Path(workspace) / "memory" / "references" / f"{slug}.chunks.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def reference_marker(ref: str, *, title: str = "") -> str:
    """Structural marker telling the LLM this is a coherent reference doc."""
    suffix = f" ({title})" if title else ""
    return f"=== REFERENCE: {ref}{suffix} ==="
