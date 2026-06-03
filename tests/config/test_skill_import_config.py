from durin.config.schema import Config


def test_default_allowlist_empty():
    assert Config().memory.skill_import.allowlist == []


def test_allowlist_camel_roundtrip():
    cfg = Config.model_validate({"memory": {"skillImport": {"allowlist": ["github:NousResearch/"]}}})
    assert cfg.memory.skill_import.allowlist == ["github:NousResearch/"]
