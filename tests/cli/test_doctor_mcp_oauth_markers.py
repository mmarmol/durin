"""`durin doctor` flags OAuth-enabled MCP servers with an orphaned
refresh write-ahead marker (Task B2) — an interrupted token-rotation that
would otherwise surface only as a confusing auth failure on the next connect.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.cli.doctor import check_mcp_oauth_refresh_markers
from durin.config.schema import Config, MCPServerConfig


@pytest.fixture()
def isolated_secrets(tmp_path, monkeypatch):
    """Point the secret store at a temp path (mirrors
    test_mcp_oauth_write_ahead.py's fixture) so marker writes don't touch
    the real ~/.durin secrets."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_file
    )
    import durin.security.secrets as s

    s._STORE = None
    yield secrets_file
    s._STORE = None


def _cfg_with_oauth_server(name: str = "acme", url: str = "https://mcp.example.com") -> Config:
    c = Config()
    c.tools.mcp_servers[name] = MCPServerConfig(url=url, oauth=True)
    return c


def _run(cfg):
    with patch("durin.cli.doctor.load_config", return_value=cfg):
        return check_mcp_oauth_refresh_markers()


def test_ok_when_no_oauth_servers(isolated_secrets):
    r = _run(Config())
    assert r.status == "ok"


def test_ok_when_oauth_server_has_no_marker(isolated_secrets):
    r = _run(_cfg_with_oauth_server())
    assert r.status == "ok"


def test_warns_on_orphaned_marker(isolated_secrets):
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    cfg = _cfg_with_oauth_server()
    SecretsTokenStorage("acme", server_url="https://mcp.example.com").write_refresh_marker()

    r = _run(cfg)
    assert r.status == "warn"
    assert "acme" in r.message
    assert r.fix and "durin mcp login" in r.fix


def test_clears_after_marker_removed(isolated_secrets):
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    cfg = _cfg_with_oauth_server()
    storage = SecretsTokenStorage("acme", server_url="https://mcp.example.com")
    storage.write_refresh_marker()
    assert _run(cfg).status == "warn"

    storage.clear_refresh_marker()
    assert _run(cfg).status == "ok"


def _write_raw_marker(payload: str) -> None:
    """Write a marker blob verbatim (bypassing write_refresh_marker) so tests
    can plant corrupt timestamp shapes."""
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    storage = SecretsTokenStorage("acme", server_url="https://mcp.example.com")
    storage._write(storage._marker_name, payload)


def test_warns_with_coerced_age_on_naive_timestamp(isolated_secrets):
    """A naive ISO ts (no tz) must not crash doctor: coerced to UTC, still warn."""
    import json

    _write_raw_marker(json.dumps({"server": "acme", "ts": "2026-07-17T12:00:00"}))
    r = _run(_cfg_with_oauth_server())
    assert r.status == "warn"
    assert "acme" in r.message
    assert "unknown" not in r.message  # naive is parseable — age is computed


def test_warns_with_unknown_age_on_numeric_timestamp(isolated_secrets):
    """A non-string ts must not crash doctor: age degrades to 'unknown'."""
    import json

    _write_raw_marker(json.dumps({"server": "acme", "ts": 123}))
    r = _run(_cfg_with_oauth_server())
    assert r.status == "warn"
    assert "acme (unknown)" in r.message


def test_check_survives_missing_mcp_sdk(isolated_secrets, monkeypatch):
    """Installs without the [mcp] extra must get a skipped result, not an
    ImportError that kills the whole doctor run: mcp_oauth imports the mcp
    SDK at module top, and run_checks has no per-check exception guard."""
    import sys

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError(f"No module named '{name}'")
            return None

    # Purge cached modules so the check's `from durin.agent.tools.mcp_oauth
    # import ...` really re-executes the mcp import under the blocker.
    for mod in list(sys.modules):
        if mod == "durin.agent.tools.mcp_oauth" or mod == "mcp" or mod.startswith("mcp."):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocker(), *sys.meta_path])

    r = _run(_cfg_with_oauth_server())
    assert r.status == "ok"
    assert "skipped" in r.message


def test_non_oauth_server_ignored_even_with_marker(isolated_secrets):
    """A marker for a server that isn't OAuth-enabled must not be reported
    (defensive: the marker key is server+url-derived, so this should never
    happen in practice, but the filter must not accidentally pick it up)."""
    from durin.agent.tools.mcp_oauth import SecretsTokenStorage

    c = Config()
    c.tools.mcp_servers["plain"] = MCPServerConfig(url="https://mcp.example.com")
    SecretsTokenStorage("plain", server_url="https://mcp.example.com").write_refresh_marker()

    r = _run(c)
    assert r.status == "ok"
