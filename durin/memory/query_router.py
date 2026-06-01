"""Query analysis + FTS5 routing decisions.

Per `docs/architecture/memory/03_search_pipeline.md` §3.1 + §5.1:

  1. **NFC normalise** + **whitespace collapse** so the FTS query
     matches whatever was indexed.
  2. **Count CJK characters** (CJK Unified Ideographs, Hiragana,
     Katakana, Hangul).
  3. **Route to one of three lexical paths**:
     - ``UNICODE61``      — `memory_fts` (default tokenizer)
     - ``TRIGRAM``        — `memory_fts_trigram` for CJK + substring
     - ``LIKE_SUBSTRING`` — fallback for short CJK queries that
       trigram cannot tokenise (< 3 chars)

The decision is pure (no I/O) and side-effect free. The lexical
search layer consumes :class:`RoutingDecision` and executes the
appropriate SQL.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "LexicalRoute",
    "RoutingDecision",
    "count_cjk_chars",
    "decide_lexical_route",
    "normalize_query",
]


# FTS5 operators that count as "structural" not "content" — their
# length does not invalidate the "all tokens ≥ 3 chars" condition.
_OPERATORS: frozenset[str] = frozenset({"AND", "OR", "NOT", "NEAR"})


# CJK character ranges, per Unicode 15.x. The lists cover the
# spec-relevant blocks: ideographs (CJK Unified), Hiragana, Katakana,
# Hangul (precomposed + jamo). Compatibility ideographs are included
# because they appear in real-world text.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0x1100, 0x11FF),   # Hangul Jamo
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0x20000, 0x2A6DF), # CJK Unified Ideographs Extension B
)


_WHITESPACE_RE = re.compile(r"\s+")


# Identifier patterns (P3.3). Order matters: longer / more specific
# patterns first so a URL doesn't get truncated to "://" segment.
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # HTTPS / HTTP URLs.
    re.compile(r"https?://[^\s]+"),
    # File paths — must contain `/` and at least one path segment
    # with an extension or three+ chars. Skips leading punctuation.
    re.compile(r"(?:/|[A-Za-z0-9._-]+/)[A-Za-z0-9._/-]+\.[A-Za-z0-9]+"),
    re.compile(r"/[A-Za-z0-9._/-]+(?:/[A-Za-z0-9._-]+)+"),
    # UUIDs (with or without dashes).
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # Email addresses.
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


def _detect_auto_keywords(query: str) -> Optional[str]:
    """Pick the first identifier-looking token from *query*, if any.

    Returns the matched substring verbatim so the lexical search can
    quote it. None when no identifier is present.

    Doc 03 §3.1 footnote: version strings (`v1.2.3`) are intentionally
    NOT matched — they're too ambiguous.
    """
    for pattern in _IDENTIFIER_PATTERNS:
        match = pattern.search(query)
        if match:
            return match.group(0)
    return None


class LexicalRoute(str, enum.Enum):
    """Which lexical retrieval path the search pipeline should use."""

    UNICODE61 = "unicode61"
    TRIGRAM = "trigram"
    LIKE_SUBSTRING = "like_substring"


@dataclass(frozen=True)
class RoutingDecision:
    """Output of :func:`decide_lexical_route`."""

    normalized_query: str
    route: LexicalRoute
    cjk_chars: int
    keywords: Optional[str] = None
    # P3.3: auto-detected identifier token. When the query contains
    # an email, URL, UUID, or file path, surface it here so the
    # search pipeline applies the lexical boost without the agent
    # having to pass ``keywords`` explicitly.
    auto_keywords: Optional[str] = None


def count_cjk_chars(text: object) -> int:
    """Number of characters in CJK Unicode blocks.

    Non-strings return ``0``. Punctuation outside the ideograph blocks
    (Latin commas, CJK fullwidth punctuation, etc.) is not counted —
    only "content" characters.
    """
    if not isinstance(text, str):
        return 0
    count = 0
    for ch in text:
        cp = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= cp <= hi:
                count += 1
                break
    return count


def normalize_query(query: str) -> str:
    """NFC-normalise + collapse internal whitespace + trim ends."""
    if not isinstance(query, str):
        return ""
    norm = unicodedata.normalize("NFC", query)
    return _WHITESPACE_RE.sub(" ", norm).strip()


def decide_lexical_route(
    query: str,
    *,
    keywords: Optional[str] = None,
) -> RoutingDecision:
    """Decide which FTS5 table (or LIKE fallback) handles *query*.

    The routing thresholds match Hermes-agent
    ``hermes_state.py:2197-2280`` (verified pattern, per doc 03 §3.1):

    - **CJK ≥ 3 + every non-operator token ≥ 3 chars** → trigram.
    - **CJK > 0 with short CJK tokens** → LIKE fallback (trigram
      cannot match tokens shorter than 3 chars).
    - **Otherwise (Latin only, or short query)** → unicode61.
    """
    normalized = normalize_query(query)
    cjk = count_cjk_chars(normalized)

    if cjk == 0:
        route = LexicalRoute.UNICODE61
    else:
        tokens = normalized.split()
        non_operator = [t for t in tokens if t.upper() not in _OPERATORS]
        if cjk >= 3 and all(len(t) >= 3 for t in non_operator):
            route = LexicalRoute.TRIGRAM
        else:
            route = LexicalRoute.LIKE_SUBSTRING

    return RoutingDecision(
        normalized_query=normalized,
        route=route,
        cjk_chars=cjk,
        keywords=keywords,
        auto_keywords=_detect_auto_keywords(normalized),
    )
