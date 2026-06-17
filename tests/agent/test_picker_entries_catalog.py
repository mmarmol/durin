import durin.providers.provider_catalog as pc
from durin.agent.model_picker import picker_entries
from durin.config.schema import Config
from durin.providers.provider_catalog import ModelInfo


def test_picker_groups_from_catalog_with_caps(monkeypatch):
    monkeypatch.setattr(
        pc,
        "_load_index",
        lambda: {
            "zai_coding_plan": [
                ModelInfo(id="glm-5.2", max_input_tokens=1_000_000, supports_reasoning=True),
                ModelInfo(id="glm-5v-turbo", max_input_tokens=200_000, supports_vision=True),
            ]
        },
    )
    monkeypatch.setattr(
        "durin.agent.model_picker.configured_provider_names",
        lambda _c: {"zai_coding_plan"},
    )
    cfg = Config()
    cfg.agents.defaults.model = "glm-5.2"
    cfg.agents.defaults.provider = "zai_coding_plan"

    entries = picker_entries(cfg, presets={}, recent=[], active=None)
    cat = [e for e in entries if e.role == "catalog"]
    assert {e.name for e in cat} == {"glm-5.2", "glm-5v-turbo"}
    turbo = next(e for e in cat if e.name == "glm-5v-turbo")
    assert turbo.provider == "zai_coding_plan"
    assert turbo.supports_vision is True
    assert turbo.ref == "zai_coding_plan glm-5v-turbo"
    # default easy-pick row commits by name and carries caps
    default = next(e for e in entries if e.role == "default")
    assert default.provider == "zai_coding_plan"
    assert default.max_input_tokens == 1_000_000
    assert default.ref == "default"


def test_recent_only_surfaced_when_resolvable(monkeypatch):
    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"zai_coding_plan": [ModelInfo(id="glm-5.2")]},
    )
    monkeypatch.setattr(
        "durin.agent.model_picker.configured_provider_names",
        lambda _c: {"zai_coding_plan"},
    )
    cfg = Config()
    entries = picker_entries(
        cfg, presets={}, recent=["glm-5.2", "ghost-model"], active=None
    )
    recents = [e for e in entries if e.role == "recent"]
    # glm-5.2 resolves via the catalog; ghost-model does not → not guessed, dropped
    assert [e.name for e in recents] == ["glm-5.2"]
    assert recents[0].provider == "zai_coding_plan"
