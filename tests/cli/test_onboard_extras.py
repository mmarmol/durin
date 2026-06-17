import types

import durin.cli.onboard_wizard as ow


def _config(*, slack=False, discord=False, provider="anthropic", memory=False):
    return types.SimpleNamespace(
        memory=types.SimpleNamespace(
            enabled=memory,
            search=types.SimpleNamespace(
                cross_encoder=types.SimpleNamespace(enabled=False)
            ),
        ),
        channels=types.SimpleNamespace(
            slack=types.SimpleNamespace(enabled=slack),
            discord=types.SimpleNamespace(enabled=discord),
        ),
        agents=types.SimpleNamespace(
            defaults=types.SimpleNamespace(provider=provider)
        ),
    )


def test_reconcile_adds_channel_and_oauth_extras():
    extras: set[str] = set()
    ow._reconcile_extras_from_config(
        _config(slack=True, discord=True, provider="openai_codex"), extras
    )
    assert {"slack", "discord", "oauth"}.issubset(extras)
    assert "memory" not in extras  # disabled in this config


def test_reconcile_no_extras_for_plain_config():
    extras: set[str] = set()
    ow._reconcile_extras_from_config(_config(provider="anthropic"), extras)
    assert extras == set()


def test_reconcile_adds_skill_yara_by_default():
    # yara.enabled defaults True -> the [skill-yara] extra is installed by onboard.
    from durin.config.schema import Config

    extras: set[str] = set()
    ow._reconcile_extras_from_config(Config(), extras)
    assert "skill-yara" in extras


def test_reconcile_skips_skill_yara_when_disabled():
    from durin.config.schema import Config

    cfg = Config.model_validate({"skills": {"security": {"yara": {"enabled": False}}}})
    extras: set[str] = set()
    ow._reconcile_extras_from_config(cfg, extras)
    assert "skill-yara" not in extras
