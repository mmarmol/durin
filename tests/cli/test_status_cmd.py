"""Tests for `durin status` — runtime-aware snapshot + --json."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import _status_data, _status_sections, app
from durin.config.schema import Config

runner = CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def config(fake_home: Path) -> Config:
    cfg = Config()
    cfg.agents.defaults.workspace = str(fake_home / ".durin" / "workspace")
    cfg.channels.__pydantic_extra__["telegram"] = {"enabled": True}
    return cfg


def _config_path(fake_home: Path) -> Path:
    return fake_home / ".durin" / "config.json"


def test_memory_line_separates_entities_docs_and_fragments(
    config: Config, fake_home: Path
) -> None:
    """The status Memory line breaks memory into its three real object kinds:
    entities (the knowledge graph), docs (the ingested Library), and fragments
    (the raw class-folder buffer). Historically it showed only the fragment
    count mislabelled as "docs", so the number disagreed with the webui — which
    shows the Library (references) and the entity graph."""
    ws = Path(config.agents.defaults.workspace)
    # 1 fragment (canonical class folder)
    (ws / "memory" / "stable").mkdir(parents=True)
    (ws / "memory" / "stable" / "a.md").write_text("x", encoding="utf-8")
    # 2 entities (knowledge graph, per type)
    (ws / "memory" / "entities" / "person").mkdir(parents=True)
    (ws / "memory" / "entities" / "person" / "m.md").write_text("x", encoding="utf-8")
    (ws / "memory" / "entities" / "topic").mkdir(parents=True)
    (ws / "memory" / "entities" / "topic" / "t.md").write_text("x", encoding="utf-8")
    # 1 Library doc (references shelf — what the webui shows)
    (ws / "memory" / "references").mkdir(parents=True)
    (ws / "memory" / "references" / "guide.md").write_text("x", encoding="utf-8")
    # archived entries count toward none of the three
    (ws / "memory" / "archive").mkdir(parents=True)
    (ws / "memory" / "archive" / "old.md").write_text("x", encoding="utf-8")

    data = _status_data(config, _config_path(fake_home), None)
    assert data["memory"]["fragments"] == 1
    assert data["memory"]["entities"] == 2
    assert data["memory"]["docs"] == 1

    rows = _status_sections(config, _config_path(fake_home), None)
    memory_line = dict(rows)["Memory"]
    assert "2 entities" in memory_line
    assert "1 docs" in memory_line
    assert "1 fragments" in memory_line


def test_channels_overlay_runtime_state(config: Config, fake_home: Path) -> None:
    runtime = {
        "url": "http://127.0.0.1:8765/",
        "version": "1.0",
        "uptime_s": 60.0,
        "channels": [{"name": "telegram", "enabled": True, "running": True}],
        "cron": None,
    }
    data = _status_data(config, _config_path(fake_home), runtime)
    assert data["channels"] == [
        {"name": "telegram", "enabled": True, "port": None, "running": True}
    ]


def test_channels_running_unknown_when_gateway_down(
    config: Config, fake_home: Path
) -> None:
    data = _status_data(config, _config_path(fake_home), None)
    assert data["channels"][0]["running"] is None


def test_gateway_stale_flag_on_version_mismatch(config: Config, fake_home: Path) -> None:
    runtime = {
        "url": "http://127.0.0.1:8765/",
        "version": "0.0.0-old",
        "uptime_s": 60.0,
        "channels": None,
        "cron": None,
    }
    data = _status_data(config, _config_path(fake_home), runtime)
    assert data["gateway"]["stale"] is True

    rows = _status_sections(config, _config_path(fake_home), runtime)
    joined = " ".join(v for _, v in rows)
    assert "gateway restart" in joined


def test_gateway_absent_when_nothing_runs(config: Config, fake_home: Path) -> None:
    data = _status_data(config, _config_path(fake_home), None)
    assert data["gateway"] is None


def test_cron_row_renders_job_count(config: Config, fake_home: Path) -> None:
    runtime = {
        "url": "u",
        "version": "1.0",
        "uptime_s": 1.0,
        "channels": None,
        "cron": {"enabled": True, "jobs": 2, "next_wake_at_ms": None},
    }
    rows = _status_sections(config, _config_path(fake_home), runtime)
    labels = [label for label, _ in rows]
    assert "Cron" in labels


def test_status_shows_dashboard_url_and_web_token(config: Config, fake_home: Path) -> None:
    config.gateway.webui_enabled = True
    config.gateway.public_url = "https://durin.tail9e5f5d.ts.net"
    extra = config.channels.__pydantic_extra__
    extra["websocket"] = {"enabled": True, "token": "secret-token"}

    rows = _status_sections(config, _config_path(fake_home), None)
    labels = dict(rows)
    assert labels["Dashboard"] == "https://durin.tail9e5f5d.ts.net"
    assert labels["Web token"] == "secret-token"


def test_status_web_token_is_the_effective_bootstrap_secret(config: Config, fake_home: Path) -> None:
    """The webui login gate accepts token_issue_secret WITH PRECEDENCE over the
    static token (websocket bootstrap: `token_issue_secret or token`). Status
    must show the value the login form actually accepts — showing the static
    token on a deployment that also sets token_issue_secret hands the operator
    a credential the gate rejects."""
    config.gateway.webui_enabled = True
    extra = config.channels.__pydantic_extra__
    extra["websocket"] = {
        "enabled": True,
        "token": "static-token",
        "token_issue_secret": "issue-secret-wins",
    }

    rows = _status_sections(config, _config_path(fake_home), None)
    assert dict(rows)["Web token"] == "issue-secret-wins"


def test_status_resolves_secret_ref_web_token(config: Config, fake_home: Path) -> None:
    """The websocket token may be stored as a ${secret:} reference (same as
    any channel credential) — status must resolve it for display the same
    way the channel resolves it at startup, not print the raw ${secret:...}
    placeholder."""
    import durin.security.secrets as _secrets
    from durin.security.secrets import SecretStore, get_secret_store, make_ref

    _secrets._STORE = None
    try:
        config.gateway.webui_enabled = True
        extra = config.channels.__pydantic_extra__
        extra["websocket"] = {"enabled": True, "token": make_ref("WS_TOKEN")}

        store = SecretStore().load()
        store.put("WS_TOKEN", value="resolved-secret-value", service="channel:websocket")
        store.save()
        get_secret_store(reload=True)

        rows = _status_sections(config, _config_path(fake_home), None)
        labels = dict(rows)
        assert labels["Web token"] == "resolved-secret-value"
    finally:
        _secrets._STORE = None


def test_status_shows_raw_value_when_secret_ref_unresolvable(
    config: Config, fake_home: Path
) -> None:
    """A dangling ${secret:} reference (secret deleted/never stored) must not
    crash `status` — it falls back to showing the raw stored value."""
    import durin.security.secrets as _secrets

    _secrets._STORE = None
    try:
        config.gateway.webui_enabled = True
        extra = config.channels.__pydantic_extra__
        extra["websocket"] = {"enabled": True, "token": "${secret:MISSING_TOKEN}"}

        rows = _status_sections(config, _config_path(fake_home), None)
        labels = dict(rows)
        assert labels["Web token"] == "${secret:MISSING_TOKEN}"
    finally:
        _secrets._STORE = None


def test_status_omits_token_when_unset(config: Config, fake_home: Path) -> None:
    config.gateway.webui_enabled = True
    extra = config.channels.__pydantic_extra__
    extra["websocket"] = {"enabled": True}

    rows = _status_sections(config, _config_path(fake_home), None)
    labels = dict(rows)
    assert "Web token" not in labels


def test_status_omits_dashboard_when_webui_disabled(config: Config, fake_home: Path) -> None:
    config.gateway.webui_enabled = False

    rows = _status_sections(config, _config_path(fake_home), None)
    labels = dict(rows)
    assert "Dashboard" not in labels
    assert "Web token" not in labels


def test_status_json_is_parseable(fake_home: Path, monkeypatch) -> None:
    cfg_path = fake_home / ".durin" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    data = Config().model_dump(mode="json", by_alias=True)
    data["agents"]["defaults"]["workspace"] = str(fake_home / ".durin" / "workspace")
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    # Keep the test off the network: no live gateway probe.
    monkeypatch.setattr(
        "durin.cli.commands._probe_gateway_runtime", lambda *a, **k: None
    )
    with patch("durin.config.loader.get_config_path", return_value=cfg_path):
        result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "model" in payload and "gateway" in payload and "config" in payload
