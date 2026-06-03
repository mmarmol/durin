from durin.config.schema import Config


def test_discovery_defaults():
    d = Config().skills.discovery
    assert d.search_limit == 10
    assert [r.kind for r in d.registries] == ["skills.sh"]
    assert d.registries[0].enabled is True


def test_registry_camel_roundtrip():
    c = Config.model_validate({"skills": {"discovery": {"registries": [
        {"name": "clawhub", "kind": "clawhub", "enabled": False, "apiKeySecret": "ch"}]}}})
    r = c.skills.discovery.registries[0]
    assert r.kind == "clawhub" and r.enabled is False and r.api_key_secret == "ch"
