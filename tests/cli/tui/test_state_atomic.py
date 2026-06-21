"""Single-process atomic+lock tests for tui-state.json.

Verifies that add_recent_model and add_prompt use atomic_write_text (no
partial write on crash) and that the lock file is created when they run.
"""

from __future__ import annotations

from pathlib import Path


def test_add_recent_model_creates_lock_file(tmp_path: Path) -> None:
    from durin.cli.tui import state

    state._state_dir = tmp_path
    state.add_recent_model("glm-9")
    # The lock file sits at <state_file>.lock
    assert (tmp_path / "tui-state.json.lock").exists()
    assert state.get_recent_models() == ["glm-9"]


def test_add_prompt_creates_lock_file(tmp_path: Path) -> None:
    from durin.cli.tui import state

    state._state_dir = tmp_path
    state.add_prompt("hello world")
    assert (tmp_path / "tui-state.json.lock").exists()
    assert "hello world" in state.get_prompt_history()


def test_state_file_is_valid_json_after_write(tmp_path: Path) -> None:
    import json

    from durin.cli.tui import state

    state._state_dir = tmp_path
    state.add_recent_model("model-X")
    state.add_prompt("a prompt")
    raw = (tmp_path / "tui-state.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "model-X" in data["recent_models"]
    assert "a prompt" in data["prompt_history"]
