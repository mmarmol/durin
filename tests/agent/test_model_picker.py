from durin.agent.model_picker import PickerEntry, picker_entries
from durin.config.schema import Config


def _a_model_of(provider: str, prefix: str = "") -> str:
    """An id the vendored catalog currently lists for *provider*.

    Read at test time instead of written down: the catalog is refreshed
    weekly from upstream, and a pinned id silently retires with the model it
    names — which is how these tests began failing on a data-only commit that
    swapped glm-5.1 for glm-5.2. Callers that care about the *shape* of an id
    (a keyword-matching prefix, say) pass one; what matters to every test here
    is that the id is real, not which release it happens to be.
    """
    from durin.providers.provider_catalog import provider_models

    ids = [m.id for m in provider_models(provider) if m.id.startswith(prefix)]
    assert ids, f"the vendored catalog lists no {prefix!r} model for {provider}"
    return ids[0]


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
        cfg, presets={"fast": ModelPresetConfig(model=_a_model_of("gemini"), provider="gemini")},
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
    model = _a_model_of("gemini")
    cfg = _cfg(monkeypatch, gemini="k")
    entries = picker_entries(cfg, presets={}, recent=[model], active=None)
    easy = [e for e in entries if e.group == "Easy pick"]
    assert any(e.name == model and e.role == "recent" for e in easy)


def test_recent_uses_configured_provider_not_keyword_guess(monkeypatch):
    # glm-* keyword-matches zhipu, but the user configured zai_coding_plan. A
    # recent must resolve to the configured provider that serves it, not the
    # keyword guess — otherwise its ref fails with "No API key for zhipu".
    glm = _a_model_of("zai_coding_plan", "glm-")
    cfg = _cfg(monkeypatch, zai_coding_plan="k")
    entries = picker_entries(cfg, presets={}, recent=[glm], active=None)
    rec = next(e for e in entries if e.role == "recent")
    assert rec.provider == "zai_coding_plan"
    assert rec.ref == f"zai_coding_plan {glm}"


def test_picker_entry_carries_provider():
    e = PickerEntry(name="m", provider="p", group="g", role="catalog", ref="p m")
    assert (e.name, e.provider, e.group, e.role, e.ref) == ("m", "p", "g", "catalog", "p m")
