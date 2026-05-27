"""Archive helpers for consolidated memory content.

Per `docs/memory/01_data_and_entities.md` §3.6 + §5.3.

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

import yaml

__all__ = ["archive_episodic", "archive_entity"]


_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)$",
    re.DOTALL,
)


def _annotate_frontmatter(
    md_text: str,
    *,
    archived_at: str,
    archived_into: str,
    reason: str | None = None,
) -> str:
    """Inject `archived_at`, `archived_into`, and optional `archived_reason`
    into a markdown file's YAML frontmatter. Returns the modified text.

    Preserves existing frontmatter fields. If the file has no frontmatter,
    a new one is added.
    """
    match = _FRONTMATTER_RE.match(md_text)
    if match:
        front_data = yaml.safe_load(match.group("front")) or {}
        body = match.group("body")
    else:
        front_data = {}
        body = md_text

    front_data["archived_at"] = archived_at
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
    dest.write_text(annotated, encoding="utf-8")
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
    dest.write_text(annotated, encoding="utf-8")
    entity_path.unlink()
    return dest
