"""Tests for the task-oriented onboarding wizard.

The wizard is a state machine: a forced direct setup (provider → key →
model) when no provider exists, then a re-entrant hub whose rows open
submenus that always return to the hub.

These tests drive it with ``_ScriptedQuestionary`` — a mock mimicking
``questionary``'s API where each prompt-builder call returns a stub
whose ``.ask()`` pops the next pre-scripted answer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from durin.cli.onboard_wizard import (
    PROVIDER_CHOICES,
    SECTIONS,
    WizardResult,
    run_section,
    run_wizard,
)
from durin.config.schema import Config


class _ScriptedQuestionary:
    """A ``questionary`` lookalike that returns scripted answers."""

    def __init__(self, answers: list[Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _next_answer(self) -> Any:
        if not self._answers:
            raise AssertionError(
                "questionary mock ran out of scripted answers — "
                "add more or trim the wizard flow"
            )
        return self._answers.pop(0)

    def _stub(self, kind: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
        self.calls.append((kind, args, kwargs))

        # A `confirm` expects a bool. If the next scripted answer isn't a
        # bool it was meant for a later prompt — treat this confirm as
        # declined and DON'T consume the answer. Keeps tests robust when
        # optional confirm prompts are inserted into the flow.
        if kind == "confirm":
            if not self._answers or not isinstance(self._answers[0], bool):
                class _DeclinedStub:
                    def ask(self):
                        return False
                return _DeclinedStub()

        nxt = self._next_answer()

        # `select` choice labels carry dynamic state text. Tests script a
        # plain label; resolve it to the real choice so they survive
        # label-format changes.
        if kind == "select" and isinstance(nxt, str):
            choices = kwargs.get("choices")
            if not choices and len(args) >= 2:
                choices = args[1]
            if choices:
                exact = [c for c in choices if c == nxt]
                if not exact:
                    def _label_of(c: str) -> str:
                        return c.split("  ")[0].strip()

                    by_label = [
                        c for c in choices
                        if isinstance(c, str) and _label_of(c) == nxt
                    ]
                    if len(by_label) == 1:
                        nxt = by_label[0]
                    else:
                        pref = [
                            c for c in choices
                            if isinstance(c, str) and c.startswith(nxt)
                        ]
                        if len(pref) == 1:
                            nxt = pref[0]
                        else:
                            subs = [
                                c for c in choices
                                if isinstance(c, str) and nxt in c
                            ]
                            if len(subs) == 1:
                                nxt = subs[0]

        class _Stub:
            def ask(self):
                return nxt

        return _Stub()

    def select(self, *args, **kwargs):
        return self._stub("select", args, kwargs)

    def text(self, *args, **kwargs):
        return self._stub("text", args, kwargs)

    def password(self, *args, **kwargs):
        return self._stub("password", args, kwargs)

    def confirm(self, *args, **kwargs):
        return self._stub("confirm", args, kwargs)


def _configured_config() -> Config:
    """A Config that already has a working provider + model set up."""
    c = Config()
    c.agents.defaults.provider = "zhipu"
    c.agents.defaults.model = "glm-5.1"
    c.providers.zhipu.api_key = "sk-existing"
    return c


_FINISH = "✓ Finish onboarding"


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


def test_provider_choices_have_recommended_first() -> None:
    label, name, model = PROVIDER_CHOICES[0]
    assert "recommended" in label.lower()
    assert name and model


# ---------------------------------------------------------------------------
# Direct setup (forced when no provider configured)
# ---------------------------------------------------------------------------


def test_wizard_minimal_happy_path_sets_provider_and_model() -> None:
    """Fresh install: provider → key → model, then finish at the hub."""
    answers = ["Zhipu AI", "sk-zhipu-test", "glm-5.1", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    cfg = result.config
    assert cfg.agents.defaults.provider == "zhipu"
    assert cfg.agents.defaults.model == "glm-5.1"
    assert cfg.providers.zhipu.api_key == "sk-zhipu-test"
    assert any("zhipu" in line.lower() for line in result.summary_lines)


def test_wizard_cancelled_when_provider_selection_is_aborted() -> None:
    q = _ScriptedQuestionary([None])  # Ctrl+C on the provider pick
    result = run_wizard(Config(), q=q)
    assert isinstance(result, WizardResult)
    assert result.cancelled is True


def test_wizard_cancel_row_on_fresh_provider_list_cancels() -> None:
    """A fresh install has nothing to keep — the provider list's top row
    is an explicit cancel that ends the wizard."""
    q = _ScriptedQuestionary(["✗ Cancel onboarding"])
    result = run_wizard(Config(), q=q)
    assert result.cancelled is True


def test_wizard_model_picker_back_returns_to_provider_list() -> None:
    """'← Back' in the model picker bounces to the provider list."""
    answers = [
        "Zhipu AI", "sk-x",
        "← Back",                    # model picker → back to providers
        "OpenAI", "sk-y", "gpt-5",   # second time around
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    assert result.config.agents.defaults.provider == "openai"
    assert result.config.agents.defaults.model == "gpt-5"


def test_wizard_custom_provider_path_with_typed_model() -> None:
    """A provider with no model shortlist falls back to free-text entry."""
    answers = ["Custom", "", "my-local-model", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.agents.defaults.provider == "custom"
    assert result.config.agents.defaults.model == "my-local-model"


def test_wizard_tests_model_after_a_new_pick() -> None:
    """Confirming the post-pick test runs check_model_ping on the
    in-memory config."""
    from durin.cli.doctor import CheckResult

    answers = ["Zhipu AI", "sk-x", "glm-5.1", True, _FINISH]
    q = _ScriptedQuestionary(answers)
    fake = CheckResult("model ping", "ok", "glm-5.1 responded.", category="providers")
    with patch("durin.cli.doctor.check_model_ping", return_value=fake) as mock_ping:
        result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    mock_ping.assert_called_once()
    assert mock_ping.call_args.kwargs.get("cfg") is not None


def test_wizard_applies_capabilities_on_model_pick() -> None:
    """Picking glm-5.1 pulls its real ~203K context window."""
    answers = ["Zhipu AI", "sk-x", "glm-5.1", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.agents.defaults.context_window_tokens > 65_536


# ---------------------------------------------------------------------------
# Hub — re-entrant main menu
# ---------------------------------------------------------------------------


def test_wizard_keeps_existing_provider_and_finishes() -> None:
    """A configured install skips the direct setup and lands on the hub;
    finishing leaves the provider untouched."""
    q = _ScriptedQuestionary([_FINISH])
    result = run_wizard(_configured_config(), q=q)
    assert result.cancelled is False
    assert result.config.agents.defaults.provider == "zhipu"
    assert result.config.providers.zhipu.api_key == "sk-existing"


def test_hub_change_model_only_keeps_provider() -> None:
    """The model submenu changes the model without re-picking provider."""
    answers = [
        "Model & provider",
        "Change model only",
        "glm-4.6",
        "← Back",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.agents.defaults.provider == "zhipu"
    assert result.config.agents.defaults.model == "glm-4.6"


def test_hub_change_provider() -> None:
    """The model submenu's 'Change provider' runs the full pick flow."""
    answers = [
        "Model & provider",
        "Change provider",
        "OpenAI", "sk-new", "gpt-5",
        "← Back",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.agents.defaults.provider == "openai"
    assert result.config.agents.defaults.model == "gpt-5"
    assert result.config.providers.openai.api_key == "sk-new"


def test_hub_memory_records_extra_and_embedding() -> None:
    answers = [
        "Vector memory",
        True,
        "BGE-M3",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert "memory" in result.extras_to_install
    assert result.config.memory.embedding.model == "BAAI/bge-m3"


def test_hub_web_enables_search_and_records_extra() -> None:
    answers = [
        "Web search",
        True,
        "DuckDuckGo — no API key, default",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.tools.web.enable is True
    assert "web" in result.extras_to_install


def test_hub_dashboard_enables_webui_and_daemon() -> None:
    answers = ["Web dashboard", True, True, _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.gateway.webui_enabled is True
    assert result.config.gateway.daemon is True


def test_hub_channels_enable_then_disable() -> None:
    """The channels submenu is a two-way toggle."""
    enable = _ScriptedQuestionary([
        "Chat channels", "Telegram", "tg-token", "→ Done with channels", _FINISH,
    ])
    result = run_wizard(_configured_config(), q=enable)
    tg = (result.config.channels.__pydantic_extra__ or {}).get("telegram")
    assert tg is not None and tg.get("enabled") is True

    cfg = _configured_config()
    cfg.channels.__pydantic_extra__ = {"telegram": {"enabled": True, "token": "x"}}
    disable = _ScriptedQuestionary([
        "Chat channels", "Telegram", "→ Done with channels", _FINISH,
    ])
    result = run_wizard(cfg, q=disable)
    tg = (result.config.channels.__pydantic_extra__ or {}).get("telegram")
    assert tg is not None and tg.get("enabled") is False


def test_hub_channels_omits_the_dashboard_websocket() -> None:
    """The websocket channel is the dashboard's transport — not a row in
    the chat-channels picker."""
    answers = ["Chat channels", "→ Done with channels", _FINISH]
    q = _ScriptedQuestionary(answers)
    run_wizard(_configured_config(), q=q)
    channel_call = next(
        (a, kw) for k, a, kw in q.calls
        if k == "select" and "channel" in str(a).lower()
    )
    choices = channel_call[1].get("choices") or []
    assert choices
    assert not any("websocket" in str(c).lower() for c in choices)


def test_hub_workspace_updates_path() -> None:
    answers = ["Workspace", "/tmp/durin-ws", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.agents.defaults.workspace == "/tmp/durin-ws"


def test_hub_test_the_model_action() -> None:
    """The hub's 'Test the model' row pings without leaving the hub."""
    from durin.cli.doctor import CheckResult

    answers = ["Test the model", _FINISH]
    q = _ScriptedQuestionary(answers)
    fake = CheckResult("model ping", "ok", "ok.", category="providers")
    with patch("durin.cli.doctor.check_model_ping", return_value=fake) as mock_ping:
        result = run_wizard(_configured_config(), q=q)
    assert result.cancelled is False
    mock_ping.assert_called_once()


# ---------------------------------------------------------------------------
# Vision / audio submenu
# ---------------------------------------------------------------------------


def test_hub_vision_aux_model_picked_from_list() -> None:
    """The vision picker lists capable models from configured providers;
    zhipu offers glm-4.5v."""
    answers = [
        "Vision / audio",
        "Vision",
        "glm-4.5v",
        "← Back",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    aux = result.config.agents.aux_models
    assert aux is not None and aux.vision is not None
    assert aux.vision.model == "glm-4.5v"
    assert aux.vision.provider == "zhipu"


def test_hub_audio_aux_model_via_other() -> None:
    """Audio has no capable model among zhipu's catalog, so the user
    falls back to the free-text 'Other' entry."""
    answers = [
        "Vision / audio",
        "Audio",
        "Other (type a model id)",
        "whisper-1",
        "openai",
        "← Back",
        _FINISH,
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    aux = result.config.agents.aux_models
    assert aux is not None and aux.audio is not None
    assert aux.audio.model == "whisper-1"
    assert aux.audio.provider == "openai"


def test_capable_aux_models_filters_by_capability_and_provider() -> None:
    """`_capable_aux_models` only returns models with the modality, and
    only from configured providers."""
    from durin.cli.onboard_wizard import _capable_aux_models

    cfg = _configured_config()  # only zhipu configured
    vision = _capable_aux_models(cfg, "vision")
    assert ("glm-4.5v", "zhipu") in vision
    assert all(prov == "zhipu" for _model, prov in vision)


def test_caps_marks_reports_text_vision_audio() -> None:
    from durin.cli.onboard_wizard import _caps_marks

    marks = _caps_marks("glm-4.5v", "zhipu")
    assert "text✓" in marks
    assert "vision" in marks and "audio" in marks


# ---------------------------------------------------------------------------
# run_section — `durin onboard <section>`
# ---------------------------------------------------------------------------


def test_run_section_rejects_unknown_section() -> None:
    q = _ScriptedQuestionary([])
    with pytest.raises(ValueError, match="Unknown section"):
        run_section(Config(), "bogus", q=q)


def test_run_section_model_on_fresh_install_runs_direct_setup() -> None:
    answers = ["OpenAI", "sk-new", "gpt-5"]
    q = _ScriptedQuestionary(answers)
    result = run_section(Config(), "model", q=q)
    assert result.cancelled is False
    assert result.config.agents.defaults.provider == "openai"
    assert result.config.agents.defaults.model == "gpt-5"


def test_run_section_model_on_configured_opens_model_submenu() -> None:
    answers = ["Change model only", "glm-4.6", "← Back"]
    q = _ScriptedQuestionary(answers)
    result = run_section(_configured_config(), "model", q=q)
    assert result.config.agents.defaults.model == "glm-4.6"
    assert result.config.agents.defaults.provider == "zhipu"


def test_run_section_web_enables_search() -> None:
    answers = [True, "DuckDuckGo — no API key, default"]
    q = _ScriptedQuestionary(answers)
    result = run_section(Config(), "web", q=q)
    assert result.config.tools.web.enable is True
    assert "web" in result.extras_to_install


def test_run_section_vision_opens_vision_audio_submenu() -> None:
    answers = ["Vision", "glm-4.5v", "← Back"]
    q = _ScriptedQuestionary(answers)
    result = run_section(_configured_config(), "vision", q=q)
    aux = result.config.agents.aux_models
    assert aux is not None and aux.vision is not None
    assert aux.vision.model == "glm-4.5v"


def test_run_section_workspace_updates_path() -> None:
    answers = ["/tmp/ws-section"]
    q = _ScriptedQuestionary(answers)
    result = run_section(_configured_config(), "workspace", q=q)
    assert result.config.agents.defaults.workspace == "/tmp/ws-section"


# ---------------------------------------------------------------------------
# Capability matrix + detection
# ---------------------------------------------------------------------------


def test_wizard_result_carries_availability_matrix() -> None:
    answers = ["Zhipu AI", "sk-x", "glm-5.1", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.availability_lines
    assert result.availability_lines[0].startswith("✓ Chat model")
    memory_line = next(
        ln for ln in result.availability_lines if "memory" in ln.lower()
    )
    assert memory_line.startswith("✗")
    assert "durin onboard memory" in memory_line


def test_availability_matrix_shows_vision_audio_rows() -> None:
    answers = ["Zhipu AI", "sk-x", "glm-5.1", _FINISH]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    text = "\n".join(result.availability_lines).lower()
    assert "vision" in text
    assert "audio" in text


def test_detect_configured_features_marks_existing_setup() -> None:
    from durin.cli.onboard_wizard import _detect_configured_features
    from durin.config.schema import AuxModelConfig, AuxModelsConfig

    c = Config()
    c.agents.aux_models = AuxModelsConfig(
        vision=AuxModelConfig(model="glm-4.5v", provider="zhipu"),
    )
    c.tools.web.enable = True
    found = _detect_configured_features(c)
    assert "vision" in found
    assert "web" in found
    assert "audio" not in found


def test_apply_model_capabilities_sets_context_window() -> None:
    from durin.cli.onboard_wizard import apply_model_capabilities

    config = Config()
    assert config.agents.defaults.context_window_tokens == 65_536
    changed = apply_model_capabilities(config, "glm-5.1", "zhipu")
    assert config.agents.defaults.context_window_tokens > 65_536
    assert any("context window" in line for line in changed)


def test_apply_model_capabilities_noop_for_unknown_model() -> None:
    from durin.cli.onboard_wizard import apply_model_capabilities

    config = Config()
    before = config.agents.defaults.context_window_tokens
    changed = apply_model_capabilities(config, "totally-made-up-model-zzz", "custom")
    assert isinstance(changed, list)
    assert config.agents.defaults.context_window_tokens > 0
    if not changed:
        assert config.agents.defaults.context_window_tokens == before


def test_sections_are_all_handled() -> None:
    """Every name in SECTIONS must be a section run_section accepts."""
    for section in SECTIONS:
        q = _ScriptedQuestionary([None] * 12)
        # Should not raise ValueError — None answers just bail out early.
        run_section(_configured_config(), section, q=q)
