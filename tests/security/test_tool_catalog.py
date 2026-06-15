from durin.security.tool_catalog import load_catalog


def test_load_seed_catalog():
    cat = load_catalog(workspace=None)
    assert "gh" in cat
    assert cat["gh"]["primary"] == {"kind": "brew", "value": "gh"}


def test_workspace_override_merges(tmp_path):
    ws = tmp_path / "skills"
    ws.mkdir()
    (ws / ".tool-catalog.json").write_text(
        '{"gh": {"primary": {"kind": "apt", "value": "gh"}}, "newtool": {"primary": {"kind": "brew", "value": "newtool"}}}'
    )
    cat = load_catalog(workspace=tmp_path)
    assert cat["gh"]["primary"] == {"kind": "apt", "value": "gh"}
    assert cat["rg"]["primary"] == {"kind": "brew", "value": "ripgrep"}
    assert cat["newtool"]["primary"] == {"kind": "brew", "value": "newtool"}


def test_no_workspace_returns_seed_only():
    cat = load_catalog(workspace=None)
    assert set(cat.keys()) >= {"gh", "rg", "jq", "ffmpeg"}


def test_missing_workspace_file_returns_seed(tmp_path):
    cat = load_catalog(workspace=tmp_path)
    assert "gh" in cat
