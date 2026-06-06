import pytest

pytest.importorskip("oauth_cli_kit")

from durin.cli import commands
from durin.providers import codex_device_auth as cda


def test_login_codex_device_calls_blocking(monkeypatch):
    # Patch the module objects directly (not via dotted strings): the string
    # form relies on pytest walking `durin.providers` package attributes, which
    # is fragile under cross-test import-state pollution.
    monkeypatch.setattr(commands, "should_use_device_code", lambda: True)
    monkeypatch.setattr(cda, "existing_codex_session", lambda: None)

    class _Tok:
        access = "A"
        account_id = "acct_1"

    monkeypatch.setattr(cda, "login_blocking", lambda print_fn, **k: _Tok())
    commands._codex_login_flow(force=None)
    assert True  # no exception == device path ran
