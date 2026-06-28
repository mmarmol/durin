from durin.config.schema import MemoryDreamConfig


def test_skill_suggestions_enabled_defaults_true():
    assert MemoryDreamConfig().skill_suggestions_enabled is True


def test_skill_suggestions_enabled_camel_alias():
    cfg = MemoryDreamConfig.model_validate({"skillSuggestionsEnabled": False})
    assert cfg.skill_suggestions_enabled is False
