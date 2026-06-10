"""Generate a `VAULT_README.md` at workspace root explaining the
on-disk layout for human viewers (Obsidian users, future webui /
desktop app users, anyone browsing the markdown files directly).

P9 (2026-05-30) ships this as part of the vault-friendly read-only
viewer plan (see ``docs/backlog.md::P9``). The README is
idempotent: written once at workspace boot if missing, never
overwritten (so a user who hand-edits it doesn't lose changes on
next start).

Why at workspace root and not inside ``memory/``: ``walk_memory()``
indexes any ``*.md`` it finds under ``memory/`` — putting the README
there would inject it into the FTS5 + vector index as if it were a
memory entry. Workspace root is the natural place for "what is this
folder?" documentation anyway.
"""

from __future__ import annotations

import logging
from pathlib import Path
from durin.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

__all__ = [
    "CLASS_INDEX_FILENAME",
    "VAULT_README_FILENAME",
    "ensure_class_indices",
    "ensure_vault_readme",
]

VAULT_README_FILENAME = "VAULT_README.md"
# Per-class navigation helper. Prefixed with `_` so `walk_memory()`
# skips it (P9 Cambio 5 — see `durin/memory/paths.py`). Lives inside
# each `memory/<class>/` folder.
CLASS_INDEX_FILENAME = "_INDEX.md"


def ensure_vault_readme(workspace: Path) -> bool:
    """Write `VAULT_README.md` at the workspace root if it doesn't
    already exist. Returns True if the file was created, False if it
    was already present (or the write failed and we degraded).

    Idempotent + safe: never overwrites an existing file (user edits
    are preserved). On filesystem errors (read-only mount,
    permissions) logs at WARNING and returns False — the agent must
    not crash because a help file couldn't be written.
    """
    target = workspace / VAULT_README_FILENAME
    if target.exists():
        return False
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, _README_CONTENT)
        logger.info("vault readme written at %s", target)
        return True
    except OSError as exc:
        logger.warning(
            "failed to write vault readme at %s: %s — continuing",
            target, exc,
        )
        return False


def ensure_class_indices(workspace: Path) -> int:
    """Write `_INDEX.md` inside each `memory/<class>/` folder if missing.

    Returns the count of new index files written (0 if all already
    exist). Idempotent + safe: existing files preserved, write errors
    logged + degraded.

    The `_` prefix in the filename matters: `walk_memory()` skips any
    path whose components start with `_`, so these navigational helpers
    are NOT indexed as memory entries.
    """
    # Import inside function to avoid module-cycle with paths.py during
    # module init (paths.py is imported very early by other modules).
    from durin.memory.paths import MEMORY_CLASSES

    memory_root = workspace / "memory"
    if not memory_root.is_dir():
        return 0
    written = 0
    for class_name in MEMORY_CLASSES:
        class_dir = memory_root / class_name
        if not class_dir.is_dir():
            continue
        target = class_dir / CLASS_INDEX_FILENAME
        if target.exists():
            continue
        content = _CLASS_INDEX_CONTENT.get(class_name, _CLASS_INDEX_DEFAULT)
        try:
            atomic_write_text(target, content)
            logger.info("class index written at %s", target)
            written += 1
        except OSError as exc:
            logger.warning(
                "failed to write class index at %s: %s — continuing",
                target, exc,
            )
    return written


_README_CONTENT = """\
# durin workspace

This folder is the on-disk state of your durin agent — a memory store,
ingested artifacts, session transcripts, and indices. Everything human-
readable is plain markdown with YAML frontmatter.

This README exists so a human (you, or anyone you give the folder to)
can navigate the workspace without consulting source code.

## Layout

```
<workspace>/
├── VAULT_README.md          ← this file
├── memory/                  ← the agent's persistent memory (markdown)
│   ├── stable/              ← durable facts (preferences, IDs, durable choices)
│   ├── episodic/            ← observations, conversation fragments
│   ├── corpus/              ← chunks of ingested documents
│   ├── pending/             ← intake buffer (transient — Dream processes these)
│   ├── session_summary/     ← distilled summaries of past sessions
│   ├── entities/<type>/     ← canonical entity pages (person, project, etc.)
│   └── archive/             ← consolidated/absorbed entries (recovery surface)
├── ingested/<id>/           ← original artifacts (PDFs, notes, etc.)
├── sessions/                ← raw conversation transcripts per session
├── dream/                   ← Dream consolidator working files
├── skills/<name>/SKILL.md   ← user-authored skill definitions
└── .durin/                  ← internal indices (FTS5 + LanceDB) — opaque
    └── index/
        ├── fts.sqlite       ← SQLite FTS5 lexical index
        └── lance/           ← LanceDB vector index
```

## Read this, don't edit it

durin (the agent) and Dream (the background consolidator) are the only
processes that should write to `memory/` files. The on-disk format is
the source of truth for what the agent knows — manual edits work
mechanically (the file-watcher picks them up and re-indexes) but
breaking edits will confuse the agent.

If you want to browse / explore / consult: yes, open this folder in
any markdown reader. Recommended setup further down.

If you want to delete: use `durin memory forget <uri>` or edit through
the agent's `memory_store` tool. Don't `rm` individual files.

## Recommended viewers

### Obsidian

Point Obsidian at the workspace root and it becomes a navigable vault.
Key built-in features that work well:

- **Graph view** — see entities and their related fragments as a graph
- **Backlinks** — for each entity, see all fragments that reference it
- **Properties view** — frontmatter (`valid_from`, `entities`, `author`)
  is rendered as sortable/filterable properties
- **Search** — full-text across all `.md`

Plugins that meaningfully improve the experience (none required):

- **Front Matter Title** — replaces hash filenames (`9b6f1c81724a.md`)
  with the entry's `headline` in sidebar / graph / search
- **Dataview** — query language over frontmatter. Example:
  ```dataview
  TABLE headline, valid_from
  FROM "memory/episodic"
  WHERE contains(entities, "person:marcelo")
  SORT valid_from DESC
  ```
- **Graph Analysis** — centrality metrics, communities, paths between
  notes (useful to find "hub" entities in your memory)

### durin webui

`durin gateway` ships a `MemoryGraphView` component that renders the
same on-disk content as an interactive D3 force-graph, plus search /
filter UI. Open the dashboard in your browser to use it without
installing Obsidian.

## Folders that may look weird

- **`memory/pending/`** — often empty. This is the intake buffer where
  new observations land before Dream consolidates them into canonical
  entity pages. If you see entries here, they'll be moved/archived
  on the next Dream pass.
- **`memory/archive/`** — entries that Dream has absorbed into canonical
  pages. Kept around as a recovery surface (you can read the original
  fragment that contributed to a page). Safe to ignore for browsing.

## Where to find more

- Source: `https://github.com/mmarmol/durin`
- Memory architecture: `docs/architecture/memory/00_overview.md`
- Search pipeline: `docs/architecture/memory/03_search_pipeline.md`
- Data layout (this file's source of truth): `docs/architecture/memory/01_data_and_entities.md`
"""


# Per-class `_INDEX.md` content. Each is short — meant as a one-screen
# orientation when a human opens the class folder + a copy-pasteable
# Dataview snippet for browsing.
_CLASS_INDEX_DEFAULT = """\
# Memory class

The entries in this folder are markdown files with YAML frontmatter.
See `../../VAULT_README.md` for full context.

## Recommended view

If you have the Dataview Obsidian plugin installed:

```dataview
TABLE headline, valid_from, entities
FROM "memory"
WHERE file.folder = this.file.folder
SORT valid_from DESC
LIMIT 30
```
"""

_CLASS_INDEX_CONTENT = {
    "stable": """\
# stable — durable facts

Entries the agent (or you) marked as durable. Preferences ("I always
fly window seat"), persistent attributes ("primary email is X"),
fundamental choices that don't drift week-to-week. Survive consolidation;
the canonical entity pages tend to reference these.

## Browse

```dataview
TABLE headline, valid_from, entities
FROM "memory/stable"
SORT valid_from DESC
```
""",
    "episodic": """\
# episodic — observations + conversation fragments

Atomic memories from conversations and the agent's observations. These
are the raw material Dream consolidates into canonical entity pages.
Most numerous class; high turnover (entries get absorbed into entity
pages and moved to `../archive/` over time).

## Browse by entity

```dataview
TABLE headline, valid_from
FROM "memory/episodic"
WHERE contains(entities, "person:")
SORT valid_from DESC
LIMIT 30
```

## Recent (last 30 days)

```dataview
TABLE headline, entities
FROM "memory/episodic"
WHERE date(valid_from) > date(today) - dur(30 days)
SORT valid_from DESC
```
""",
    "corpus": """\
# corpus — chunks of ingested documents

When you (or the agent) ingest a PDF, article, or long document, it
gets split into chunks; each chunk is a markdown file here. The chunks
are searchable individually; the original artifact lives in
`../../ingested/<id>/`.

## Browse by source

```dataview
TABLE headline, source_refs
FROM "memory/corpus"
SORT source_refs ASC
```
""",
    "pending": """\
# pending — intake buffer (transient)

New observations land here before Dream consolidates them into the
appropriate class + canonical entity pages. **Often empty.** If you
see entries, they'll be moved on the next Dream pass.

Safe to ignore for browsing. Don't manually move/edit entries here —
Dream owns this directory.
""",
    "session_summary": """\
# session_summary — past conversation digests

When a session ends, the agent produces a distilled summary and stores
it here. Useful for "what did I talk to the agent about on date X?"
queries.

## Browse chronologically

```dataview
TABLE headline, valid_from
FROM "memory/session_summary"
SORT valid_from DESC
```
""",
}
