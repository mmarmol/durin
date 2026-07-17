"""Consolidation preserves file paths discovered in the archived span.

Tool results used to be summarized away with their discovered paths
(2026-07-17 incident: zendesk-ticket-evaluation forgotten 4 times).
Paths now ride the session summary mechanically — no LLM trust involved.
history.jsonl (dream input) stays untouched.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.memory import (
    Consolidator,
    MemoryStore,
    extract_discovered_paths,
)


def _tool_call_msg(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def test_extracts_paths_from_tool_call_arguments():
    messages = [_tool_call_msg("list_dir", {"path": "/ws/openclaw/skills"})]
    assert extract_discovered_paths(messages) == ["/ws/openclaw/skills"]


def test_extracts_paths_from_exec_command_strings():
    messages = [_tool_call_msg("exec", {"command": "cat /etc/durin/config.json | head"})]
    assert "/etc/durin/config.json" in extract_discovered_paths(messages)


def test_extracts_relative_paths_from_tool_results():
    messages = [{
        "role": "tool", "name": "grep",
        "content": "workspace-legolas/skills/zendesk-ticket-evaluation/SKILL.md",
    }]
    assert extract_discovered_paths(messages) == [
        "workspace-legolas/skills/zendesk-ticket-evaluation/SKILL.md"
    ]


def test_ignores_urls_and_version_tokens():
    messages = [{
        "role": "tool", "name": "exec",
        "content": "see https://example.com/foo/bar.html and https://api.github.com/repos/x/y",
    }, {
        "role": "tool", "name": "exec",
        "content": "installed durin-agent/0.2.0 from pypi",
    }]
    assert extract_discovered_paths(messages) == []


def test_dedups_caps_and_ignores_non_discovery_tools():
    noise = [{
        "role": "tool", "name": "memory_search",
        "content": "/should/not/appear.md",
    }]
    flood = [{
        "role": "tool", "name": "exec",
        "content": "\n".join(f"/tmp/file_{i}.txt" for i in range(40)),
    }]
    dup = [_tool_call_msg("read_file", {"path": "/tmp/file_0.txt"})]
    paths = extract_discovered_paths(dup + flood + noise)
    assert paths[0] == "/tmp/file_0.txt"
    assert len(paths) == 15
    assert "/should/not/appear.md" not in paths


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


async def test_archive_appends_paths_to_summary_not_history(
    consolidator, mock_provider, store,
):
    mock_provider.chat_with_retry.return_value = MagicMock(
        content="- migrated some docs"
    )
    messages = [
        {"role": "user", "content": "what skills exist?"},
        _tool_call_msg("list_dir", {"path": "/ws/skills"}),
        {"role": "tool", "name": "list_dir", "content": "zendesk-eval"},
    ]
    summary, _tags = await consolidator.archive(messages)
    assert summary is not None
    assert "Files/paths examined in this span" in summary
    assert "/ws/skills" in summary
    entries = store.read_unprocessed_history(since_cursor=0)
    assert len(entries) == 1
    assert "Files/paths examined" not in entries[0]["content"]
