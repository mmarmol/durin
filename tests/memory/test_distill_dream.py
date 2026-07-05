"""Tests for the reference-distillation dream pass (outline structure pass)."""

from __future__ import annotations

import json
from pathlib import Path

from durin.memory.distill_dream import (
    _sections_from_chunks,
    outline_path_for,
    parse_outline,
    run_distill_reference_pass,
)
from durin.memory.llm_invoke import LLMResponse
from durin.memory.reference import ingest_reference, reference_chunks

_DOC = (
    "# Book\n\nintro paragraph.\n\n"
    "## Chapter One\n\nbody of chapter one.\n\n"
    "## Chapter Two\n\nbody of chapter two.\n"
)


def _stub(sections_map: dict[str, str], abstract: str = "A handbook."):
    def invoke(prompt: str, *, model=None) -> LLMResponse:
        out = {"abstract": abstract, "sections": sections_map}
        return LLMResponse(text=json.dumps(out), prompt_tokens=1, completion_tokens=1)

    return invoke


def _ingest(ws: Path) -> str:
    res = ingest_reference(ws, "handbook", _DOC)
    return res.ref.split(":", 1)[1]


# --- parse_outline -----------------------------------------------------------


def test_parse_outline_valid() -> None:
    out = parse_outline('{"abstract": "x", "sections": {"A": "sa", "B": "sb"}}')
    assert out == {"abstract": "x", "sections": {"A": "sa", "B": "sb"}}


def test_parse_outline_fenced() -> None:
    out = parse_outline('```json\n{"abstract": "x", "sections": {}}\n```')
    assert out is not None and out["abstract"] == "x"


def test_parse_outline_garbage_is_none() -> None:
    assert parse_outline("not json at all") is None
    assert parse_outline('{"abstract": "", "sections": {}}') is None


def test_parse_outline_drops_nonstring_summaries() -> None:
    out = parse_outline('{"abstract": "x", "sections": {"A": 5, "B": "ok"}}')
    assert out["sections"] == {"B": "ok"}


# --- sectioning --------------------------------------------------------------


def test_sections_group_by_breadcrumb_in_order(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = f"reference:{_ingest(ws)}"
    sections = _sections_from_chunks(reference_chunks(ws, ref))
    crumbs = [c for c, _idxs, _t in sections]
    assert crumbs == ["Book", "Book › Chapter One", "Book › Chapter Two"]
    assert all(len(idxs) >= 1 for _c, idxs, _t in sections)


# --- run_distill_reference_pass ---------------------------------------------


def test_distill_writes_outline_sidecar(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    slug = _ingest(ws)
    stub = _stub({
        "Book": "The intro.",
        "Book › Chapter One": "About one.",
        "Book › Chapter Two": "About two.",
    })

    result = run_distill_reference_pass(ws, llm_invoke=stub)
    assert result["references"] == 1
    assert result["outlined"] == 1
    assert result["errors"] == []

    outline = json.loads(outline_path_for(ws, slug).read_text())
    assert outline["ref"] == f"reference:{slug}"
    assert outline["chunk_count"] == 3
    assert outline["abstract"] == "A handbook."
    crumbs = {s["breadcrumb"]: s for s in outline["sections"]}
    assert crumbs["Book › Chapter One"]["summary"] == "About one."
    # every section carries the chunk indices it summarizes
    assert all(s["chunk_indices"] for s in outline["sections"])


def test_distill_is_idempotent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _ingest(ws)
    stub = _stub({"Book": "x"})

    first = run_distill_reference_pass(ws, llm_invoke=stub)
    second = run_distill_reference_pass(ws, llm_invoke=stub)
    assert first["outlined"] == 1
    assert second["outlined"] == 0
    assert second["skipped"] == 1


def test_distill_redistills_when_document_changes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _ingest(ws)
    stub = _stub({"Book": "x"})
    run_distill_reference_pass(ws, llm_invoke=stub)

    # Re-ingest a longer document under the same slug → chunk_count changes.
    ingest_reference(ws, "handbook", _DOC + "\n\n## Chapter Three\n\nmore.\n")
    result = run_distill_reference_pass(ws, llm_invoke=stub)
    assert result["outlined"] == 1


def test_distill_records_llm_error_without_writing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    slug = _ingest(ws)

    def boom(prompt: str, *, model=None):
        raise RuntimeError("llm down")

    result = run_distill_reference_pass(ws, llm_invoke=boom)
    assert result["outlined"] == 0
    assert result["errors"] and "llm down" in result["errors"][0]
    assert not outline_path_for(ws, slug).exists()


def test_distill_no_references_is_noop(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    result = run_distill_reference_pass(ws, llm_invoke=_stub({}))
    assert result == {
        "references": 0, "outlined": 0, "skipped": 0,
        "errors": [], "duration_ms": result["duration_ms"],
    }


# --- run_seed_entities_pass --------------------------------------------------


def _seed_stub(entities: list[dict]):
    def invoke(prompt: str, *, model=None) -> LLMResponse:
        return LLMResponse(text=json.dumps(entities), prompt_tokens=1, completion_tokens=1)

    return invoke


def _distilled(ws: Path) -> str:
    """Ingest + distil (outline) so the seed pass has an outline to read."""
    slug = _ingest(ws)
    run_distill_reference_pass(ws, llm_invoke=_stub({
        "Book": "i", "Book › Chapter One": "1", "Book › Chapter Two": "2"}))
    return slug


def test_seed_writes_entities_with_derived_from(tmp_path: Path) -> None:
    from durin.memory.distill_dream import run_seed_entities_pass
    from durin.memory.entity_page import EntityPage

    ws = tmp_path / "ws"
    ws.mkdir()
    slug = _distilled(ws)
    stub = _seed_stub([
        {"ref": "person:ada", "name": "Ada", "significance": "The author."},
        {"ref": "concept:looms", "name": "Looms",
         "relations": [{"to": "person:ada", "type": "studied_by"}]},
    ])

    result = run_seed_entities_pass(ws, llm_invoke=stub)
    assert result["entities"] == 2
    assert result["seeded_docs"] == 1
    assert result["errors"] == []

    page = EntityPage.from_file(ws / "memory" / "entities" / "person" / "ada.md")
    assert page.name == "Ada"
    assert page.derived_from == [f"reference:{slug}"]


def test_seed_is_idempotent(tmp_path: Path) -> None:
    from durin.memory.distill_dream import run_seed_entities_pass

    ws = tmp_path / "ws"
    ws.mkdir()
    _distilled(ws)
    stub = _seed_stub([{"ref": "person:ada", "name": "Ada"}])
    first = run_seed_entities_pass(ws, llm_invoke=stub)
    second = run_seed_entities_pass(ws, llm_invoke=stub)
    assert first["entities"] == 1
    assert second["entities"] == 0
    assert second["skipped"] == 1


def test_seed_caps_entities_per_document(tmp_path: Path) -> None:
    from durin.memory.distill_dream import _MAX_ENTITIES_PER_DOC, run_seed_entities_pass

    ws = tmp_path / "ws"
    ws.mkdir()
    _distilled(ws)
    many = [{"ref": f"concept:c{i}", "name": f"C{i}"} for i in range(_MAX_ENTITIES_PER_DOC + 10)]
    result = run_seed_entities_pass(ws, llm_invoke=_seed_stub(many))
    assert result["entities"] == _MAX_ENTITIES_PER_DOC


def test_seed_skips_when_not_distilled(tmp_path: Path) -> None:
    from durin.memory.distill_dream import run_seed_entities_pass

    ws = tmp_path / "ws"
    ws.mkdir()
    _ingest(ws)  # ingested but NOT distilled → no outline to seed from
    result = run_seed_entities_pass(ws, llm_invoke=_seed_stub([{"ref": "x:y", "name": "Y"}]))
    assert result["references"] == 0
    assert result["entities"] == 0


def test_seed_empty_proposals_marks_done_without_entities(tmp_path: Path) -> None:
    from durin.memory.distill_dream import run_seed_entities_pass

    ws = tmp_path / "ws"
    ws.mkdir()
    _distilled(ws)
    result = run_seed_entities_pass(ws, llm_invoke=_seed_stub([]))
    assert result["entities"] == 0
    assert result["seeded_docs"] == 1
    # marker set → a re-run skips
    again = run_seed_entities_pass(ws, llm_invoke=_seed_stub([{"ref": "x:y", "name": "Y"}]))
    assert again["skipped"] == 1 and again["entities"] == 0
