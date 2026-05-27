"""Git history formatter for the ``{recent_history}`` prompt slot.

Per `docs/memory/05_dream_cold_path.md` §5.1: the LLM gets a compact
view of the last ~30 days of git commits to its target entity page so
it can avoid undoing its own recent decisions. The git source is
``memory/.git/`` (the memory subsystem has its own repo per doc 02).

The formatter:
  - Walks `git log --since='30 days ago' -- <entity_path>` on
    ``memory/.git/``.
  - Parses each commit's subject + first paragraph of body.
  - Returns a compact multi-line block ready for prompt injection.
  - Returns ``"(no recent history)"`` when the repo doesn't exist,
    the entity has no commits, or the call fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from durin.memory.dream_git_history import format_recent_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_memory_repo(workspace: Path) -> Path:
    """Initialize ``workspace/memory/.git/`` and return memory root."""
    memory_root = workspace / "memory"
    memory_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=memory_root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@durin.local"],
        cwd=memory_root, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=memory_root, check=True,
    )
    return memory_root


def _commit_file(memory_root: Path, rel_path: str, body: str,
                  message: str) -> None:
    path = memory_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", rel_path],
        cwd=memory_root, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=memory_root, check=True,
    )


# ---------------------------------------------------------------------------
# No repo / no history
# ---------------------------------------------------------------------------


def test_missing_memory_repo_returns_no_history(tmp_path: Path) -> None:
    out = format_recent_history(tmp_path, "person:marcelo")
    assert out == "(no recent history)"


def test_repo_without_entity_commits_returns_no_history(
    tmp_path: Path,
) -> None:
    memory_root = _init_memory_repo(tmp_path)
    _commit_file(memory_root, "stable/intro.md",
                 "---\nid: x\n---\n", "Add intro stable")
    out = format_recent_history(tmp_path, "person:marcelo")
    assert out == "(no recent history)"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_lists_commits_for_target_entity(tmp_path: Path) -> None:
    memory_root = _init_memory_repo(tmp_path)
    _commit_file(
        memory_root,
        "entities/person/marcelo.md",
        "---\ntype: person\nname: Marcelo\n---\n",
        "Bootstrap Marcelo's canonical page",
    )
    _commit_file(
        memory_root,
        "entities/person/marcelo.md",
        "---\ntype: person\nname: Marcelo\naliases: [m]\n---\n",
        "Add alias m to Marcelo",
    )
    out = format_recent_history(tmp_path, "person:marcelo")
    assert "Bootstrap Marcelo's canonical page" in out
    assert "Add alias m to Marcelo" in out


def test_unrelated_commits_excluded(tmp_path: Path) -> None:
    memory_root = _init_memory_repo(tmp_path)
    _commit_file(
        memory_root,
        "entities/person/susana.md",
        "---\ntype: person\nname: Susana\n---\n",
        "Add Susana's page",
    )
    _commit_file(
        memory_root,
        "entities/person/marcelo.md",
        "---\ntype: person\nname: Marcelo\n---\n",
        "Bootstrap Marcelo's canonical page",
    )
    out = format_recent_history(tmp_path, "person:marcelo")
    assert "Bootstrap Marcelo's canonical page" in out
    assert "Susana" not in out


def test_commit_subjects_listed_newest_first(tmp_path: Path) -> None:
    memory_root = _init_memory_repo(tmp_path)
    _commit_file(memory_root, "entities/person/m.md",
                 "---\ntype: person\nname: M\n---\n",
                 "First commit")
    _commit_file(memory_root, "entities/person/m.md",
                 "---\ntype: person\nname: M\naliases: [m]\n---\n",
                 "Second commit")
    _commit_file(memory_root, "entities/person/m.md",
                 "---\ntype: person\nname: M\naliases: [m, mm]\n---\n",
                 "Third commit")
    out = format_recent_history(tmp_path, "person:m")
    # Newest first: "Third" before "Second" before "First".
    assert out.index("Third commit") < out.index("Second commit")
    assert out.index("Second commit") < out.index("First commit")


# ---------------------------------------------------------------------------
# Robustness — malformed inputs don't crash
# ---------------------------------------------------------------------------


def test_malformed_entity_ref_returns_no_history(tmp_path: Path) -> None:
    _init_memory_repo(tmp_path)
    out = format_recent_history(tmp_path, "no_colon")
    assert out == "(no recent history)"


def test_git_binary_missing_or_failure_returns_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `git` itself crashes, the formatter must not propagate —
    the dream pass continues without recent_history."""
    _init_memory_repo(tmp_path)
    _commit_file(tmp_path / "memory", "entities/person/x.md",
                 "---\ntype: person\nname: X\n---\n", "Init")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git binary not found")
    monkeypatch.setattr(subprocess, "run", _boom)

    out = format_recent_history(tmp_path, "person:x")
    assert out == "(no recent history)"
