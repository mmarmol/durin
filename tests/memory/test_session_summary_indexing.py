"""Session summaries as markdown projections (audit A10).

Per doc 02 §3.3 and doc 11 audit A10:

- The summary lives at `memory/session_summary/<sanitized_key>.md`
  (single source of truth — A4 principle).
- `_persist_last_summary` no longer carries the text in
  `session.metadata["_last_summary"]`. Pre-A10 sessions are
  migrated on the next compaction (the field is popped + session
  saved).
- Walker picks up the new directory; indexer assigns
  `class_name = "session_summary"`.

Per [[feedback-sync-tests-exercise-behavior]]: these tests
exercise the BEHAVIOUR — write the markdown via the store, read it
back, verify the entry shape is index-ready, and check the
legacy migration path.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from durin.memory.paths import MEMORY_CLASSES
from durin.memory.session_summary_store import (
    SESSION_SUMMARY_CLASS,
    delete_session_summary,
    get_session_summary,
    sanitize_session_key,
    session_summary_path,
    write_session_summary,
)


# ---------------------------------------------------------------------------
# walker / class registration
# ---------------------------------------------------------------------------


def test_session_summary_is_in_memory_classes() -> None:
    """MEMORY_CLASSES must include `session_summary` so the walker
    iterates the directory."""
    assert SESSION_SUMMARY_CLASS in MEMORY_CLASSES


def test_session_summary_not_in_agent_facing_enum_for_memory_store() -> None:
    """Like `pending` (A2), `session_summary` exists in
    MEMORY_CLASSES but the LLM-facing memory_store enum excludes
    it — the agent should never write summaries directly. The
    compactor is the only producer."""
    from durin.agent.tools.memory_store import _AGENT_FACING_CLASSES

    assert SESSION_SUMMARY_CLASS not in _AGENT_FACING_CLASSES


# ---------------------------------------------------------------------------
# sanitize_session_key
# ---------------------------------------------------------------------------


def test_sanitize_simple_key_passes_through() -> None:
    assert sanitize_session_key("cli_direct") == "cli_direct"


def test_sanitize_replaces_colon() -> None:
    """`telegram:123` is a real session-key shape; the colon would
    be invalid in some filesystems, so we collapse it to `_`."""
    assert sanitize_session_key("telegram:123") == "telegram_123"


def test_sanitize_drops_path_traversal() -> None:
    """Runs of dots are collapsed so a malicious key can't escape
    the directory."""
    safe = sanitize_session_key("../etc/passwd")
    assert ".." not in safe
    assert "/" not in safe


def test_sanitize_empty_key_returns_default() -> None:
    """Empty input produces a stable fallback rather than `""`."""
    assert sanitize_session_key("") == "default"


# ---------------------------------------------------------------------------
# write_session_summary + get_session_summary round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    """The body returned by `get_session_summary` equals what
    `write_session_summary` persisted."""
    summary_text = "Marcelo wants to discuss memory ranking."
    path = write_session_summary(
        tmp_path, "cli:test", summary_text,
        last_active="2026-05-20T10:00:00Z",
    )
    assert path is not None
    text, last_active = get_session_summary(tmp_path, "cli:test")
    assert text == summary_text
    assert last_active == datetime.date(2026, 5, 20)


def test_write_empty_summary_returns_none(tmp_path: Path) -> None:
    """Empty / sentinel input does not write a zero-byte entry."""
    assert write_session_summary(tmp_path, "x", "") is None
    assert write_session_summary(tmp_path, "x", "(nothing)") is None
    assert not session_summary_path(tmp_path, "x").exists()


def test_update_overwrites_same_path(tmp_path: Path) -> None:
    """Re-persisting the same session updates the file in place —
    the id is the sanitised key, so we get update semantics."""
    p1 = write_session_summary(tmp_path, "x", "first text")
    p2 = write_session_summary(tmp_path, "x", "second text")
    assert p1 == p2
    text, _ = get_session_summary(tmp_path, "x")
    assert text == "second text"


def test_get_returns_none_when_missing(tmp_path: Path) -> None:
    text, last = get_session_summary(tmp_path, "no_such_session")
    assert text is None
    assert last is None


def test_delete_removes_md(tmp_path: Path) -> None:
    write_session_summary(tmp_path, "x", "to be deleted")
    assert session_summary_path(tmp_path, "x").exists()
    deleted = delete_session_summary(tmp_path, "x")
    assert deleted is True
    assert not session_summary_path(tmp_path, "x").exists()
    # Second delete is a no-op.
    assert delete_session_summary(tmp_path, "x") is False


# ---------------------------------------------------------------------------
# entry shape is index-ready
# ---------------------------------------------------------------------------


def test_persisted_entry_is_pydantic_valid(tmp_path: Path) -> None:
    """The markdown round-trips through `load_entry` cleanly so the
    indexer can read it without crashing."""
    from durin.memory.storage import load_entry

    write_session_summary(
        tmp_path, "cli:test", "Body text.",
        last_active="2026-05-20",
    )
    entry = load_entry(session_summary_path(tmp_path, "cli:test"))
    assert entry.body == "Body text."
    assert entry.summary == "Body text."
    assert entry.headline  # auto-derived; non-empty
    assert entry.valid_from == datetime.date(2026, 5, 20)
    assert entry.author == "agent_created"


def test_indexer_assigns_session_summary_class_name(tmp_path: Path) -> None:
    """The indexer's _payload_for assigns class_name from parts[0]
    — `memory/session_summary/<key>.md` → class_name="session_summary".
    This is what makes the new rows show up with the right type in
    LanceDB / FTS5."""
    from durin.memory.indexer import _payload_for

    write_session_summary(
        tmp_path, "cli:test", "Body text.", last_active="2026-05-20",
    )
    md_path = session_summary_path(tmp_path, "cli:test")
    payload = _payload_for(tmp_path, md_path)
    assert payload is not None
    assert payload["type_"] == SESSION_SUMMARY_CLASS
