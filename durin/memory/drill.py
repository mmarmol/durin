"""Resolve a markdown URI (``path.md#anchor``) to the addressed section.

Used by the ``memory_drill`` tool to fetch a single turn from a session,
a single section from an ingested document, or a whole memory entry —
the same URI scheme that appears in :class:`MemoryEntry.source_refs`.

A section is everything from a header line ``## <anchor>`` up to (but
excluding) the next header at the same or higher level. ``memory_drill``
is read-only and free of any LLM call; it's just a structured ``Read``.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["DrillError", "drill", "extract_section"]


class DrillError(ValueError):
    """Raised when a drill URI cannot be resolved."""


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


# G6 (audit fourth pass, 2026-05-28). `memory_search` emits the
# canonical URI shape `memory/entity_page/<type>:<slug>` for entity
# page hits, but the on-disk file lives at
# `memory/entities/<type>/<slug>.md`. Pre-G6 `drill()` resolved the
# URI literally and failed for every canonical hit — every
# `=== CANONICAL: person:marcelo ===` the agent received was
# undrillable. The translation below is a pure URI-shape mapping;
# any other path is passed through unchanged.
_ENTITY_PAGE_URI_RE = re.compile(
    r"^memory/entity_page/(?P<type>[^:/]+):(?P<slug>[^/]+?)(?:\.md)?$"
)


def _translate_entity_page_uri(path_part: str) -> str:
    """Map `memory/entity_page/<type>:<slug>(.md)?` to
    `memory/entities/<type>/<slug>.md`. Returns the original string
    when it doesn't match (so non-entity URIs pass through)."""
    match = _ENTITY_PAGE_URI_RE.match(path_part)
    if not match:
        return path_part
    type_ = match.group("type")
    slug = match.group("slug")
    return f"memory/entities/{type_}/{slug}.md"


def _translate_skill_uri(path_part: str) -> str:
    """Map a skill uri to its on-disk SKILL.md path.

    Skills surface under two uri forms: the on-disk
    ``skills/<slug>/SKILL.md`` (what `memory_search` emits and what
    resolves literally) and the internal index id ``skill/<slug>``
    (FTS/vector). Translate the latter to the former so an agent that
    drills the internal id still resolves the file. Any other string
    passes through unchanged."""
    if path_part.startswith("skills/") and path_part.endswith("/SKILL.md"):
        return path_part
    if path_part.startswith("skill/"):
        from durin.memory.paths import skill_path_from_uri

        return skill_path_from_uri(path_part)
    return path_part


def drill(workspace: Path, uri: str) -> str:
    """Return the markdown section addressed by ``uri``.

    URIs accepted:

    - ``sessions/<key>.md#turn-N`` — one turn of a rendered session view.
    - ``ingested/<id>/source.md#section`` — one section of an ingested doc.
    - ``memory/<class>/<id>`` (no extension, no anchor) — the full memory
      entry file. The ``.md`` suffix is appended automatically.
    - ``memory/entity_page/<type>:<slug>`` — canonical entity page URI as
      emitted by `memory_search` for the CANONICAL section. Audit G6
      (2026-05-28) added the translation to the on-disk path
      ``memory/entities/<type>/<slug>.md``.
    - ``memory/archive/<class>/<id>.md`` — archived content surfaced by
      ``memory_search(scope='archive')``. The full relative path under
      ``memory/archive/`` is emitted by that path after G6.
    - ``skills/<slug>/SKILL.md`` — a skill page as emitted by
      `memory_search` for the SKILL section. The internal index id
      ``skill/<slug>`` is also accepted and translated to this path.
    - Any absolute or workspace-relative path with optional ``#anchor``.

    Returns the section text (or full file content when no anchor is
    present), with a trailing newline.
    """
    if not uri or not uri.strip():
        raise DrillError("uri is required")

    if "#" in uri:
        path_part, anchor = uri.rsplit("#", 1)
    else:
        path_part, anchor = uri, ""

    # G6: translate the canonical entity page URI shape before resolving.
    # Skills (H28, 2026-06-03): translate the internal `skill/<slug>` id
    # to its on-disk `skills/<slug>/SKILL.md` path.
    original_uri = uri
    path_part = _translate_entity_page_uri(path_part)
    path_part = _translate_skill_uri(path_part)

    raw = Path(path_part).expanduser()
    full_path = raw if raw.is_absolute() else (workspace / raw)

    # Memory entries are addressed without an extension (memory/<class>/<id>);
    # the on-disk file is .md.
    if not full_path.suffix and full_path.parent.name and not full_path.is_file():
        candidate = full_path.with_suffix(".md")
        if candidate.is_file():
            full_path = candidate

    if not full_path.is_file():
        # G6: surface the original URI (not just the resolved disk path)
        # in the error so the agent can debug a canonical lookup that
        # missed without parsing absolute paths.
        raise DrillError(
            f"file not found: {full_path} (uri: {original_uri})"
        )

    text = full_path.read_text(encoding="utf-8")
    if not anchor:
        return text if text.endswith("\n") else text + "\n"

    return extract_section(text, anchor)


def extract_section(text: str, anchor: str) -> str:
    """Pull the markdown section under the header ``## <anchor>``.

    Header level is determined from the actual header line. The section
    ends at the next header at the same or higher (smaller number of
    ``#``) level, or at end of file.
    """
    lines = text.splitlines()
    start = -1
    start_level = 0
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if m and m.group(2).strip() == anchor:
            start = i
            start_level = len(m.group(1))
            break

    if start == -1:
        raise DrillError(f"anchor not found: {anchor}")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        m = _HEADER_RE.match(lines[i])
        if m and len(m.group(1)) <= start_level:
            end = i
            break

    return "\n".join(lines[start:end]).rstrip() + "\n"
