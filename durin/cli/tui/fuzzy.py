"""Simple fuzzy subsequence matcher for TUI filtering.

A query matches text if every character of the query appears in the
text **in order** (not necessarily contiguous).  Case-insensitive.
No scoring or ranking — just a boolean filter.
"""

from __future__ import annotations


def fuzzy_match(query: str, text: str) -> bool:
    """Return ``True`` if *query* is a subsequence of *text* (case-insensitive)."""
    if not query:
        return True
    if not text:
        return False
    q = query.lower()
    t = text.lower()
    it = iter(t)
    return all(ch in it for ch in q)
