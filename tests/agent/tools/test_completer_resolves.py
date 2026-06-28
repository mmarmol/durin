"""Regression test for completer-offered workspace-relative paths resolving
correctly even when a work_dir is active.

This pins the behavior from Task 2 (resolve_workspace_path with work_dir parameter).
A managed-prefix path like "ingested/abc/source.md" must resolve to the workspace root,
not to the work directory, preserving the anchored intent of the completer.
"""

from pathlib import Path

from durin.agent.tools.path_utils import resolve_workspace_path


def test_managed_reference_resolves_with_active_work_dir(tmp_path: Path):
    """A completer-offered workspace-relative path under a managed prefix must
    still resolve to the workspace even when a work dir is active."""
    ws = tmp_path
    (ws / "ingested" / "abc").mkdir(parents=True)
    (ws / "ingested" / "abc" / "source.md").write_text("doc")
    work = ws / "work" / "s1"
    work.mkdir(parents=True)
    out = resolve_workspace_path("ingested/abc/source.md", ws, allowed_dir=ws, work_dir=work)
    assert out == (ws / "ingested" / "abc" / "source.md").resolve()
