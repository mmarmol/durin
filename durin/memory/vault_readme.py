"""Generate a `VAULT_README.md` at workspace root explaining the
on-disk layout for human viewers (Obsidian users, future webui /
desktop app users, anyone browsing the markdown files directly).

P9 (2026-05-30) ships this as part of the vault-friendly read-only
viewer plan (see ``docs/20_pendings.md::P9``). The README is
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

logger = logging.getLogger(__name__)

__all__ = ["VAULT_README_FILENAME", "ensure_vault_readme"]

VAULT_README_FILENAME = "VAULT_README.md"


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
        target.write_text(_README_CONTENT, encoding="utf-8")
        logger.info("vault readme written at %s", target)
        return True
    except OSError as exc:
        logger.warning(
            "failed to write vault readme at %s: %s — continuing",
            target, exc,
        )
        return False


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
- Memory architecture: `docs/memory/00_overview.md`
- Search pipeline: `docs/memory/03_search_pipeline.md`
- Data layout (this file's source of truth): `docs/memory/01_data_and_entities.md`
"""
