"""Entity / reference deletion + un-merge via archive + tombstones (§2.13/§2.14).

Deletion is structural and user-driven: the file moves to ``memory/archive/``
(git-tracked, traceable — never hard-deleted) and a permanent tombstone is
recorded so the extract dream does NOT re-create the entity from stale
sessions. The user overrides by explicitly re-creating it (which clears the
tombstone).

Un-merge restores an absorbed entity from the archive and writes the
``do_not_absorb`` tombstone (refine_dream) so the refine never re-merges the pair.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from durin.memory.refine_dream import add_tombstone

__all__ = [
    "is_deleted",
    "clear_delete_tombstone",
    "delete_entity",
    "delete_reference",
    "unmerge",
]

_DELETED_FILE = ".deleted.json"


def _deleted_path(workspace: Path) -> Path:
    return Path(workspace) / "memory" / _DELETED_FILE


def _load_deleted(workspace: Path) -> set[str]:
    p = _deleted_path(workspace)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_deleted(workspace: Path, refs: set[str]) -> None:
    p = _deleted_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(refs)), encoding="utf-8")


def is_deleted(workspace: Path, ref: str) -> bool:
    return ref in _load_deleted(workspace)


def clear_delete_tombstone(workspace: Path, ref: str) -> None:
    """User override: re-creating a deleted entity clears its tombstone."""
    refs = _load_deleted(workspace)
    if ref in refs:
        refs.discard(ref)
        _save_deleted(workspace, refs)


def _archive_file(src: Path, dest: Path, stamp: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    stamp_lines = "".join(f"{k}: {v}\n" for k, v in stamp.items())
    if text.startswith("---\n"):
        text = "---\n" + stamp_lines + text[4:]
    else:
        text = "---\n" + stamp_lines + "---\n\n" + text
    dest.write_text(text, encoding="utf-8")
    src.unlink()


def delete_entity(workspace: Path, ref: str, *, reason: str = "user_delete") -> Path | None:
    """Archive the entity + record a permanent delete tombstone."""
    type_, _, slug = ref.partition(":")
    src = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    refs = _load_deleted(workspace)
    refs.add(ref)
    _save_deleted(workspace, refs)
    if not src.exists():
        return None
    dest = Path(workspace) / "memory" / "archive" / "entities" / type_ / f"{slug}.md"
    now = datetime.now(timezone.utc).isoformat()
    _archive_file(src, dest, {"deleted": "true", "deleted_at": now,
                              "deleted_reason": reason})
    _invalidate_alias_cache(workspace)
    return dest


def delete_reference(workspace: Path, ref: str, *, reason: str = "user_delete") -> Path | None:
    """Archive the reference (+ its chunk sidecar) + record a tombstone."""
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    src = Path(workspace) / "memory" / "references" / f"{slug}.md"
    refs = _load_deleted(workspace)
    refs.add(ref)
    _save_deleted(workspace, refs)
    if not src.exists():
        return None
    dest = Path(workspace) / "memory" / "archive" / "references" / f"{slug}.md"
    now = datetime.now(timezone.utc).isoformat()
    _archive_file(src, dest, {"deleted": "true", "deleted_at": now,
                              "deleted_reason": reason})
    chunks = src.with_name(f"{slug}.chunks.jsonl")
    if chunks.exists():
        chunks.rename(dest.with_name(f"{slug}.chunks.jsonl"))
    return dest


def unmerge(workspace: Path, canonical: str, absorbed: str,
            *, reason: str = "user_unmerge") -> bool:
    """Restore ``absorbed`` from the archive and tombstone the pair.

    Returns True if a file was restored. Always writes the ``do_not_absorb``
    tombstone so the refine dream never re-merges this pair.
    """
    type_, _, slug = absorbed.partition(":")
    arch = Path(workspace) / "memory" / "archive" / "entities" / type_ / f"{slug}.md"
    dest = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    restored = False
    if arch.exists() and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_strip_archive_stamp(arch.read_text(encoding="utf-8")),
                        encoding="utf-8")
        arch.unlink()
        restored = True
    _invalidate_alias_cache(workspace)              # the shared index dropped it on absorb
    add_tombstone(workspace, canonical, absorbed)   # do_not_absorb
    return restored


def _invalidate_alias_cache(workspace: Path) -> None:
    """The alias index is a workspace-shared cache mutated in place during
    absorb(); after a file move it must be rebuilt so candidates reflect disk."""
    try:
        from durin.memory.aliases_cache import invalidate_alias_index
        invalidate_alias_index(Path(workspace) / "memory")
    except Exception:  # pragma: no cover - best effort
        pass


_ARCHIVE_KEYS = {"archived_into", "archived_at", "archived_reason",
                 "deleted", "deleted_at", "deleted_reason"}


def _strip_archive_stamp(text: str) -> str:
    """Drop archive/delete frontmatter stamps so a restored page is clean again
    (otherwise its ``archived_into`` makes the alias index treat it as merged)."""
    lines = [ln for ln in text.splitlines()
             if ln.split(":", 1)[0].strip() not in _ARCHIVE_KEYS]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
