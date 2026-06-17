from durin.providers.selection import (
    configured_provider_names,
    infer_provider,
    matching_provider_names,
)


def test_infer_provider_keyword():
    assert infer_provider("gpt-5") == "openai"
    assert infer_provider("claude-opus-4-7") == "anthropic"
    assert infer_provider("totally-unknown") == "auto"


def test_matching_provider_names_in_registry_order():
    names = matching_provider_names("glm-5.1")
    assert "zhipu" in names  # keyword 'glm'


def test_configured_provider_names_uses_api_key(monkeypatch):
    from durin.config.schema import Config

    import durin.providers.codex_device_auth as _cda

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    monkeypatch.setattr(_cda, "codex_token_present", lambda: False)
    cfg = Config()
    cfg.providers.gemini.api_key = "k"
    got = configured_provider_names(cfg)
    assert "gemini" in got
    assert "anthropic" not in got


def test_configured_detects_codex_via_secret_store(monkeypatch):
    # openai_codex keeps its OAuth token in the secret store, not a file path —
    # configured detection must consult codex_token_present(), not the
    # file-only any_token_present(). Otherwise the codex catalog group is missing.
    from durin.config.schema import Config

    import durin.providers.codex_device_auth as _cda

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    monkeypatch.setattr(_cda, "codex_token_present", lambda: True)
    got = configured_provider_names(Config())
    assert "openai_codex" in got
