"""Memory entry persistence — markdown + YAML frontmatter.

Round-trip: ``save_entry`` writes a :class:`MemoryEntry` as a markdown
file with a YAML frontmatter block; ``load_entry`` parses such a file
back into a :class:`MemoryEntry`. The on-disk format is a strict
superset of CommonMark with a leading frontmatter block delimited by
``---`` lines.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from durin.memory.schema import MemoryEntry

__all__ = [
    "FrontmatterError",
    "load_entry",
    "save_entry",
    "split_frontmatter",
]


_DELIMITER = "---"


class FrontmatterError(ValueError):
    """Raised when a memory entry file's frontmatter cannot be parsed."""


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into (frontmatter dict, body).

    Raises :class:`FrontmatterError` if the document does not start with
    a ``---`` delimiter or the frontmatter block is unclosed.
    """
    if not text.startswith(f"{_DELIMITER}\n"):
        raise FrontmatterError("missing leading --- delimiter")

    end = text.find(f"\n{_DELIMITER}\n", len(_DELIMITER) + 1)
    if end == -1:
        raise FrontmatterError("unclosed frontmatter block")

    fm_text = text[len(_DELIMITER) + 1 : end]
    body = text[end + len(f"\n{_DELIMITER}\n") :].lstrip("\n").rstrip("\n")

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"malformed YAML in frontmatter: {exc}") from exc

    if not isinstance(fm, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping, got {type(fm).__name__}"
        )

    return fm, body


def save_entry(entry: MemoryEntry, path: Path) -> None:
    """Write a memory entry to ``path`` as markdown + YAML frontmatter."""
    payload = entry.model_dump(exclude={"body"}, mode="json")
    yaml_block = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    body = entry.body.rstrip("\n")
    content = f"{_DELIMITER}\n{yaml_block}{_DELIMITER}\n"
    if body:
        content += f"\n{body}\n"
    path.write_text(content, encoding="utf-8")


def load_entry(path: Path) -> MemoryEntry:
    """Read and parse a memory entry from ``path``.

    Raises :class:`FrontmatterError` for parse-level issues and
    :class:`pydantic.ValidationError` for schema violations.
    """
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    try:
        return MemoryEntry(**fm, body=body)
    except ValidationError:
        raise
