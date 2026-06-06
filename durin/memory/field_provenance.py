"""Per-field provenance: who set each attribute/relation, from where, when.

Distinct from the PAGE-level author (durin/memory/provenance.py, 2 values
user_authored/agent_created). This is FIELD-level, 3 values, and drives the
write-time precedence (user > dream > agent). The two coexist (design §2.4).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

__all__ = ["FieldAuthor", "make_entry", "author_rank", "incoming_wins"]

FieldAuthor = Literal["user", "agent", "dream"]
_VALID_AUTHORS = frozenset({"user", "agent", "dream"})
# Precedence rank: higher wins. user > dream > agent.
_RANK: dict[str, int] = {"agent": 0, "dream": 1, "user": 2}


def make_entry(*, source_ref: str, author: str, at: datetime) -> dict[str, str]:
    """Build a per-field provenance entry ``{source_ref, extracted_at, author}``."""
    if author not in _VALID_AUTHORS:
        raise ValueError(
            f"author {author!r} must be one of {sorted(_VALID_AUTHORS)}"
        )
    return {
        "source_ref": source_ref,
        "extracted_at": at.isoformat(),
        "author": author,
    }


def author_rank(author: str) -> int:
    """Precedence rank for an author string. Unknown/missing ranks lowest."""
    return _RANK.get(author, -1)


def incoming_wins(*, existing: dict[str, Any] | None,
                  incoming: dict[str, Any]) -> bool:
    """Decide whether ``incoming`` overwrites ``existing`` for one field.

    Rule (design §2.4): higher author-rank wins (user > dream > agent);
    same rank → newer ``extracted_at`` wins; missing existing → incoming wins.
    """
    if not existing:
        return True
    er = author_rank(str(existing.get("author", "")))
    ir = author_rank(str(incoming.get("author", "")))
    if ir != er:
        return ir > er
    # same rank: recency tiebreak (ISO-8601 strings sort chronologically)
    return str(incoming.get("extracted_at", "")) >= str(existing.get("extracted_at", ""))
