"""Query analysis + FTS5 routing decisions.

  1. **NFC normalise** + **whitespace collapse** so the FTS query
     matches whatever was indexed.
  2. **Count CJK characters** (CJK Unified Ideographs, Hiragana,
     Katakana, Hangul).
  3. **Route to one of three lexical paths**:
     - ``UNICODE61``      — `memory_fts` (default tokenizer)
     - ``TRIGRAM``        — `memory_fts_trigram` for CJK + substring
     - ``LIKE_SUBSTRING`` — fallback for short CJK queries that
       trigram cannot tokenise (< 3 chars), or for a TRIGRAM-eligible
       query whose required term (a quoted phrase, a `keywords`
       token/phrase, or `auto_keywords`) is itself under 3 chars —
       trigram cannot tokenise that term either.

The decision is pure (no I/O) and side-effect free. The lexical
search layer consumes :class:`RoutingDecision` and executes the
appropriate SQL. ``_split_terms``/``_extract_phrases`` live here
(not in ``lexical_search``) because the routing decision itself
needs to know a query's required-term lengths before any FTS5
expression is built; ``lexical_search`` imports them back from here
so both stay in lock-step with one parse of the query.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "LexicalRoute",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_TOKENS",
    "RoutingDecision",
    "count_cjk_chars",
    "decide_lexical_route",
    "normalize_query",
]


# Hard bound on what counts as a "query". Every token of the query gets
# quoted into one FTS5 MATCH expression, and sqlite's memory for a MATCH
# grows with the term count — a whole transcript passed as a query (~50k
# tokens) costs hundreds of MB per call and matches nothing. Any caller
# with a longer text wants retrieval *about* it, not retrieval *of* it,
# so the head is a faithful stand-in. Chars bound CJK (no whitespace
# tokens); tokens bound Latin.
MAX_QUERY_CHARS = 2048
MAX_QUERY_TOKENS = 64


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


# Identifier patterns. Order matters: longer / more specific
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

    Version strings (`v1.2.3`) are intentionally NOT matched — they're
    too ambiguous.
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
    # True when the incoming query exceeded MAX_QUERY_CHARS/MAX_QUERY_TOKENS
    # and normalized_query is its bounded head.
    truncated: bool = False
    # Auto-detected identifier token. When the query contains an email,
    # URL, UUID, or file path, surface it here so the search pipeline
    # applies the lexical boost without the agent having to pass
    # ``keywords`` explicitly.
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

    The routing thresholds match the Hermes-agent verified pattern:

    - **CJK ≥ 3 + every non-operator token ≥ 3 chars** → trigram,
      unless a required term (see below) is itself under 3 chars, in
      which case → LIKE fallback (trigram cannot tokenise it either).
    - **CJK > 0 with short CJK tokens** → LIKE fallback (trigram
      cannot match tokens shorter than 3 chars).
    - **Otherwise (Latin only, or short query)** → unicode61.

    "Required term" is whatever `lexical_search` would AND into the
    FTS5 expression for this query: a balanced quoted phrase in
    *query*, plus every token/quoted group of the effective keywords
    (*keywords* if given, else the auto-detected identifier) — the
    same split :func:`_split_terms` performs for the expression
    builder.
    """
    normalized = normalize_query(query)
    truncated = False
    if len(normalized) > MAX_QUERY_CHARS:
        normalized = normalized[:MAX_QUERY_CHARS].rstrip()
        truncated = True
    tokens = normalized.split()
    if len(tokens) > MAX_QUERY_TOKENS:
        normalized = " ".join(tokens[:MAX_QUERY_TOKENS])
        truncated = True
    cjk = count_cjk_chars(normalized)
    auto_keywords = _detect_auto_keywords(normalized)

    if cjk == 0:
        route = LexicalRoute.UNICODE61
    else:
        tokens = normalized.split()
        non_operator = [t for t in tokens if t.upper() not in _OPERATORS]
        if cjk >= 3 and all(len(t) >= 3 for t in non_operator):
            route = LexicalRoute.TRIGRAM
            required, _optional = _split_terms(
                normalized, keywords or auto_keywords)
            if any(len(t) < 3 for t in required):
                route = LexicalRoute.LIKE_SUBSTRING
        else:
            route = LexicalRoute.LIKE_SUBSTRING

    return RoutingDecision(
        normalized_query=normalized,
        route=route,
        cjk_chars=cjk,
        keywords=keywords,
        auto_keywords=auto_keywords,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# term splitting — shared by the router (short-required-term check above)
# and lexical_search's FTS5 expression builder / LIKE fallback.
# ---------------------------------------------------------------------------


def _split_terms(query: str, keywords: str | None) -> tuple[list[str], list[str]]:
    """Raw, un-quoted ``(required, optional)`` terms behind one query.

    Required = balanced quoted phrases in ``query`` plus every token of
    ``keywords``. Optional = the loose (non-phrase) tokens of ``query``.
    Shared by :func:`decide_lexical_route` (the short-required-term
    reroute), ``build_fts_expression`` (which quotes these for FTS5),
    and the LIKE fallback (which interpolates them directly into
    ``LIKE`` patterns) so all three agree on what counts as required.
    """
    phrases, loose, balanced = _extract_phrases(query or "")
    if not balanced:
        loose = [tok.replace('"', "") for tok in loose]
    required: list[str] = [p for p in phrases if p.strip()]
    if keywords and keywords.strip():
        kw_phrases, kw_loose, kw_balanced = _extract_phrases(keywords)
        if not kw_balanced:
            kw_loose = [tok.replace('"', "") for tok in kw_loose]
        required += [t for t in kw_loose if t]
        required += [p for p in kw_phrases if p.strip()]
    optional = [t for t in loose if t]
    return required, optional


def _extract_phrases(query: str) -> tuple[list[str], list[str], bool]:
    """Split ``query`` into ``(phrases, loose_tokens, balanced)``.

    A double-quoted substring becomes one entry in ``phrases``; the
    remainder is whitespace-split into ``loose_tokens``. ``balanced``
    is False when the query contains an odd number of unescaped
    double quotes; callers degrade to token-only parsing in that
    case.

    Examples
    --------
    >>> _extract_phrases('"Marcelo Marmol" lives in Spain')
    (['Marcelo Marmol'], ['lives', 'in', 'Spain'], True)
    >>> _extract_phrases('hello world')
    ([], ['hello', 'world'], True)
    >>> _extract_phrases('Marcelo "incomplete')
    ([], ['Marcelo', '"incomplete'], False)
    """
    phrases: list[str] = []
    loose: list[str] = []
    chunks: list[str] = []  # text between quoted segments
    cursor = 0
    open_idx: Optional[int] = None
    for i, ch in enumerate(query):
        if ch != '"':
            continue
        if open_idx is None:
            chunks.append(query[cursor:i])
            open_idx = i
        else:
            phrases.append(query[open_idx + 1:i])
            cursor = i + 1
            open_idx = None
    if open_idx is not None:
        # Unbalanced — drop everything from the dangling quote onward
        # so the tokenless tail can't bias the AND-join. Tokens before
        # the lone quote stay; the rest is discarded.
        loose_tokens = query[:open_idx].split()
        return [], loose_tokens, False
    chunks.append(query[cursor:])
    for chunk in chunks:
        loose.extend(chunk.split())
    return phrases, loose, True
