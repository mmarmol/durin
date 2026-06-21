"""Test optimistic CAS guard in edit_file.

edit_file must re-hash the file immediately before atomic_write_bytes.
If the on-disk content changed since the initial read, the edit must be
aborted with a clear error and the concurrent change must be preserved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from durin.agent.tools.filesystem import EditFileTool
from durin.agent.tools.file_state import FileStates, _hash_file


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def tool(workspace: Path) -> EditFileTool:
    fs = FileStates()
    return EditFileTool(workspace=workspace, file_states=fs)


class TestEditFileCAS:

    @pytest.mark.asyncio
    async def test_abort_when_file_changed_between_read_and_write(
        self, tool: EditFileTool, workspace: Path
    ) -> None:
        """Abort path: a concurrent writer mutates the file AFTER edit_file reads
        it but BEFORE the re-hash guard fires. Simulated by patching _hash_file
        in filesystem so the first call returns the original hash and the second
        call (the pre-write recheck) returns a different hash — exactly what
        happens when another process overwrites the file in that window.

        The edit must be rejected and the original on-disk content preserved.
        """
        f = workspace / "target.py"
        original = b"def foo():\n    return 1\n"
        f.write_bytes(original)
        original_hash = _hash_file(str(f))
        other_hash = hashlib.sha256(b"def foo():\n    return 999\n").hexdigest()

        # First call to _hash_file (read-hash capture) returns the real hash.
        # Second call (pre-write recheck) returns a different hash — simulating
        # a concurrent writer that changed the file in the window.
        call_count = 0

        import durin.agent.tools.filesystem as fs_module

        def _hash_file_mock(path: str) -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return original_hash  # the hash we read against
            return other_hash         # concurrent change detected at pre-write recheck

        with patch.object(fs_module, "_hash_file", side_effect=_hash_file_mock):
            result = await tool.execute(
                path=str(f),
                old_text="return 1",
                new_text="return 2",
            )

        # edit_file must have detected the concurrent change and aborted
        assert isinstance(result, str)
        assert "changed on disk" in result.lower() or "re-read" in result.lower(), (
            f"Expected abort message, got: {result!r}"
        )

        # The original bytes must still be on disk (edit_file must not have written)
        assert f.read_bytes() == original, (
            "edit_file must not overwrite when CAS check fails"
        )

    @pytest.mark.asyncio
    async def test_happy_path_unchanged_file(
        self, tool: EditFileTool, workspace: Path
    ) -> None:
        """Happy path: file is not modified between read and write — edit applies."""
        f = workspace / "target.py"
        f.write_bytes(b"def foo():\n    return 1\n")

        result = await tool.execute(
            path=str(f),
            old_text="return 1",
            new_text="return 42",
        )

        assert "Successfully" in result
        assert b"return 42" in f.read_bytes()
