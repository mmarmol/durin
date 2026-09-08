"""Sidecar persistence for the frozen eager-surface snapshot (SNAPSHOT_KEY).

``SNAPSHOT_KEY`` is a derived-metadata key
(``SessionManager._DERIVED_METADATA_KEYS``): it is a rendering artifact
reconstructible from the memory store, not session content, so it must ride
in the ``<key>.meta.json`` sidecar's ``derived`` block and never in the
``.jsonl`` identity line — same split as ``_last_summary``. ``Session.clear()``
must also drop it, the same way it drops ``_last_summary``, so a fresh
session (``/new``) never inherits a snapshot rendered for the old one.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.memory.eager_surface import SNAPSHOT_KEY
from durin.session.manager import Session, SessionManager
from durin.session.session_meta import meta_path_for, read_meta


def _line0_metadata(sm: SessionManager, key: str) -> dict:
    path = sm._get_session_path(key)
    with path.open("r", encoding="utf-8") as f:
        return json.loads(f.readline().strip())


def _sample_snapshot() -> dict:
    return {
        "pinned": "pinned block text",
        "hot": "hot layer text",
        "refs": ["person:marcelo"],
        "turn": 2,
        "frozen_at": "2026-09-08T12:00:00+00:00",
    }


def test_save_routes_eager_surface_snapshot_to_sidecar(tmp_path: Path):
    """The snapshot must NOT appear in the session.jsonl metadata header —
    it lives in the .meta.json sidecar, like _last_summary."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata[SNAPSHOT_KEY] = _sample_snapshot()
    sm.save(session)

    line0 = _line0_metadata(sm, "test")
    assert SNAPSHOT_KEY not in line0["metadata"]

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"][SNAPSHOT_KEY] == _sample_snapshot()


def test_clear_pops_eager_surface_snapshot_from_metadata():
    """Session.clear() must drop the snapshot from in-memory metadata, the
    same way it already drops _last_summary."""
    session = Session(key="test")
    session.metadata[SNAPSHOT_KEY] = _sample_snapshot()

    session.clear()

    assert SNAPSHOT_KEY not in session.metadata


def test_clear_then_save_wipes_the_snapshot_from_the_sidecar(tmp_path: Path):
    """A cleared session that gets saved again must not leave a stale
    snapshot behind in the sidecar — otherwise a fresh session (/new) could
    still see a snapshot rendered for the previous one."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata[SNAPSHOT_KEY] = _sample_snapshot()
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert SNAPSHOT_KEY in sidecar["derived"]

    session.clear()
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert SNAPSHOT_KEY not in sidecar["derived"]


def test_load_merges_eager_surface_snapshot_back_from_sidecar(tmp_path: Path):
    """A fresh SessionManager (no cache) recovers the snapshot from the
    sidecar into Session.metadata, the same way _last_summary round-trips."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata[SNAPSHOT_KEY] = _sample_snapshot()
    sm.save(session)

    sm.invalidate("test")
    reloaded = sm.get_or_create("test")
    assert reloaded.metadata[SNAPSHOT_KEY] == _sample_snapshot()
