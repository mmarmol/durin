"""Tests for the P4 hot-layer dedup of memory_search output.

A hit whose rendered body is already fully visible in the caller's
hot layer collapses to a pointer line; anything carrying more than the
prefix excerpt passes through whole (semantics: containment, never
information loss). Subagent-scoped tools skip the dedup entirely —
their system prompt has no hot layer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.memory.context_dedup import (
    prefix_map,
    render_in_context_section,
    split_in_context,
)
from durin.memory.hot_layer import HotLayer
from durin.memory.section_markers import (
    canonical_marker,
    end_marker,
    fragment_marker,
)
from durin.memory.sectioned_output import SectionedHit


def _hot_layer(
    canonical: list[str] | None = None,
    fragments: list[str] | None = None,
) -> HotLayer:
    return HotLayer(
        identity="",
        canonical_blocks=canonical or [],
        fragment_blocks=fragments or [],
        headlines=[],
        entities=[],
    )


def _canonical_block(ref: str, body: str, ts: str = "2026-06-01") -> str:
    return "\n".join(
        [canonical_marker(ref, ts=ts), body, end_marker("canonical")]
    )


def _fragment_block(path: str, body: str, ts: str = "2026-06-01") -> str:
    return "\n".join(
        [fragment_marker(path, ts=ts), body, end_marker("fragment")]
    )


# ---------------------------------------------------------------------------
# prefix_map
# ---------------------------------------------------------------------------


def test_prefix_map_keys_canonical_by_ref_and_fragment_by_md_less_path():
    hot = _hot_layer(
        canonical=[_canonical_block("person:marcelo", "Architect of durin.")],
        fragments=[
            _fragment_block("memory/episodic/abc123.md", "Met on Tuesday."),
        ],
    )

    prefix = prefix_map(hot)

    assert "person:marcelo" in prefix
    assert "memory/episodic/abc123" in prefix
    assert "architect of durin." in prefix["person:marcelo"]


def test_prefix_map_ignores_malformed_blocks():
    hot = _hot_layer(canonical=["not a marker block at all"])
    assert prefix_map(hot) == {}


# ---------------------------------------------------------------------------
# split_in_context
# ---------------------------------------------------------------------------


def _patch_hot(monkeypatch, hot: HotLayer) -> None:
    monkeypatch.setattr(
        "durin.memory.context_dedup.read_hot_layer", lambda ws: hot
    )


def test_contained_entity_hit_is_deduped(monkeypatch, tmp_path):
    _patch_hot(monkeypatch, _hot_layer(
        canonical=[_canonical_block(
            "person:marcelo", "Architect of durin. Prefers Spanish.",
        )],
    ))
    hit = SectionedHit(
        uri="memory/entity_page/person:marcelo",
        type="entity",
        path="person:marcelo",
        score=1.0,
        summary="Prefers   Spanish.",  # whitespace-normalised containment
    )

    kept, redundant = split_in_context(tmp_path, [hit])

    assert kept == []
    assert redundant == [hit]


def test_entity_hit_with_extra_body_is_kept(monkeypatch, tmp_path):
    """Semantics 3: a hit carrying info beyond the prefix excerpt
    passes through whole — dedup must never lose information."""
    _patch_hot(monkeypatch, _hot_layer(
        canonical=[_canonical_block("person:marcelo", "Architect of durin.")],
    ))
    hit = SectionedHit(
        uri="memory/entity_page/person:marcelo",
        type="entity",
        path="person:marcelo",
        score=1.0,
        summary="Architect of durin. Founded mxhero in 2012.",
    )

    kept, redundant = split_in_context(tmp_path, [hit])

    assert kept == [hit]
    assert redundant == []


def test_contained_fragment_hit_is_deduped(monkeypatch, tmp_path):
    _patch_hot(monkeypatch, _hot_layer(
        fragments=[
            _fragment_block("memory/episodic/abc123.md", "Met on Tuesday."),
        ],
    ))
    hit = SectionedHit(
        uri="memory/episodic/abc123",
        type="episodic",
        path="memory/episodic/abc123",
        score=1.0,
        summary="Met on Tuesday.",
    )

    kept, redundant = split_in_context(tmp_path, [hit])

    assert kept == []
    assert redundant == [hit]


def test_session_and_skill_hits_never_dedup(monkeypatch, tmp_path):
    _patch_hot(monkeypatch, _hot_layer(
        canonical=[_canonical_block("person:marcelo", "Met on Tuesday.")],
    ))
    hits = [
        SectionedHit(
            uri="sessions/s1", type="session_summary", path="sessions/s1",
            score=1.0, summary="Met on Tuesday.",
        ),
        SectionedHit(
            uri="skill/foo", type="skill", path="skills/foo/SKILL.md",
            score=1.0, summary="Met on Tuesday.",
        ),
    ]

    kept, redundant = split_in_context(tmp_path, hits)

    assert kept == hits
    assert redundant == []


def test_empty_hot_layer_keeps_everything(monkeypatch, tmp_path):
    _patch_hot(monkeypatch, _hot_layer())
    hit = SectionedHit(
        uri="memory/entity_page/person:marcelo", type="entity",
        path="person:marcelo", score=1.0, summary="Anything.",
    )

    kept, redundant = split_in_context(tmp_path, [hit])

    assert kept == [hit]
    assert redundant == []


def test_hot_layer_read_failure_degrades_to_no_dedup(monkeypatch, tmp_path):
    def _boom(ws):
        raise OSError("disk gone")

    monkeypatch.setattr("durin.memory.context_dedup.read_hot_layer", _boom)
    hit = SectionedHit(
        uri="memory/entity_page/person:marcelo", type="entity",
        path="person:marcelo", score=1.0, summary="Anything.",
    )

    kept, redundant = split_in_context(tmp_path, [hit])

    assert kept == [hit]
    assert redundant == []


# ---------------------------------------------------------------------------
# render_in_context_section
# ---------------------------------------------------------------------------


def test_pointer_section_lists_uri_and_ts():
    hits = [
        SectionedHit(
            uri="memory/entity_page/person:marcelo", type="entity",
            path="person:marcelo", score=1.0, ts="2026-06-01",
        ),
        SectionedHit(
            uri="memory/episodic/abc123", type="episodic",
            path="memory/episodic/abc123", score=0.5,
        ),
    ]

    text = render_in_context_section(hits)

    assert "memory_drill" in text
    assert "- memory/entity_page/person:marcelo (ts 2026-06-01)" in text
    assert "- memory/episodic/abc123" in text


def test_pointer_section_empty_for_no_hits():
    assert render_in_context_section([]) == ""


# ---------------------------------------------------------------------------
# MemorySearchTool gating by ToolContext.scope
# ---------------------------------------------------------------------------


def _tool_ctx(tmp_path, scope: str | None):
    ctx = MagicMock()
    ctx.workspace = str(tmp_path)
    ctx.app_config = None
    if scope is None:
        del ctx.scope  # simulate ad-hoc contexts without the field
    else:
        ctx.scope = scope
    return ctx


def test_create_disables_dedup_for_subagent_scope(tmp_path):
    from durin.agent.tools.memory_search import MemorySearchTool

    tool = MemorySearchTool.create(_tool_ctx(tmp_path, "subagent"))
    assert tool._context_dedup is False


def test_create_enables_dedup_for_core_scope_and_missing_scope(tmp_path):
    from durin.agent.tools.memory_search import MemorySearchTool

    assert MemorySearchTool.create(
        _tool_ctx(tmp_path, "core")
    )._context_dedup is True
    assert MemorySearchTool.create(
        _tool_ctx(tmp_path, None)
    )._context_dedup is True
