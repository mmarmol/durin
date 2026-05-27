"""Indexer state file: `<workspace>/.durin/index/meta.json`.

Per `docs/memory/02_indexing.md` §2 + §7.2 the file carries the
indexer's notion of "what does the index correspond to". Today the
relevant fields are:

- ``schema_version`` (int) — bumped when the indexer's row shape or
  derivation rules change.
- ``embedding_model_id`` (str) — set when the index was last built /
  rebuilt; on startup the indexer refuses to operate if this differs
  from the model currently in code (which would silently produce
  results against incompatible vectors).
- ``last_full_rebuild`` (ISO str or ``null``) — most recent
  ``durin reindex`` time.
- ``previous_models`` (tuple of strings) — audit trail of model
  migrations.

Phase 0 scope (per ``docs/memory/09_implementation_roadmap.md`` §3
deliverable 6) is **the field plumbing**. The §7.2 enforcement
consumer (refuse to operate on mismatch, auto-rebuild if absent)
lands in a later phase that wires this into the indexer entry point.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "IndexMeta",
    "load_index_meta",
    "meta_path",
    "save_index_meta",
]


# Bumped any time the on-disk schema (frontmatter fields, archive
# layout, derivation rules) changes in a way that requires a reindex.
# Phase 0 introduces walker + archive + slug + EntityPage v2 + decay
# fields — they are all additive against v1 entries, but the indexer
# now skips the top-level archive and that's enough to call it v2.
CURRENT_SCHEMA_VERSION: int = 2


@dataclass(frozen=True)
class IndexMeta:
    """Snapshot of the indexer's state file."""

    schema_version: int
    embedding_model_id: str
    last_full_rebuild: Optional[str] = None
    previous_models: tuple[str, ...] = field(default_factory=tuple)


def meta_path(workspace: Path) -> Path:
    """Resolve the canonical meta.json path for *workspace*."""
    return Path(workspace) / ".durin" / "index" / "meta.json"


def load_index_meta(workspace: Path) -> Optional[IndexMeta]:
    """Read ``meta.json``; return ``None`` when missing or unreadable.

    Returning ``None`` for both "absent" and "corrupt" is deliberate:
    the caller's fresh-install path handles both by rebuilding the
    index. Throwing on corruption would block the agent from ever
    booting.
    """
    path = meta_path(workspace)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    schema_version = raw.get("schema_version")
    embedding_model_id = raw.get("embedding_model_id")
    if not isinstance(schema_version, int) or not isinstance(
        embedding_model_id, str
    ):
        return None
    last_full_rebuild = raw.get("last_full_rebuild")
    if last_full_rebuild is not None and not isinstance(last_full_rebuild, str):
        last_full_rebuild = None
    previous_models_raw = raw.get("previous_models") or ()
    if isinstance(previous_models_raw, list):
        previous_models = tuple(
            m for m in previous_models_raw if isinstance(m, str)
        )
    else:
        previous_models = ()
    return IndexMeta(
        schema_version=schema_version,
        embedding_model_id=embedding_model_id,
        last_full_rebuild=last_full_rebuild,
        previous_models=previous_models,
    )


def save_index_meta(workspace: Path, meta: IndexMeta) -> None:
    """Persist *meta* atomically (temp + rename).

    Creates ``.durin/index/`` as needed. The temp file lives in the
    same directory as the destination so the rename stays on the same
    filesystem.
    """
    path = meta_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(meta)
    # tuple → list for JSON; asdict already does this for the
    # `previous_models` field by virtue of dataclass behavior, but
    # be explicit.
    payload["previous_models"] = list(meta.previous_models)
    data = json.dumps(payload, indent=2, sort_keys=False)
    fd, tmp = tempfile.mkstemp(
        prefix="meta.json.", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the temp file on failure so we don't
        # leave behind half-written sidecars.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
