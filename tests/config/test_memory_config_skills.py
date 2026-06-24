from durin.config.schema import AutoAbsorbConfig, MemoryConfig


def test_auto_absorb_enabled_by_default():
    assert AutoAbsorbConfig().enabled is True


def test_index_skills_defaults_true():
    assert MemoryConfig().index_skills is True


def test_index_skills_can_be_disabled():
    assert MemoryConfig(index_skills=False).index_skills is False


def test_index_skills_loads_from_camelcase_alias():
    # Base uses alias_generator=to_camel + populate_by_name, so the field
    # round-trips through memory.json under the camelCase `indexSkills` key.
    assert MemoryConfig.model_validate({"indexSkills": False}).index_skills is False
