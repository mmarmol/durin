"""B1 (entity pages): the grep arm must hand RRF the SAME fusion uri the
FTS indexer and the vector normaliser use — the bare ``<type>:<slug>``
ref (``indexer._payload_for``), NOT the ``memory/entity_page/<ref>``
display path.

``search_memory`` addresses a canonical page by its display path, which
is the shape drill-down and the webui expect. Copying that path straight
into the fusion dict makes RRF see the same page under two keys: the
FTS/vector hit under ``practice:jira-access`` and the grep hit under
``memory/entity_page/practice:jira-access``. The page then occupies two
of the caller's ``limit`` slots — and because the result layer re-adds
the prefix, both rows render with an identical uri, one of them with no
snippet. The display path rides along in ``path`` for the result layer.
"""

from __future__ import annotations

from durin.memory.search import Result
from durin.memory.search_pipeline import _safe_grep_fallback


def _recovery() -> dict:
    return {"sources": set(), "ms": 0.0}


def _grep_rows(monkeypatch, results: list[Result]) -> list[dict]:
    monkeypatch.setattr(
        "durin.memory.search.search_memory",
        lambda *a, **k: results,
    )
    return _safe_grep_fallback("/nonexistent", "jira", recovery=_recovery())


def test_entity_page_grep_row_uses_bare_ref_fusion_uri(monkeypatch) -> None:
    """The grep FUSION uri for a canonical page is the bare ref, so RRF
    can fuse it with the FTS/vector hit for the same page."""
    rows = _grep_rows(monkeypatch, [
        Result(
            source="memory",
            uri="memory/entity_page/practice:jira-access",
            headline="Jira access",
            snippet="basic auth via the gateway",
            class_name="entity_page",
            entities=("practice:jira-access",),
        ),
    ])
    assert len(rows) == 1
    assert rows[0]["uri"] == "practice:jira-access"
    # The drillable display path stays available for the result layer.
    assert rows[0]["path"] == "memory/entity_page/practice:jira-access"
    assert rows[0]["type"] == "entity_page"


def test_non_entity_grep_rows_keep_their_uri(monkeypatch) -> None:
    """Only canonical pages are rewritten — episodic entries and session
    turns already share their uri shape across the arms."""
    rows = _grep_rows(monkeypatch, [
        Result(
            source="memory",
            uri="memory/episodic/9b6f1c81724a",
            headline="a fragment",
            snippet="…",
            class_name="episodic",
        ),
        Result(
            source="sessions",
            uri="sessions/slack_C0AKE.md#turn-59",
            headline="a turn",
            snippet="…",
        ),
    ])
    assert [r["uri"] for r in rows] == [
        "memory/episodic/9b6f1c81724a",
        "sessions/slack_C0AKE.md#turn-59",
    ]
    assert [r["type"] for r in rows] == ["episodic", "session"]
