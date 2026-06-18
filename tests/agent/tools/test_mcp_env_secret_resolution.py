"""Phase 3 / Task 9 — resolve ${secret:NAME} in MCP env/headers at spawn."""
import pytest

from durin.agent.tools.mcp_connection import _resolve_secret_map


@pytest.fixture()
def secret_store_tmp(tmp_path, monkeypatch):
    import durin.security.secrets as s
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    monkeypatch.setattr(s, "_STORE", None)  # rebind the process-wide store to tmp
    return path


def test_env_secret_ref_resolved(secret_store_tmp):
    from durin.security.secrets import store_secret

    ref = store_secret(
        "MCP_JIRA_TOKEN", "s3cr3t-value-123",
        service="mcp:jira", scope=["mcp:jira"], origin="user",
    )
    resolved = _resolve_secret_map({"JIRA_TOKEN": ref, "JIRA_URL": "https://x"})
    assert resolved["JIRA_TOKEN"] == "s3cr3t-value-123"
    assert resolved["JIRA_URL"] == "https://x"  # non-ref values pass through


def test_resolve_none_and_empty_passthrough():
    assert _resolve_secret_map(None) is None
    assert _resolve_secret_map({}) == {}
