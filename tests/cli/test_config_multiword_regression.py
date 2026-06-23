"""Regression: multi-word nested config keys must round-trip.

`durin config set X.y_z V` followed by `durin config get X.y_z` silently
failed for any key whose path had a multi-word (snake_case) segment — the
on-disk/serialized form is camelCase (alias) but `config get` dumped snake
and then camel-normalized the lookup key, so it never matched. Single-word
segments worked by accident (camel == snake). The voice settings (barge_in,
spoken_render.mode, vad_threshold) were the first cluster of user-facing
multi-word keys to expose it; the same fix covers all of durin.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from durin.cli.commands import app

runner = CliRunner()


def _isolated(cfg_path: Path):
    return (
        patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path),
        patch("durin.config.loader.get_config_path", return_value=cfg_path),
    )


def test_multiword_nested_key_roundtrips() -> None:
    """A voice key (multi-word segment) survives set -> get."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.json"
        cfg.write_text("{}")
        p1, p2 = _isolated(cfg)
        with p1, p2:
            set_res = runner.invoke(
                app, ["config", "set", "voice.spoken_render.mode", "aux_summary"]
            )
            assert set_res.exit_code == 0, set_res.output
            get_res = runner.invoke(app, ["config", "get", "voice.spoken_render.mode"])
            assert get_res.exit_code == 0, get_res.output
            assert "aux_summary" in get_res.output
            assert "No such key" not in get_res.output


def test_general_multiword_key_roundtrips() -> None:
    """A non-voice multi-word key too (proves the fix is durin-wide)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.json"
        cfg.write_text("{}")
        p1, p2 = _isolated(cfg)
        with p1, p2:
            assert (
                runner.invoke(
                    app, ["config", "set", "transcription.cache_transcripts", "false"]
                ).exit_code
                == 0
            )
            get_res = runner.invoke(
                app, ["config", "get", "transcription.cache_transcripts"]
            )
            assert get_res.exit_code == 0, get_res.output
            assert "No such key" not in get_res.output
            assert "false" in get_res.output.lower()


def test_config_api_serializes_snake_case() -> None:
    """The webui reads snake_case; the config API must serialize by_alias=False
    so multi-word nested keys are present under their snake names."""
    from durin.config.schema import Config

    d = Config().model_dump(mode="json", by_alias=False)
    assert "spoken_render" in d["voice"]
    assert "spokenRender" not in d["voice"]
    assert "cache_transcripts" in d["transcription"]


def test_legacy_camelcase_config_loads_and_resaves_snake(tmp_path) -> None:
    """Migration safety net: a legacy camelCase on-disk config still LOADS (via
    the kept pydantic input aliases) and is REWRITTEN snake_case on save — no
    data loss, automatic on-disk migration, no migration code needed."""
    import json

    from durin.config.loader import load_config, read_persisted_config, save_config

    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "voice": {"spokenRender": {"mode": "aux_summary"}, "bargeIn": False},
                "transcription": {"cacheTranscripts": False},
            }
        )
    )
    cfg = load_config(p)
    assert cfg.voice.spoken_render.mode == "aux_summary"
    assert cfg.voice.barge_in is False
    assert cfg.transcription.cache_transcripts is False
    save_config(cfg, p)
    raw = read_persisted_config(p)
    assert "spoken_render" in raw["voice"] and "spokenRender" not in raw["voice"]
    assert "barge_in" in raw["voice"]
    assert "cache_transcripts" in raw["transcription"]
