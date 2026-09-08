"""Memory keyed by the artifact the agent is about to use.

``memory_notes_for_path`` finds memory entries that mention a workspace
path (compaction summaries carry "Files/paths examined" trailers; episodic
notes cite paths) with the lexical index only — no embedding, no grep — so
a ``read_file`` can carry them at millisecond cost. ``entities_derived_from``
lists the entity pages a reference document was distilled into — also from the
lexical index — so a drill into the document also shows what memory already
holds about it.
"""

from __future__ import annotations

import re
from pathlib import Path

from durin.memory.entity_page import EntityPage, EntityPageError
from durin.memory.fts_index import FTSIndex, fts_index_path
from durin.memory.lexical_search import lexical_search
from durin.memory.query_router import decide_lexical_route
from durin.memory.storage import load_entry

__all__ = [
    "entities_derived_from",
    "entities_derived_from_candidates",
    "memory_notes_for_path",
]

_NOTE_CLASSES = ("episodic", "stable", "session_summary")

# Ceiling on the entity rows matching the ref phrase (the FTS query is
# filtered to type="entity", so this bounds entity candidates only — session
# rows and summaries that also cite the ref never compete for it). Generous
# because it bounds a document's whole distilled set, not a page of results;
# past it the walk it replaced would have been the slower answer anyway.
_MAX_ENTITY_CANDIDATES = 500

# One rendered note is one line of prose; a long paragraph is cut here so a
# single entry can't dominate the read result.
_MAX_NOTE_CHARS = 200

# Compaction carries forward a `; `-joined list of the paths a span touched.
# Those lines mention the file but say nothing about it, so a note built from
# one is noise — render the entry headline instead.
_MECHANICAL_PREFIXES = (
    "Files/paths examined in this span",
    "Files/paths from earlier spans (evicted):",
)


def _note_text(headline: str, body: str, pattern: re.Pattern[str]) -> str:
    """The first body line that mentions the path, capped, or ``headline``.

    Falls back to the headline when the only mention is a mechanical path
    trailer, and when the match is in the headline rather than the body."""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or not pattern.search(line):
            continue
        if line.startswith(_MECHANICAL_PREFIXES):
            break
        if len(line) > _MAX_NOTE_CHARS:
            return line[:_MAX_NOTE_CHARS - 1] + "…"
        return line
    return headline


def _enabled(default_limit: int) -> tuple[bool, int]:
    try:
        from durin.config.loader import load_config
        cfg = load_config().memory.artifact_recall
        return bool(cfg.enabled), int(cfg.max_notes)
    except Exception:  # noqa: BLE001 — no config file: defaults
        return True, default_limit


def memory_notes_for_path(workspace: Path, rel_path: str, *, limit: int | None = None) -> list[str]:
    """``- <uri> — <text>`` lines for entries that mention ``rel_path``.

    ``text`` is the line of the entry that mentions the path, so the note is
    the observation itself and not just a pointer; see ``_note_text``.

    Caller-provided limit overrides config; defaults to 3 when no limit is passed
    and no config exists.
    """
    enabled, cfg_limit = _enabled(3)
    effective_limit = limit if limit is not None else cfg_limit
    rel_path = (rel_path or "").strip()
    if not enabled or not rel_path or not fts_index_path(workspace).exists():
        return []
    try:
        with FTSIndex.open(workspace) as index:
            hits = lexical_search(
                index, decide_lexical_route(rel_path, keywords=rel_path),
                limit=effective_limit * 4,
                # This is a side effect of a file read, not a memory search:
                # emitting would inflate the search count and dilute the
                # latency series with sub-millisecond lookups.
                emit=False,
            )
    except Exception:  # noqa: BLE001 — recall is a convenience, never an error
        return []
    # Build a regex that matches rel_path on a word boundary, allowing optional ./
    # prefix. This avoids false positives: "app.py" should not match "src/app.py".
    # The trailing guard lets a sentence-final period through (prose notes end
    # sentences) while a period that continues an extension still blocks the
    # match, so "loop.py" does not match "loop.py.bak".
    pattern = re.compile(
        r"(?<![\w/.\-])(?:\./)?" + re.escape(rel_path) + r"(?![\w/\-]|\.\w)",
        re.IGNORECASE,
    )
    out: list[str] = []
    for hit in hits:
        if hit.type not in _NOTE_CLASSES:
            continue
        try:
            entry = load_entry(Path(workspace) / hit.path)
        except Exception:  # noqa: BLE001
            continue
        body = entry.body or entry.summary or ""
        if not pattern.search(f"{entry.headline}\n{body}"):
            continue  # FTS matched pieces of the path, not the exact path
        out.append(f"- {hit.uri} — {_note_text(entry.headline, body, pattern)}")
        if len(out) >= effective_limit:
            break
    return out


def entities_derived_from_candidates(workspace: Path, ref: str) -> list[Path]:
    """Entity pages that may name ``ref`` in ``derived_from``, path-sorted.

    An entity page's indexed text carries its ``derived_from`` refs on their
    own line, so a phrase search for the ref narrows the walk to the handful
    of pages that mention it. The query is filtered to entity rows
    (``type_="entity"``) so ``_MAX_ENTITY_CANDIDATES`` bounds entity matches
    only — a document cited by many session summaries can't push its own
    distilled entities out of the cap. The index only narrows: a ref quoted
    in a page's body matches too, so every caller parses the candidate and
    checks ``derived_from`` itself — the page is the truth; the ``hit.type``
    check below is a belt, not the filter doing the work.

    Falls back to every entity page when the index lookup fails, and when the
    workspace has no index *file* on disk: that check only proves the file is
    present, not that every entity is in it — a present-but-empty (or
    partially stale) index answers the query empty rather than triggering
    this fallback, and the health check's row-repair pass is what backstops
    that gap. Answering from a walk when there's truly no index is slow,
    answering nothing would be wrong. An empty ref has no candidates at all —
    a ``derived_from`` entry is always a ``reference:<slug>``.
    """
    root = Path(workspace) / "memory" / "entities"
    ref = (ref or "").strip()
    if not ref or not root.is_dir():
        return []
    if not fts_index_path(workspace).exists():
        return sorted(root.rglob("*.md"))
    try:
        with FTSIndex.open(workspace) as index:
            hits = lexical_search(
                index, decide_lexical_route(ref, keywords=ref),
                limit=_MAX_ENTITY_CANDIDATES,
                type_="entity",
                # A side effect of a drill or a webui page load, not a memory
                # search: the event's row count is read as the number of
                # searches and its duration as search latency, so emitting
                # here would dilute both.
                emit=False,
            )
    except Exception:  # noqa: BLE001 — a broken index degrades to a walk
        return sorted(root.rglob("*.md"))
    out: list[Path] = []
    for hit in hits:
        if hit.type != "entity" or not hit.path:
            continue
        md = Path(workspace) / hit.path
        if md.is_file():
            out.append(md)
    return sorted(out)


def entities_derived_from(workspace: Path, reference_ref: str, *, limit: int = 12) -> list[str]:
    """Entity refs whose ``derived_from`` names ``reference_ref``, alphabetical.

    Candidates come from the index (see
    :func:`entities_derived_from_candidates`); each one is read once and its
    raw text checked for the ref before the much costlier parse, and the
    parsed ``derived_from`` decides. A page that cannot be read or parsed is
    skipped rather than dropping the whole result.
    """
    out: list[str] = []
    for md in entities_derived_from_candidates(workspace, reference_ref):
        try:
            text = md.read_text(encoding="utf-8")
            if reference_ref not in text:
                continue
            page = EntityPage.from_text(text)
        except (OSError, UnicodeDecodeError, EntityPageError):
            continue
        if page is None or reference_ref not in (page.derived_from or []):
            continue
        out.append(f"{page.type}:{md.stem}")
        if len(out) >= limit:
            break
    return out
