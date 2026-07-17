"""Oversized tool results spill to disk before save-time truncation."""
from __future__ import annotations

from pathlib import Path

from durin.utils.helpers import (
    maybe_persist_tool_result,
    parse_persisted_reference,
    persist_full_tool_result,
)


def test_persist_full_writes_bucket_file(tmp_path: Path) -> None:
    path = persist_full_tool_result(tmp_path, "web:chat", "call_1", "x" * 100)
    assert path is not None and path.exists()
    assert path.read_text(encoding="utf-8") == "x" * 100


def test_persist_full_none_workspace_is_noop() -> None:
    assert persist_full_tool_result(None, "k", "call_1", "y") is None


def test_maybe_persist_behavior_unchanged(tmp_path: Path) -> None:
    ref = maybe_persist_tool_result(tmp_path, "k", "call_2", "z" * 500, max_chars=100)
    parsed = parse_persisted_reference(ref)
    assert parsed is not None
    path, size = parsed
    assert size == 500 and Path(path).exists()
