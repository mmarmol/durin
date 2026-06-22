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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.refine_dream import add_tombstone
from durin.utils.atomic_write import atomic_write_text
from durin.utils.file_lock import cross_process_lock

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
    atomic_write_text(p, json.dumps(sorted(refs)))


def _mutate_deleted(workspace: Path, fn: "Callable[[set[str]], None]") -> None:
    """Locked read-modify-write for the tombstone set.

    Acquires cross_process_lock on .deleted.json.lock, reloads the set under
    the lock, applies fn in place, and saves.  Mirrors mutate_config in
    durin/config/loader.py.  See docs/internals/concurrency.md (hazard #16).
    """
    with cross_process_lock(_deleted_path(workspace)):
        refs = _load_deleted(workspace)
        fn(refs)
        _save_deleted(workspace, refs)


def is_deleted(workspace: Path, ref: str) -> bool:
    return ref in _load_deleted(workspace)


def clear_delete_tombstone(workspace: Path, ref: str) -> None:
    """User override: re-creating a deleted entity clears its tombstone."""
    def _clear(refs: set[str]) -> None:
        refs.discard(ref)

    _mutate_deleted(workspace, _clear)


def _archive_file(src: Path, dest: Path, stamp: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    stamp_lines = "".join(f"{k}: {v}\n" for k, v in stamp.items())
    if text.startswith("---\n"):
        text = "---\n" + stamp_lines + text[4:]
    else:
        text = "---\n" + stamp_lines + "---\n\n" + text
    atomic_write_text(dest, text)
    src.unlink()


def delete_entity(workspace: Path, ref: str, *, reason: str = "user_delete") -> Path | None:
    """Archive the entity + record a permanent delete tombstone."""
    type_, _, slug = ref.partition(":")
    src = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
    _mutate_deleted(workspace, lambda refs: refs.add(ref))
    if not src.exists():
        return None
    dest = Path(workspace) / "memory" / "archive" / "entities" / type_ / f"{slug}.md"
    now = datetime.now(timezone.utc).isoformat()
    _archive_file(src, dest, {"deleted": "true", "deleted_at": now,
                              "deleted_reason": reason})
    _alias_remove(workspace, ref)                   # surgical: drop one ref
    return dest


def delete_reference(workspace: Path, ref: str, *, reason: str = "user_delete") -> Path | None:
    """Archive the reference (+ its chunk sidecar) + record a tombstone."""
    slug = ref.split(":", 1)[1] if ":" in ref else ref
    src = Path(workspace) / "memory" / "references" / f"{slug}.md"
    _mutate_deleted(workspace, lambda refs: refs.add(ref))
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
        atomic_write_text(
            dest, _strip_archive_stamp(arch.read_text(encoding="utf-8")))
        arch.unlink()
        restored = True
    if restored:
        _alias_refresh(workspace, absorbed)         # surgical: re-add the restored ref
    add_tombstone(workspace, canonical, absorbed)   # do_not_absorb
    return restored


# The alias index is a workspace-shared in-memory cache that absorb() mutates in
# place (idx.remove). After a file move we keep it consistent SURGICALLY — one
# ref removed / re-added — never a full rebuild (which re-walks every entity on
# disk; invalidate_alias_index is reserved for out-of-band edits and tests).
def _alias_remove(workspace: Path, ref: str) -> None:
    try:
        from durin.memory.aliases_cache import get_shared_alias_index
        get_shared_alias_index(Path(workspace) / "memory").remove(ref)
    except Exception:  # pragma: no cover - best effort
        pass


def _alias_refresh(workspace: Path, ref: str) -> None:
    try:
        from durin.memory.aliases_cache import get_shared_alias_index
        type_, _, slug = ref.partition(":")
        path = Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"
        page = EntityPage.from_file(path)
        if page is not None:
            get_shared_alias_index(Path(workspace) / "memory").refresh_for(page, slug)
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
