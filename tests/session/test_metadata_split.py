"""Persistence split between session.jsonl identity metadata and the
``derived`` block in the sibling ``.meta.json`` sidecar.

Background: prior to this split, ``Session.metadata`` was serialised
verbatim into the line-0 metadata header of ``session.jsonl``. That
mixed LLM-DERIVED projections of the conversation (compaction summary
today, future embeddings / narrative summaries) with the session's
identity state (mode, plan path, todos, channel ownership). A learning
or memory pipeline walking ``session.jsonl`` had to distinguish the two.

The split rule:

- IDENTITY → line-0 of ``session.jsonl`` (source of truth).
- DERIVED (members of ``SessionManager._DERIVED_METADATA_KEYS``)
  → ``<key>.meta.json::derived`` block.

The in-memory ``Session.metadata`` dict is unchanged — at load time
the sidecar's derived values are merged back into it so consumer code
keeps reading one flat dict.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.session.manager import SessionManager
from durin.session.session_meta import meta_path_for, read_meta


def _read_jsonl_metadata(sm: SessionManager, key: str) -> dict:
    """Helper: read the line-0 metadata header from ``<key>.jsonl``."""
    path = sm._get_session_path(key)
    with path.open("r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    return json.loads(first_line)


# ---------------------------------------------------------------------------
# Save: identity stays in line-0; derived moves to sidecar
# ---------------------------------------------------------------------------


def test_save_routes_last_summary_to_sidecar(tmp_path: Path):
    """``_last_summary`` (LLM compaction output) must NOT appear in the
    session.jsonl metadata header — it lives in the .meta.json sidecar."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {
        "agent_mode": "plan",
        "_last_summary": {"text": "compaction summary", "last_active": "2026-05-20"},
    }
    sm.save(session)

    line0 = _read_jsonl_metadata(sm, "test")
    assert "_last_summary" not in line0["metadata"]
    assert line0["metadata"]["agent_mode"] == "plan"

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["_last_summary"] == {
        "text": "compaction summary",
        "last_active": "2026-05-20",
    }


def test_save_keeps_identity_metadata_in_line_zero(tmp_path: Path):
    """Identity keys (everything outside ``_DERIVED_METADATA_KEYS``)
    serialise to line 0 unchanged — they're source-of-truth session
    content."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {
        "agent_mode": "build",
        "pre_plan_mode": "plan",
        "active_plan_path": "/plans/foo.md",
        "todos": [{"content": "x", "status": "pending", "activeForm": "doing x"}],
        "title": "Some Conversation",
    }
    sm.save(session)

    line0 = _read_jsonl_metadata(sm, "test")
    assert line0["metadata"]["agent_mode"] == "build"
    assert line0["metadata"]["pre_plan_mode"] == "plan"
    assert line0["metadata"]["active_plan_path"] == "/plans/foo.md"
    assert line0["metadata"]["todos"][0]["content"] == "x"
    assert line0["metadata"]["title"] == "Some Conversation"


def test_save_with_no_derived_writes_empty_sidecar_derived(tmp_path: Path):
    """A session that has never been compacted still produces a sidecar
    with an empty ``derived`` block — consistent shape downstream."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {"agent_mode": "build"}
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"] == {}


def test_save_clearing_summary_wipes_sidecar_derived(tmp_path: Path):
    """``Session.clear()`` pops ``_last_summary`` from memory. The next
    save must propagate that to the sidecar — otherwise stale summary
    text would leak across what should be a fresh session."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["_last_summary"] = {"text": "old", "last_active": "2026-05-20"}
    sm.save(session)

    # Verify the summary was persisted.
    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["_last_summary"]["text"] == "old"

    # Clear + save.
    session.clear()
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"] == {}


# ---------------------------------------------------------------------------
# Load: sidecar merges back into Session.metadata
# ---------------------------------------------------------------------------


def test_load_merges_derived_from_sidecar(tmp_path: Path):
    """A session saved with the split is loadable back with
    ``_last_summary`` reappearing in ``Session.metadata`` — consumer code
    keeps reading one dict."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {
        "agent_mode": "plan",
        "_last_summary": {"text": "saved", "last_active": "2026-05-20"},
    }
    sm.save(session)

    sm.invalidate("test")  # force re-read from disk
    reloaded = sm.get_or_create("test")
    assert reloaded.metadata["agent_mode"] == "plan"
    assert reloaded.metadata["_last_summary"] == {
        "text": "saved", "last_active": "2026-05-20",
    }


def test_load_with_missing_sidecar_returns_identity_only(tmp_path: Path):
    """If the sidecar was deleted (or never existed), load still works —
    Session.metadata contains only the identity keys from line 0."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {"agent_mode": "build"}
    sm.save(session)

    # Delete the sidecar manually.
    meta_path_for("test", sm.sessions_dir).unlink()

    sm.invalidate("test")
    reloaded = sm.get_or_create("test")
    assert reloaded.metadata == {"agent_mode": "build"}
    assert "_last_summary" not in reloaded.metadata


# ---------------------------------------------------------------------------
# Backward compatibility: legacy session.jsonl with summary in line 0
# ---------------------------------------------------------------------------


def test_legacy_session_with_summary_in_line_zero_still_loads(tmp_path: Path):
    """Sessions written before the split kept ``_last_summary`` inside
    line 0's ``metadata`` dict. Loading them must still surface the
    summary in ``Session.metadata`` so consumer code doesn't break."""
    sm = SessionManager(workspace=tmp_path)
    path = sm._get_session_path("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a legacy-format jsonl by hand — summary INSIDE line-0
    # metadata, no sidecar.
    legacy_payload = {
        "_type": "metadata",
        "key": "legacy",
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
        "metadata": {
            "agent_mode": "build",
            "_last_summary": {"text": "legacy summary", "last_active": "2026-05-15"},
        },
        "last_consolidated": 0,
    }
    user_msg = {"role": "user", "content": "hi"}
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(legacy_payload) + "\n")
        f.write(json.dumps(user_msg) + "\n")

    reloaded = sm.get_or_create("legacy")
    assert reloaded.metadata["agent_mode"] == "build"
    assert reloaded.metadata["_last_summary"]["text"] == "legacy summary"


def test_legacy_session_self_heals_on_next_save(tmp_path: Path):
    """After reading a legacy session and saving it back, line 0 should
    no longer contain ``_last_summary`` — it now lives in the sidecar.
    Migration happens silently as a side effect of the first save."""
    sm = SessionManager(workspace=tmp_path)
    path = sm._get_session_path("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "_type": "metadata",
        "key": "legacy",
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
        "metadata": {
            "agent_mode": "build",
            "_last_summary": {"text": "legacy summary", "last_active": "2026-05-15"},
        },
        "last_consolidated": 0,
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(legacy_payload) + "\n")

    session = sm.get_or_create("legacy")
    # Trigger a save — even a no-op save should split.
    sm.save(session)

    line0 = _read_jsonl_metadata(sm, "legacy")
    assert "_last_summary" not in line0["metadata"]
    sidecar = read_meta(meta_path_for("legacy", sm.sessions_dir))
    assert sidecar["derived"]["_last_summary"]["text"] == "legacy summary"


def test_sidecar_wins_over_line_zero_for_derived_keys(tmp_path: Path):
    """If both the legacy line-0 copy AND the new sidecar disagree (an
    edge case from manual edits during migration), the sidecar wins —
    that's the canonical location after the split."""
    sm = SessionManager(workspace=tmp_path)
    path = sm._get_session_path("conflict")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "_type": "metadata",
        "key": "conflict",
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
        "metadata": {
            "agent_mode": "build",
            "_last_summary": {"text": "OLD line-0 value", "last_active": "2026-05-15"},
        },
        "last_consolidated": 0,
    }
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(legacy_payload) + "\n")

    # Write a sidecar with a DIFFERENT summary.
    sidecar_path = meta_path_for("conflict", sm.sessions_dir)
    sidecar_path.write_text(json.dumps({
        "session_key": "conflict",
        "events": [],
        "derived": {
            "_last_summary": {"text": "NEW sidecar value", "last_active": "2026-05-20"},
        },
    }))

    reloaded = sm.get_or_create("conflict")
    # Sidecar value should win for derived keys.
    assert reloaded.metadata["_last_summary"]["text"] == "NEW sidecar value"


# ---------------------------------------------------------------------------
# Deletion: removing a session also removes its sidecar
# ---------------------------------------------------------------------------


def test_delete_session_removes_sidecar(tmp_path: Path):
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["_last_summary"] = {"text": "x", "last_active": "2026-05-20"}
    sm.save(session)

    sidecar_path = meta_path_for("test", sm.sessions_dir)
    assert sidecar_path.exists()

    sm.delete_session("test")
    assert not sidecar_path.exists()
    assert not sm._get_session_path("test").exists()


def test_delete_session_handles_orphan_sidecar(tmp_path: Path):
    """A sidecar may outlive its jsonl (orphan from a prior crash).
    ``delete_session`` should clean it up even though there's no jsonl
    file to find."""
    sm = SessionManager(workspace=tmp_path)
    # Create just a sidecar with no jsonl.
    sidecar_path = meta_path_for("ghost", sm.sessions_dir)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({"session_key": "ghost", "events": [], "derived": {}}))
    assert sidecar_path.exists()

    result = sm.delete_session("ghost")
    # No jsonl was found → returns False, but the orphan sidecar IS cleaned.
    assert result is False
    assert not sidecar_path.exists()


# ---------------------------------------------------------------------------
# read_session_file (used by HTTP read endpoints) sees the merged view
# ---------------------------------------------------------------------------


def test_read_session_file_returns_merged_view(tmp_path: Path):
    """HTTP read endpoints go through ``read_session_file`` instead of
    ``get_or_create`` (no caching). Both must return the same
    merged-metadata shape so the API doesn't expose the split detail."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata = {
        "agent_mode": "plan",
        "_last_summary": {"text": "x", "last_active": "2026-05-20"},
    }
    sm.save(session)

    payload = sm.read_session_file("test")
    assert payload is not None
    assert payload["metadata"]["agent_mode"] == "plan"
    assert payload["metadata"]["_last_summary"]["text"] == "x"
