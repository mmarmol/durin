"""Regression test: tool-result spill must NOT leave stale content when filename is reused.

Hazard #13 (stale-content sub-claim): `maybe_persist_tool_result` previously skipped
the write if the spill file already existed (`if not path.exists()`).  When a
tool_call_id is reused across turns (e.g. the positional `tool_0` fallback in runner.py),
the second call would return a reference whose size/preview described the NEW content
while the file on disk still held the OLD bytes.
"""

from __future__ import annotations

from durin.utils.helpers import maybe_persist_tool_result

# must exceed max_chars so the spill path is taken
_BIG = "A" * 5_000
_BIG2 = "B" * 5_000
_MAX_CHARS = 2_048


def test_second_write_with_same_id_overwrites_stale_content(tmp_path):
    workspace = tmp_path
    session_key = "test_session"
    tool_call_id = "tool_0"

    # First call: persists AAAA... content
    ref1 = maybe_persist_tool_result(
        workspace,
        session_key,
        tool_call_id,
        _BIG,
        max_chars=_MAX_CHARS,
    )
    assert isinstance(ref1, str), "expected a reference string (content was large enough to spill)"
    assert "[tool output persisted]" in ref1

    # Second call: same id, different content (simulates tool_0 reuse across turns)
    ref2 = maybe_persist_tool_result(
        workspace,
        session_key,
        tool_call_id,
        _BIG2,
        max_chars=_MAX_CHARS,
    )
    assert isinstance(ref2, str), "expected a reference string on second call"

    # The spilled file must reflect the NEW content, not the stale one.
    import re
    path_match = re.search(r"Full output saved to: (.+\.txt)", ref2)
    assert path_match, f"could not find path in reference: {ref2!r}"
    spill_path = path_match.group(1).strip()

    disk_content = open(spill_path, encoding="utf-8").read()
    assert disk_content == _BIG2, (
        f"Stale content on disk: expected BBBB... ({len(_BIG2)} chars), "
        f"got first 20 chars = {disk_content[:20]!r}"
    )

    # The reference itself must advertise the new content's size and preview
    assert "B" * 20 in ref2, "reference preview should contain new content (BBBB...)"
    assert "A" * 20 not in ref2, "reference preview must NOT contain stale content (AAAA...)"
