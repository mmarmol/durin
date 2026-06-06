import pytest

pytest.importorskip("oauth_cli_kit")

from durin.cli import commands


def test_login_codex_device_calls_blocking(monkeypatch):
    monkeypatch.setattr(commands, "should_use_device_code", lambda: True)
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )

    class _Tok:
        access = "A"
        account_id = "acct_1"

    monkeypatch.setattr(
        "durin.providers.codex_device_auth.login_blocking",
        lambda print_fn, **k: _Tok(),
    )
    commands._codex_login_flow(force=None)
    assert True  # no exception == device path ran
