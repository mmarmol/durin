"""Scope and type as index predicates.

A retrieval call names the population it wants; both indexes apply that
before their top-k. The vector table filters on ``class_name`` (and the
``id`` prefix for entity pages), the FTS tables on the stored ``type``.
Building both from one value keeps the two legs in agreement.
"""
from __future__ import annotations

from dataclasses import dataclass

# Library material: ingested reference chunks, plus the legacy `corpus`
# class (`memory/corpus/<id>`, FTS type `corpus`, vector `class_name`
# `corpus`) that predates the reference/ingest split and still lives in
# older workspaces. Excluded from the person's memory by default, the
# whole of an explicit library search.
_LIBRARY_CLASSES = ("reference", "corpus")

# Undreamed material: raw session turns (FTS type `session`; never in the
# vector table) and the session summaries the dream writes from them
# (`memory/session_summary/<id>`, both indexes). `scope="undreamed"` is
# exactly this set; `scope="dreamed"` is the person's memory without it.
_SESSION_CLASSES = ("session", "session_summary")


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
        """The predicate behind a ``memory_search`` call.

        Under ``scope="library"`` and ``scope="undreamed"`` ``kinds`` is
        ignored: library rows and session material are never skills, so
        there is nothing for it to select between.
        """
        if scope == "library":
            where = "class_name IN (" + ", ".join(_sql_quote(x) for x in _LIBRARY_CLASSES) + ")"
            return cls(where, _LIBRARY_CLASSES, None)
        if scope == "undreamed":
            where = "class_name IN (" + ", ".join(_sql_quote(x) for x in _SESSION_CLASSES) + ")"
            return cls(where, _SESSION_CLASSES, None)
        if scope not in ("all", "dreamed"):
            return cls.none()
        if kinds == "skill":
            return cls("class_name = 'skill'", ("skill",), None)
        excluded = _LIBRARY_CLASSES
        if scope == "dreamed":
            excluded += _SESSION_CLASSES
        if kinds == "fact":
            excluded += ("skill",)
        where = "class_name NOT IN (" + ", ".join(_sql_quote(x) for x in excluded) + ")"
        return cls(where, None, excluded)

    @classmethod
    def entity_pages(cls, entity_type: str | None = None) -> "ScopePredicate":
        """Entity pages only, optionally of one type (``person``, ``place`` …)."""
        where = "class_name = 'entity_page'"
        if entity_type:
            where += f" AND id LIKE {_sql_quote(entity_type + ':%')}"
        return cls(where, ("entity",), None)
