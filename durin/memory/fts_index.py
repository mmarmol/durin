"""FTS5 lexical index for memory entries and entity pages.

Per `docs/architecture/memory/02_indexing.md` §5: one SQLite database at
``<workspace>/.durin/index/fts.sqlite`` with two FTS5 virtual tables
sharing a bookkeeping table:

  - ``memory_fts`` (``unicode61 remove_diacritics 2``) — Latin,
    Cyrillic, Greek, Arabic and similar whitespace-separated scripts.
  - ``memory_fts_trigram`` (``trigram``) — CJK + substring queries.
  - ``fts_meta`` (regular table) — per-uri mtime + indexed_at.

Both FTS5 tables carry the same row schema (uri / path / type /
entity_type / text). Every write goes to both: routing at query time
is the search pipeline's concern (Phase 3).

Thread safety: a single :class:`FTSIndex` instance is **not** thread
safe (SQLite connection is per-instance). Production typically opens
one per process; tests open per workspace.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

__all__ = ["FTSHit", "FTSIndex", "fts_index_path"]


def fts_index_path(workspace: Path) -> Path:
    """Resolve the canonical FTS sqlite path for *workspace*."""
    return Path(workspace) / ".durin" / "index" / "fts.sqlite"


@dataclass(frozen=True)
class FTSHit:
    """One search result row."""

    uri: str
    path: str
    type: str
    entity_type: Optional[str]


# --- Schema ---------------------------------------------------------------

_SCHEMA = [
    # Default tokenizer table (Latin and similar).
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        uri UNINDEXED,
        path UNINDEXED,
        type UNINDEXED,
        entity_type UNINDEXED,
        text,
        tokenize = 'unicode61 remove_diacritics 2'
    );
    """,
    # Trigram tokenizer table (CJK, substring search).
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_trigram USING fts5(
        uri UNINDEXED,
        path UNINDEXED,
        type UNINDEXED,
        entity_type UNINDEXED,
        text,
        tokenize = 'trigram'
    );
    """,
    # Bookkeeping — lets the indexer skip files whose mtime hasn't
    # changed since the last sync, and lets `durin memory stats` see
    # the indexed count cheaply.
    """
    CREATE TABLE IF NOT EXISTS fts_meta (
        uri TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        indexed_at TEXT NOT NULL
    );
    """,
]


class FTSIndex:
    """Connection-owning wrapper around the FTS5 sqlite database.

    Use as a context manager for short-lived sessions, or call
    :meth:`open` + :meth:`close` for daemon-style long-lived state.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- lifecycle --------------------------------------------------------

    @classmethod
    def open(cls, workspace: Path) -> "FTSIndex":
        """Open (or create) the index for *workspace* and ensure the
        schema is present.
        """
        path = fts_index_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` is intentional: the caller is
        # responsible for serialising access. SQLite itself serialises
        # writes via the WAL.
        conn = sqlite3.connect(str(path), check_same_thread=False)
        # WAL keeps reads non-blocking against writes — useful when the
        # search pipeline reads while the watcher is upserting.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for stmt in _SCHEMA:
            conn.executescript(stmt)
        conn.commit()
        return cls(conn)

    def close(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.Error:
            pass
        self._conn.close()

    def __enter__(self) -> "FTSIndex":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- writes -----------------------------------------------------------

    def upsert(
        self,
        *,
        uri: str,
        path: str,
        type_: str,
        entity_type: Optional[str],
        text: str,
        mtime: float,
    ) -> None:
        """Insert (or replace) one row across both tables + meta.

        Idempotent on ``uri``: a prior row for the same uri is deleted
        before the new rows go in, so re-running on the same file
        leaves exactly one row per table.
        """
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM memory_fts WHERE uri = ?", (uri,))
            cur.execute(
                "DELETE FROM memory_fts_trigram WHERE uri = ?", (uri,),
            )
            cur.execute("DELETE FROM fts_meta WHERE uri = ?", (uri,))
            cur.execute(
                "INSERT INTO memory_fts (uri, path, type, entity_type, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (uri, path, type_, entity_type, text),
            )
            cur.execute(
                "INSERT INTO memory_fts_trigram "
                "(uri, path, type, entity_type, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (uri, path, type_, entity_type, text),
            )
            cur.execute(
                "INSERT INTO fts_meta (uri, mtime, indexed_at) "
                "VALUES (?, ?, ?)",
                (uri, mtime, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    def delete_by_uri(self, uri: str) -> None:
        """Remove a uri from both FTS tables and the meta table."""
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM memory_fts WHERE uri = ?", (uri,))
            cur.execute(
                "DELETE FROM memory_fts_trigram WHERE uri = ?", (uri,),
            )
            cur.execute("DELETE FROM fts_meta WHERE uri = ?", (uri,))
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    def delete_by_uris(self, uris: list[str]) -> int:
        """Remove many uris in a single transaction (chunked IN clauses).

        Used by the health-check self-heal to prune orphan rows for files
        deleted out-of-band in one pass — keeps reconciling a bulk
        deletion cheap instead of one commit per row. Returns the count of
        distinct uris requested.
        """
        unique = [u for u in dict.fromkeys(uris) if u]
        if not unique:
            return 0
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                cur.execute(
                    f"DELETE FROM memory_fts WHERE uri IN ({placeholders})",
                    chunk,
                )
                cur.execute(
                    f"DELETE FROM memory_fts_trigram WHERE uri IN ({placeholders})",
                    chunk,
                )
                cur.execute(
                    f"DELETE FROM fts_meta WHERE uri IN ({placeholders})",
                    chunk,
                )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        return len(unique)

    def clear(self) -> None:
        """Drop all rows from both tables. Used by ``durin reindex``."""
        cur = self._conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("DELETE FROM memory_fts")
            cur.execute("DELETE FROM memory_fts_trigram")
            cur.execute("DELETE FROM fts_meta")
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise

    # --- queries ----------------------------------------------------------

    def search(self, query: str, *, limit: int = 50) -> list[FTSHit]:
        """Run a query against ``memory_fts`` (unicode61)."""
        return self._search("memory_fts", query, limit=limit)

    def search_trigram(self, query: str, *, limit: int = 50) -> list[FTSHit]:
        """Run a query against ``memory_fts_trigram`` (trigram)."""
        return self._search("memory_fts_trigram", query, limit=limit)

    def count(self) -> int:
        """Return how many distinct uris are indexed (meta-table count)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM fts_meta"
        ).fetchone()
        return int(row[0]) if row else 0

    def known_uris(self) -> Iterator[tuple[str, float]]:
        """Iterate ``(uri, mtime)`` for every indexed row.

        Used by ``durin reindex`` to skip files whose mtime matches the
        last-indexed value.
        """
        for uri, mtime in self._conn.execute(
            "SELECT uri, mtime FROM fts_meta"
        ):
            yield uri, mtime

    # --- internals --------------------------------------------------------

    def _search(self, table: str, query: str, *, limit: int) -> list[FTSHit]:
        """Both tables share the same row schema. We use a MATCH clause
        with the FTS5 query as-is; the caller's job to sanitise."""
        cur = self._conn.execute(
            f"SELECT uri, path, type, entity_type FROM {table} "
            f"WHERE {table} MATCH ? LIMIT ?",
            (query, limit),
        )
        return [
            FTSHit(uri=u, path=p, type=t, entity_type=et)
            for (u, p, t, et) in cur.fetchall()
        ]
