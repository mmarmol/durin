from durin.utils.helpers import sync_workspace_templates


def test_sync_creates_soul_files(tmp_path):
    sync_workspace_templates(tmp_path, silent=True)
    for slug in ("researcher", "engineer", "tutor"):
        assert (tmp_path / "souls" / f"{slug}.md").exists()


def test_sync_does_not_overwrite_existing_soul(tmp_path):
    (tmp_path / "souls").mkdir()
    (tmp_path / "souls" / "researcher.md").write_text("MINE", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    assert (tmp_path / "souls" / "researcher.md").read_text(encoding="utf-8") == "MINE"
