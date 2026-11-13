"""The hidden `durin memory embed-server` command names no parent by default."""

from __future__ import annotations

from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app

runner = CliRunner()


def _invoke(monkeypatch, args):
    calls: list[dict] = []
    import durin.memory.embed_server as es

    monkeypatch.setattr(es, "run_embed_server", lambda **kw: calls.append(kw))
    result = runner.invoke(memory_app, ["embed-server", *args])
    assert result.exit_code == 0, result.output
    return calls


def test_a_hand_started_server_names_no_parent(monkeypatch):
    assert _invoke(monkeypatch, []) == [
        {"port": 0, "model": None, "parent_pid": None}]


def test_a_named_parent_is_passed_through(monkeypatch):
    calls = _invoke(monkeypatch, ["--parent-pid", "4242"])
    assert calls[0]["parent_pid"] == 4242
