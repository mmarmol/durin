from durin.config.schema import DEFAULT_SKILL_ALLOWLIST, Config


def test_skills_namespace_defaults():
    c = Config()
    # governance moved out of memory.skill_import → skills.security
    assert c.skills.security.allowlist == DEFAULT_SKILL_ALLOWLIST
    assert c.skills.security.llm_judge.trigger == "off"
    assert c.skills.security.max_files == 100
    # per-agent skill-context tuning moved onto agent defaults
    assert c.agents.defaults.skills_hot_tier.frequent == 30
    # memory keeps the index toggle; loses the relocated blocks
    assert c.memory.index_skills is True
    assert not hasattr(c.memory, "skill_import")
    assert not hasattr(c.memory, "skills_hot_tier")
