"""Tests for prompt history in TUI state."""

from __future__ import annotations

import durin.cli.tui.state as state


def test_prompt_history_empty(monkeypatch, tmp_path):
    state._state_dir = tmp_path
    assert state.get_prompt_history() == []


def test_prompt_history_persists(monkeypatch, tmp_path):
    state._state_dir = tmp_path
    state.add_prompt("hello world")
    state.add_prompt("second prompt")
    assert state.get_prompt_history() == ["hello world", "second prompt"]


def test_prompt_history_max_fifty(monkeypatch, tmp_path):
    state._state_dir = tmp_path
    for i in range(60):
        state.add_prompt(f"prompt {i}")
    history = state.get_prompt_history()
    assert len(history) == 50
    assert history[0] == "prompt 10"
    assert history[-1] == "prompt 59"


def test_prompt_history_strips_whitespace(monkeypatch, tmp_path):
    state._state_dir = tmp_path
    state.add_prompt("  hello  ")
    assert state.get_prompt_history() == ["hello"]


def test_prompt_history_empty_ignored(monkeypatch, tmp_path):
    state._state_dir = tmp_path
    state.add_prompt("")
    state.add_prompt("   ")
    assert state.get_prompt_history() == []
