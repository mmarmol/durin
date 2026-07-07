"""Read-only memory surfaces consumed by the webui graph view.

Three endpoints sit on top of the existing memory primitives:

- :func:`get_entity_detail` — full page (frontmatter + body + identifiers),
  git history of the page, archived absorbed pages, and the entries that
  reference the entity. Drives the side panel's tabs.
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
import re
from pathlib import Path
from typing import Any

from durin.memory.entity_page import EntityPage
from durin.memory.paths import walk_class
from durin.memory.storage import load_entry

__all__ = [
    "forget_entry",
    "get_edge_detail",
    "get_entity_detail",
    "get_entry_backlinks",
    "get_entry_detail",
    "get_reference_detail",
    "get_session_detail",
    "list_reference_documents",
    "search_memory_api",
]

_FORGETTABLE_CLASSES = ("episodic", "stable", "corpus", "session_summary")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# entity detail — page + history + archive + referencing entries
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
            },
            "history": [
                {"sha": "abc1234", "subject": "...", "when": "...",
                 "trailers": {"Absorbed": [...], "Judge-Confidence": ["95"], ...},
                 "body": "..."},
                ...
            ],
            "archive": [
                {"slug": "marcelo-m",
                 "path": "memory/archive/entities/person/marcelo-m.md",
                 "archived_at": "...", "archived_reason": "auto"},
                ...
            ],
            "entries": [  # entries that reference this ref
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
        # Phantom: no consolidated page yet, but the entity may be tagged in
        # entries (or have archived predecessors). Return a partial detail
        # (page=None) so the side panel still renders the referencing
        # entries + archive instead of 404ing into empty tabs. Only a ref
        # with no page AND no entries AND no archive is a genuine miss.
        archive = _load_archive(memory_root, type_, slug)
        entries = _load_referencing_entries(
            memory_root, entity_ref, entries_limit,
        )
        if not entries and not archive:
            return None
        return {
            "ref": entity_ref,
            "page": None,
            "provenance": [],
            "history": [],
            "archive": archive,
            "entries": entries,
        }

    return {
        "ref": entity_ref,
        "page": _serialize_page(page),
        "provenance": _provenance_events(page),
        "history": _load_history(memory_root, page_path, history_limit),
        "archive": _load_archive(memory_root, type_, slug),
        "entries": _load_referencing_entries(
            memory_root, entity_ref, entries_limit,
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
        # Page-level authorship (user_authored | agent_created) +
        # timestamps, so the panel can show "created by <author> on <date>"
        # instead of inferring it from a terse git commit.
        "author": page.author,
        "created_at": page.created_at.isoformat() if page.created_at else None,
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
        # Relations were previously only visible as graph edges; surface
        # them in the panel too so provenance events can name them.
        "relations": [dict(r) for r in (page.relations or [])],
        # Source documents this entity was distilled from (reference:<slug>),
        # surfaced as a "Fuentes" section + navigable to the reference panel.
        "derived_from": list(page.derived_from or []),
        "extra": extra,  # leftover frontmatter the parser didn't promote
        "body": page.body or "",
    }


# Parse a provenance `source_ref` wikilink back to its session stem + turn.
# Mirrors `graph.py::_SESSION_REF_RE` but also captures the `#turn-N`
# fragment so the panel can show (and link to) the originating turn.
_SOURCE_REF_RE = re.compile(r"sessions/([^/.#]+)\.md(?:#turn-(\d+))?")


def _parse_source_ref(source_ref: Any) -> tuple[str | None, int | None]:
    """``[[sessions/<stem>.md#turn-N]]`` → ``(stem, N)``.

    Returns ``(None, None)`` for non-session refs (e.g. the
    ``memory_upsert_entity`` fallback string, or an entry/tag ref) so the
    frontend renders them as plain text with no session link.
    """
    if not source_ref:
        return (None, None)
    m = _SOURCE_REF_RE.search(str(source_ref))
    if m is None:
        return (None, None)
    turn = int(m.group(2)) if m.group(2) else None
    return (m.group(1), turn)


def _provenance_events(page: EntityPage) -> list[dict[str, Any]]:
    """Flatten an entity page's per-field provenance into UI-ready events.

    durin records, for every relation and stateful attribute, *who*
    (``author``: agent | dream | user), *when* (``extracted_at``), and
    *from where* (``source_ref`` → a session turn). The detail panel never
    surfaced it. This returns one event per provenanced field, with the
    ``source_ref`` already split into ``session_stem`` + ``turn`` so the
    frontend can make the origin clickable. Labels stay neutral (``kind`` +
    ``detail``); i18n is the frontend's job. Sorted oldest-first.
    """
    prov = page.provenance or {}
    events: list[dict[str, Any]] = []

    rel_prov = prov.get("relations")
    # Relation provenance is a {(to,type)-key: entry} dict carrying to/type.
    rel_entries = (
        [e for e in rel_prov.values() if isinstance(e, dict)]
        if isinstance(rel_prov, dict) else []
    )
    for entry in rel_entries:
        to = entry.get("to")
        detail = f"{entry.get('type', 'related')} → {to}" if to else None
        stem, turn = _parse_source_ref(entry.get("source_ref"))
        events.append({
            "kind": "relation",
            "detail": detail,
            "author": entry.get("author"),
            "when": entry.get("extracted_at"),
            "source_ref": entry.get("source_ref"),
            "session_stem": stem,
            "turn": turn,
        })

    attributes = prov.get("attributes")
    if isinstance(attributes, dict):
        for key, entry in attributes.items():
            if not isinstance(entry, dict):
                continue
            stem, turn = _parse_source_ref(entry.get("source_ref"))
            events.append({
                "kind": "attribute",
                "detail": str(key),
                "author": entry.get("author"),
                "when": entry.get("extracted_at"),
                "source_ref": entry.get("source_ref"),
                "session_stem": stem,
                "turn": turn,
            })

    # Per-link who/when for derived_from (ref-keyed provenance), or the
    # Procedencia tab would never surface where each source link came from.
    derived_from = prov.get("derived_from")
    if isinstance(derived_from, dict):
        for ref, entry in derived_from.items():
            if not isinstance(entry, dict):
                continue
            stem, turn = _parse_source_ref(entry.get("source_ref"))
            events.append({
                "kind": "derived_from",
                "detail": str(ref),
                "author": entry.get("author"),
                "when": entry.get("extracted_at"),
                "source_ref": entry.get("source_ref"),
                "session_stem": stem,
                "turn": turn,
            })

    events.sort(key=lambda e: e.get("when") or "")
    return events


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
    """Archived absorbed pages for *canonical* ``<type>:<slug>``.

    Archives live at
    ``memory/archive/entities/<type>/<absorbed_slug>.md``. Each absorbed
    page's frontmatter carries ``archived_into`` pointing back at its
    canonical (``<type>:<canonical_slug>``); this function filters to
    the pages whose ``archived_into`` matches ``type_:slug``.

    Returns ``archived_at`` / ``archived_reason`` / ``archived_into``
    (new field names per spec). Legacy ``absorbed_*`` keys are also
    recognised so older archive files keep rendering until migration.
    """
    archive_dir = memory_root / "archive" / "entities" / type_
    if not archive_dir.is_dir():
        return []
    canonical_ref = f"{type_}:{slug}"
    out: list[dict[str, Any]] = []
    for path in sorted(archive_dir.glob("*.md")):
        try:
            page = EntityPage.from_file(path)
        except Exception:  # noqa: BLE001
            page = None
        extra = dict(page.extra) if page and page.extra else {}
        archived_into = extra.get("archived_into") or extra.get("absorbed_into")
        if archived_into != canonical_ref:
            continue
        out.append({
            "slug": path.stem,
            "path": str(path.relative_to(memory_root.parent))
                if memory_root.parent in path.parents
                else str(path),
            "name": (page.name if page else path.stem),
            "archived_at": extra.get("archived_at") or extra.get("absorbed_at"),
            "archived_reason": extra.get("archived_reason")
                or extra.get("absorbed_reason"),
            "archived_into": archived_into,
        })
    return out


def _load_referencing_entries(
    memory_root: Path,
    entity_ref: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Entries that tag this entity — the raw evidence behind it.

    Walks every entry class that carries entity tags (``episodic``,
    ``stable``, ``corpus``) rather than ``episodic`` alone — a vault whose
    knowledge lives in ``stable`` entries (the common case before
    consolidation) would otherwise show an empty side panel even though the
    entity is well-referenced.

    Two-track model (N3): fragments are not consolidated into the page, so
    every referencing entry surfaces (recency-ordered, capped by ``limit``).
    """
    rows: list[tuple[str, dict[str, Any]]] = []
    for class_name in ("episodic", "stable", "corpus"):
        for path in walk_class(memory_root.parent, class_name):
            try:
                entry = load_entry(path)
            except Exception:  # noqa: BLE001
                continue
            if entity_ref not in (entry.entities or []):
                continue
            ts = entry.valid_from.isoformat() if entry.valid_from else ""
            rows.append((ts, {
                "id": entry.id,
                "valid_from": ts,
                "headline": entry.headline,
                "summary": entry.summary,
                "body": (entry.body or "")[:2000],  # cap; full body via memory_drill
                "class": class_name,
                "entities": list(entry.entities or []),
            }))
    # Newest first for the panel.
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return [r[1] for r in rows[:limit]]


# ---------------------------------------------------------------------------
# search — mirror memory_search tool surface
# ---------------------------------------------------------------------------


async def search_memory_api(
    workspace: Path,
    query: str,
    *,
    scope: str = "all",
    level: str = "warm",
    kinds: str = "all",
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Run the same search the LLM tool uses; return its JSON output.

    Wraps :class:`MemorySearchTool` so the webui filter behaves
    identically to the agent's ``memory_search`` invocations — vector
    retrieval when fastembed is available, entity-aware reranking when
    the AliasIndex has data, grep fallback otherwise. The returned
    shape includes ``kind`` + ``rendered`` per result so
    the frontend can display canonical vs fragment markers consistently
    with how the LLM sees them.
    """
    if not query or not query.strip():
        return {"results": [], "total": 0, "strategy": "noop", "ranking": "default"}

    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool(workspace=workspace, embedding_model=embedding_model)
    payload = await tool.execute(
        query=query, scope=scope, level=level, kinds=kinds,
    )
    return payload


# ---------------------------------------------------------------------------
# edge detail — entries co-mentioning two refs
# ---------------------------------------------------------------------------


def get_session_detail(
    workspace: Path,
    session_stem: str,
    *,
    recent_messages: int = 2000,
    entries_limit: int = 50,
    tool_call_limit: int = 40,
) -> dict[str, Any] | None:
    """Read-only summary of one session for the graph view side panel.

    ``session_stem`` is the filename stem (``websocket_<uuid>`` or
    ``cli_direct``), NOT the full session key with the channel prefix.
    Returns None when the corresponding ``.jsonl`` doesn't exist.

    Shape:

    ::

        {
            "session_ref": "session:cli_direct",
            "session_key": "cli:cli_direct",   # from meta or inferred
            "info": {
                "title": "...",
                "message_count": 39,
                "channel": "cli",
                "model": "<model-id>",
                "created_at": "...",
                "updated_at": "...",
            },
            "entities_tagged": {
                "from_meta": [...],            # derived._last_tags.entities
                "from_source_refs": [...],     # entities in entries that link to this session
            },
            "events": [                        # meta.json::events list
                {"type": "plan", "title": "...", "created_at": "...", ...},
                {"type": "tool_call", "tool": "memory_search", ...},
            ],
            "memory_ops": [                    # subset of events filtered to memory_* tools
                {"tool": "memory_store", "args": "...", "result_id": "..."},
            ],
            "recent_messages": [               # last N message preview from jsonl
                {"role": "user", "content": "...", "ts": ...},
            ],
            "entries_linked": [...],           # episodic entries with source_refs pointing here
        }
    """
    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    jsonl_path = sessions_dir / f"{session_stem}.jsonl"
    if not jsonl_path.is_file():
        return None
    meta_path = sessions_dir / f"{session_stem}.meta.json"

    # Identity (line 0) + recent messages
    info, recent = _read_session_info_and_tail(jsonl_path, recent_messages)
    # Meta events + derived
    meta_payload = _read_session_meta(meta_path)
    events = meta_payload.get("events", []) if meta_payload else []
    memory_ops = _filter_memory_ops(events, limit=tool_call_limit)
    derived = meta_payload.get("derived", {}) if meta_payload else {}
    last_tags = (derived.get("_last_tags") or {}) if isinstance(derived, dict) else {}
    meta_entities = [
        e for e in (last_tags.get("entities") or []) if isinstance(e, str) and ":" in e
    ]

    # Entries authored from this session (source_refs link back)
    entries_linked = _entries_linked_to_session(
        workspace, session_stem, limit=entries_limit,
    )
    entries_from_refs = sorted(
        {ent for e in entries_linked for ent in (e.get("entities") or [])}
    )

    return {
        "session_ref": f"session:{session_stem}",
        "session_key": meta_payload.get("session_key") if meta_payload else None,
        "info": info,
        "entities_tagged": {
            "from_meta": meta_entities,
            "from_source_refs": entries_from_refs,
        },
        "events": events,
        "memory_ops": memory_ops,
        "recent_messages": recent,
        "entries_linked": entries_linked,
    }


def _read_session_info_and_tail(
    jsonl_path: Path, n: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse line 0 (identity) + the message thread (chronological).

    Returns the full thread (each message carrying its ``index``, ``role``,
    ``ts`` and a 280-char ``preview``), capped at ``n`` most-recent messages
    so a huge session can't blow out the payload. The panel needs the
    *whole* thread — not just the tail — to scroll to the message nearest a
    provenance event's timestamp, which may be far from the end.
    """
    import json

    info: dict[str, Any] = {"title": None, "message_count": 0, "channel": None,
                             "model": None, "created_at": None, "updated_at": None}
    messages: list[dict[str, Any]] = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return info, messages
    msg_index = 0
    for i, raw in enumerate(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if i == 0 and isinstance(obj, dict) and "role" not in obj:
            # Identity block
            info.update({
                "title": obj.get("title") or obj.get("display_name") or obj.get("name"),
                "channel": obj.get("channel"),
                "model": obj.get("model"),
                "created_at": obj.get("created_at"),
                "updated_at": obj.get("updated_at"),
            })
            continue
        if not isinstance(obj, dict) or "role" not in obj:
            continue
        info["message_count"] += 1
        # Reduce to a compact preview to keep payload small.
        content = obj.get("content")
        if isinstance(content, list):
            # Multimodal: take first text part if any.
            text = next(
                (p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"),
                "",
            )
        else:
            text = str(content or "")
        messages.append({
            "index": msg_index,
            "role": obj.get("role"),
            "ts": obj.get("ts") or obj.get("timestamp"),
            "preview": text[:280],
        })
        msg_index += 1
    # Cap to the most-recent `n` (chronological order preserved). Best-effort
    # for very long sessions: an older provenance moment beyond the cap falls
    # back to the top of the returned window.
    if n and len(messages) > n:
        messages = messages[-n:]
    return info, messages


def _read_session_meta(meta_path: Path) -> dict[str, Any] | None:
    if not meta_path.is_file():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            import json

            return json.load(fh)
    except (OSError, ValueError):
        return None


def _filter_memory_ops(
    events: list[Any], *, limit: int,
) -> list[dict[str, Any]]:
    """Subset of events for memory_* tool calls."""
    ops: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "tool_call":
            continue
        tool = ev.get("tool") or ev.get("name") or ""
        if not isinstance(tool, str) or not tool.startswith("memory_"):
            continue
        ops.append({
            "tool": tool,
            "ts": ev.get("ts") or ev.get("created_at"),
            "args_preview": _truncate(ev.get("args"), 200),
            "result_preview": _truncate(ev.get("result"), 200),
            "msg_index": ev.get("msg_index"),
        })
        if len(ops) >= limit:
            break
    return ops


def _truncate(value: Any, n: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            import json

            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = repr(value)
    return value if len(value) <= n else value[:n] + "…"


def _entries_linked_to_session(
    workspace: Path, session_stem: str, *, limit: int,
) -> list[dict[str, Any]]:
    """Entries whose source_refs include ``sessions/<session_stem>.md``."""
    import re

    pat = re.compile(rf"sessions/{re.escape(session_stem)}\.md(?:#turn-\d+)?")
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in walk_class(workspace, "episodic"):
        try:
            entry = load_entry(path)
        except Exception:  # noqa: BLE001
            continue
        if not any(pat.search(str(r)) for r in (entry.source_refs or [])):
            continue
        ts = entry.valid_from.isoformat() if entry.valid_from else ""
        rows.append((ts, {
            "id": entry.id,
            "valid_from": ts,
            "headline": entry.headline,
            "summary": entry.summary,
            "snippet": (entry.body or "")[:240],
            "entities": list(entry.entities or []),
        }))
    rows.sort(key=lambda kv: kv[0], reverse=True)
    return [r[1] for r in rows[:limit]]


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
    rows: list[tuple[str, dict[str, Any]]] = []
    for path in walk_class(Path(workspace), "episodic"):
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


# ---------------------------------------------------------------------------
# individual entry browse / forget / backlinks
# ---------------------------------------------------------------------------


def _parse_entry_uri(uri: str) -> tuple[str, str] | None:
    """Split ``memory/<class>/<id>`` → ``(class, id)``. Returns ``None``
    on malformed input (URL was tampered with or junk). Tolerates a
    trailing ``.md`` and a leading ``./``.
    """
    if not uri:
        return None
    cleaned = uri.strip().lstrip("./")
    if cleaned.endswith(".md"):
        cleaned = cleaned[:-3]
    parts = cleaned.split("/")
    if len(parts) != 3 or parts[0] != "memory":
        return None
    return parts[1], parts[2]


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a ``---`` YAML frontmatter block from a markdown doc body."""
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            import yaml

            try:
                fm = yaml.safe_load(parts[1]) or {}
            except Exception:  # noqa: BLE001
                fm = {}
            return (fm if isinstance(fm, dict) else {}), parts[2].lstrip("\n")
    return {}, raw


def get_entry_detail(workspace: Path, uri: str) -> dict[str, Any] | None:
    """Return one entry's frontmatter + body, or ``None`` on bad URI / 404.

    Reads ``workspace/memory/<class>/<id>.md`` via
    :func:`durin.memory.storage.load_entry`. Used by the webui's
    Entries tab when the operator opens a row. Also resolves
    ``reference:<slug>`` uris (ingested reference docs under
    ``memory/references/``) so the webui can open a reference's content —
    references aren't graph nodes or entries, but they ARE readable pages.
    """
    if uri.startswith("reference:"):
        from durin.memory.reference import load_reference

        raw = load_reference(workspace, uri)
        if raw is None:
            return None
        fm, body = _split_frontmatter(raw)
        return {
            "uri": uri,
            "class_name": "reference",
            "frontmatter": {
                "id": uri,
                "headline": str(fm.get("title") or uri.split(":", 1)[-1]),
                "summary": "",
                "valid_from": fm.get("ingested_at"),
                "author": "ingested",
                "entities": [],
                "source_refs": [str(fm.get("source"))] if fm.get("source") else [],
                "related": [],
            },
            "body": body,
            "exists": True,
        }
    parsed = _parse_entry_uri(uri)
    if parsed is None:
        return None
    class_name, entry_id = parsed
    if class_name not in _FORGETTABLE_CLASSES:
        return None
    path = workspace / "memory" / class_name / f"{entry_id}.md"
    if not path.is_file():
        return None
    try:
        entry = load_entry(path)
    except Exception:  # noqa: BLE001
        logger.exception("get_entry_detail: load_entry failed for %s", path)
        return None
    return {
        "uri": f"memory/{class_name}/{entry_id}",
        "class_name": class_name,
        "frontmatter": {
            "id": entry.id,
            "headline": entry.headline,
            "summary": entry.summary,
            "valid_from": entry.valid_from.isoformat() if entry.valid_from else None,
            "author": entry.author,
            "entities": list(entry.entities or ()),
            "source_refs": list(entry.source_refs or ()),
            "related": list(entry.related or ()),
        },
        "body": entry.body or "",
        "exists": True,
    }


def forget_entry(workspace: Path, uri: str) -> dict[str, Any]:
    """Archive an entry on behalf of the webui.

    Returns ``{"result": "archived" | "not_found" | "protected" | "invalid"}``.

    - ``protected`` for ``memory/entities/...`` (those have their own
      absorb/revert lifecycle).
    - ``invalid`` for unparseable URIs or unsupported classes.
    - ``not_found`` when the file is missing.
    - ``archived`` on success; best-effort cleans the vector + FTS rows.
    """
    parsed = _parse_entry_uri(uri)
    if parsed is None:
        # Maybe an `entities/...` URI got through — that has 4+ parts
        # so the parser returned None; detect explicitly so the UI can
        # surface "protected" instead of a generic invalid.
        cleaned = (uri or "").strip().lstrip("./")
        if cleaned.startswith("memory/entities/"):
            return {"result": "protected"}
        return {"result": "invalid"}
    class_name, entry_id = parsed
    if class_name == "entities":
        return {"result": "protected"}
    if class_name not in _FORGETTABLE_CLASSES:
        return {"result": "invalid"}
    path = workspace / "memory" / class_name / f"{entry_id}.md"
    if not path.is_file():
        return {"result": "not_found"}

    from durin.memory.archive import (
        archive_episodic,
        archive_generic_entry,
    )

    try:
        if class_name == "episodic":
            archive_episodic(
                workspace=workspace,
                episodic_path=path,
                into_uri="",
                reason="user_forget",
            )
        else:
            archive_generic_entry(
                workspace=workspace,
                entry_path=path,
                reason="user_forget",
            )
    except FileNotFoundError:
        # Raced with another archiver — already gone.
        return {"result": "not_found"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("forget_entry archive failed for %s", uri)
        return {"result": "error", "detail": str(exc)}

    # Best-effort index cleanup. Mirrors what the CLI does. Failures
    # are logged but don't change the result — the markdown move
    # already succeeded.
    try:
        from durin.config.loader import load_config
        from durin.memory.vector_index import (
            VectorIndex,
            vector_index_available,
        )
        cfg = load_config()
        if (
            vector_index_available()
            and getattr(cfg.memory, "enabled", False)
            and cfg.memory.embedding.model
        ):
            from durin.memory.embedding import FastembedProvider
            vi = VectorIndex(
                workspace,
                FastembedProvider(model=cfg.memory.embedding.model),
            )
            vi.delete_by_id(entry_id)
    except Exception:  # noqa: BLE001
        logger.warning("forget_entry: vector cleanup skipped for %s", uri)

    try:
        from durin.memory.indexer import reindex_one_file
        reindex_one_file(workspace, path, trigger="forget")
    except Exception:  # noqa: BLE001
        logger.warning("forget_entry: FTS cleanup skipped for %s", uri)

    return {"result": "archived"}


def forget_reference(workspace: Path, slug: str) -> dict[str, Any]:
    """Forget an ingested Library document on behalf of the webui / agent tool.

    ``slug`` is the bare reference slug (``the-durin-handbook``) or a full
    ``reference:<slug>``. Archives the document + its chunk sidecar and drops
    its FTS + vector rows via :func:`durin.memory.forget.forget_reference`.

    Returns ``{"result": "archived" | "not_found" | "error"}``. Unlike
    entities, references have no absorb/revert lifecycle, so there is no
    ``protected`` outcome.
    """
    slug = slug.split(":", 1)[1] if slug.startswith("reference:") else slug
    ref = f"reference:{slug}"
    try:
        from durin.memory.forget import forget_reference as _forget_reference

        dest = _forget_reference(workspace, ref, reason="user_forget")
    except Exception as exc:  # noqa: BLE001
        logger.exception("forget_reference failed for %s", ref)
        return {"result": "error", "detail": str(exc)}
    if dest is None:
        return {"result": "not_found"}
    return {"result": "archived"}


def get_entry_backlinks(
    workspace: Path,
    uri: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Find entries that reference ``uri`` via ``source_refs``, ``related``,
    or body wikilinks.

    Walks every entry under ``memory/`` (excluding ``archive/`` /
    ``pending/`` — see :func:`walk_memory`). Synchronous because
    workspaces have O(thousands) of entries max; well under 100 ms in
    normal operation.

    Returns ``{"uri": ..., "backlinks": [...], "truncated": bool}``.
    Each backlink: ``{"uri", "context", "headline"}`` where ``context``
    is one of ``"source_refs"``, ``"related"``, or ``"body"``.
    """
    parsed = _parse_entry_uri(uri)
    if parsed is None:
        return {"uri": uri, "backlinks": [], "truncated": False}
    class_name, entry_id = parsed
    target_path = f"memory/{class_name}/{entry_id}"

    from durin.memory.paths import walk_memory

    results: list[dict[str, Any]] = []
    for md_path in walk_memory(workspace, include_archive=False):
        # Skip the target itself (don't self-reference).
        try:
            rel = md_path.relative_to(workspace / "memory")
        except ValueError:
            continue
        rel_no_ext = f"memory/{rel.with_suffix('').as_posix()}"
        if rel_no_ext == target_path:
            continue
        try:
            entry = load_entry(md_path)
        except Exception:  # noqa: BLE001
            continue

        contexts: list[str] = []
        # Frontmatter refs may carry the URI verbatim, wrapped in
        # ``[[...]]``, or inside a markdown link like
        # ``[turn](memory/...)``. We accept any occurrence.
        for field_name, values in (
            ("source_refs", entry.source_refs or ()),
            ("related", entry.related or ()),
        ):
            if any(target_path in v for v in values):
                contexts.append(field_name)
        if f"[[{target_path}]]" in (entry.body or ""):
            contexts.append("body")

        if not contexts:
            continue
        results.append({
            "uri": f"memory/{rel.parts[0]}/{md_path.stem}",
            "context": ",".join(contexts),
            "headline": entry.headline or "",
        })
        if len(results) >= limit + 1:
            # We collected one extra to know we're truncating.
            break

    truncated = len(results) > limit
    return {
        "uri": f"memory/{class_name}/{entry_id}",
        "backlinks": results[:limit],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# reference documents — the Library "shelf" (list + per-document detail)
# ---------------------------------------------------------------------------


def list_reference_documents(workspace: Path) -> list[dict[str, Any]]:
    """List ingested reference documents, newest first.

    One row per ``memory/references/<slug>.md`` carrying its frontmatter
    summary (title / source / ingested_at / chunk_count) plus ``distilled`` —
    whether the dream has written an ``<slug>.outline.json`` for it yet.
    Read-only; drives the webui Documents shelf.
    """
    refs_dir = Path(workspace) / "memory" / "references"
    if not refs_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for md_path in sorted(refs_dir.glob("*.md")):
        slug = md_path.stem
        try:
            fm, _ = _split_frontmatter(md_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        rows.append({
            "slug": slug,
            "ref": f"reference:{slug}",
            "title": str(fm.get("title") or slug),
            "source": (str(fm.get("source") or "") or None),
            "ingested_at": fm.get("ingested_at"),
            "chunk_count": int(fm.get("chunk_count") or 0),
            "distilled": (refs_dir / f"{slug}.outline.json").is_file(),
        })
    rows.sort(key=lambda r: str(r.get("ingested_at") or ""), reverse=True)
    return rows


def _clean_significance(body: str) -> str | None:
    """A seeded entity's one-line significance = its body, sans provenance markers."""
    lines = [
        ln.strip()
        for ln in (body or "").splitlines()
        if ln.strip() and not ln.strip().startswith("<!--")
    ]
    text = " ".join(lines).strip()
    return text[:200] or None


def _entities_derived_from(memory_root: Path, ref: str) -> list[dict[str, Any]]:
    """Entities linked to a reference via ``derived_from`` — with the KIND of link.

    ``derived_from`` is written by two producers with different meaning, and the
    per-link provenance author tells them apart:

    - ``author == "dream"`` → ``relation="distilled"``: the nightly pass extracted
      this entity FROM the document's content (the document is about it).
    - otherwise (``"agent"`` / unknown) → ``relation="referenced"``: an entity
      linked to the document as a *source it consulted* during a conversation —
      the document does not necessarily describe it (e.g. a patient whose workup
      cited a paper).

    Distilled entities sort first, then by name.
    """
    entities_dir = memory_root / "entities"
    if not entities_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for md in sorted(entities_dir.rglob("*.md")):
        try:
            page = EntityPage.from_file(md)
        except Exception:  # noqa: BLE001
            page = None
        if page is None or ref not in (page.derived_from or []):
            continue
        prov_df = (page.provenance or {}).get("derived_from")
        link = prov_df.get(ref) if isinstance(prov_df, dict) else None
        author = link.get("author") if isinstance(link, dict) else None
        out.append({
            "ref": f"{md.parent.name}:{md.stem}",
            "type": md.parent.name,
            "name": page.name,
            "relation": "distilled" if author == "dream" else "referenced",
            "significance": _clean_significance(page.body or ""),
        })
    out.sort(key=lambda e: (e["relation"] != "distilled", str(e.get("name") or "").lower()))
    return out


def get_reference_detail(
    workspace: Path,
    slug: str,
    *,
    chunk_preview_limit: int = 12,
) -> dict[str, Any] | None:
    """Full detail for one reference document, or ``None`` if it doesn't exist.

    Metadata + the distilled outline (abstract + per-section summaries, when the
    dream has run) + the entities seeded from it (``derived_from``) + a bounded
    preview of its structure-aware chunks. Read-only; drives the Documents
    shelf's per-document view. The whole raw document stays reachable via
    :func:`get_entry_detail` (``reference:<slug>`` uri).
    """
    import json

    refs_dir = Path(workspace) / "memory" / "references"
    md_path = refs_dir / f"{slug}.md"
    if not md_path.is_file():
        return None
    try:
        fm, _ = _split_frontmatter(md_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    ref = f"reference:{slug}"

    outline: dict[str, Any] | None = None
    outline_path = refs_dir / f"{slug}.outline.json"
    if outline_path.is_file():
        try:
            raw = json.loads(outline_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = None
        if isinstance(raw, dict):
            outline = {
                "abstract": str(raw.get("abstract") or ""),
                "sections": [
                    {
                        "breadcrumb": str(s.get("breadcrumb") or ""),
                        "summary": str(s.get("summary") or ""),
                        "chunk_indices": [
                            int(i) for i in (s.get("chunk_indices") or [])
                        ],
                    }
                    for s in (raw.get("sections") or [])
                    if isinstance(s, dict)
                ],
            }

    from durin.memory.reference import reference_chunks

    chunks = reference_chunks(workspace, ref)
    chunks_preview = [
        {
            "idx": int(c.get("idx", i)),
            "breadcrumb": str(c.get("breadcrumb") or ""),
            "text": str(c.get("text") or "")[:600],
        }
        for i, c in enumerate(chunks[:chunk_preview_limit])
    ]

    return {
        "slug": slug,
        "ref": ref,
        "title": str(fm.get("title") or slug),
        "source": (str(fm.get("source") or "") or None),
        "ingested_at": fm.get("ingested_at"),
        "chunk_count": int(fm.get("chunk_count") or len(chunks)),
        "chunks_total": len(chunks),
        "outline": outline,
        "entities": _entities_derived_from(Path(workspace) / "memory", ref),
        "chunks_preview": chunks_preview,
    }
