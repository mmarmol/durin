"""`_all_provider_rows` must detect openai_codex as configured via the secret
store (its OAuth token lives there, not on a file path), so the onboarding
wizard does not show codex as unconfigured."""


def test_all_provider_rows_detects_codex_via_secret_store(monkeypatch):
    import durin.providers.codex_device_auth as _cda
    from durin.cli.onboard_wizard import _all_provider_rows
    from durin.config.schema import Config

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    monkeypatch.setattr(_cda, "codex_token_present", lambda: True)

    rows = _all_provider_rows(Config())
    codex = next((r for r in rows if r[0] == "openai_codex"), None)
    assert codex is not None, "openai_codex row missing"
    assert codex[2] is True  # (name, label, configured, is_default)


def test_all_provider_rows_codex_unconfigured_without_token(monkeypatch):
    import durin.providers.codex_device_auth as _cda
    from durin.cli.onboard_wizard import _all_provider_rows
    from durin.config.schema import Config

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    monkeypatch.setattr(_cda, "codex_token_present", lambda: False)

    rows = _all_provider_rows(Config())
    codex = next((r for r in rows if r[0] == "openai_codex"), None)
    assert codex is not None
    assert codex[2] is False
