"""Parser for entity pages: `memory/entities/<type>/<slug>.md`.

Each page is a markdown document with a YAML frontmatter header followed
by a free-form body. Per ``docs/18_entity_centric_plan.md`` §3.2 the
**minimum required** frontmatter is:

    type: <type>             # lowercase [a-z][a-z0-9_]*
    name: <display name>
    aliases: [<list>]
    dream_processed_through: <msg_idx|null>
    created_at: <iso>
    updated_at: <iso>

The dream can add **emergent fields** (``identifiers``, future ones).
Those are preserved verbatim through a round-trip parse → write — the
parser does not enforce a closed schema. This is the architectural
choice that makes "amplio y podar" + open vocabulary work in practice.

Lenient on read (malformed YAML returns ``None`` from
:func:`EntityPage.from_text`); strict on write
(:func:`EntityPage.save` validates fields before serializing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "EntityPage",
    "EntityPageError",
]


_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# Known top-level fields the parser explicitly understands. Anything
# outside this set lands in ``extra`` and is preserved on write.
_KNOWN_FIELDS = frozenset(
    {
        "type",
        "name",
        "aliases",
        "dream_processed_through",
        "created_at",
        "updated_at",
    }
)


class EntityPageError(Exception):
    """Raised on write-path validation failures."""


@dataclass
class EntityPage:
    """One entity page parsed from disk (or constructed in-memory)."""

    type: str                                 # e.g. "person"
    name: str                                 # display name
    aliases: list[str] = field(default_factory=list)
    body: str = ""

    # Optional cursor + timestamps.
    dream_processed_through: int | str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Emergent fields preserved as-is. The dream may add ``identifiers``,
    # ``related``, etc. without parser changes. Survives round-trip.
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def from_text(cls, text: str) -> "EntityPage | None":
        """Parse a markdown file's contents. Lenient: return None on bad YAML.

        The body is everything after the second ``---`` separator. If the
        frontmatter doesn't have the required minimum fields (``type``,
        ``name``), returns ``None``.
        """
        frontmatter, body = _split_frontmatter(text)
        if frontmatter is None:
            return None
        try:
            data = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            return None
        if not isinstance(data, dict):
            return None

        type_ = data.get("type")
        name = data.get("name")
        if not isinstance(type_, str) or not isinstance(name, str):
            return None

        aliases_raw = data.get("aliases") or []
        if not isinstance(aliases_raw, list):
            aliases_raw = []
        aliases = [str(a) for a in aliases_raw if isinstance(a, (str, int, float))]

        cursor = data.get("dream_processed_through")
        created_at = _coerce_dt(data.get("created_at"))
        updated_at = _coerce_dt(data.get("updated_at"))

        extra = {k: v for k, v in data.items() if k not in _KNOWN_FIELDS}

        return cls(
            type=type_,
            name=name,
            aliases=aliases,
            body=body,
            dream_processed_through=cursor,
            created_at=created_at,
            updated_at=updated_at,
            extra=extra,
        )

    @classmethod
    def from_file(cls, path: Path) -> "EntityPage | None":
        """Read and parse a page file. Returns None on malformed input.

        Raises ``FileNotFoundError`` if the file doesn't exist (caller
        should know whether the path was supposed to be there).
        """
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_text(text)

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        """Render frontmatter + body. Validates basic shape."""
        self._validate()
        frontmatter: dict[str, Any] = {
            "type": self.type,
            "name": self.name,
            "aliases": list(self.aliases),
        }
        if self.dream_processed_through is not None:
            frontmatter["dream_processed_through"] = self.dream_processed_through
        if self.created_at is not None:
            frontmatter["created_at"] = self.created_at.isoformat()
        if self.updated_at is not None:
            frontmatter["updated_at"] = self.updated_at.isoformat()
        # Emergent fields appended after known ones for stable diff ordering.
        for key, value in self.extra.items():
            frontmatter[key] = value

        # Use block style for readability — entity pages are user-readable.
        yaml_block = yaml.safe_dump(
            frontmatter,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        body = self.body.rstrip("\n")
        if body:
            return f"---\n{yaml_block}---\n\n{body}\n"
        return f"---\n{yaml_block}---\n"

    def save(self, path: Path) -> None:
        """Write the page to *path*. Parent dirs created as needed."""
        text = self.to_markdown()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------
    # derived
    # ------------------------------------------------------------------

    @property
    def entity_ref(self) -> str:
        """Canonical entity reference: ``<type>:<slug>``.

        ``slug`` is derived from the *name* (lowercase, spaces →
        underscores). For pages already on disk, the *filename* is the
        authoritative slug — use :meth:`slug_from_path` if you have the
        path.
        """
        return f"{self.type}:{_slugify(self.name)}"

    @staticmethod
    def slug_from_path(path: Path) -> str:
        """Extract slug from a page filename: ``marcelo.md`` → ``marcelo``."""
        return Path(path).stem

    def identifying_strings(self) -> list[str]:
        """Return all strings that identify this entity (for alias_index).

        Combines ``name``, ``aliases``, and emergent fields. For each
        ``extra`` field we extract identifying strings one level deep:

        - Scalar string → include verbatim.
        - List of strings → include each item.
        - Dict with scalar / list-of-string values → include the values
          (NOT the keys, NOT nested dicts).

        This matches the shapes the dream LLM emits in practice (see
        ``docs/research/phase0_results.md``): some times ``identifiers``
        comes as a flat list, sometimes as a typed dict ``{email: ...,
        slack: ...}``. Both surface here without prescribing the shape.

        Deduplicates while preserving insertion order.
        """
        out: list[str] = []
        seen: set[str] = set()

        def push(value: object) -> None:
            if not isinstance(value, str):
                return
            value = value.strip()
            if not value or value in seen:
                return
            out.append(value)
            seen.add(value)

        push(self.name)
        for alias in self.aliases:
            push(alias)

        for key, value in self.extra.items():
            if isinstance(value, str):
                push(value)
            elif isinstance(value, list):
                for item in value:
                    push(item)
            elif isinstance(value, dict):
                # One level deep: values can be scalars or lists of scalars.
                for sub_value in value.values():
                    if isinstance(sub_value, str):
                        push(sub_value)
                    elif isinstance(sub_value, list):
                        for item in sub_value:
                            push(item)
                    # Nested dicts (dict-of-dict) are intentionally skipped:
                    # at that depth the values are more likely metadata
                    # than identity strings. If a future shape demands it,
                    # we'll add tests + extend, not blindly recurse.
        return out

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        if not _TYPE_PATTERN.match(self.type):
            raise EntityPageError(
                f"invalid type {self.type!r}: must match [a-z][a-z0-9_]*"
            )
        if not isinstance(self.name, str) or not self.name.strip():
            raise EntityPageError("name is required and must be non-empty")
        if not isinstance(self.aliases, list):
            raise EntityPageError("aliases must be a list")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split ``---\\nYAML\\n---\\nbody`` into (yaml_text, body).

    Returns (None, full_text) when there's no frontmatter block.
    """
    if not text.startswith("---"):
        return None, text
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    frontmatter = match.group(1)
    body = text[match.end():]
    return frontmatter, body


def _coerce_dt(value: Any) -> datetime | None:
    """Best-effort datetime coercion. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    """Conservative slug: lowercase, non-alnum → underscore, trim."""
    lowered = name.lower()
    slug = _SLUG_CHARS.sub("_", lowered).strip("_")
    return slug or "unnamed"
