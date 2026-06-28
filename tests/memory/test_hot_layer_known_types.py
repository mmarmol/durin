from __future__ import annotations

from pathlib import Path

from loguru import logger

from durin.memory.hot_layer import HotLayer, _read_type_list, read_hot_layer


def _make_entity(ws: Path, type_: str, slug: str) -> None:
    d = ws / "memory" / "entities" / type_
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f"# {slug}\n", encoding="utf-8")


def test_read_type_list_empty_when_no_entities(tmp_path: Path) -> None:
    assert _read_type_list(tmp_path) == []


def test_read_type_list_alphabetical_distinct_types(tmp_path: Path) -> None:
    _make_entity(tmp_path, "project", "durin")
    _make_entity(tmp_path, "person", "marcelo")
    _make_entity(tmp_path, "person", "juan")  # same type, not duplicated
    assert _read_type_list(tmp_path) == ["person", "project"]


def test_read_type_list_skips_empty_type_dirs(tmp_path: Path) -> None:
    (tmp_path / "memory" / "entities" / "ghost").mkdir(parents=True)
    _make_entity(tmp_path, "person", "marcelo")
    assert _read_type_list(tmp_path) == ["person"]


def test_read_type_list_caps_and_warns(tmp_path: Path) -> None:
    for i in range(5):
        _make_entity(tmp_path, f"t{i:02d}", "x")
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        result = _read_type_list(tmp_path, cap=3)
    finally:
        logger.remove(sink_id)
    assert len(result) == 3
    assert any("sprawl" in m for m in messages)


def test_render_includes_known_types_section() -> None:
    hl = HotLayer(identity="", canonical_blocks=[], fragment_blocks=[],
                  headlines=[], entities=[], types=["person", "project"])
    out = hl.render()
    assert "## Memory: Known types" in out
    assert "person, project" in out


def test_render_omits_known_types_when_empty() -> None:
    hl = HotLayer(identity="", canonical_blocks=[], fragment_blocks=[],
                  headlines=[], entities=[], types=[])
    assert "Known types" not in hl.render()


def test_read_hot_layer_populates_types(tmp_path: Path) -> None:
    _make_entity(tmp_path, "topic", "memory")
    assert "topic" in read_hot_layer(tmp_path).types
