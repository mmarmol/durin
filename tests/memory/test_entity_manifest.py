"""Tests for the build_entity_manifest primitive."""
from pathlib import Path
from datetime import datetime, timezone

import pytest

from durin.memory.entity_manifest import build_entity_manifest
from durin.memory.memory_writer import write_entity
from durin.memory.field_patch import FieldPatch


def _seed(ws: Path, ref: str, name: str, body: str) -> None:
    write_entity(ws, ref,
                 [FieldPatch(kind="body_replace", value=body, author="dream",
                             source_ref="test", at=datetime.now(timezone.utc))],
                 create=True, name=name)


def test_types_mode_lists_all_entities_of_those_types(tmp_path):
    ws = tmp_path / "ws"
    _seed(ws, "feedback:spanish", "Spanish replies", "User wants Spanish.")
    _seed(ws, "feedback:brevity", "Brevity", "Prefers short answers.")
    _seed(ws, "person:marcelo", "Marcelo", "The owner.")
    out = build_entity_manifest(ws, types=["feedback"])
    assert "feedback:spanish" in out
    assert "feedback:brevity" in out
    assert "person:marcelo" not in out      # other type excluded
    assert "Spanish replies" in out         # name shown


def test_empty_when_no_entities(tmp_path):
    assert build_entity_manifest(tmp_path / "ws", types=["feedback"]) == ""


def test_query_mode_returns_relevant_entity(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _seed(ws, "topic:durin", "durin", "A personal AI agent project.")

    # Monkeypatch the search pipeline to return a fake hit whose uri is
    # the entity ref. This tests that build_entity_manifest correctly maps
    # SectionedHit.uri -> entity page -> manifest line, without requiring a
    # populated FTS/vector index in the test workspace.
    from durin.memory.sectioned_output import SectionedHit
    from durin.memory.search_pipeline import SearchPipelineResult

    fake_result = SearchPipelineResult(
        hits=[SectionedHit(uri="topic:durin", type="entity", path="", score=1.0)],
        vector_count=0,
        lexical_count=0,
    )

    captured_calls: list[tuple] = []

    def _fake_pipeline(*args, **kwargs):
        captured_calls.append((args, kwargs))
        return fake_result

    import durin.memory.search_pipeline as _sp
    monkeypatch.setattr(_sp, "run_search_pipeline", _fake_pipeline)

    query = "tell me about the durin agent"
    out = build_entity_manifest(ws, query=query, limit=5)

    assert "topic:durin" in out
    assert len(captured_calls) == 1, "run_search_pipeline should be called exactly once"
    call_args, call_kwargs = captured_calls[0]
    # First positional arg is the workspace path (may be wrapped in Path()).
    assert Path(call_args[0]) == Path(ws), "first positional arg must be the workspace path"
    # Query is passed as the second positional arg.
    assert call_args[1] == query, "second positional arg must be the query string"
