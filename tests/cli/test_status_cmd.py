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


def test_memory_docs_count_matches_canonical_classes(
    config: Config, fake_home: Path
) -> None:
    """status must count the same canonical memory classes as doctor /
    `/memory list` — a raw recursive glob used to inflate the number with
    entities/ and archive/ files."""
    ws = Path(config.agents.defaults.workspace)
    (ws / "memory" / "stable").mkdir(parents=True)
    (ws / "memory" / "stable" / "a.md").write_text("x", encoding="utf-8")
    (ws / "memory" / "entities").mkdir(parents=True)
    (ws / "memory" / "entities" / "noise.md").write_text("x", encoding="utf-8")
    (ws / "memory" / "archive").mkdir(parents=True)
    (ws / "memory" / "archive" / "old.md").write_text("x", encoding="utf-8")

    data = _status_data(config, _config_path(fake_home), None)
    assert data["memory"]["docs"] == 1


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
