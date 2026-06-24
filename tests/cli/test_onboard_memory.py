"""Onboarding question for the auto-absorb opt-in.

The text ships alongside the absorb-judge feature. Tests verify the
contract: the helper returns a bool reflecting the user's choice,
defaults to False (off), and the prompt body contains the expected anchors.
"""

from __future__ import annotations

from typing import Any

import pytest

from durin.cli.onboard_memory import (
    AUTO_ABSORB_QUESTION_TEXT,
    AUX_MODEL_QUESTION_TEXT,
    CROSS_ENCODER_QUESTION_TEXT,
    prompt_enable_auto_absorb,
    prompt_enable_cross_encoder,
    prompt_memory_aux_model,
)

# ---------------------------------------------------------------------------
# Static contract — question text exists and contains the spec anchors
# ---------------------------------------------------------------------------


def test_question_text_anchors() -> None:
    """The text the user sees must communicate: what auto-absorb does,
    why it's OFF by default, the defaults when enabled, and the y/N prompt."""
    text = AUTO_ABSORB_QUESTION_TEXT
    assert "auto" in text.lower() and "absorb" in text.lower()
    assert "OFF by default" in text
    # The three defaults that the user is implicitly accepting:
    assert "95" in text                # confidence threshold
    assert "quarantine" in text.lower()  # run-scoped quarantine
    assert "Dream consolidator model" in text or "dream model" in text.lower()
    # The y/N prompt — N (capital) signals default
    assert "[y/N]" in text


# ---------------------------------------------------------------------------
# Prompt behavior — yes / no answers
# ---------------------------------------------------------------------------


class _FakeQuestionary:
    """Minimal stand-in for the questionary import used inside the
    helper. Captures the rendered prompt text and returns a canned
    answer."""

    def __init__(self, answer) -> None:
        self.captured_message: str | None = None
        self._answer = answer
        self._default = None

    def confirm(self, message: str, default: bool = False) -> "_FakeQuestionary":
        self.captured_message = message
        self._default = default
        return self

    def select(self, message, choices=None, default=None) -> "_FakeQuestionary":
        self.captured_message = message
        self._default = default
        return self

    def text(self, message, default=None) -> "_FakeQuestionary":
        self.captured_message = message
        self._default = default
        return self

    def ask(self):
        return self._answer


def test_returns_true_when_user_says_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuestionary(answer=True)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    assert prompt_enable_auto_absorb(current=False) is True
    assert "auto-absorb" in (fake.captured_message or "").lower()


def test_returns_false_when_user_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuestionary(answer=False)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    assert prompt_enable_auto_absorb(current=False) is False


def test_default_is_current_value_when_already_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user has previously enabled the feature, the re-prompt
    default flips to True (preserve choice)."""
    fake = _FakeQuestionary(answer=True)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    prompt_enable_auto_absorb(current=True)
    assert fake._default is True


def test_default_is_false_when_currently_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuestionary(answer=False)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    prompt_enable_auto_absorb(current=False)
    assert fake._default is False


def test_cross_encoder_question_anchors() -> None:
    text = CROSS_ENCODER_QUESTION_TEXT
    assert "cross-encoder" in text.lower()
    # Cost figures the user weighs the toggle against.
    assert "300-800ms" in text
    assert "~600MB" in text
    # 2026-06-11: copy is now neutral (was a Yes-recommendation). The
    # LoCoMo A/B found no aggregate gain, so onboarding presents it as
    # an opt-in — N (capital) signals the default. Engineering rationale
    # and the model name are deliberately omitted (kept minimal).
    assert "Optional" in text
    assert "Off by default" in text
    assert "[y/N]" in text
    assert "Recommended" not in text


def test_cross_encoder_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeQuestionary(answer=True)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    assert prompt_enable_cross_encoder(current=False) is True


def test_cross_encoder_preselected_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh prompt pre-selects No (recommended=False, 2026-06-11)."""
    fake = _FakeQuestionary(answer=False)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    prompt_enable_cross_encoder(current=False)
    assert fake._default is False


def test_none_answer_returns_current_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Questionary returns None when the user aborts (Ctrl+C in the
    prompt). The helper must preserve the existing setting in that
    case rather than flip silently to False."""

    class _Cancelled:
        def confirm(self, *a: Any, **kw: Any) -> "_Cancelled":
            return self

        def ask(self) -> None:
            return None

    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: _Cancelled(),
    )
    assert prompt_enable_auto_absorb(current=True) is True
    assert prompt_enable_auto_absorb(current=False) is False


# Q6.1 "Enable memory? [Y/n]" prompt (prompt_enable_memory_subsystem +
# MEMORY_ENABLE_QUESTION_TEXT) was removed — the wizard enables vector memory
# via the "Enable vector memory" toggle (ON by default), never that prompt.


# ---------------------------------------------------------------------------
# Q6.4 — Aux model picker
# ---------------------------------------------------------------------------


def test_aux_model_text_anchors() -> None:
    text = AUX_MODEL_QUESTION_TEXT
    assert "Dream" in text
    assert "memory" in text.lower()


def test_aux_model_same_as_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeQuestionary(answer="same as agent")
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    result = prompt_memory_aux_model(agent_model="glm-5.1")
    assert result == "glm-5.1"


def test_aux_model_skip_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuestionary(answer="skip")
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    result = prompt_memory_aux_model(agent_model="glm-5.1")
    assert result is None


def test_aux_model_specify_uses_text_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When 'specify' is chosen, the text prompt fires and its value
    becomes the model id."""
    # Two-step interaction: first `select` returns "specify", then
    # `text` returns the typed value. The fake's `.ask()` reads
    # whatever the test set last.
    answers = iter(["specify", "claude-haiku-4-5"])
    fake = _FakeQuestionary(answer=None)

    def _ask():
        try:
            return next(answers)
        except StopIteration:
            return None

    fake.ask = _ask  # type: ignore[assignment]
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    result = prompt_memory_aux_model(agent_model="glm-5.1")
    assert result == "claude-haiku-4-5"
