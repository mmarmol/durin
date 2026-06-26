"""The pass/fail contract for a routing agent node: it ends its reply with a
verdict line, parsed here. Default FAIL so an unparseable answer loops back
rather than silently passing.

Multi-way routing uses parse_label instead: the agent ends its reply with one
of the declared case labels; the last matching line wins."""
from __future__ import annotations

import re
from typing import Iterable

_PUNCT = re.compile(r"^[^\w]+|[^\w]+$")


def normalize_label(s: str) -> str:
    """Strip leading/trailing punctuation and uppercase.

    Used both when matching agent output and when validating that declared case
    labels are distinct — two labels that normalize to the same form would cause
    a silent mis-route, so the spec rejects them at parse time.
    """
    return _PUNCT.sub("", s).upper()


def parse_verdict(text: str) -> bool:
    """Return True iff the first non-empty line of *text* starts with 'PASS' (case-insensitive)."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s.upper().startswith("PASS")
    return False


def parse_label(text: str, labels: Iterable[str]) -> str | None:
    """Return the label (from *labels*) that the last matching non-empty line equals.

    Matching is case-insensitive and tolerates surrounding punctuation/whitespace
    on the line (e.g. "GROUNDED." or "**missing**" match "GROUNDED" and "MISSING").
    The whole stripped, de-punctuated line must equal the label exactly — a label
    that is a substring of a longer word does not match.

    Scans lines from the end; returns the original label (preserving its case) from
    the *labels* iterable on the first match, or None if no line matches any label.
    """
    # Build a lookup: normalized form -> original label (last one wins for duplicates).
    label_map: dict[str, str] = {}
    for label in labels:
        norm = normalize_label(label)
        if norm:
            label_map[norm] = label

    lines = (text or "").splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        norm_line = normalize_label(stripped)
        if norm_line in label_map:
            return label_map[norm_line]
    return None
