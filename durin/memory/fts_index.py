"""FTS5 lexical index for memory entries and entity pages.

One SQLite database at
``<workspace>/.durin/index/fts.sqlite`` with two FTS5 virtual tables
sharing a bookkeeping table:

  - ``memory_fts`` (``porter unicode61 remove_diacritics 2``) — Latin,
    Cyrillic, Greek, Arabic and similar whitespace-separated scripts,
    with English Porter stemming (write/writes/writing share a token).
  - ``memory_fts_trigram`` (``trigram``) — CJK + substring queries.
  - ``fts_meta`` (regular table) — per-uri mtime + indexed_at.

Both FTS5 tables carry the same row schema (uri / path / type /
entity_type / text). Every write goes to both: routing at query time
is the search pipeline's concern.

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

from durin.utils.sqlite_util import connect as _sqlite_connect, execute_write as _execute_write

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
    # Default tokenizer table (Latin and similar). Porter stemming
    # is enabled so that `write` / `writes` / `writing` are treated
    # as the same token — without it, a query using one form never
    # matches a doc using another. Porter is an
    # English suffix-stripper — non-English tokens pass through mostly
    # untouched (terminal-s plural stripping mildly helps Spanish);
    # CJK is unaffected (routed to the trigram table).
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        uri UNINDEXED,
        path UNINDEXED,
        type UNINDEXED,
        entity_type UNINDEXED,
        text,
        tokenize = 'porter unicode61 remove_diacritics 2'
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
        # sqlite_util.connect sets check_same_thread=False, WAL mode, and
        # busy_timeout so concurrent cross-process writers don't get an
        # unretried SQLITE_BUSY.
        conn = _sqlite_connect(path)
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
        now = datetime.now(timezone.utc).isoformat()

        def _do(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM memory_fts WHERE uri = ?", (uri,))
            c.execute("DELETE FROM memory_fts_trigram WHERE uri = ?", (uri,))
            c.execute("DELETE FROM fts_meta WHERE uri = ?", (uri,))
            c.execute(
                "INSERT INTO memory_fts (uri, path, type, entity_type, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (uri, path, type_, entity_type, text),
            )
            c.execute(
                "INSERT INTO memory_fts_trigram "
                "(uri, path, type, entity_type, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (uri, path, type_, entity_type, text),
            )
            c.execute(
                "INSERT INTO fts_meta (uri, mtime, indexed_at) VALUES (?, ?, ?)",
                (uri, mtime, now),
            )

        _execute_write(self._conn, _do)

    def delete_by_uri(self, uri: str) -> None:
        """Remove a uri from both FTS tables and the meta table."""
        _execute_write(self._conn, lambda c: (
            c.execute("DELETE FROM memory_fts WHERE uri = ?", (uri,)),
            c.execute("DELETE FROM memory_fts_trigram WHERE uri = ?", (uri,)),
            c.execute("DELETE FROM fts_meta WHERE uri = ?", (uri,)),
        ))

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

        def _do(c: sqlite3.Connection) -> None:
            for start in range(0, len(unique), 500):
                chunk = unique[start:start + 500]
                placeholders = ",".join("?" * len(chunk))
                c.execute(
                    f"DELETE FROM memory_fts WHERE uri IN ({placeholders})",
                    chunk,
                )
                c.execute(
                    f"DELETE FROM memory_fts_trigram WHERE uri IN ({placeholders})",
                    chunk,
                )
                c.execute(
                    f"DELETE FROM fts_meta WHERE uri IN ({placeholders})",
                    chunk,
                )

        _execute_write(self._conn, _do)
        return len(unique)

    def clear(self) -> None:
        """Drop all rows from both tables. Used by ``durin reindex``."""
        _execute_write(self._conn, lambda c: (
            c.execute("DELETE FROM memory_fts"),
            c.execute("DELETE FROM memory_fts_trigram"),
            c.execute("DELETE FROM fts_meta"),
        ))

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

    def uris_with_prefix(self, prefix: str) -> set[str]:
        """Indexed uris starting with ``prefix`` (meta-table scan).

        Used by the incremental session reindex to skip turns that
        already have rows. LIKE wildcards in the prefix are escaped so
        keys containing ``_`` / ``%`` match literally.
        """
        like = (
            prefix.replace("\\", "\\\\")
            .replace("%", r"\%")
            .replace("_", r"\_")
        ) + "%"
        cur = self._conn.execute(
            "SELECT uri FROM fts_meta WHERE uri LIKE ? ESCAPE '\\'",
            (like,),
        )
        return {u for (u,) in cur.fetchall()}

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
        with the FTS5 query as-is; the caller's job to sanitise.

        ``ORDER BY rank`` is load-bearing: without it FTS5 returns
        MATCH results in rowid (insertion) order, so the "ranked"
        list fed to RRF fusion was really file-walk order and rows
        indexed later always lost regardless of BM25 relevance.
        """
        cur = self._conn.execute(
            f"SELECT uri, path, type, entity_type FROM {table} "
            f"WHERE {table} MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        )
        return [
            FTSHit(uri=u, path=p, type=t, entity_type=et)
            for (u, p, t, et) in cur.fetchall()
        ]
