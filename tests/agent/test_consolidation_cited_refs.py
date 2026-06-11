"""Tests for P5 — consolidation preserves memory refs cited in the span.

Explicit memory_search / memory_drill results in evicted turns used to
be summarized away entirely. The refs (pointers, not content) now ride
the session summary, extracted mechanically from the tool results —
no LLM trust involved. history.jsonl (dream input) stays untouched.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.memory import (
    Consolidator,
    MemoryStore,
    extract_cited_memory_refs,
)


def _search_tool_message(rendered: str) -> dict:
    """A persisted memory_search tool result (json-encoded, LLM shape)."""
    return {
        "role": "tool",
        "name": "memory_search",
        "content": json.dumps({
            "total": 2,
            "strategy": "hybrid",
            "sectioned_rendered": rendered,
        }),
    }


_RENDERED = (
    "## Canonical\n\n"
    "=== CANONICAL: person:marcelo (consolidated 2026-06-01, preview 80/600) ===\n"
    "Architect of durin.\n"
    "=== END CANONICAL ===\n\n"
    "=== FRAGMENT: memory/episodic/abc123.md (ts 2026-06-02) ===\n"
    "Met on Tuesday.\n"
    "=== END FRAGMENT ===\n\n"
    "=== SKILL: deploy-gateway (complete) ===\n"
    "Steps to deploy.\n"
    "=== END SKILL ==="
)


# ---------------------------------------------------------------------------
# extract_cited_memory_refs
# ---------------------------------------------------------------------------


def test_extracts_marker_refs_without_qualifiers_and_skips_skills():
    refs = extract_cited_memory_refs([_search_tool_message(_RENDERED)])
    assert refs == ["person:marcelo", "memory/episodic/abc123.md"]


def test_dedups_across_messages_preserving_first_seen_order():
    msg = _search_tool_message(_RENDERED)
    other = _search_tool_message(
        "=== SESSION: sessions/s42 (ts 2026-06-03) ===\nsummary\n=== END SESSION ==="
    )
    refs = extract_cited_memory_refs([msg, other, msg])
    assert refs == [
        "person:marcelo", "memory/episodic/abc123.md", "sessions/s42",
    ]


def test_ignores_non_memory_tools_and_non_tool_roles():
    messages = [
        {"role": "tool", "name": "read_file", "content": _RENDERED},
        {"role": "assistant", "content": _RENDERED},
        {"role": "user", "content": _RENDERED},
    ]
    assert extract_cited_memory_refs(messages) == []


def test_caps_total_refs():
    rendered = "\n".join(
        f"=== FRAGMENT: memory/episodic/e{i}.md (ts 2026) ===\nx\n=== END FRAGMENT ==="
        for i in range(30)
    )
    refs = extract_cited_memory_refs([_search_tool_message(rendered)])
    assert len(refs) == 20


# ---------------------------------------------------------------------------
# archive() wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def consolidator(store, mock_provider):
    sessions = MagicMock()
    sessions.save = MagicMock()
    return Consolidator(
        store=store,
        provider=mock_provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=1000,
        build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
    )


async def test_archive_appends_refs_line_to_summary_only(
    consolidator, mock_provider, store,
):
    mock_provider.chat_with_retry.return_value = MagicMock(
        content="- user asked about marcelo's role"
    )
    messages = [
        {"role": "user", "content": "who is marcelo?"},
        _search_tool_message(_RENDERED),
        {"role": "assistant", "content": "Architect of durin."},
    ]

    summary, _tags = await consolidator.archive(messages)

    assert summary is not None
    assert "Memory refs cited in this span" in summary
    assert "person:marcelo; memory/episodic/abc123.md" in summary
    # history.jsonl (dream input) carries the raw LLM output, untouched.
    entries = store.read_unprocessed_history(since_cursor=0)
    assert len(entries) == 1
    assert "Memory refs cited" not in entries[0]["content"]


async def test_archive_without_memory_tool_results_leaves_summary_unchanged(
    consolidator, mock_provider,
):
    mock_provider.chat_with_retry.return_value = MagicMock(
        content="- nothing memory-related"
    )
    messages = [{"role": "user", "content": "hello"}]

    summary, _tags = await consolidator.archive(messages)

    assert summary == "- nothing memory-related"
