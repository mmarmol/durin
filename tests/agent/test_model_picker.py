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
    e = PickerEntry(name="m", provider="p", group="g", role="catalog")
    assert (e.name, e.provider, e.group, e.role) == ("m", "p", "g", "catalog")
