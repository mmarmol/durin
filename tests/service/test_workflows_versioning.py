"""Every editor mutation of a workflow reaches the version store.

The webui's write paths used to change the tree directly — validated and locked,
but never committed — so an edit made in the workflow editor left no history and
could not be rolled back, while the agent's `workflow_edit` (which goes through
`save_workflow_definition`) did commit. Renames were worse: three filesystem
mutations with no commit at all, which is what left a live workspace showing a
deletion that no later snapshot could ever stage.
"""

import pytest

from durin.service.principal import Principal
from durin.service.workflows import (
    WorkflowDeleteCommand,
    WorkflowDuplicateCommand,
    WorkflowRenameCommand,
    WorkflowSaveCommand,
    WorkflowScriptPutCommand,
    WorkflowsService,
)
from durin.workflow.version_store import WorkflowVersionStore

_VALID = {"name": "wf", "start": "a", "nodes": [{"id": "a", "kind": "work"}]}


def _svc(tmp_path):
    return WorkflowsService(workspace=tmp_path)


def _store(tmp_path) -> WorkflowVersionStore:
    return WorkflowVersionStore(tmp_path / "workflows")


def _dirty(tmp_path) -> list[str]:
    """Paths the version store still sees as uncommitted — the fingerprint of a
    write that bypassed it (or of a change it was unable to stage)."""
    from dulwich import porcelain
    from dulwich.repo import Repo

    root = tmp_path / "workflows"
    with Repo(str(root)) as repo:
        st = porcelain.status(repo, untracked_files="all")
        out = {p.decode() if isinstance(p, bytes) else p for p in list(st.unstaged) + list(st.untracked)}
        for key in ("add", "modify", "delete"):
            for p in st.staged.get(key, []):
                out.add(p.decode() if isinstance(p, bytes) else p)
    # The lock target lives beside the dir, never inside it; nothing else may linger.
    return sorted(p for p in out if not p.endswith(".lock"))


@pytest.mark.asyncio
async def test_save_commits_and_leaves_the_tree_clean(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)

    assert _store(tmp_path).history("wf"), "editor save left no version history"
    assert _dirty(tmp_path) == []


@pytest.mark.asyncio
async def test_delete_commits_the_removal(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    before = len(_store(tmp_path).history())

    await svc.delete(WorkflowDeleteCommand(name="wf"), p)

    # A deletion the store cannot stage stays dirty forever — no later snapshot
    # can ever pick it up, which is exactly what happened on the live box.
    assert _dirty(tmp_path) == []
    assert len(_store(tmp_path).history()) > before


@pytest.mark.asyncio
async def test_duplicate_commits_the_copy(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)

    await svc.duplicate(WorkflowDuplicateCommand(name="wf", target="wf-copy"), p)

    assert _store(tmp_path).history("wf-copy"), "duplicate left no version history"
    assert _dirty(tmp_path) == []


@pytest.mark.asyncio
async def test_rename_is_a_single_commit_covering_every_touched_file(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="child", definition=_VALID), p)
    caller = {
        "name": "caller", "start": "s",
        "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "child"}],
    }
    await svc.save(WorkflowSaveCommand(name="caller", definition=caller), p)
    before = len(_store(tmp_path).history())

    await svc.rename(WorkflowRenameCommand(name="child", target="kid"), p)

    # One commit: the new definition, the removal of the old, and the caller's
    # repointed reference. A partial commit would show a history where `caller`
    # points at a workflow that does not exist.
    assert len(_store(tmp_path).history()) == before + 1
    assert _dirty(tmp_path) == []
    assert not (tmp_path / "workflows" / "child.json").exists()


@pytest.mark.asyncio
async def test_script_put_commits(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.put_script(WorkflowScriptPutCommand(name="do.py", content="print(1)\n"), p)

    assert _store(tmp_path).history(), "script write left no version history"
    assert _dirty(tmp_path) == []
