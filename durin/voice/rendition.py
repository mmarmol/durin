"""Spoken rendition: turn a full agent response into TTS-speakable text.

Pure module. The deterministic ``speakable_transform`` is the always-on floor
(code/tables/URLs are described, never read); the summary policy lives in
``build_spoken_rendition``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class SpeakableLabels:
    """User-facing descriptive phrases (injected so they can be localized)."""

    code_block: str = "the code is on screen"
    image: str = "an image"
    link: str = "a link"
    table: str = "a table"


_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.MULTILINE)
# Only * and ~~ — underscore emphasis is skipped to avoid mangling snake_case.
_EMPHASIS_RE = re.compile(r"(\*{1,3}|~~)(.+?)\1", re.DOTALL)
_LIST_MARKER_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+", re.MULTILINE)


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return "-" in s and set(s) <= set("|-: ")


def _describe_tables(text: str, table_label: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i]:
            j = i
            while j < len(lines) and "|" in lines[j]:
                j += 1
            block = lines[i:j]
            if len(block) >= 2 and any(_is_table_sep(b) for b in block):
                out.append(f" {table_label} ")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def speakable_transform(text: str, *, labels: SpeakableLabels | None = None) -> str:
    """Describe non-speakable content instead of reading it. Always safe to run."""
    lab = labels or SpeakableLabels()
    text = _FENCE_RE.sub(f" {lab.code_block} ", text)
    text = _describe_tables(text, lab.table)
    text = _IMG_RE.sub(lambda m: f" {m.group(1)} " if m.group(1) else f" {lab.image} ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub(f" {lab.link} ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _RULE_RE.sub("", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _LIST_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
