"""Automation definitions are versioned, like loops, workflows, and skills.

Mirrors tests/loops/test_version_store.py's coverage against the automations
store/version-store pair (style reference only — written fresh here).
"""

import pytest

from durin.automations.spec import AutomationNotFound, parse_automation
from durin.automations.store import automations_dir, delete_automation, save_automation
from durin.automations.version_store import AutomationVersionStore


def _spec(name="nightly-sync", workflow="w"):
    return parse_automation({"name": name, "workflow": workflow})


def _dirty(tmp_path) -> list[str]:
    from dulwich import porcelain
    from dulwich.repo import Repo

    with Repo(str(automations_dir(tmp_path))) as repo:
        st = porcelain.status(repo, untracked_files="all")
        out = {p.decode() if isinstance(p, bytes) else p
               for p in list(st.unstaged) + list(st.untracked)}
        for key in ("add", "modify", "delete"):
            for p in st.staged.get(key, []):
                out.add(p.decode() if isinstance(p, bytes) else p)
    return sorted(out)


def test_history_before_any_save_is_empty(tmp_path):
    assert AutomationVersionStore(automations_dir(tmp_path)).history() == []


def test_save_commits_and_leaves_the_tree_clean(tmp_path):
    save_automation(tmp_path, _spec(), actor="user", reason="created in the automations editor")

    assert AutomationVersionStore(automations_dir(tmp_path)).history("nightly-sync")
    # The per-automation lock file lives inside automations/ by design; it
    # must be ignored, not committed, or every save would carry a lock
    # artifact.
    assert _dirty(tmp_path) == []


def test_edit_records_a_second_version(tmp_path):
    save_automation(tmp_path, _spec(workflow="w1"), actor="user", reason="create")
    save_automation(tmp_path, _spec(workflow="w2"), actor="agent", reason="pointed at the new workflow")

    history = AutomationVersionStore(automations_dir(tmp_path)).history("nightly-sync")
    assert len(history) >= 2
    assert _dirty(tmp_path) == []


def test_delete_commits_the_removal(tmp_path):
    save_automation(tmp_path, _spec(), actor="user", reason="create")
    before = len(AutomationVersionStore(automations_dir(tmp_path)).history())

    delete_automation(tmp_path, "nightly-sync", actor="user", reason="retired")

    assert not (automations_dir(tmp_path) / "nightly-sync.json").exists()
    assert len(AutomationVersionStore(automations_dir(tmp_path)).history()) > before
    assert _dirty(tmp_path) == []


def test_the_commit_records_who_changed_it(tmp_path):
    save_automation(tmp_path, _spec(), actor="agent", reason="authored by the agent")

    head = AutomationVersionStore(automations_dir(tmp_path)).history("nightly-sync")[0]
    trailers = head.trailers or {}
    actor = trailers.get("Actor")
    assert (actor[0] if isinstance(actor, list) else actor) == "agent"


def test_deleting_a_missing_automation_still_raises(tmp_path):
    """Versioning must not change the store's contract."""
    save_automation(tmp_path, _spec(), actor="user", reason="create")
    with pytest.raises(AutomationNotFound):
        delete_automation(tmp_path, "ghost", actor="user", reason="r")


def test_versioning_failure_never_breaks_a_save(tmp_path, monkeypatch):
    """Best-effort, like the workflow store: the write already landed."""
    def boom(*a, **k):
        raise RuntimeError("git is unavailable")

    monkeypatch.setattr(AutomationVersionStore, "commit_paths", boom)
    save_automation(tmp_path, _spec(), actor="user", reason="create")

    assert (automations_dir(tmp_path) / "nightly-sync.json").is_file()
