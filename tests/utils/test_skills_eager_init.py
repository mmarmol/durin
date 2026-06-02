from durin.utils.helpers import sync_workspace_templates


def test_workspace_setup_inits_skills_store(tmp_path):
    sync_workspace_templates(tmp_path, silent=True)
    assert (tmp_path / "skills").is_dir()
    assert (tmp_path / "skills" / ".git").is_dir()
