from durin.agent.model_picker import PickerEntry, picker_entries
from durin.config.schema import Config


def _cfg(monkeypatch, **keys):
    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    c = Config()
    c.agents.defaults.model = "base-model"
    for name, val in keys.items():
        getattr(c.providers, name).api_key = val
    return c


def test_easy_pick_has_default_first(monkeypatch):
    cfg = _cfg(monkeypatch, gemini="k")
    entries = picker_entries(cfg, presets={}, recent=[], active=None)
    easy = [e for e in entries if e.group == "Easy pick"]
    assert easy and easy[0].role in ("default", "active")
    assert any(e.name == "base-model" for e in easy)
    # The default commits `/model default` (not a temp pair) to keep its params.
    default = next(e for e in easy if e.role == "default")
    assert default.ref == "default"


def test_refs_preset_by_name_catalog_by_pair(monkeypatch):
    from durin.config.schema import ModelPresetConfig

    cfg = _cfg(monkeypatch, gemini="k")
    entries = picker_entries(
        cfg, presets={"fast": ModelPresetConfig(model="gemini-2.5-flash", provider="gemini")},
        recent=[], active=None,
    )
    preset = next(e for e in entries if e.role == "preset")
    assert preset.ref == "fast"  # switch by preset name, preserves params
    catalog = next(e for e in entries if e.role == "catalog")
    assert catalog.ref == f"{catalog.provider} {catalog.name}"  # explicit pair


def test_catalog_grouped_by_configured_provider_only(monkeypatch):
    cfg = _cfg(monkeypatch, gemini="k")
    entries = picker_entries(cfg, presets={}, recent=[], active=None)
    groups = {e.group for e in entries if e.group != "Easy pick"}
    assert "gemini" in groups
    assert "anthropic" not in groups  # unconfigured


def test_recent_pinned_in_easy_pick(monkeypatch):
    cfg = _cfg(monkeypatch, gemini="k")
    entries = picker_entries(cfg, presets={}, recent=["gemini-2.5-flash"], active=None)
    easy = [e for e in entries if e.group == "Easy pick"]
    assert any(e.name == "gemini-2.5-flash" and e.role == "recent" for e in easy)


def test_picker_entry_carries_provider():
    e = PickerEntry(name="m", provider="p", group="g", role="catalog", ref="p m")
    assert (e.name, e.provider, e.group, e.role, e.ref) == ("m", "p", "g", "catalog", "p m")
