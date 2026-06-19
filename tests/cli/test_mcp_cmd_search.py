from typer.testing import CliRunner
from durin.cli.mcp_cmd import mcp_app
import durin.cli.mcp_cmd as cmd


def test_search_all_flag(monkeypatch):
    seen = {}

    async def fake_search(query, *, limit, quality, min_stars):
        seen["quality"] = quality
        from durin.agent.mcp_registry import McpServerHit
        return [McpServerHit(name="com.acme/x", ref="com.acme/x", registry="official",
                             kind="local", description="d",
                             signals={"stars": 1200, "owner_login": "acme"})]

    monkeypatch.setattr(cmd, "search_mcp_registries", fake_search)
    out = CliRunner().invoke(mcp_app, ["search", "acme", "--all"])
    assert out.exit_code == 0
    assert seen["quality"] == "all"
    assert "com.acme/x" in out.stdout
    assert "1200" in out.stdout or "1.2k" in out.stdout


def test_search_shows_owner(monkeypatch):
    async def fake_search(query, *, limit, quality, min_stars):
        from durin.agent.mcp_registry import McpServerHit
        return [McpServerHit(name="com.acme/x", ref="com.acme/x", registry="official",
                             kind="local", description="d",
                             signals={"stars": 42, "owner_login": "acme"})]

    monkeypatch.setattr(cmd, "search_mcp_registries", fake_search)
    out = CliRunner().invoke(mcp_app, ["search", "acme"])
    assert out.exit_code == 0
    assert "@acme" in out.stdout
    assert "42" in out.stdout


def test_search_no_stars_no_crash(monkeypatch):
    async def fake_search(query, *, limit, quality, min_stars):
        from durin.agent.mcp_registry import McpServerHit
        return [McpServerHit(name="com.acme/y", ref="com.acme/y", registry="official",
                             kind="remote", description="",
                             signals={})]

    monkeypatch.setattr(cmd, "search_mcp_registries", fake_search)
    out = CliRunner().invoke(mcp_app, ["search", "acme"])
    assert out.exit_code == 0
    assert "com.acme/y" in out.stdout
