"""Read-only memory surfaces consumed by the webui graph view.

Three endpoints sit on top of the existing memory primitives:

- :func:`get_entity_detail` — full page (frontmatter + body + identifiers),
  git history of the page, archived absorbed pages, post-cursor entries
  that reference the entity. Drives the side panel's tabs.
- :func:`search_memory_api` — same logic as ``memory_search`` tool
  (vector + entity-aware reranking + grep fallback). Used as a filter
  on the graph + results list.
- :func:`get_edge_detail` — entries that co-mention two refs (the
  raw evidence behind a graph edge). Used when the user clicks an
  edge to drill into "why are these connected".

All three are pure: read-only over disk + LanceDB. No LLM call. No
mutation. JSON-serialisable payloads matching what the frontend
expects.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage
from durin.memory.search import search_memory
from durin.memory.storage import load_entry

__all__ = [
    "get_edge_detail",
    "get_entity_detail",
    "search_memory_api",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# entity detail — page + history + archive + post-cursor entries
# ---------------------------------------------------------------------------


def get_entity_detail(
    workspace: Path,
    entity_ref: str,
    *,
    history_limit: int = 20,
    entries_limit: int = 50,
) -> dict[str, Any] | None:
    """Return everything the side panel needs about one entity, or None.

    Shape:

    ::

        {
            "ref": "person:marcelo",
            "page": {
                "type": "person",
                "name": "Marcelo Marmol",
                "aliases": [...],
                "identifiers": {"email": [...], ...},
                "extra": {...},  # any frontmatter the parser didn't promote
                "body": "## Current State\\n...",
                "dream_processed_through": "2026-05-20T...",
            },
            "history": [
                {"sha": "abc1234", "subject": "...", "when": "...",
                 "trailers": {"Absorbed": [...], "Judge-Confidence": ["95"], ...},
                 "body": "..."},
                ...
            ],
            "archive": [
                {"slug": "marcelo-m", "path": "entities/person/marcelo/archive/marcelo-m.md",
                 "absorbed_at": "...", "absorbed_reason": "auto"},
                ...
            ],
            "entries": [  # post-cursor episodic entries that reference this ref
                {"id": "e123", "valid_from": "2026-05-23T...", "headline": "...",
                 "summary": "...", "body": "..."},
                ...
            ],
        }

    Returns ``None`` when the page does not exist (caller renders 404).
    Archive subfolders are detected even when the canonical page itself
    is missing — useful for forensics on an entity that was renamed.
    """
    memory_root = Path(workspace) / "memory"
    type_, _, slug = entity_ref.partition(":")
    if not type_ or not slug:
        return None

    page_path = memory_root / "entities" / type_ / f"{slug}.md"
    page: EntityPage | None = None
    if page_path.is_file():
        try:
            page = EntityPage.from_file(page_path)
        except Exception:  # noqa: BLE001
            page = None
    if page is None:
        # No canonical page; bail early. The caller can still surface
        # the entity via search if it was tagged in entries.
        return None

    return {
        "ref": entity_ref,
        "page": _serialize_page(page),
        "history": _load_history(memory_root, page_path, history_limit),
        "archive": _load_archive(memory_root, type_, slug),
        "entries": _load_post_cursor_entries(
            memory_root, entity_ref, page.dream_processed_through, entries_limit,
        ),
    }


def _serialize_page(page: EntityPage) -> dict[str, Any]:
    extra = dict(page.extra or {})
    identifiers = extra.pop("identifiers", None)
    return {
        "type": page.type,
        "name": page.name,
        "aliases": list(page.aliases or []),
        "identifiers": identifiers if isinstance(identifiers, dict) else None,
        "extra": extra,  # leftover frontmatter (created_at, updated_at, etc.)
        "body": page.body or "",
        "dream_processed_through": page.dream_processed_through,
    }


def _load_history(
    memory_root: Path, page_path: Path, limit: int,
) -> list[dict[str, Any]]:
    """Git history filtered to this page's file path.

    Each commit carries its subject, body, trailers, sha, and an ISO
    timestamp. Used to surface auto-absorb commits with their
    `Judge-Confidence` and `Judge-Reasoning` to the user.
    """
    if not (memory_root / ".git").is_dir():
        return []
    try:
        from durin.utils.git_repo import GitRepo

        repo = GitRepo(memory_root)
        commits = repo.log(page_path, max_count=limit)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for c in commits:
        out.append({
            "sha": c.sha,
            "short_sha": c.sha[:8],
            "subject": c.subject,
            "body": c.body,
            "when": c.timestamp.isoformat() if c.timestamp else "",
            "trailers": {k: list(v) for k, v in (c.trailers or {}).items()},
        })
    return out


def _load_archive(
    memory_root: Path, type_: str, slug: str,
) -> list[dict[str, Any]]:
    """Absorbed pages parked under ``entities/<type>/<slug>/archive/``.

    Each entry returns the absorbed slug + workspace-relative path +
    metadata stamped at absorb-time (`absorbed_at`, `absorbed_reason`).
    """
    archive_dir = memory_root / "entities" / type_ / slug / "archive"
    if not archive_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(archive_dir.glob("*.md")):
        try:
            page = EntityPage.from_file(path)
        except Exception:  # noqa: BLE001
            page = None
        extra = dict(page.extra) if page and page.extra else {}
        out.append({
            "slug": path.stem,
            "path": str(path.relative_to(memory_root.parent))
                if memory_root.parent in path.parents
                else str(path),
            "name": (page.name if page else path.stem),
            "absorbed_at": extra.get("absorbed_at"),
            "absorbed_reason": extra.get("absorbed_reason"),
            "absorbed_into": extra.get("absorbed_into"),
        })
    return out


def _load_post_cursor_entries(
    memory_root: Path,
    entity_ref: str,
    cursor: Any,
    limit: int,
) -> list[dict[str, Any]]:
    """Episodic entries that tag this entity and are newer than its cursor.

    These are the "fragments" the dream consolidator has not yet folded
    into the page body. Showing them in the side panel surfaces the
    raw evidence backing the consolidated content.
    """
    episodic = memory_root / "episodic"
    if not episodic.is_dir():
        return []
    cursor_dt = _parse_cursor(cursor)
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in episodic.glob("*.md"):
        try:
            entry = load_entry(path)
        except Exception:  # noqa: BLE001
            continue
        if entity_ref not in (entry.entities or []):
            continue
        ts = entry.valid_from.isoformat() if entry.valid_from else ""
        if cursor_dt is not None and ts:
            try:
                entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                entry_dt = None
            # Normalise to UTC — episodic entries may carry naive
            # timestamps while the cursor is always tz-aware.
            if entry_dt is not None and entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            if entry_dt is not None and entry_dt <= cursor_dt:
                continue  # already consolidated
        rows.append((ts, {
            "id": entry.id,
            "valid_from": ts,
            "headline": entry.headline,
            "summary": entry.summary,
            "body": (entry.body or "")[:2000],  # cap; full body via memory_drill
            "class": "episodic",
            "entities": list(entry.entities or []),
        }))
    # Newest first for the panel.
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return [r[1] for r in rows[:limit]]


def _parse_cursor(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None  # numeric cursors not comparable to ISO ts
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# search — mirror memory_search tool surface
# ---------------------------------------------------------------------------


async def search_memory_api(
    workspace: Path,
    query: str,
    *,
    scope: str = "all",
    level: str = "warm",
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Run the same search the LLM tool uses; return its JSON output.

    Wraps :class:`MemorySearchTool` so the webui filter behaves
    identically to the agent's ``memory_search`` invocations — vector
    retrieval when fastembed is available, entity-aware reranking when
    the AliasIndex has data, grep fallback otherwise. The returned
    shape includes ``kind`` + ``rendered`` per result (doc 25 §2.H) so
    the frontend can display canonical vs fragment markers consistently
    with how the LLM sees them.
    """
    if not query or not query.strip():
        return {"results": [], "total": 0, "strategy": "noop", "ranking": "default"}

    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool(workspace=workspace, embedding_model=embedding_model)
    payload = await tool.execute(query=query, scope=scope, level=level)
    return payload


# ---------------------------------------------------------------------------
# edge detail — entries co-mentioning two refs
# ---------------------------------------------------------------------------


def get_edge_detail(
    workspace: Path,
    ref_a: str,
    ref_b: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Return the episodic entries that tag BOTH refs (raw co-occurrence).

    The graph edge shows the weight; this endpoint shows the evidence.
    Useful when the user wants to know "what binds Marcelo to durin"
    instead of just "they're connected".
    """
    memory_root = Path(workspace) / "memory"
    episodic = memory_root / "episodic"
    if not episodic.is_dir():
        return {"source": ref_a, "target": ref_b, "entries": []}
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in episodic.glob("*.md"):
        try:
            entry = load_entry(path)
        except Exception:  # noqa: BLE001
            continue
        ents = entry.entities or []
        if ref_a in ents and ref_b in ents:
            ts = entry.valid_from.isoformat() if entry.valid_from else ""
            rows.append((ts, {
                "id": entry.id,
                "valid_from": ts,
                "headline": entry.headline,
                "summary": entry.summary,
                "snippet": (entry.body or "")[:240],
                "entities": list(ents),
            }))
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return {
        "source": ref_a,
        "target": ref_b,
        "entries": [r[1] for r in rows[:limit]],
        "total": len(rows),
    }
