"""TUI/CLI surface — `durin mcp search`."""
from typer.testing import CliRunner

from durin.cli.mcp_cmd import mcp_app

runner = CliRunner()


def _config(tmp_path, monkeypatch):
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)


class _Reg:
    name = "official"

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [{"name": "io.x/jira", "description": "Jira issues"}], None


def test_mcp_search_cli(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_Reg()]
    )
    res = runner.invoke(mcp_app, ["search", "jira"])
    assert res.exit_code == 0
    assert "io.x/jira" in res.stdout


def test_mcp_search_cli_empty(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    monkeypatch.setattr("durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [])
    res = runner.invoke(mcp_app, ["search", "zzz"])
    assert res.exit_code == 0
    assert "No servers found" in res.stdout
