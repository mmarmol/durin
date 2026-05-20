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

from durin.utils.helpers import ensure_dir

__all__ = [
    "MEMORY_CLASSES",
    "dream_dir",
    "ingested_dir",
    "ingested_entry_dir",
    "memory_class_dir",
    "memory_dir",
]

MEMORY_CLASSES: tuple[str, ...] = ("stable", "episodic", "corpus", "pending")


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
