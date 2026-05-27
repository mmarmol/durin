"""Memory subsystem directory layout (workspace-scoped).

The layout matches `docs/08_memory_phase2_proposal.md` §0c.2. All
memory artifacts live inside the agent workspace so different
workspaces have independent memory:

    <workspace>/
    ├── sessions/                  (existing — managed by SessionManager)
    ├── ingested/<id>/             (Phase 1.5)
    ├── memory/
    │   ├── stable/<id>.md         (class A + C)
    │   ├── episodic/<id>.md       (class B)
    │   ├── corpus/<id>.md         (class D)
    │   └── pending/<id>.md        (class F)
    └── dream/cursor.json          (Phase 3)
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from durin.utils.helpers import ensure_dir

__all__ = [
    "MEMORY_CLASSES",
    "dream_dir",
    "ingested_dir",
    "ingested_entry_dir",
    "memory_class_dir",
    "memory_dir",
    "walk_class",
    "walk_memory",
]

MEMORY_CLASSES: tuple[str, ...] = ("stable", "episodic", "corpus", "pending")

# All directory names that `walk_class` accepts. Includes `entities`
# (recursive) and `archive` (recursive across archived classes), beyond
# the canonical entry classes.
_KNOWN_CLASS_DIRS: tuple[str, ...] = MEMORY_CLASSES + ("entities", "archive")


def memory_dir(workspace: Path) -> Path:
    """Return the memory root inside the given workspace."""
    return ensure_dir(workspace / "memory")


def memory_class_dir(workspace: Path, class_name: str) -> Path:
    """Return the per-class memory subdirectory.

    Raises ``ValueError`` if ``class_name`` is not one of the canonical
    memory classes (``stable``, ``episodic``, ``corpus``, ``pending``).
    """
    if class_name not in MEMORY_CLASSES:
        raise ValueError(
            f"unknown memory class: {class_name!r}; "
            f"expected one of {MEMORY_CLASSES}"
        )
    return ensure_dir(memory_dir(workspace) / class_name)


def ingested_dir(workspace: Path) -> Path:
    """Return the root directory for ingested source artifacts."""
    return ensure_dir(workspace / "ingested")


def ingested_entry_dir(workspace: Path, entry_id: str) -> Path:
    """Return the directory dedicated to a single ingested artifact."""
    return ensure_dir(ingested_dir(workspace) / entry_id)


def dream_dir(workspace: Path) -> Path:
    """Return the dream subsystem's working directory."""
    return ensure_dir(workspace / "dream")


def walk_memory(
    workspace: Path,
    *,
    include_archive: bool = False,
) -> Iterator[Path]:
    """Walk `memory/` and yield every `.md` file that should be processed.

    Single chokepoint for "which markdown files under memory/ does the
    rest of the system see". Every caller in the codebase (indexer,
    entity_ranker, alias bootstrap, etc.) MUST use this walker so the
    exclusion rules (archive, pending) stay consistent.

    Excludes by default:
    - `memory/archive/**` — consolidated content; reachable only via
      explicit recovery surface (`01_data_and_entities.md` §3.6).
    - `memory/pending/**` — intake buffer; not user-visible yet.

    Set ``include_archive=True`` to include archived files (for
    recovery / diagnostic surfaces only).
    """
    root = workspace / "memory"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if parts and parts[0] == "pending":
            continue
        if parts and parts[0] == "archive" and not include_archive:
            continue
        yield path


def walk_class(
    workspace: Path,
    class_name: str,
    *,
    include_archive: bool = False,
) -> Iterator[Path]:
    """Walk a specific memory class and yield its `.md` files.

    Convenience wrapper around :func:`walk_memory` for callers that need
    a single class. Reasons to prefer this over `class_dir.glob("*.md")`:

    - Centralizes the "exclude archive when nested" rule.
    - Raises on typo in ``class_name`` instead of silently yielding
      nothing.
    - Recurses into `entities/<type>/` and `archive/<class>/` so the
      caller does not need a two-level loop.

    Valid class names:
    - ``stable``, ``episodic``, ``corpus``, ``pending`` — entries
      (top-level `.md` files under each).
    - ``entities`` — recurses through `<type>/<slug>.md`.
    - ``archive`` — recurses through nested archive structure; only
      meaningful when the caller explicitly wants archived content
      (recovery / diagnostic surfaces).

    ``include_archive`` is only consulted when ``class_name != "archive"``
    AND the caller wants archived items of that class included. In
    practice this matters for the ``entities`` walk: passing
    ``include_archive=True`` also yields `archive/entities/**`.
    """
    if class_name not in _KNOWN_CLASS_DIRS:
        raise ValueError(
            f"unknown memory class: {class_name!r}; "
            f"expected one of {_KNOWN_CLASS_DIRS}"
        )
    class_dir = workspace / "memory" / class_name
    if not class_dir.is_dir():
        return
    if class_name in ("entities", "archive"):
        yield from sorted(class_dir.rglob("*.md"))
    else:
        yield from sorted(class_dir.glob("*.md"))
    if include_archive and class_name != "archive":
        nested = workspace / "memory" / "archive" / class_name
        if nested.is_dir():
            yield from sorted(nested.rglob("*.md") if class_name == "entities" else nested.glob("*.md"))
