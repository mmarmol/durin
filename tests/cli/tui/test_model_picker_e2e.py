"""End-to-end: the real TUI picker, built from a real config against the real
vendored catalog, surfaces a configured provider's models with the exact ref."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList


class _HostApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Input()


async def test_picker_shows_real_catalog_for_configured_provider(monkeypatch):
    import durin.providers.codex_device_auth as _cda
    from durin.cli.tui.model_catalog import build_entries
    from durin.cli.tui.screens.model_picker import ModelPickerScreen
    from durin.config.schema import Config

    # Only zai_coding_plan is configured; keep codex undetected (network-free).
    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    monkeypatch.setattr(_cda, "codex_token_present", lambda: False)

    cfg = Config()
    cfg.providers.zai_coding_plan.api_key = "sk-test"

    entries = build_entries(config=cfg, presets={}, recent=[], active=None)
    refs = {e.ref for e in entries}
    # Real vendored provider_models.json → glm-5.2 under zai_coding_plan.
    assert "zai_coding_plan glm-5.2" in refs
    glm = next(e for e in entries if e.ref == "zai_coding_plan glm-5.2")
    assert glm.provider == "zai_coding_plan"

    screen = ModelPickerScreen(entries, active=None)
    async with _HostApp().run_test() as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()
        ol = screen.query_one("#model-picker-list", OptionList)
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        # The screen commits the provider-qualified ref, not the bare name.
        assert "zai_coding_plan glm-5.2" in ids
