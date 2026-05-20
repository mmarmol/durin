"""Tests for the memory hot layer reader and ContextBuilder wiring."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from durin.memory.hot_layer import read_hot_layer
from durin.memory.store import store_memory


def test_empty_workspace_yields_empty_hot_layer(tmp_path: Path) -> None:
    layer = read_hot_layer(tmp_path)
    assert layer.identity == ""
    assert layer.headlines == []
    assert layer.entities == []
    assert layer.render() == ""


def test_identity_md_populates_identity(tmp_path: Path) -> None:
    stable_dir = tmp_path / "memory" / "stable"
    stable_dir.mkdir(parents=True)
    (stable_dir / "IDENTITY.md").write_text(
        "---\nid: IDENTITY\nheadline: bound identity\n---\n\n"
        "User is Marcelo. Prefers terse responses.\n",
        encoding="utf-8",
    )
    layer = read_hot_layer(tmp_path)
    assert "Marcelo" in layer.identity
    assert "terse" in layer.identity


def test_identity_md_without_frontmatter_still_loads(tmp_path: Path) -> None:
    stable_dir = tmp_path / "memory" / "stable"
    stable_dir.mkdir(parents=True)
    (stable_dir / "IDENTITY.md").write_text(
        "plain identity text without frontmatter",
        encoding="utf-8",
    )
    layer = read_hot_layer(tmp_path)
    assert "plain identity text" in layer.identity


def test_headlines_sorted_by_valid_from_desc(tmp_path: Path) -> None:
    store_memory(
        tmp_path,
        content="old learning",
        headline="OLD",
        valid_from=date(2024, 1, 1),
    )
    store_memory(
        tmp_path,
        content="new learning",
        headline="NEW",
        valid_from=date(2026, 5, 20),
    )
    layer = read_hot_layer(tmp_path)
    assert layer.headlines[0] == "NEW"
    assert layer.headlines[1] == "OLD"


def test_identity_md_excluded_from_headlines(tmp_path: Path) -> None:
    """IDENTITY.md surfaces only in the identity section, not in headlines."""
    stable_dir = tmp_path / "memory" / "stable"
    stable_dir.mkdir(parents=True)
    (stable_dir / "IDENTITY.md").write_text(
        "---\nid: IDENTITY\nheadline: identity headline\n---\n\nbody\n",
        encoding="utf-8",
    )
    store_memory(tmp_path, content="regular", headline="REGULAR")
    layer = read_hot_layer(tmp_path)
    assert "identity headline" not in layer.headlines


def test_entities_aggregated_dedup_and_sorted(tmp_path: Path) -> None:
    store_memory(tmp_path, content="x", entities=["zoo", "alpha"])
    store_memory(tmp_path, content="y", entities=["alpha", "beta"])
    layer = read_hot_layer(tmp_path)
    assert layer.entities == ["alpha", "beta", "zoo"]


def test_render_produces_three_sections(tmp_path: Path) -> None:
    stable_dir = tmp_path / "memory" / "stable"
    stable_dir.mkdir(parents=True)
    (stable_dir / "IDENTITY.md").write_text(
        "---\nid: IDENTITY\nheadline: id\n---\n\nuser is X\n",
        encoding="utf-8",
    )
    store_memory(tmp_path, content="body", headline="h", entities=["e1"])
    rendered = read_hot_layer(tmp_path).render()
    assert "## Memory: Identity" in rendered
    assert "## Memory: Key Points" in rendered
    assert "## Memory: Known Entities" in rendered


def test_headlines_budget_truncates_at_limit(tmp_path: Path) -> None:
    """If many large headlines exist, the budget caps the list."""
    for i in range(40):
        long = "X" * 200
        store_memory(tmp_path, content=f"body {i}", headline=f"{long} {i}")
    layer = read_hot_layer(tmp_path)
    total_chars = sum(len(h) + 2 for h in layer.headlines)
    assert total_chars <= 2000


def test_context_builder_injects_hot_layer_into_stable_tier(tmp_path: Path) -> None:
    """End-to-end: ContextBuilder._build_stable_layer includes the hot layer."""
    from durin.agent.context import ContextBuilder

    store_memory(tmp_path, content="useful", headline="UNIQUE_HEADLINE_TOKEN")

    builder = ContextBuilder(workspace=tmp_path)
    stable = builder._build_stable_layer(channel=None)
    assert "UNIQUE_HEADLINE_TOKEN" in stable
    assert "## Memory: Key Points" in stable


def test_context_builder_omits_hot_layer_when_empty(tmp_path: Path) -> None:
    """No memory entries → no hot-layer section appended to stable."""
    from durin.agent.context import ContextBuilder

    builder = ContextBuilder(workspace=tmp_path)
    stable = builder._build_stable_layer(channel=None)
    assert "## Memory: Key Points" not in stable
    assert "## Memory: Identity" not in stable
    assert "## Memory: Known Entities" not in stable
