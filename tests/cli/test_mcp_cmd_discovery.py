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


def _seed(monkeypatch, servers):
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: servers)


def test_mcp_search_cli(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    _seed(monkeypatch, [
        {"name": "io.x/jira", "ref": "io.x/jira",
         "description": "Jira issues", "official": True},
    ])
    res = runner.invoke(mcp_app, ["search", "jira"])
    assert res.exit_code == 0
    assert "io.x/jira" in res.stdout


def test_mcp_search_cli_empty(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    _seed(monkeypatch, [])
    res = runner.invoke(mcp_app, ["search", "zzz"])
    assert res.exit_code == 0
    assert "No servers found" in res.stdout
