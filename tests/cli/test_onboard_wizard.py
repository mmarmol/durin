"""Tests for the task-oriented onboarding wizard.

Drives ``run_wizard`` with a mock that mimics ``questionary``'s API:
each call (``select``, ``text``, ``password``, ``confirm``) returns an
object with an ``.ask()`` method that yields the next pre-scripted
answer.
"""

from __future__ import annotations

from typing import Any

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
    """A ``questionary`` lookalike that returns scripted answers.

    Each prompt-builder method (``select``, ``text``, etc.) records the
    fact that it was called and returns a stub whose ``.ask()`` pops
    the next scripted answer off the queue.
    """

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

        # A `confirm` prompt expects a bool. If the next scripted answer
        # isn't a bool, it was meant for a later prompt — so this
        # confirm wasn't scripted: treat it as declined (False) and DON'T
        # consume the answer. This keeps tests robust when new optional
        # confirm prompts are inserted into the flow.
        if kind == "confirm":
            if not self._answers or not isinstance(self._answers[0], bool):
                class _DeclinedStub:
                    def ask(_self):  # noqa: ANN001
                        return False
                return _DeclinedStub()

        nxt = self._next_answer()

        # For `select`, choice labels carry dynamic state text (e.g.
        # "Z.AI ...  — not set"). Tests script a substring; resolve it
        # to the real choice so they survive label-format changes.
        if kind == "select" and isinstance(nxt, str):
            choices = kwargs.get("choices")
            if not choices and len(args) >= 2:
                choices = args[1]
            if choices:
                exact = [c for c in choices if c == nxt]
                if not exact:
                    # Choice rows are `f"{label:<26} {tag}"` — the label
                    # is everything before the run of padding spaces.
                    # Match `nxt` against that stripped label exactly
                    # first (unambiguous: "OpenAI" ≠ "OpenAI Codex");
                    # then prefix; then a unique substring.
                    def _label_of(c: str) -> str:
                        return c.split("  ")[0].strip()

                    by_label = [
                        c for c in choices
                        if isinstance(c, str) and _label_of(c) == nxt
                    ]
                    if len(by_label) == 1:
                        nxt = by_label[0]
                    else:
                        pref = [c for c in choices if isinstance(c, str) and c.startswith(nxt)]
                        if len(pref) == 1:
                            nxt = pref[0]
                        else:
                            subs = [c for c in choices if isinstance(c, str) and nxt in c]
                            if len(subs) == 1:
                                nxt = subs[0]

        class _Stub:
            def ask(_self):  # noqa: ANN001
                return nxt

        return _Stub()

    # Mimic questionary's public surface.
    def select(self, *args, **kwargs):
        return self._stub("select", args, kwargs)

    def text(self, *args, **kwargs):
        return self._stub("text", args, kwargs)

    def password(self, *args, **kwargs):
        return self._stub("password", args, kwargs)

    def confirm(self, *args, **kwargs):
        return self._stub("confirm", args, kwargs)


def test_provider_choices_have_recommended_first() -> None:
    """The first option should be the durin-recommended provider so
    a user pressing Enter without thinking lands on something sane."""
    label, name, model = PROVIDER_CHOICES[0]
    assert "recommended" in label.lower()
    assert name and model


def test_wizard_returns_cancelled_when_provider_selection_is_aborted() -> None:
    q = _ScriptedQuestionary([None])  # user pressed Ctrl+C on provider pick
    result = run_wizard(Config(), q=q)
    assert isinstance(result, WizardResult)
    assert result.cancelled is True


def test_wizard_minimal_happy_path_sets_provider_and_model() -> None:
    """User picks Z.AI, types a key, accepts the suggested model, skips
    everything else, and the resulting Config carries the right fields."""
    answers = [
        # Stage 1: required.
        "Zhipu AI",   # select provider
        "sk-zhipu-test",                     # password api_key
        "glm-5.1",                           # select default model
        # Stage 2: skip everything.
        "→ Continue (finish onboarding)",
        # Stage 3: workspace — accept the default.
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    cfg = result.config
    assert cfg.agents.defaults.provider == "zhipu"
    assert cfg.agents.defaults.model == "glm-5.1"
    assert cfg.providers.zhipu.api_key == "sk-zhipu-test"
    assert result.extras_to_install == []
    # Summary should mention the provider line at least.
    assert any("zhipu" in line.lower() for line in result.summary_lines)


def test_wizard_memory_feature_records_extra_and_writes_embedding() -> None:
    """Configuring memory toggles the [memory] extra and sets the model."""
    answers = [
        # Stage 1.
        "Zhipu AI", "sk-x", "glm-5.1",
        # Stage 2: enter memory submenu.
        "[ ] 📁 Vector memory",
        True,                                # confirm enable
        "multilingual-e5-small (default, 130MB, 100+ languages)",
        # Back at menu: continue.
        "→ Continue (finish onboarding)",
        # Stage 3.
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    assert "memory" in result.extras_to_install
    assert result.config.memory.embedding.model == "intfloat/multilingual-e5-small"
    assert any("memory" in s.lower() for s in result.summary_lines)


def test_wizard_web_feature_records_extra_and_default_backend() -> None:
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "🔍 Web search + fetch",   # enter web feature
        True,                       # confirm enable
        "DuckDuckGo",               # search backend (no key needed)
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert "web" in result.extras_to_install
    assert result.config.tools.web.search.provider == "duckduckgo"


def test_wizard_web_feature_brave_backend_prompts_for_key() -> None:
    """Picking Brave (needs a key) must prompt for and store the API key."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "🔍 Web search + fetch",
        True,
        "Brave Search",             # needs API key
        "brave-key-xyz",            # password prompt
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.tools.web.search.provider == "brave"
    assert result.config.tools.web.search.api_key == "brave-key-xyz"


def test_wizard_vision_feature_sets_aux_model() -> None:
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "[ ] 👁️  Vision (interpret_image)", True,
        "glm-5v-turbo",                     # vision model id
        "zhipu",                             # provider
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    aux = result.config.agents.aux_models
    assert aux is not None and aux.vision is not None
    assert aux.vision.model == "glm-5v-turbo"
    assert aux.vision.provider == "zhipu"


def test_wizard_skip_everything_path() -> None:
    """`× Skip everything` exits stage 2 immediately with nothing extra."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "× Skip everything (use defaults)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert not result.cancelled
    assert result.extras_to_install == []


def test_wizard_custom_provider_path_with_typed_model() -> None:
    """Custom OpenAI-compat provider has no model suggestions — user types one."""
    answers = [
        "Custom",
        "sk-custom",
        "my-local-model",                    # typed model name
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.agents.defaults.provider == "custom"
    assert result.config.agents.defaults.model == "my-local-model"
    assert result.config.providers.custom.api_key == "sk-custom"


def test_wizard_empty_api_key_is_accepted_for_local_providers() -> None:
    """A blank key shouldn't crash — useful for local OpenAI-compat endpoints."""
    answers = [
        "Custom",
        "",                                  # no key
        "ollama-llama3",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    # api_key stays None (left unset).
    assert result.config.providers.custom.api_key is None


def test_wizard_workspace_override_is_persisted() -> None:
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "→ Continue (finish onboarding)",
        "/tmp/my-custom-workspace",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.agents.defaults.workspace == "/tmp/my-custom-workspace"


def _configured_config() -> Config:
    """A Config that already has a working provider + model set up."""
    c = Config()
    c.agents.defaults.provider = "zhipu"
    c.agents.defaults.model = "glm-5.1"
    c.providers.zhipu.api_key = "sk-existing"
    return c


def test_wizard_keeps_existing_provider_without_reconfiguring() -> None:
    """Re-running onboard on a configured setup → 'Keep it and continue'
    must NOT force the provider/key/model questions again."""
    answers = [
        "Keep it and continue",            # stage 1: keep existing
        "→ Continue (finish onboarding)",  # stage 2: skip optionals
        "",                                 # stage 3: workspace default
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.cancelled is False
    # Untouched.
    assert result.config.agents.defaults.model == "glm-5.1"
    assert result.config.providers.zhipu.api_key == "sk-existing"
    # No password / provider-select prompt was issued.
    kinds = [k for k, _a, _kw in q.calls]
    assert "password" not in kinds


def test_wizard_test_model_then_continue() -> None:
    """'Test the model first' runs check_model_ping, then loops back to the
    keep/test/change menu; the user can then keep & continue."""
    from unittest.mock import patch

    from durin.cli.doctor import CheckResult

    answers = [
        "Test the model first",            # stage 1: test
        "Keep it and continue",            # stage 1: then keep
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    fake = CheckResult("model ping", "ok", "glm-5.1 responded.", category="providers")
    with patch("durin.cli.doctor.check_model_ping", return_value=fake) as mock_ping:
        result = run_wizard(_configured_config(), q=q)
    assert result.cancelled is False
    mock_ping.assert_called_once()
    assert result.config.agents.defaults.model == "glm-5.1"


def test_wizard_change_provider_falls_through_to_pick_flow() -> None:
    """'Change provider / model' drops into the full pick flow."""
    answers = [
        "Change provider / model",
        "OpenAI", "sk-new", "gpt-5",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(_configured_config(), q=q)
    assert result.config.agents.defaults.provider == "openai"
    assert result.config.agents.defaults.model == "gpt-5"
    assert result.config.providers.openai.api_key == "sk-new"


def test_detect_configured_features_marks_existing_setup() -> None:
    from durin.cli.onboard_wizard import _detect_configured_features
    from durin.config.schema import AuxModelConfig, AuxModelsConfig

    c = Config()
    c.agents.aux_models = AuxModelsConfig(
        vision=AuxModelConfig(model="glm-5v-turbo", provider="zhipu"),
    )
    c.tools.web.enable = True
    found = _detect_configured_features(c)
    assert "vision" in found
    assert "web" in found
    assert "audio" not in found


def test_wizard_summary_is_human_readable() -> None:
    """Summary should mention provider + model in plain language."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    summary = "\n".join(result.summary_lines).lower()
    assert "zhipu" in summary or "z.ai" in summary
    assert "glm-5.1" in summary


def test_wizard_dashboard_feature_enables_webui_and_daemon() -> None:
    """The Dashboard feature (decoupled from channels) flips
    gateway.webui_enabled + gateway.daemon."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "🖥️  Web dashboard",   # enter dashboard feature
        True,                   # enable webui? yes
        True,                   # daemon mode? yes
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    assert result.config.gateway.webui_enabled is True
    assert result.config.gateway.daemon is True


def test_apply_model_capabilities_sets_context_window() -> None:
    """Picking glm-5.1 should pull its real ~203K context window from the
    capability snapshot, replacing the wrong 65536 schema default."""
    from durin.cli.onboard_wizard import apply_model_capabilities

    config = Config()
    assert config.agents.defaults.context_window_tokens == 65_536  # schema default
    changed = apply_model_capabilities(config, "glm-5.1", "zhipu")
    # glm-5.1's snapshot window is far larger than 65536.
    assert config.agents.defaults.context_window_tokens > 65_536
    assert any("context window" in line for line in changed)


def test_apply_model_capabilities_noop_for_unknown_model() -> None:
    """An unknown model has no snapshot window → nothing changes."""
    from durin.cli.onboard_wizard import apply_model_capabilities

    config = Config()
    before = config.agents.defaults.context_window_tokens
    changed = apply_model_capabilities(config, "totally-made-up-model-zzz", "custom")
    # Heuristic fallback may or may not produce a window; if it doesn't,
    # the value is unchanged and `changed` is empty. Either way it
    # must not crash and must not set a nonsense value.
    assert isinstance(changed, list)
    assert config.agents.defaults.context_window_tokens > 0
    if not changed:
        assert config.agents.defaults.context_window_tokens == before


def test_wizard_applies_capabilities_on_model_pick() -> None:
    """The full wizard flow should sync the context window automatically."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.agents.defaults.context_window_tokens > 65_536


def test_wizard_dashboard_feature_can_disable_webui() -> None:
    """Saying NO to the webui in the Dashboard feature sets it False."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "🖥️  Web dashboard",
        False,   # enable webui? no
        False,   # daemon? no
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.config.gateway.webui_enabled is False
    assert result.config.gateway.daemon is False


def test_wizard_channels_feature_lists_and_enables_a_channel() -> None:
    """The Channels feature lists real channels; enabling one flips its
    `enabled` flag on in the config."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "💬 Chat channels",   # enter channels feature
        "Telegram",            # pick a channel to enable
        "tg-bot-token",        # its credential prompt
        "→ Done with channels",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    extra = result.config.channels.__pydantic_extra__ or {}
    tg = extra.get("telegram")
    assert tg is not None and tg.get("enabled") is True


def test_wizard_result_carries_availability_matrix() -> None:
    """A finished wizard returns a capability matrix — chat model is
    always ✓; un-configured optional features show ✗ with a fix hint."""
    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    result = run_wizard(Config(), q=q)
    assert result.availability_lines
    assert result.availability_lines[0].startswith("✓ Chat model")
    # A feature nobody configured should be ✗ and point at its section.
    memory_line = next(
        ln for ln in result.availability_lines if "memory" in ln.lower()
    )
    assert memory_line.startswith("✗")
    assert "durin onboard memory" in memory_line


def test_wizard_tests_model_after_a_new_pick() -> None:
    """After picking a brand-new model the wizard offers a round-trip
    test; confirming runs check_model_ping against the in-memory config."""
    from unittest.mock import patch

    from durin.cli.doctor import CheckResult

    answers = [
        "Zhipu AI", "sk-x", "glm-5.1",
        True,                              # "Test this model now?" → yes
        "→ Continue (finish onboarding)",
        "",
    ]
    q = _ScriptedQuestionary(answers)
    fake = CheckResult("model ping", "ok", "glm-5.1 responded.", category="providers")
    with patch("durin.cli.doctor.check_model_ping", return_value=fake) as mock_ping:
        result = run_wizard(Config(), q=q)
    assert result.cancelled is False
    mock_ping.assert_called_once()
    # The ping must target the in-memory config the wizard just built.
    assert mock_ping.call_args.kwargs.get("cfg") is not None


def test_run_section_rejects_unknown_section() -> None:
    q = _ScriptedQuestionary([])
    with pytest.raises(ValueError, match="Unknown section"):
        run_section(Config(), "bogus", q=q)


def test_run_section_model_reconfigures_provider_only() -> None:
    """`durin onboard model` re-runs just provider/key/model — no
    optional-feature menu, no workspace prompt."""
    answers = ["OpenAI", "sk-new", "gpt-5"]
    q = _ScriptedQuestionary(answers)
    result = run_section(Config(), "model", q=q)
    assert result.cancelled is False
    assert result.config.agents.defaults.provider == "openai"
    assert result.config.agents.defaults.model == "gpt-5"
    # No workspace text prompt was issued (that stage belongs to the
    # full wizard, not a section run).
    text_prompts = [a for k, a, _kw in q.calls if k == "text"]
    assert all("Workspace" not in str(a) for a in text_prompts)


def test_run_section_web_enables_search_and_records_extra() -> None:
    """`durin onboard web` runs only the web sub-wizard."""
    answers = [True, "DuckDuckGo — no API key, default"]
    q = _ScriptedQuestionary(answers)
    result = run_section(Config(), "web", q=q)
    assert result.cancelled is False
    assert result.config.tools.web.enable is True
    assert "web" in result.extras_to_install


def test_sections_constant_covers_every_optional_feature() -> None:
    """Every optional-feature key must be reachable as a section so the
    `✗ … durin onboard <section>` hints in the matrix are never dead."""
    from durin.cli.onboard_wizard import _OPTIONAL_FEATURES

    section_keys = {s.replace("-", "_") for s in SECTIONS}
    for key, _label, _desc in _OPTIONAL_FEATURES:
        assert key in section_keys, f"feature '{key}' has no onboard section"
