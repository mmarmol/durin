"""Generic write tools may not touch the registries that own a versioned door.

`skills/` was already guarded. `workflows/` and `loops/` were not, so the only
thing keeping an agent from rewriting a workflow definition or the script a
script node executes — unvalidated, unlocked and unversioned — was an instruction
in a skill. The dream holds those same generic tools over the whole workspace.
"""

import pytest

from durin.agent.tools.file_state import FileStates
from durin.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool


def _write_tool(tmp_path):
    return WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path, file_states=FileStates())


@pytest.mark.asyncio
@pytest.mark.parametrize("relpath", [
    "skills/some-skill/SKILL.md",
    "workflows/ticket-pipeline.json",
    "workflows/scripts/run-investigation.py",
    "loops/nightly.json",
    "automations/chase-invoice-4471.json",
])
async def test_generic_write_is_refused_in_guarded_registries(tmp_path, relpath):
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)

    out = await _write_tool(tmp_path).execute(path=str(target), content="x")

    assert "Error" in out, f"{relpath} was writable through the generic door"
    assert not target.exists()


@pytest.mark.asyncio
async def test_unguarded_workspace_paths_still_write(tmp_path):
    target = tmp_path / "notes" / "scratch.md"

    out = await _write_tool(tmp_path).execute(path=str(target), content="hello")

    assert "Error" not in out
    assert target.read_text() == "hello"


@pytest.mark.asyncio
async def test_reads_stay_legitimate(tmp_path):
    """Only writes are guarded — the agent must still be able to read a workflow."""
    wf = tmp_path / "workflows" / "wf.json"
    wf.parent.mkdir(parents=True)
    wf.write_text('{"name": "wf"}')

    out = await ReadFileTool(
        workspace=tmp_path, allowed_dir=tmp_path, file_states=FileStates(),
    ).execute(path=str(wf))

    assert '"name"' in out


@pytest.mark.asyncio
async def test_edit_is_refused_too(tmp_path):
    wf = tmp_path / "workflows" / "wf.json"
    wf.parent.mkdir(parents=True)
    wf.write_text('{"name": "wf"}')
    tool = EditFileTool(workspace=tmp_path, allowed_dir=tmp_path, file_states=FileStates())

    out = await tool.execute(path=str(wf), old_string='"wf"', new_string='"hacked"')

    assert "Error" in out
    assert "hacked" not in wf.read_text()


@pytest.mark.asyncio
async def test_automations_edit_is_refused_too(tmp_path):
    """Twin of test_edit_is_refused_too for automations/ — the automations tool
    is what validates a definition and versions the change; a generic edit must
    not be able to rewrite one (e.g. flipping "enabled" or a trigger) unvalidated
    and unversioned."""
    automation = tmp_path / "automations" / "a1.json"
    automation.parent.mkdir(parents=True)
    automation.write_text('{"name": "a1"}')
    tool = EditFileTool(workspace=tmp_path, allowed_dir=tmp_path, file_states=FileStates())

    out = await tool.execute(path=str(automation), old_text='"a1"', new_text='"hacked"')

    assert "Error" in out
    assert "hacked" not in automation.read_text()


@pytest.mark.asyncio
async def test_automations_runs_sibling_dir_is_not_swept_into_the_guard(tmp_path):
    """automations-runs/ (the run-manifest log, durin.automations.run_log.runs_root)
    sits right beside the guarded automations/ registry and shares its prefix as a
    plain string — proving containment is checked by path segment (Path.relative_to),
    not by a naive string-prefix test that would also catch this sibling."""
    target = tmp_path / "automations-runs" / "a1" / "run1.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    out = await _write_tool(tmp_path).execute(path=str(target), content="{}")

    assert "Error" not in out
    assert target.read_text() == "{}"


@pytest.mark.asyncio
async def test_sanctioned_script_door_writes_and_versions(tmp_path):
    """Closing the generic door must not remove the capability: the agent gets a
    door that validates and commits, the same one the editor route uses."""
    from durin.agent.tools.workflow_script_write import WorkflowScriptWriteTool
    from durin.workflow.version_store import WorkflowVersionStore

    out = await WorkflowScriptWriteTool(workspace=tmp_path).execute(
        name="probe.py", content="print(1)\n", rationale="deterministic step",
    )

    assert '"ok": true' in out.lower()
    assert (tmp_path / "workflows" / "scripts" / "probe.py").read_text() == "print(1)\n"
    assert WorkflowVersionStore(tmp_path / "workflows").history(), "script write left no history"


@pytest.mark.asyncio
async def test_sanctioned_script_door_rejects_path_traversal(tmp_path):
    from durin.agent.tools.workflow_script_write import WorkflowScriptWriteTool

    out = await WorkflowScriptWriteTool(workspace=tmp_path).execute(
        name="../../escape.py", content="x", rationale="r",
    )

    assert "error" in out.lower()
    assert not (tmp_path.parent / "escape.py").exists()
