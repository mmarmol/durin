"""Tests for the ``direction`` parameter on :func:`truncate_text`.

Pi-inspired: shell/exec output truncated from the head (keep the tail)
because errors arrive at the end; file reads truncated from the tail
(keep the head) because context is at the top. The selector lives in
``durin.agent.loop._truncate_tool_output`` (name-based dispatch).
"""

from __future__ import annotations

from durin.agent.loop import _TAIL_TRUNCATION_TOOLS, _truncate_tool_output
from durin.utils.helpers import truncate_text

# ---------------------------------------------------------------------------
# truncate_text — direction parameter
# ---------------------------------------------------------------------------


def test_head_direction_keeps_first_chars():
    text = "abcdef" + "X" * 100 + "TAIL_CONTENT"
    out = truncate_text(text, 10)  # default direction is head
    assert out.startswith("abcdef")
    assert "TAIL_CONTENT" not in out
    assert "truncated" in out


def test_tail_direction_keeps_last_chars():
    """Errors-at-the-end pattern: tail mode preserves the suffix."""
    text = "HEAD_NOISE" + "X" * 100 + "ERROR_AT_END_OF_BUILD_LOG"
    out = truncate_text(text, 30, direction="tail")
    assert "ERROR_AT_END_OF_BUILD_LOG" in out
    assert "HEAD_NOISE" not in out
    assert "truncated" in out


def test_zero_or_negative_cap_disables_truncation():
    text = "anything here"
    assert truncate_text(text, 0) == text
    assert truncate_text(text, -1, direction="tail") == text


def test_short_text_passes_through_unchanged():
    assert truncate_text("hi", 100) == "hi"
    assert truncate_text("hi", 100, direction="tail") == "hi"


def test_unknown_direction_defaults_to_head():
    """Direction strings we don't know about behave like 'head' rather
    than crashing — defensive against typos."""
    text = "abc" + "X" * 50 + "tail"
    out = truncate_text(text, 5, direction="unknown_value")
    assert out.startswith("abc")
    assert "tail" not in out


# ---------------------------------------------------------------------------
# _truncate_tool_output — name-based dispatch
# ---------------------------------------------------------------------------


def test_shell_tool_uses_tail_truncation():
    """``shell`` and ``exec`` are in the tail-truncation whitelist; the
    helper picks ``direction='tail'`` so the model sees the tail of the
    output (where the actual error usually is)."""
    text = "setup noise " * 100 + "FATAL: missing dependency"
    out = _truncate_tool_output(text, 60, "shell")
    assert "FATAL: missing dependency" in out


def test_exec_tool_uses_tail_truncation():
    text = "compilation starting...\n" * 100 + "Error: undefined symbol"
    out = _truncate_tool_output(text, 60, "exec")
    assert "Error: undefined symbol" in out


def test_read_file_tool_uses_head_truncation():
    """``read_file`` (and any other tool not in the whitelist) keeps the
    head — file content is most meaningful from the top."""
    text = "# Document title\n\nFirst paragraph...\n" + "X" * 1000
    out = _truncate_tool_output(text, 50, "read_file")
    assert "# Document title" in out


def test_unknown_tool_name_defaults_to_head():
    text = "head text " + "X" * 1000
    out = _truncate_tool_output(text, 30, "totally-unknown-tool")
    assert out.startswith("head text")


def test_none_tool_name_defaults_to_head():
    """When the persisted tool message has no ``name`` (legacy or
    malformed), the helper still works — defaults to head."""
    text = "header data " + "X" * 1000
    out = _truncate_tool_output(text, 30, None)
    assert out.startswith("header data")


def test_short_output_passes_through_regardless_of_tool():
    out = _truncate_tool_output("brief", 1000, "shell")
    assert out == "brief"


def test_tail_truncation_tools_set_is_well_known():
    """Document the current policy in a test — the whitelist contains
    only shell-like tools whose error output is at the tail."""
    assert "exec" in _TAIL_TRUNCATION_TOOLS
    assert "shell" in _TAIL_TRUNCATION_TOOLS
    # Tools whose output is meaningful from the top must NOT be in here.
    for excluded in ("read_file", "grep", "web_fetch", "list_dir", "repo_overview"):
        assert excluded not in _TAIL_TRUNCATION_TOOLS
