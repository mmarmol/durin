from durin.utils.helpers import sync_workspace_templates


def test_workspace_setup_inits_skills_store(tmp_path):
    sync_workspace_templates(tmp_path, silent=True)
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "skills" / ".git").is_dir()


def test_workspace_setup_does_not_create_legacy_memory_md(tmp_path):
    # MEMORY.md is superseded by the entity-page model:
    # nothing injects or writes it, so the scaffold must not create it.
    sync_workspace_templates(tmp_path, silent=True)
    assert not (tmp_path / "memory" / "MEMORY.md").exists()
    assert (tmp_path / "memory" / "history.jsonl").exists()  # scaffold still works
