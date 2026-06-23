import subprocess
from pathlib import Path

from typer.testing import CliRunner

from durin.cli.commands import _prefetch_speech_models, app

runner = CliRunner()


def test_voice_warmup_reports_nothing_when_no_local_engine_present(tmp_path, monkeypatch):
    # Force "extra absent" so the command is hermetic — never triggers a real
    # ~260 MB model download in the test env, regardless of what's installed.
    monkeypatch.setattr("durin.extras._module_present", lambda m: False)
    cfg = tmp_path / "config.json"
    cfg.write_text('{"transcription": {"provider": "local"}, "tts": {"provider": "local"}}')
    res = runner.invoke(app, ["voice", "warmup", "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "Nothing to warm" in res.output


def test_prefetch_spawns_warmup_only_for_speech_extras(monkeypatch):
    calls: list[list[str]] = []

    class _R:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]) or _R())

    _prefetch_speech_models(["memory", "web"], Path("/tmp/c.json"))
    assert calls == []  # no speech extra → no subprocess spawned

    _prefetch_speech_models(["memory", "tts"], Path("/tmp/c.json"))
    assert len(calls) == 1
    # Spawned as `python -m durin voice warmup --config …` in a fresh interpreter.
    assert calls[0][1:5] == ["-m", "durin", "voice", "warmup"]
