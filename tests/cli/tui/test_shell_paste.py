"""D3.2 — shell paste tests (! and !!)."""

from __future__ import annotations

import pytest

from durin.cli.tui.shell_paste import ShellPasteResult, process_shell_paste


def test_plain_text_passes_through() -> None:
    result = process_shell_paste("hola que tal")
    assert result.send is True
    assert result.message == "hola que tal"
    assert result.ran_command is None


def test_single_bang_runs_and_appends_output() -> None:
    result = process_shell_paste("!echo hello")
    assert result.send is True
    assert "hello" in result.message
    assert "echo hello" in result.message
    assert "exit 0" in result.message
    assert result.ran_command == "echo hello"
    assert result.exit_code == 0


def test_double_bang_runs_silently() -> None:
    result = process_shell_paste("!!echo silent")
    assert result.send is False
    assert result.message == ""
    assert result.ran_command == "echo silent"


def test_single_bang_empty_command_passes_through() -> None:
    """`!` followed by whitespace is not a command — pass through."""
    result = process_shell_paste("!  ")
    assert result.send is True
    assert result.message == "!  "


def test_double_bang_empty_command_passes_through() -> None:
    result = process_shell_paste("!!  ")
    assert result.send is True
    assert result.message == "!!  "


def test_command_with_non_zero_exit_captured() -> None:
    """A failing command still surfaces its output + exit code."""
    result = process_shell_paste("!false")
    assert result.send is True
    assert result.exit_code == 1
    assert "exit 1" in result.message


def test_command_output_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long output gets truncated to keep the prompt under control."""
    from durin.cli.tui import shell_paste

    monkeypatch.setattr(shell_paste, "_MAX_OUTPUT_CHARS", 100)
    result = process_shell_paste("!python3 -c 'print(\"x\" * 500)'")
    assert "truncated" in result.message
