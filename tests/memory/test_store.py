"""Tests for memory entry storage (durin.memory.store + MemoryStoreTool)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from durin.memory.provenance import author_scope
from durin.memory.store import StoreError, store_memory
from durin.memory.storage import load_entry


# ---------------------------------------------------------------------------
# store_memory (pure function)
# ---------------------------------------------------------------------------


def test_store_minimal_entry_writes_to_episodic_by_default(tmp_path: Path) -> None:
    result = store_memory(tmp_path, content="A learning worth keeping.")
    assert result["class"] == "episodic"
    path = Path(result["path"])
    assert path.is_file()
    assert path.parent == tmp_path / "memory" / "episodic"
    loaded = load_entry(path)
    assert loaded.body == "A learning worth keeping."
    assert loaded.headline.startswith("A learning")


def test_store_auto_headline_from_first_words(tmp_path: Path) -> None:
    body = (
        "User prefiere terse responses sin emojis a menos que los pida "
        "explicitamente."
    )
    result = store_memory(tmp_path, content=body)
    # First 10 words by default
    expected = " ".join(body.split()[:10])
    assert result["headline"] == expected


def test_store_explicit_headline_wins(tmp_path: Path) -> None:
    result = store_memory(
        tmp_path,
        content="long body text here",
        headline="my custom headline",
    )
    assert result["headline"] == "my custom headline"


def test_store_idempotent_for_same_class_and_content(tmp_path: Path) -> None:
    first = store_memory(tmp_path, content="identical body")
    second = store_memory(tmp_path, content="identical body")
    assert first["id"] == second["id"]
    assert first["path"] == second["path"]


def test_store_distinct_classes_get_distinct_ids(tmp_path: Path) -> None:
    first = store_memory(tmp_path, content="same body", class_name="episodic")
    second = store_memory(tmp_path, content="same body", class_name="stable")
    assert first["id"] != second["id"]


def test_store_records_full_frontmatter(tmp_path: Path) -> None:
    result = store_memory(
        tmp_path,
        content="Body of the entry.",
        class_name="stable",
        headline="explicit headline",
        summary="explicit summary",
        source_refs=["[turn 42](../sessions/abc.md#turn-42)"],
        entities=["marcelo", "durin"],
        valid_from=date(2026, 5, 20),
    )
    loaded = load_entry(Path(result["path"]))
    assert loaded.headline == "explicit headline"
    assert loaded.summary == "explicit summary"
    assert loaded.source_refs == ["[turn 42](../sessions/abc.md#turn-42)"]
    assert loaded.entities == ["marcelo", "durin"]
    assert loaded.valid_from == date(2026, 5, 20)
    assert loaded.body == "Body of the entry."


def test_store_uses_current_author_by_default(tmp_path: Path) -> None:
    """Without an author_scope, author defaults to user_authored."""
    result = store_memory(tmp_path, content="hand-edited content")
    loaded = load_entry(Path(result["path"]))
    assert loaded.author == "user_authored"


def test_store_inside_agent_scope_marks_agent_created(tmp_path: Path) -> None:
    with author_scope("agent_created"):
        result = store_memory(tmp_path, content="agent-derived content")
    loaded = load_entry(Path(result["path"]))
    assert loaded.author == "agent_created"


def test_store_rejects_empty_content(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="content"):
        store_memory(tmp_path, content="")
    with pytest.raises(StoreError, match="content"):
        store_memory(tmp_path, content="   \n  \t  ")


def test_store_rejects_unknown_class(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="unknown class"):
        store_memory(tmp_path, content="body", class_name="feedback")


# ---------------------------------------------------------------------------
# MemoryStoreTool (agent-facing wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_happy_path_marks_agent_created(tmp_path: Path) -> None:
    """Tool implicitly applies author_scope('agent_created')."""
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(content="Something the agent learned.")
    assert "error" not in out
    assert out["author"] == "agent_created"
    loaded = load_entry(Path(out["path"]))
    assert loaded.author == "agent_created"


@pytest.mark.asyncio
async def test_tool_error_on_empty_content(tmp_path: Path) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(content="")
    assert out == {"error": "content is required"}


@pytest.mark.asyncio
async def test_tool_threads_optional_metadata(tmp_path: Path) -> None:
    from durin.agent.tools.memory_store import MemoryStoreTool

    tool = MemoryStoreTool(workspace=tmp_path)
    out = await tool.execute(
        content="body",
        class_name="stable",
        headline="explicit",
        summary="s",
        source_refs=["[t1](../sessions/x.md#turn-1)"],
        entities=["e1", "e2"],
    )
    loaded = load_entry(Path(out["path"]))
    assert loaded.headline == "explicit"
    assert loaded.summary == "s"
    assert loaded.source_refs == ["[t1](../sessions/x.md#turn-1)"]
    assert loaded.entities == ["e1", "e2"]
