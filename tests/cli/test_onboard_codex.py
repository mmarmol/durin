from durin.cli import onboard_wizard as ow


def test_codex_in_provider_choices():
    names = [name for _, name, _ in ow.PROVIDER_CHOICES]
    assert "openai_codex" in names


def test_codex_default_models_present():
    assert "openai_codex" in ow.DEFAULT_MODELS
    assert "gpt-5.5" in ow.DEFAULT_MODELS["openai_codex"]
