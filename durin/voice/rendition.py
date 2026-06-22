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


@dataclass
class SpokenRendition:
    """Result of separating spoken from displayed text."""

    spoken: str       # what the TTS pronounces
    displayed: str    # the unchanged full agent text (goes to the thread)
    summarized: bool  # telemetry: did we summarize?
    lead_present: bool  # telemetry: was a usable lead found?


def _model_led_lead(speakable: str) -> tuple[str, bool]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", speakable) if p.strip()]
    if not paras:
        return speakable, False
    lead = paras[0]
    lead_present = len(lead.split()) >= 4  # prose heuristic for telemetry
    return lead, lead_present


async def _aux_summary(speakable: str, summarizer) -> tuple[str, bool]:
    if summarizer is None:
        return _model_led_lead(speakable)  # degrade
    try:
        s = await summarizer(speakable)
        if s and s.strip():
            return s.strip(), True
    except Exception as e:  # noqa: BLE001 — degrade rather than fail the turn
        logger.warning("aux summarizer failed ({}); degrading to lead", e)
    return _model_led_lead(speakable)


async def build_spoken_rendition(
    full_text: str,
    *,
    mode: str = "model_led",
    long_threshold_words: int = 60,
    summarizer=None,
    pointer: str = "The full answer is on screen.",
    labels: SpeakableLabels | None = None,
) -> SpokenRendition:
    speakable = speakable_transform(full_text, labels=labels)
    if mode == "verbatim" or len(speakable.split()) <= long_threshold_words:
        return SpokenRendition(
            spoken=speakable, displayed=full_text, summarized=False, lead_present=False
        )
    if mode == "model_led":
        lead, lead_present = _model_led_lead(speakable)
        spoken = f"{lead} {pointer}".strip()
        return SpokenRendition(
            spoken=spoken, displayed=full_text, summarized=True, lead_present=lead_present
        )
    if mode == "aux_summary":
        summary, ok = await _aux_summary(speakable, summarizer)
        spoken = f"{summary} {pointer}".strip()
        return SpokenRendition(
            spoken=spoken, displayed=full_text, summarized=True, lead_present=ok
        )
    raise ValueError(f"Unknown spoken-rendition mode: {mode}")
