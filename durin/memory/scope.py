"""Scope and type as index predicates.

A retrieval call names the population it wants; both indexes apply that
before their top-k. The vector table filters on ``class_name`` (and the
``id`` prefix for entity pages), the FTS tables on the stored ``type``.
Building both from one value keeps the two legs in agreement.
"""
from __future__ import annotations

from dataclasses import dataclass

# Library material: ingested reference chunks. Excluded from the person's
# memory by default, the whole of an explicit library search.
_LIBRARY_CLASS = "reference"


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class ScopePredicate:
    vector_where: str | None
    fts_include: tuple[str, ...] | None
    fts_exclude: tuple[str, ...] | None

    @classmethod
    def none(cls) -> "ScopePredicate":
        return cls(None, None, None)

    @classmethod
    def for_search(cls, scope: str, kinds: str = "all") -> "ScopePredicate":
        """The predicate behind a ``memory_search`` call."""
        if scope == "library":
            return cls(f"class_name = {_sql_quote(_LIBRARY_CLASS)}", (_LIBRARY_CLASS,), None)
        if scope not in ("all", "dreamed", "undreamed"):
            return cls.none()
        if kinds == "skill":
            return cls("class_name = 'skill'", ("skill",), None)
        excluded = (_LIBRARY_CLASS, "skill") if kinds == "fact" else (_LIBRARY_CLASS,)
        if len(excluded) == 1:
            where = f"class_name != {_sql_quote(excluded[0])}"
        else:
            where = "class_name NOT IN (" + ", ".join(_sql_quote(x) for x in excluded) + ")"
        return cls(where, None, excluded)

    @classmethod
    def entity_pages(cls, entity_type: str | None = None) -> "ScopePredicate":
        """Entity pages only, optionally of one type (``person``, ``place`` …)."""
        where = "class_name = 'entity_page'"
        if entity_type:
            where += f" AND id LIKE {_sql_quote(entity_type + ':%')}"
        return cls(where, ("entity",), None)
