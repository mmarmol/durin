from durin.config.schema import Config


def test_defaults():
    cfg = Config()
    ht = cfg.memory.skills_hot_tier
    assert ht.enabled is True
    assert ht.recent == 15
    assert ht.frequent == 30
    assert ht.frequent_window_hours == 168.0
    assert ht.recent_window_hours == 24.0


def test_camel_alias_roundtrip():
    cfg = Config.model_validate(
        {"memory": {"skillsHotTier": {"enabled": False, "recent": 5, "frequent": 8}}}
    )
    ht = cfg.memory.skills_hot_tier
    assert ht.enabled is False and ht.recent == 5 and ht.frequent == 8
    assert ht.frequent_window_hours == 168.0
