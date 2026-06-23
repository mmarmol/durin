"""The pass/fail contract for a routing agent node: it ends its reply with a
verdict line, parsed here. Default FAIL so an unparseable answer loops back
rather than silently passing."""
from __future__ import annotations


def parse_verdict(text: str) -> bool:
    """Return True iff the first non-empty line of *text* starts with 'PASS' (case-insensitive)."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s:
            return s.upper().startswith("PASS")
    return False
