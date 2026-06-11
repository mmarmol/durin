"""Archive helpers for consolidated memory content.

Per `docs/architecture/memory/01_data_and_entities.md` §3.6 + §5.3.

When Dream consolidates an episodic entry into a canonical entity page,
the original episodic moves to `memory/archive/episodic/<id>.md` (not
deleted). When absorption merges entity B into A, B's page moves to
`memory/archive/entities/<type>/<slug>.md`. Both operations annotate
the archived file with `archived_at` and `archived_into` frontmatter
fields so provenance is auditable.

`memory/archive/` is excluded from all default search paths (see
`walk_memory` in `durin/memory/paths.py`); recovery is opt-in via
explicit flags.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from durin.utils.atomic_write import atomic_write_text

import yaml

__all__ = ["archive_episodic", "archive_entity", "archive_generic_entry"]


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)$",
    re.DOTALL,
)


def _annotate_frontmatter(
    md_text: str,
    *,
    archived_at: str,
    archived_into: str | None,
    reason: str | None = None,
) -> str:
    """Inject `archived_at`, `archived_into`, and optional `archived_reason`
    into a markdown file's YAML frontmatter. Returns the modified text.

    Preserves existing frontmatter fields. If the file has no frontmatter,
    a new one is added. ``archived_into`` is omitted when ``None`` or empty
    (generic archives without a consolidation target).
    """
    match = _FRONTMATTER_RE.match(md_text)
    if match:
        front_data = yaml.safe_load(match.group("front")) or {}
        body = match.group("body")
    else:
        front_data = {}
        body = md_text

    front_data["archived_at"] = archived_at
    if archived_into:
        front_data["archived_into"] = archived_into
    if reason is not None:
        front_data["archived_reason"] = reason

    new_front = yaml.safe_dump(
        front_data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip()
    return f"---\n{new_front}\n---\n\n{body.lstrip()}"


def archive_episodic(
    workspace: Path,
    episodic_path: Path,
    *,
    into_uri: str,
    reason: str | None = None,
) -> Path:
    """Move an episodic entry to `memory/archive/episodic/<id>.md`.

    Annotates the archived file's frontmatter with `archived_at` (now,
    UTC ISO 8601), `archived_into` (the URI it was consolidated into),
    and `archived_reason` if provided.

    Returns the new path. Raises `FileNotFoundError` if the source does
    not exist; raises `ValueError` if the source is not under
    `memory/episodic/`.
    """
    if not episodic_path.exists():
        raise FileNotFoundError(f"episodic entry not found: {episodic_path}")
    expected_root = workspace / "memory" / "episodic"
    try:
        episodic_path.relative_to(expected_root)
    except ValueError as exc:
        raise ValueError(
            f"not an episodic entry under {expected_root}: {episodic_path}"
        ) from exc

    archived_at = datetime.now(timezone.utc).isoformat()
    content = episodic_path.read_text(encoding="utf-8")
    annotated = _annotate_frontmatter(
        content,
        archived_at=archived_at,
        archived_into=into_uri,
        reason=reason,
    )

    dest_dir = workspace / "memory" / "archive" / "episodic"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / episodic_path.name
    atomic_write_text(dest, annotated)
    episodic_path.unlink()
    return dest


def archive_entity(
    workspace: Path,
    entity_path: Path,
    *,
    into_uri: str,
    reason: str | None = None,
) -> Path:
    """Move an entity page to `memory/archive/entities/<type>/<slug>.md`.

    Annotates the archived file's frontmatter with `archived_at` (now,
    UTC ISO 8601), `archived_into` (the URI of the canonical entity it
    was absorbed into), and `archived_reason` if provided (judge
    reasoning, manual operator note, etc.).

    The `<type>` segment is preserved from the source path. Returns the
    new path. Raises `FileNotFoundError` if the source does not exist;
    raises `ValueError` if the source is not under `memory/entities/`.
    """
    if not entity_path.exists():
        raise FileNotFoundError(f"entity page not found: {entity_path}")
    entities_root = workspace / "memory" / "entities"
    try:
        rel = entity_path.relative_to(entities_root)
    except ValueError as exc:
        raise ValueError(
            f"not an entity page under {entities_root}: {entity_path}"
        ) from exc
    if len(rel.parts) < 2:
        raise ValueError(
            f"entity path must be <type>/<slug>.md, got {rel}"
        )

    archived_at = datetime.now(timezone.utc).isoformat()
    content = entity_path.read_text(encoding="utf-8")
    annotated = _annotate_frontmatter(
        content,
        archived_at=archived_at,
        archived_into=into_uri,
        reason=reason,
    )

    dest_dir = workspace / "memory" / "archive" / "entities" / rel.parts[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / entity_path.name
    atomic_write_text(dest, annotated)
    entity_path.unlink()
    return dest


def archive_generic_entry(
    workspace: Path,
    entry_path: Path,
    *,
    reason: str | None = None,
) -> Path:
    """Move a non-episodic, non-entity memory entry to ``memory/archive/<class>/<id>.md``.

    For ``stable``, ``corpus``, and ``session_summary`` entries. Mirrors
    :func:`archive_episodic` (frontmatter annotation + atomic move) but
    without the ``into_uri`` field — generic archives don't have a
    "consolidated into" target. ``archived_reason`` is recorded as
    ``"user_forget"`` by default callers.

    Returns the new path. Raises ``FileNotFoundError`` if the source
    does not exist; raises ``ValueError`` if the source is not under one
    of the supported classes.
    """
    supported = ("stable", "corpus", "session_summary")
    if not entry_path.exists():
        raise FileNotFoundError(f"entry not found: {entry_path}")
    memory_root = workspace / "memory"
    try:
        rel = entry_path.relative_to(memory_root)
    except ValueError as exc:
        raise ValueError(
            f"not under {memory_root}: {entry_path}"
        ) from exc
    if len(rel.parts) < 2 or rel.parts[0] not in supported:
        raise ValueError(
            f"unsupported class for generic archive (got '{rel.parts[0] if rel.parts else ''}'; "
            f"expected one of {supported}). Use archive_episodic / archive_entity "
            "for episodic / entities."
        )

    archived_at = datetime.now(timezone.utc).isoformat()
    content = entry_path.read_text(encoding="utf-8")
    annotated = _annotate_frontmatter(
        content,
        archived_at=archived_at,
        archived_into=None,
        reason=reason,
    )

    dest_dir = memory_root / "archive" / rel.parts[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / entry_path.name
    atomic_write_text(dest, annotated)
    entry_path.unlink()
    return dest
