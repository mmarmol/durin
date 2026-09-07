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
    assert layer.types == []
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


def test_render_produces_identity_and_key_points(tmp_path: Path) -> None:
    stable_dir = tmp_path / "memory" / "stable"
    stable_dir.mkdir(parents=True)
    (stable_dir / "IDENTITY.md").write_text(
        "---\nid: IDENTITY\nheadline: id\n---\n\nuser is X\n",
        encoding="utf-8",
    )
    store_memory(tmp_path, content="body", headline="h", entities=["topic:e1"])
    rendered = read_hot_layer(tmp_path).render()
    assert "## Memory: Identity" in rendered
    assert "## Memory: Key Points" in rendered
    assert "Known Entities" not in rendered


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
    assert "## Memory: Known types" not in stable


def test_canonical_block_renders_sources_from_derived_from() -> None:
    from durin.memory.entity_page import EntityPage
    from durin.memory.hot_layer import _render_canonical_block

    page = EntityPage(
        type="patient", name="Drako",
        derived_from=["reference:paper-a", "reference:paper-b"],
    )
    block = _render_canonical_block(
        "patient:drako", page, consolidated_ts="2026-07-05",
    )
    assert "Sources: reference:paper-a, reference:paper-b." in block


def test_canonical_block_omits_sources_when_no_derived_from() -> None:
    from durin.memory.entity_page import EntityPage
    from durin.memory.hot_layer import _render_canonical_block

    page = EntityPage(type="topic", name="Uroperitoneum")
    block = _render_canonical_block(
        "topic:uroperitoneum", page, consolidated_ts="2026-07-05",
    )
    assert "Sources:" not in block


def test_context_builder_renders_a_pinned_page_once(tmp_path: Path) -> None:
    """An always_on page is rendered in the pinned block and must NOT be
    repeated as a canonical block a few lines below."""
    from datetime import datetime, timezone

    from durin.agent.context import ContextBuilder
    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    from durin.memory.principal import mark_always_on

    now = datetime.now(timezone.utc)
    write_entity(tmp_path, "practice:spanish",
                 [FieldPatch(kind="body_append", value="Always respond in Spanish.",
                             author="agent", source_ref="s", at=now)],
                 create=True, name="Always Spanish")
    mark_always_on(tmp_path, "practice:spanish")

    stable = ContextBuilder(workspace=tmp_path)._build_stable_layer(channel=None)

    assert stable.count("Always respond in Spanish.") == 1
    assert "## Always-on guidance" in stable
    assert "=== CANONICAL: practice:spanish" not in stable
