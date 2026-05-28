"""Phase-1 grep over memory entries, session views, and ingested artifacts.

Scopes:

- ``dreamed``: ``memory/<class>/*.md`` — matches against frontmatter
  (headline / summary / entities) plus the body. Returns one result per
  entry with the per-resolution fields the caller asked for.
- ``undreamed``: ``sessions/<key>.md`` plus ``ingested/<id>/`` — sessions
  match against ``meta.json::derived.tags`` and the rendered markdown
  body (with the nearest ``#turn-N`` anchor preserved on hits); ingested
  artifacts match against ``meta.json::derived`` and the source text.
- ``all``: union of both, dreamed first.

This module is intentionally simple — no vector, no fuzzy. Phase 2
layers LanceDB on top via the same public ``search_memory`` entrypoint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from durin.memory.paths import MEMORY_CLASSES, walk_class
from durin.memory.schema import MemoryEntry
from durin.memory.storage import FrontmatterError, load_entry

__all__ = [
    "Result",
    "search_dreamed",
    "search_memory",
    "search_undreamed",
]

Scope = Literal["dreamed", "undreamed", "all"]
Level = Literal["warm", "cold"]

_SNIPPET_RADIUS = 80


@dataclass(frozen=True)
class Result:
    """One match returned by search.

    Per doc 25 §2.H (fragment/canonical retrieval contract): each result
    carries enough metadata for the LLM to distinguish a canonical
    entity page from a recent post-cursor fragment, and to know when
    the data was valid. The model can drill into the canonical via the
    ``entities`` pointer.

    Fields beyond the original `source / uri / headline / snippet /
    summary / body` set:

    - ``class_name``: ``"entity_page"`` for canonical pages,
      ``"episodic"|"stable"|"corpus"|"pending"`` for entries, ``""``
      for session/ingested matches.
    - ``valid_from``: ISO timestamp the entry observed (empty for
      entity pages, sessions, ingested).
    - ``entities``: ``type:value`` refs this result pertains to. For a
      canonical page it is the page's own ref; for a fragment it's the
      list of entities the entry tagged.
    """

    source: Literal["memory", "sessions", "ingested"]
    uri: str
    headline: str
    snippet: str
    summary: str = ""
    body: str = ""
    # §2.H fragment/canonical contract fields. All optional with safe
    # defaults so existing callers (and grep paths that don't have the
    # info yet) keep working.
    class_name: str = ""
    valid_from: str = ""
    entities: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        """Marker label for the §2.H contract. One of:

        - ``"canonical"`` — entity_page row
        - ``"fragment"`` — episodic/stable/corpus/pending entry
        - ``"session"`` — session view match (singular, normalised from
          the plural ``source="sessions"`` for cleaner LLM consumption)
        - ``"ingested"`` — ingested artifact match
        """
        if self.source == "sessions":
            return "session"
        if self.source != "memory":
            return self.source
        return "canonical" if self.class_name == "entity_page" else "fragment"

    def to_dict(self) -> dict:
        d: dict = {
            "source": self.source,
            "uri": self.uri,
            "headline": self.headline,
            "snippet": self.snippet,
            # §2.H: kind is the marker the LLM uses to decide whether
            # to treat this as the main answer or as recent context.
            "kind": self.kind,
        }
        if self.summary:
            d["summary"] = self.summary
        if self.body:
            d["body"] = self.body
        if self.class_name:
            d["class_name"] = self.class_name
        if self.valid_from:
            d["valid_from"] = self.valid_from
        if self.entities:
            d["entities"] = list(self.entities)
        return d

    # Audit F4 (2026-05-28): `render_block` retired. The LLM-facing
    # rendering moved to `durin.memory.sectioned_output.render_sectioned`,
    # which groups hits by section, adds intros, applies the
    # per-source cap (doc 03 §12.4), and emits per-block markers
    # with the same END / body-preference / entities-tail semantics
    # this method used to provide. Direct callers should consume
    # `to_dict()` for raw fields and `sectioned_rendered` from the
    # tool response for the marker output.


def search_memory(
    workspace: Path,
    query: str,
    *,
    scope: Scope = "all",
    level: Level = "warm",
) -> list[Result]:
    """Public dispatcher. Empty query returns empty results."""
    if not query or not query.strip():
        return []
    needle = query.strip().lower()
    if scope == "dreamed":
        return search_dreamed(workspace, needle, level=level)
    if scope == "undreamed":
        return search_undreamed(workspace, needle)
    # "all"
    return search_dreamed(workspace, needle, level=level) + search_undreamed(
        workspace, needle
    )


# ---------------------------------------------------------------------------
# dreamed: memory/<class>/*.md
# ---------------------------------------------------------------------------


def search_dreamed(
    workspace: Path,
    needle: str,
    *,
    level: Level = "warm",
) -> list[Result]:
    """Grep over ``memory/<class>/*.md`` plus ``memory/entities/<type>/*.md``.

    Per doc 25 §2.H: canonical entity pages and per-entry fragments
    must both be reachable from the lazy retrieval path so the LLM can
    receive them with the right marker. Previously this function only
    walked the four legacy classes (stable/episodic/corpus/pending) —
    canonical pages were vector-only, breaking the fallback contract.
    """
    needle_low = needle.lower()
    results: list[Result] = []
    memory_root = workspace / "memory"
    if not memory_root.is_dir():
        return results
    for class_name in MEMORY_CLASSES:
        for path in walk_class(workspace, class_name):
            try:
                entry = load_entry(path)
            except (FrontmatterError, Exception):
                continue
            if not _entry_matches(entry, needle_low):
                continue
            snippet = _entry_snippet(entry, needle_low)
            uri = f"memory/{class_name}/{path.stem}"
            results.append(
                Result(
                    source="memory",
                    uri=uri,
                    headline=entry.headline,
                    snippet=snippet,
                    summary=entry.summary if level == "warm" else "",
                    body=entry.body if level == "cold" else "",
                    # §2.H: carry the fields the LLM needs to tell a
                    # fragment from a canonical and to know its time
                    # reference + which entity it pertains to.
                    class_name=class_name,
                    valid_from=(
                        entry.valid_from.isoformat() if entry.valid_from else ""
                    ),
                    entities=tuple(entry.entities),
                )
            )

    # §2.H: also walk canonical entity pages — these are the "main
    # memory" that fragments above amend. Skip the `archive/`
    # subfolders (absorbed pages stay reachable via expand only).
    results.extend(_search_entity_pages(memory_root, needle_low, level))
    return results


def _search_entity_pages(
    memory_root: Path,
    needle_low: str,
    level: Level,
) -> list[Result]:
    """Grep over ``memory/entities/<type>/<slug>.md`` (canonical pages).

    Returns results with ``class_name="entity_page"`` so the §2.H
    contract surfaces them as CANONICAL when rendered.
    """
    from durin.memory.entity_page import EntityPage

    workspace = memory_root.parent
    out: list[Result] = []
    for page_path in walk_class(workspace, "entities"):
        page = EntityPage.from_file(page_path)
        if page is None:
            continue
        haystack_parts = [
            page.name,
            " ".join(page.aliases or []),
            page.body or "",
        ]
        haystack = " ".join(haystack_parts).lower()
        if needle_low not in haystack:
            continue
        slug = page_path.stem
        ref = f"{page.type}:{slug}"
        summary = (
            f"{page.name}"
            + (f" — aliases: {', '.join(page.aliases)}" if page.aliases else "")
        )
        out.append(
            Result(
                source="memory",
                uri=f"memory/entity_page/{ref}",
                headline=page.name,
                snippet=summary[:160],
                summary=summary if level == "warm" else "",
                body=page.body if level == "cold" else "",
                class_name="entity_page",
                # Entity pages don't carry a single valid_from; the
                # body uses prose ("since 2026-03-15...") for
                # temporal claims, per doc 18 §6 protocol α.
                valid_from="",
                entities=(ref,),
            )
        )
    return out


def _entry_matches(entry: MemoryEntry, needle_low: str) -> bool:
    if needle_low in entry.headline.lower():
        return True
    if needle_low in entry.summary.lower():
        return True
    if needle_low in entry.body.lower():
        return True
    if any(needle_low in e.lower() for e in entry.entities):
        return True
    return False


def _entry_snippet(entry: MemoryEntry, needle_low: str) -> str:
    for haystack in (entry.headline, entry.summary, entry.body):
        snippet = _make_snippet(haystack, needle_low)
        if snippet:
            return snippet
    return entry.headline


# ---------------------------------------------------------------------------
# undreamed: sessions/<key>.md (with tag filter) + ingested/<id>/
# ---------------------------------------------------------------------------


def search_undreamed(workspace: Path, needle: str) -> list[Result]:
    """Grep over rendered session views and ingested artifacts."""
    needle_low = needle.lower()
    return [
        *_search_sessions(workspace, needle_low),
        *_search_ingested(workspace, needle_low),
    ]


def _search_sessions(workspace: Path, needle_low: str) -> list[Result]:
    sessions_dir = workspace / "sessions"
    if not sessions_dir.is_dir():
        return []
    results: list[Result] = []
    for md_path in sorted(sessions_dir.glob("*.md")):
        # Filter 1: check tags in sibling meta.json::derived.tags
        meta_path = sessions_dir / f"{md_path.stem}.meta.json"
        tag_hit = _tag_match(meta_path, needle_low)

        # Filter 2: grep the rendered markdown body, capturing the
        # nearest ## turn-N anchor preceding the match.
        try:
            md_text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        body_hits = _grep_session_md(md_text, needle_low, session_key=md_path.stem)

        if tag_hit and not body_hits:
            # Tag-only match → surface the session at turn-1 as a default landing.
            results.append(
                Result(
                    source="sessions",
                    uri=f"sessions/{md_path.stem}.md",
                    headline=f"Session {md_path.stem}",
                    snippet=tag_hit,
                )
            )
        elif body_hits:
            results.extend(body_hits)
    return results


def _tag_match(meta_path: Path, needle_low: str) -> str:
    if not meta_path.is_file():
        return ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    derived = meta.get("derived") or {}
    tags = derived.get("tags") or {}
    if not isinstance(tags, dict):
        return ""
    for key in ("entities", "topics"):
        values = tags.get(key) or []
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and needle_low in value.lower():
                    return f"tag match in {key}: {value}"
    return ""


_ANCHOR_RE = re.compile(r"^##\s+(turn-\d+|consolidated-\d+)\s*$")


def _grep_session_md(
    text: str,
    needle_low: str,
    *,
    session_key: str,
) -> list[Result]:
    results: list[Result] = []
    current_anchor = ""
    for line in text.splitlines():
        m = _ANCHOR_RE.match(line)
        if m:
            current_anchor = m.group(1)
            continue
        if needle_low in line.lower():
            uri = f"sessions/{session_key}.md"
            if current_anchor:
                uri += f"#{current_anchor}"
            snippet = _make_snippet(line, needle_low) or line.strip()
            results.append(
                Result(
                    source="sessions",
                    uri=uri,
                    headline=f"Session {session_key} ({current_anchor or 'turn-?'})",
                    snippet=snippet,
                )
            )
    return results


def _search_ingested(workspace: Path, needle_low: str) -> list[Result]:
    ingested_dir = workspace / "ingested"
    if not ingested_dir.is_dir():
        return []
    results: list[Result] = []
    for entry_dir in sorted(ingested_dir.iterdir()):
        if not entry_dir.is_dir():
            continue
        meta_path = entry_dir / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        derived = meta.get("derived") or {}
        snippet = _ingested_match_snippet(derived, needle_low)

        # Fall back to grepping the source file when present.
        if not snippet:
            sources = sorted(entry_dir.glob("source.*"))
            for src in sources:
                try:
                    text = src.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if needle_low in text.lower():
                    snippet = _make_snippet(text, needle_low)
                    break
        if snippet:
            results.append(
                Result(
                    source="ingested",
                    uri=f"ingested/{entry_dir.name}/source",
                    headline=f"Ingested {entry_dir.name}",
                    snippet=snippet,
                )
            )
    return results


def _ingested_match_snippet(derived: dict, needle_low: str) -> str:
    summary = derived.get("summary") or ""
    if isinstance(summary, str) and needle_low in summary.lower():
        return _make_snippet(summary, needle_low) or summary[:160]
    for key in ("entities", "relations"):
        values = derived.get(key) or []
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and needle_low in value.lower():
                    return f"{key} match: {value}"
    return ""


# ---------------------------------------------------------------------------
# snippet helpers
# ---------------------------------------------------------------------------


def _make_snippet(text: str, needle_low: str) -> str:
    if not text or not needle_low:
        return ""
    pos = text.lower().find(needle_low)
    if pos == -1:
        return ""
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + len(needle_low) + _SNIPPET_RADIUS)
    snippet = text[start:end].replace("\n", " ").strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _all_classes_iter() -> Iterable[str]:
    return MEMORY_CLASSES
