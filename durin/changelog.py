"""Locate and parse the project changelog for runtime consultation.

CHANGELOG.md lives at the repo root. In a built wheel it is force-included as
package data at ``durin/CHANGELOG.md`` (see pyproject.toml); in an editable/dev
checkout it is read from the repo root. This module finds it either way and
splits it into per-version sections so ``durin changelog`` can show the entry
for the running version.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Section:
    """One ``## <version> — <date>`` block of the changelog."""

    version: str
    heading: str
    body: str


def _version_token(heading_line: str) -> str:
    """Version from a ``## 0.3.3 — 2026-07-19`` heading line ("0.3.3")."""
    text = heading_line[3:].strip()  # drop the leading "## "
    return text.split()[0] if text else ""


def parse(text: str) -> list[Section]:
    """Split changelog text into sections, newest first (file order).

    A section starts at a line beginning with ``## `` (two hashes + space);
    ``### `` subsections stay inside the body. Content before the first ``## ``
    heading (the file preamble) is ignored.
    """
    sections: list[Section] = []
    heading: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if heading is not None:
            body = "\n".join(lines).rstrip("\n")
            sections.append(Section(_version_token(heading), heading, body))

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            heading = line
            lines = [line]
        elif heading is not None:
            lines.append(line)
    flush()
    return sections


def find(sections: list[Section], version: str) -> Section | None:
    """The section whose version exactly equals ``version``."""
    for sec in sections:
        if sec.version == version:
            return sec
    return None


def versions(sections: list[Section]) -> list[str]:
    """All version tokens, newest first."""
    return [s.version for s in sections]


def current(sections: list[Section]) -> tuple[Section | None, bool]:
    """Section for the running version.

    ``(section, False)`` on an exact match; ``(newest, True)`` when the running
    version has no entry (e.g. an unreleased dev build); ``(None, False)`` when
    there are no sections at all.
    """
    from durin import __version__

    hit = find(sections, __version__)
    if hit is not None:
        return hit, False
    if sections:
        return sections[0], True
    return None, False


def _locate() -> str | None:
    """Changelog text: package resource first, repo-root fallback (dev)."""
    try:
        from importlib.resources import files

        res = files("durin").joinpath("CHANGELOG.md")
        if res.is_file():
            return res.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    root = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    if root.is_file():
        return root.read_text(encoding="utf-8")
    return None
