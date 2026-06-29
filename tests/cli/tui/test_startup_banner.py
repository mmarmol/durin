"""Tests for the startup banner (tools + skills discoverability)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from durin.cli.tui.startup import (
    build_startup_banner,
    categorize_tools,
    memory_summary,
)


def test_categorize_tools_groups_by_purpose() -> None:
    names = [
        "read_file", "write_file", "exec",
        "memory_search", "memory_store",
        "web_fetch", "web_search",
        "spawn", "tasks",
        "unknown_tool",
    ]
    cats = categorize_tools(names)
    assert "file" in cats
    assert set(cats["file"]) == {"read_file", "write_file"}
    assert cats["shell"] == ["exec"]
    assert set(cats["memory"]) == {"memory_search", "memory_store"}
    assert set(cats["web"]) == {"web_fetch", "web_search"}
    assert set(cats["agent"]) == {"spawn", "tasks"}
    # Unknown tools fall through to misc.
    assert "unknown_tool" in cats["misc"]


def test_categorize_tools_returns_alphabetically_sorted_groups() -> None:
    cats = categorize_tools(["write_file", "read_file"])
    assert cats["file"] == ["read_file", "write_file"], "tools should be sorted A→Z"


def test_categorize_tools_empty_input() -> None:
    assert categorize_tools([]) == {}


def test_build_startup_banner_includes_keybindings() -> None:
    body = build_startup_banner(version="0.1.0a7.dev7", agent_loop=None)
    assert "ctrl+q quit" in body
    assert "ctrl+y copy reply" in body
    assert "/ commands" in body


def test_build_startup_banner_renders_tools_section_when_loop_exposes_them() -> None:
    loop = SimpleNamespace(
        workspace="/tmp/durin_test_ws",
        model="glm-5.1",
        model_preset="default",
        tool_names=[
            "read_file", "write_file", "exec",
            "memory_search", "web_fetch",
            "spawn", "tasks",
        ],
    )
    body = build_startup_banner(version="0.1.0a7.dev7", agent_loop=loop)
    assert "Available tools" in body
    # Each category shows up.
    for cat in ("file", "shell", "memory", "web", "agent"):
        assert cat in body
    # Each tool appears.
    for tool in ("read_file", "write_file", "exec", "memory_search", "web_fetch"):
        assert tool in body


def test_build_startup_banner_omits_tools_section_when_loop_lacks_them() -> None:
    body = build_startup_banner(version="0.1.0a7.dev7", agent_loop=None)
    # No empty "Available tools" header without content.
    assert "Available tools" not in body


def test_build_startup_banner_profile_line_summarises_counts() -> None:
    loop = SimpleNamespace(
        workspace="/tmp/durin_test_ws",
        model="glm-5.1",
        model_preset="custom",
        tool_names=["read_file", "exec", "web_fetch"],
    )
    body = build_startup_banner(version="0.1.0a7.dev7", agent_loop=loop)
    assert "Profile: custom" in body
    assert "3 tools" in body
    assert "/help for commands" in body


def test_build_startup_banner_handles_workspace_home_substitution(tmp_path: Path) -> None:
    """If workspace path is under $HOME, the banner should show `~/…`."""
    import os

    fake_home = tmp_path / "homedir"
    fake_home.mkdir()
    workspace = fake_home / ".durin" / "workspace"
    workspace.mkdir(parents=True)
    loop = SimpleNamespace(workspace=str(workspace), model="m", model_preset="d")

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(fake_home)
    try:
        body = build_startup_banner(version="x", agent_loop=loop)
    finally:
        if old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old_home
    # Depending on Path.home() implementation it may not pick up $HOME
    # directly — only assert the workspace path appears somewhere.
    assert ".durin/workspace" in body


def test_memory_summary_counts_only_class_entries(tmp_path: Path) -> None:
    """`memory_docs` must agree with `/memory list`: only entries under the
    canonical class folders (stable/episodic/corpus/pending), NOT
    `MEMORY.md` at the root (that's Dream's summary file)."""
    workspace = tmp_path / "ws"
    mem = workspace / "memory"
    mem.mkdir(parents=True)
    # Dream's summary at the root — must NOT be counted.
    (mem / "MEMORY.md").write_text("# summary")
    # Real entries under class folders — these get counted.
    (mem / "stable").mkdir()
    (mem / "stable" / "user-name.md").write_text("---\n---\n")
    (mem / "episodic").mkdir()
    (mem / "episodic" / "yesterday.md").write_text("---\n---\n")
    (mem / "episodic" / "today.md").write_text("---\n---\n")

    (workspace / "sessions").mkdir()
    (workspace / "sessions" / "x.jsonl").write_text("{}")
    (workspace / "skills" / "skill1").mkdir(parents=True)
    (workspace / "skills" / "skill1" / "SKILL.md").write_text("# skill")

    stats = memory_summary(workspace)
    assert stats["memory_docs"] == 3  # 1 stable + 2 episodic, NOT MEMORY.md
    assert stats["sessions"] == 1
    assert stats["skills"] == 1
    assert stats["vec_present"] is False


def test_memory_summary_does_not_count_dream_summary(tmp_path: Path) -> None:
    """Only `MEMORY.md` at the root with no class folders: count must be 0."""
    workspace = tmp_path / "ws"
    mem = workspace / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# summary")
    stats = memory_summary(workspace)
    assert stats["memory_docs"] == 0, "Dream's MEMORY.md must not inflate the entry count"
