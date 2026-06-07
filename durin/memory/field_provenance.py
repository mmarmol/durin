"""Per-field provenance: who set each attribute/relation, from where, when.

Distinct from the PAGE-level author (durin/memory/provenance.py, 2 values
user_authored/agent_created). This is FIELD-level, 3 values, and drives the
write-time precedence (user > dream > agent). The two coexist (design §2.4).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

__all__ = [
    "FieldAuthor", "make_entry", "author_rank", "incoming_wins",
    "relation_prov_key", "coerce_relation_prov",
]

# Q1: relation provenance is keyed by a (to, type) composite — merge-safe,
# replacing the fragile positional `{index}` form. The separator is a control
# char that can't appear in a ref or relation type.
_REL_KEY_SEP = "\x1f"

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


def relation_prov_key(to: str, rtype: str) -> str:
    """Composite key for a relation's provenance entry: ``(to, type)``.

    Replaces the legacy positional ``{index}`` key so a merge can fold relation
    provenance by key (like attributes), not by fragile re-indexing.
    """
    return f"{to}{_REL_KEY_SEP}{rtype}"


def coerce_relation_prov(
    raw: Any, relations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return relation provenance as a ``{composite_key: entry}`` dict.

    Lenient migration: accepts the legacy index-keyed LIST form
    (``[{"index": i, ...}]``) and converts each entry by resolving ``index``
    against ``relations`` to recover ``(to, type)``; a dict is returned as a
    shallow copy. Each emitted entry carries ``to``/``type`` so readers never
    need the positional index again.
    """
    if isinstance(raw, dict):
        return dict(raw)
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for e in raw:
            if not isinstance(e, dict):
                continue
            idx = e.get("index")
            if isinstance(idx, int) and 0 <= idx < len(relations):
                rel = relations[idx]
                to, rtype = rel.get("to"), rel.get("type")
                if to and rtype:
                    entry = {k: v for k, v in e.items() if k != "index"}
                    out[relation_prov_key(str(to), str(rtype))] = {
                        "to": to, "type": rtype, **entry,
                    }
    return out


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
