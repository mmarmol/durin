from durin.config.schema import Config, PersonaConfig


def test_personas_roundtrip_through_json():
    cfg = Config(personas={"researcher": PersonaConfig(soul="researcher", model="anthropic claude-opus-4-5")})
    data = cfg.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    again = Config.model_validate(data)
    assert again.personas["researcher"].soul == "researcher"
    assert again.personas["researcher"].model == "anthropic claude-opus-4-5"


def test_resolve_persona_user_then_builtin_then_none():
    cfg = Config(personas={"mine": PersonaConfig(soul="default")})
    cfg.agents.defaults.persona = "mine"
    assert cfg.resolve_persona().soul == "default"            # global default
    assert cfg.resolve_persona("mine").soul == "default"      # explicit user
    assert cfg.resolve_persona("researcher") is not None      # built-in
    assert cfg.resolve_persona("does-not-exist") is None
    assert cfg.resolve_persona(None) is not None              # falls to agents.defaults.persona


def test_resolve_persona_none_when_no_default():
    cfg = Config()
    assert cfg.resolve_persona() is None


def test_persona_names_merges_user_and_builtin():
    cfg = Config(personas={"mine": PersonaConfig()})
    names = cfg.persona_names()
    assert "mine" in names and "researcher" in names
