"""Onboarding question for the auto-absorb opt-in (doc 06 §6.3).

The text ships in Phase 1 (alongside the absorb-judge feature). The
full wizard integration is Phase 6 — for now we verify the contract:
the helper returns a bool reflecting the user's choice, defaults to
False (off), and the prompt body matches the doc 06 wording verbatim.
"""

from __future__ import annotations

from typing import Any

import pytest

from durin.cli.onboard_memory import (
    AUTO_ABSORB_QUESTION_TEXT,
    CROSS_ENCODER_QUESTION_TEXT,
    prompt_enable_auto_absorb,
    prompt_enable_cross_encoder,
)


# ---------------------------------------------------------------------------
# Static contract — question text exists and contains the spec anchors
# ---------------------------------------------------------------------------


def test_question_text_anchors() -> None:
    """The text the user sees must communicate the four things
    doc 06 §6.3 lists: what auto-absorb does, why it's OFF by default,
    the defaults when enabled, and the y/N prompt."""
    text = AUTO_ABSORB_QUESTION_TEXT
    assert "auto" in text.lower() and "absorb" in text.lower()
    assert "OFF by default" in text
    # The three defaults that the user is implicitly accepting:
    assert "95" in text                # confidence threshold
    assert "24h" in text or "24 h" in text  # min_age_hours
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

    def __init__(self, answer: bool) -> None:
        self.captured_message: str | None = None
        self._answer = answer

    def confirm(self, message: str, default: bool = False) -> "_FakeQuestionary":
        self.captured_message = message
        self._default = default
        return self

    def ask(self) -> bool:
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
    assert "300-1500ms" in text
    assert "~1GB" in text
    assert "[y/N]" in text


def test_cross_encoder_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeQuestionary(answer=True)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    assert prompt_enable_cross_encoder(current=False) is True


def test_cross_encoder_default_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeQuestionary(answer=False)
    monkeypatch.setattr(
        "durin.cli.onboard_memory._get_questionary", lambda: fake,
    )
    prompt_enable_cross_encoder(current=True)
    assert fake._default is True


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
