"""Shared constants for the deliberation subsystem."""

from __future__ import annotations

CRITICAL_TOOLS: frozenset[str] = frozenset({
    "exec", "shell", "write_file", "delete_file", "git_push",
    "deploy", "run_command",
})
