"""Tests for TUI state persistence (~/.durin/tui-state.json)."""

from __future__ import annotations


def test_get_recent_models_empty(tmp_path, monkeypatch):
    from durin.cli.tui import state

    monkeypatch.setattr(state, "_state_dir", tmp_path)
    assert state.get_recent_models() == []


def test_add_recent_model_persists(tmp_path, monkeypatch):
    from durin.cli.tui import state

    monkeypatch.setattr(state, "_state_dir", tmp_path)
    state.add_recent_model("glm-5.2")
    assert state.get_recent_models() == ["glm-5.2"]


def test_add_recent_model_dedup_and_order(tmp_path, monkeypatch):
    from durin.cli.tui import state

    monkeypatch.setattr(state, "_state_dir", tmp_path)
    state.add_recent_model("glm-5.2")
    state.add_recent_model("claude-sonnet-4-6")
    state.add_recent_model("glm-5.2")  # re-added → moves to front
    result = state.get_recent_models()
    assert result == ["glm-5.2", "claude-sonnet-4-6"]


def test_add_recent_model_max_five(tmp_path, monkeypatch):
    from durin.cli.tui import state

    monkeypatch.setattr(state, "_state_dir", tmp_path)
    for i in range(7):
        state.add_recent_model(f"model-{i}")
    result = state.get_recent_models()
    assert len(result) == 5
    assert result[0] == "model-6"  # most recent first
    assert "model-0" not in result  # oldest evicted


def test_corrupt_state_file_returns_empty(tmp_path, monkeypatch):
    from durin.cli.tui import state

    monkeypatch.setattr(state, "_state_dir", tmp_path)
    state._state_file().write_text("{invalid json", encoding="utf-8")
    assert state.get_recent_models() == []
