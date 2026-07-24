"""Loop definitions are versioned, like workflows and skills.

Loops were the one editable surface with no history at all: `save_loop` wrote the
file and `delete_loop` unlinked it, so a change made in the webui, by the agent
or from the CLI could not be reviewed or rolled back. The commit lives in the
store rather than in each caller, so every door gets it structurally.
"""

import pytest

from durin.loops.spec import LoopNotFound, parse_loop
from durin.loops.store import delete_loop, loops_dir, save_loop
from durin.loops.version_store import LoopVersionStore


def _spec(name="nightly", intent="goal met"):
    return parse_loop({"name": name, "workflow": "w", "goal": {"intent": intent}})


def _dirty(tmp_path) -> list[str]:
    from dulwich import porcelain
    from dulwich.repo import Repo

    with Repo(str(loops_dir(tmp_path))) as repo:
        st = porcelain.status(repo, untracked_files="all")
        out = {p.decode() if isinstance(p, bytes) else p
               for p in list(st.unstaged) + list(st.untracked)}
        for key in ("add", "modify", "delete"):
            for p in st.staged.get(key, []):
                out.add(p.decode() if isinstance(p, bytes) else p)
    return sorted(out)


def test_save_commits_and_leaves_the_tree_clean(tmp_path):
    save_loop(tmp_path, _spec(), actor="user", reason="created in the loops editor")

    assert LoopVersionStore(loops_dir(tmp_path)).history("nightly")
    # The per-loop lock file lives inside loops/ by design; it must be ignored,
    # not committed, or every save would carry a lock artifact.
    assert _dirty(tmp_path) == []


def test_edit_records_a_second_version(tmp_path):
    save_loop(tmp_path, _spec(intent="first goal"), actor="user", reason="create")
    save_loop(tmp_path, _spec(intent="second goal"), actor="agent", reason="refined the goal")

    history = LoopVersionStore(loops_dir(tmp_path)).history("nightly")
    assert len(history) >= 2
    assert _dirty(tmp_path) == []


def test_delete_commits_the_removal(tmp_path):
    save_loop(tmp_path, _spec(), actor="user", reason="create")
    before = len(LoopVersionStore(loops_dir(tmp_path)).history())

    delete_loop(tmp_path, "nightly", actor="user", reason="retired")

    assert not (loops_dir(tmp_path) / "nightly.json").exists()
    assert len(LoopVersionStore(loops_dir(tmp_path)).history()) > before
    assert _dirty(tmp_path) == []


def test_the_commit_records_who_changed_it(tmp_path):
    save_loop(tmp_path, _spec(), actor="agent", reason="authored by the agent")

    head = LoopVersionStore(loops_dir(tmp_path)).history("nightly")[0]
    trailers = head.trailers or {}
    actor = trailers.get("Actor")
    assert (actor[0] if isinstance(actor, list) else actor) == "agent"


def test_deleting_a_missing_loop_still_raises(tmp_path):
    """Versioning must not change the store's contract."""
    save_loop(tmp_path, _spec(), actor="user", reason="create")
    with pytest.raises(LoopNotFound):
        delete_loop(tmp_path, "ghost", actor="user", reason="r")


def test_versioning_failure_never_breaks_a_save(tmp_path, monkeypatch):
    """Best-effort, like the workflow store: the write already landed."""
    def boom(*a, **k):
        raise RuntimeError("git is unavailable")

    monkeypatch.setattr(LoopVersionStore, "commit_paths", boom)
    save_loop(tmp_path, _spec(), actor="user", reason="create")

    assert (loops_dir(tmp_path) / "nightly.json").is_file()
