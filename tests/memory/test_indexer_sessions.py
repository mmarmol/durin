"""FTS indexing of raw session turns.

Sessions (``sessions/<key>.md``) were grep-only by design: not in
LanceDB, not in FTS5. That locked the raw conversational record out of
lexical ranking — a session hit could only ever earn the grep source's
RRF contribution (w=0.3), so a session containing the best answer
still lost to any indexed entry. The dream pipeline no longer distills
sessions into episodic entries (the legacy consolidator was removed),
so this content is not represented anywhere else in the index.

Fix: ``rebuild_fts_index`` walks ``sessions/*.md`` and upserts one FTS
row per turn. Per-turn rows (not per-file) because:

- the grep path emits ``sessions/<key>.md#turn-N`` URIs — FTS must
  emit the SAME shape or RRF fusion never accumulates the two sources
  (the H28 principle: build the same uri shape across sources);
- BM25 over turn-sized documents ranks better than over whole
  transcripts (a strong match isn't diluted by the session's length);
- the turn header carries role + timestamp in the indexed text, so a
  hit shows when it was said without schema changes.

Vector stays out of sessions for now (embedding cost; FTS is the
cheap tier that fixes the structural lockout).
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import rebuild_fts_index
from durin.memory.session_md import regenerate_session_md


def _write_session(
    workspace: Path,
    key: str,
    messages: list[dict],
    *,
    render: bool = True,
) -> Path:
    """Write sessions/<key>.jsonl (+ rendered .md) like the session
    manager does: metadata line first, one message per line."""
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    jsonl = sessions / f"{key}.jsonl"
    meta = {
        "_type": "metadata",
        "key": key,
        "created_at": "2026-06-08T10:00:00",
        "updated_at": "2026-06-08T10:05:00",
        "last_consolidated": 0,
    }
    lines = [json.dumps(meta)] + [json.dumps(m) for m in messages]
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if render:
        regenerate_session_md(jsonl)
    return jsonl


_MESSAGES = [
    {"role": "user", "content": "what is the gateway websocket port?",
     "timestamp": "2026-06-08T10:00:01"},
    {"role": "assistant",
     "content": "The dashboard listens on the zanzibar-flamingo port.",
     "timestamp": "2026-06-08T10:00:02"},
]


# ---------------------------------------------------------------------------
# rebuild_fts_index walks sessions/
# ---------------------------------------------------------------------------


def test_rebuild_indexes_session_turns(tmp_path: Path) -> None:
    _write_session(tmp_path, "cli_test", _MESSAGES)
    stats = rebuild_fts_index(tmp_path)
    assert stats.errors == 0
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search('"zanzibar-flamingo"')
    assert len(hits) == 1
    assert hits[0].uri == "sessions/cli_test.md#turn-2"
    assert hits[0].path == "sessions/cli_test.md"
    assert hits[0].type == "session"


def test_session_turn_uri_matches_grep_uri(tmp_path: Path) -> None:
    """Load-bearing fusion property: the FTS row uri for a turn must
    equal the grep path's uri for the same match, or RRF treats them
    as different documents and the contributions never accumulate."""
    from durin.memory.search import search_memory

    _write_session(tmp_path, "cli_test", _MESSAGES)
    rebuild_fts_index(tmp_path)
    grep_uris = {r.uri for r in search_memory(
        tmp_path, "zanzibar-flamingo", scope="all", level="warm")}
    with FTSIndex.open(tmp_path) as idx:
        fts_uris = {h.uri for h in idx.search('"zanzibar-flamingo"')}
    assert fts_uris, "FTS found nothing"
    assert fts_uris <= grep_uris


def test_indexed_turn_text_carries_role_and_timestamp(tmp_path: Path) -> None:
    """The turn header (role + timestamp) is part of the indexed text
    so a session hit shows WHEN it was said — the LLM does its own
    temporal reasoning (faithful retrieval; no ts column needed)."""
    import sqlite3

    from durin.memory.fts_index import fts_index_path

    _write_session(tmp_path, "cli_test", _MESSAGES)
    rebuild_fts_index(tmp_path)
    conn = sqlite3.connect(fts_index_path(tmp_path))
    try:
        row = conn.execute(
            "SELECT text FROM memory_fts WHERE uri = ?",
            ("sessions/cli_test.md#turn-2",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "2026-06-08T10:00:02" in row[0]
    assert "assistant" in row[0]


def test_session_without_rendered_md_is_skipped(tmp_path: Path) -> None:
    """jsonl with no .md sibling: nothing to index, no error."""
    _write_session(tmp_path, "raw_only", _MESSAGES, render=False)
    stats = rebuild_fts_index(tmp_path)
    assert stats.errors == 0
    with FTSIndex.open(tmp_path) as idx:
        assert idx.search('"zanzibar-flamingo"') == []


def test_rebuild_without_sessions_dir_still_works(tmp_path: Path) -> None:
    stats = rebuild_fts_index(tmp_path)
    assert stats.errors == 0


def test_rebuild_is_idempotent_per_turn(tmp_path: Path) -> None:
    """Re-running the rebuild leaves exactly one row per turn."""
    _write_session(tmp_path, "cli_test", _MESSAGES)
    rebuild_fts_index(tmp_path)
    rebuild_fts_index(tmp_path)
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search('"zanzibar-flamingo"')
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# sectioned output — the new type buckets into the session section
# ---------------------------------------------------------------------------


def test_session_type_maps_to_session_section() -> None:
    from durin.memory.sectioned_output import _SECTION_FOR_TYPE

    assert _SECTION_FOR_TYPE.get("session") == "session"


# ---------------------------------------------------------------------------
# pipeline integration — lexical + grep accumulate on the same uri
# ---------------------------------------------------------------------------


def test_pipeline_surfaces_session_hit_via_lexical(tmp_path: Path) -> None:
    from durin.memory.search_pipeline import run_search_pipeline

    _write_session(tmp_path, "cli_test", _MESSAGES)
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(tmp_path, "zanzibar-flamingo")
    assert result.lexical_count > 0, "session turn did not reach lexical"
    session_hits = [
        h for h in result.hits if h.uri == "sessions/cli_test.md#turn-2"
    ]
    assert session_hits, f"no session hit in {[h.uri for h in result.hits]}"
    assert session_hits[0].type == "session"


# ---------------------------------------------------------------------------
# type prior — distilled entry outranks raw session at comparable relevance
# ---------------------------------------------------------------------------


def test_distilled_entry_outranks_session_turn(tmp_path: Path) -> None:
    """A session turn with a stronger BM25 score (term repeated) would
    out-rank the curated episodic entry on raw fusion. The session
    type prior tips comparable evidence toward the distillate while
    keeping the session hit available right below."""
    from durin.memory.schema import MemoryEntry
    from durin.memory.search_pipeline import run_search_pipeline
    from durin.memory.storage import save_entry

    _write_session(tmp_path, "cli_test", [
        {"role": "user",
         "content": "quopazine quopazine quopazine quopazine variants",
         "timestamp": "2026-06-08T10:00:01"},
    ])
    epi_dir = tmp_path / "memory" / "episodic"
    epi_dir.mkdir(parents=True, exist_ok=True)
    save_entry(
        MemoryEntry(id="fact1", headline="quopazine dosage is 5mg",
                    body="The distilled dosage fact."),
        epi_dir / "fact1.md",
    )
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(tmp_path, "quopazine")
    uris = [h.uri for h in result.hits]
    assert "memory/episodic/fact1" in uris
    assert "sessions/cli_test.md#turn-1" in uris
    assert uris.index("memory/episodic/fact1") < uris.index(
        "sessions/cli_test.md#turn-1"
    ), f"distilled entry did not lead: {uris}"


# ---------------------------------------------------------------------------
# reactive reindex — new turns become searchable without a full rebuild
# ---------------------------------------------------------------------------


def test_reindex_session_file_is_incremental(tmp_path: Path) -> None:
    """Only turns missing from the index are upserted — session saves
    happen per message, so the reactive path must be O(new turns),
    not O(session length)."""
    from durin.memory.indexer import reindex_session_file

    jsonl = _write_session(tmp_path, "cli_test", _MESSAGES)
    md_path = jsonl.with_suffix(".md")
    assert reindex_session_file(tmp_path, md_path) == 2
    # Append a third turn and re-render.
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(
        {"role": "user", "content": "now about the kumquat-rocket",
         "timestamp": "2026-06-08T10:00:03"},
    ))
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
    regenerate_session_md(jsonl)
    assert reindex_session_file(tmp_path, md_path) == 1
    with FTSIndex.open(tmp_path) as idx:
        assert len(idx.search('"kumquat-rocket"')) == 1
        assert len(idx.search('"zanzibar-flamingo"')) == 1


def test_reindex_session_file_missing_md_is_noop(tmp_path: Path) -> None:
    from durin.memory.indexer import reindex_session_file

    assert reindex_session_file(
        tmp_path, tmp_path / "sessions" / "ghost.md") == 0


def test_session_manager_save_indexes_new_turns(tmp_path: Path) -> None:
    """The session save path (the producer of sessions/<key>.md) also
    upserts the new turns' FTS rows — sessions become searchable as
    they happen, not only after the next full rebuild."""
    from durin.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("cli_live")
    session.add_message("user", "remember the plumbus-quine identifier")
    mgr.save(session)
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search('"plumbus-quine"')
    assert hits, "saved turn not searchable via FTS"
    assert hits[0].type == "session"
    assert hits[0].uri.startswith("sessions/cli_live.md#turn-")


def test_grep_only_session_hit_typed_session(tmp_path: Path) -> None:
    """Sessions reachable only via grep (index not yet rebuilt) must
    still carry type "session" — the type prior and the section
    renderer key off it. The grep fallback used to default these rows
    to "episodic"."""
    from durin.memory.search_pipeline import run_search_pipeline

    _write_session(tmp_path, "cli_test", _MESSAGES)
    result = run_search_pipeline(tmp_path, "zanzibar-flamingo")
    session_hits = [
        h for h in result.hits if h.uri.startswith("sessions/cli_test.md")
    ]
    assert session_hits, f"no session hit in {[h.uri for h in result.hits]}"
    assert session_hits[0].type == "session"
