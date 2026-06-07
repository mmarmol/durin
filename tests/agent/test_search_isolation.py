import inspect

from durin.agent import skill_registry, skills_store


def test_search_registries_signature_unchanged():
    sig = inspect.signature(skill_registry.search_registries)
    assert list(sig.parameters) == ["query", "adapters", "allowlist", "limit"]


def test_skill_search_hit_fields_unchanged():
    fields = {f.name for f in skill_registry.SkillSearchHit.__dataclass_fields__.values()}
    assert fields == {"name", "ref", "registry", "description", "signals"}


def test_web_skill_search_signature_unchanged():
    sig = inspect.signature(skills_store.web_skill_search)
    assert list(sig.parameters) == ["workspace", "query", "limit"]
