"""Path-guard: the write tools refuse writes to durin's own config/secret
stores under DURIN_HOME — wherever DURIN_HOME actually is, not just when it
happens to live inside the workspace (the ordinary allowed-directory
containment check never sees it otherwise: DURIN_HOME is almost never inside
the workspace)."""

import pytest

from durin.agent.tools.filesystem import EditFileTool, WriteFileTool
from durin.agent.tools.path_utils import protected_durin_store_paths


@pytest.fixture()
def durin_home(tmp_path, monkeypatch):
    """Point the config loader at a config.json under a DURIN_HOME distinct
    from the tool's workspace — the exact shape of the reported gap."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("durin.config.loader._current_config_path", home / "config.json")
    return home


def test_protected_paths_track_the_config_loader(durin_home):
    paths = protected_durin_store_paths()
    assert durin_home / "config.json" in paths
    assert durin_home / "config.json.d" in paths
    assert durin_home / "secrets.json" in paths
    assert durin_home / "api_tokens.json" in paths
    assert durin_home / "pairing.json" in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [
    "config.json", "config.json.d/agents.json", "secrets.json", "api_tokens.json",
    "pairing.json",
])
async def test_write_file_refuses_durin_stores_unrestricted(tmp_path, durin_home, rel):
    """No restrict_to_workspace (the common default) — this is exactly the
    reported gap: the allowed-dir containment check is skipped entirely, so
    nothing but this dedicated guard stands between the model and config.json."""
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteFileTool(workspace=ws)
    target = durin_home / rel
    out = await tool.execute(path=str(target), content="{}")
    assert "Error" in out
    assert "durin's configuration is changed by the person" in out
    assert not target.exists()


@pytest.mark.asyncio
async def test_write_file_refuses_durin_store_even_when_restricted(tmp_path, durin_home):
    """restrict_to_workspace=True (allowed_dir set): the specific, actionable
    message must win over the generic 'outside allowed directory' boundary
    error, so the model is told WHY, not just that the path is out of bounds."""
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteFileTool(workspace=ws, allowed_dir=ws)
    out = await tool.execute(path=str(durin_home / "config.json"), content="{}")
    assert "durin's configuration is changed by the person" in out


@pytest.mark.asyncio
async def test_edit_file_refuses_durin_stores(tmp_path, durin_home):
    ws = tmp_path / "ws"
    ws.mkdir()
    secrets_path = durin_home / "secrets.json"
    secrets_path.write_text('{"real": "secret"}', encoding="utf-8")
    tool = EditFileTool(workspace=ws)
    out = await tool.execute(path=str(secrets_path), old_text="real", new_text="pwned")
    assert "durin's configuration is changed by the person" in out
    assert secrets_path.read_text(encoding="utf-8") == '{"real": "secret"}'


@pytest.mark.asyncio
async def test_write_file_still_works_in_the_workspace(tmp_path, durin_home):
    """The guard must not overreach: an ordinary workspace write is untouched."""
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteFileTool(workspace=ws)
    out = await tool.execute(path="notes.txt", content="hi")
    assert "Successfully wrote" in out
    assert (ws / "notes.txt").read_text(encoding="utf-8") == "hi"
