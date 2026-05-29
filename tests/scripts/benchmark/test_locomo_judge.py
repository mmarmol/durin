"""Tests for the LoCoMo judge retry/jitter behaviour (audit H2, 2026-05-29).

Previous behaviour (`max_retries=2`, no backoff, no temperature
variance) made the judge fragile under transient upstream outages:
during the 2026-05-29 bench, z.ai returned empty strings for the
judge call across all 3 attempts in <5 seconds for 4/5 QAs, marking
real passes as `judge_error_possible`.

H2 hardens the loop:
- max_retries default bumped to 4 (5 attempts total).
- Exponential backoff between attempts (1, 2, 4, 8 seconds).
- Temperature jitter on retries (0.0, 0.2, 0.4, 0.6, 0.8) — when an
  upstream model wedges on an empty completion at temp=0, varying
  the temperature gives the next attempt a chance to break out.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_JUDGE_PATH = _REPO_ROOT / "scripts" / "benchmark" / "locomo_judge.py"


def _load_judge_module():
    import sys
    name = "scripts_benchmark_locomo_judge_under_test"
    spec = importlib.util.spec_from_file_location(name, _JUDGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass needs it discoverable during exec
    spec.loader.exec_module(mod)
    return mod


_judge_mod = _load_judge_module()
JudgeError = _judge_mod.JudgeError
judge_answer = _judge_mod.judge_answer


_GOOD_VERDICT = (
    "===SCORE===\n1\n===CONFIDENCE===\n95\n===REASONING===\n"
    "exact match\n===END===\n"
)


def _empty_invoke(prompt, *, model, **_kw):
    return ""


def test_empty_answer_short_circuits_without_llm_call() -> None:
    """Existing behaviour preserved: empty got bypasses the LLM."""
    calls: list[int] = []

    def _invoke(prompt, *, model, **_kw):
        calls.append(1)
        return _GOOD_VERDICT

    verdict = judge_answer(
        "q?", "expected", "", llm_invoke=_invoke,
    )
    assert verdict.score == 0.0
    assert not calls


def test_max_retries_default_is_four_meaning_five_attempts() -> None:
    """H2: default is now 4 retries (= 5 attempts) — not 2."""
    attempts: list[int] = []

    def _invoke(prompt, *, model, **_kw):
        attempts.append(1)
        return ""  # every attempt returns empty → parse failure

    with pytest.raises(JudgeError):
        judge_answer("q?", "e", "got", llm_invoke=_invoke)
    assert len(attempts) == 5, (
        f"expected 5 attempts, got {len(attempts)} — judge retry budget regressed"
    )


def test_succeeds_after_transient_empty_responses(monkeypatch) -> None:
    """A few empty responses early should not doom the judge."""
    # Skip the real sleep so the test is fast.
    sleeps: list[float] = []
    monkeypatch.setattr(
        _judge_mod.time, "sleep",
        lambda s: sleeps.append(s),
    )

    seq = iter(["", "", _GOOD_VERDICT])

    def _invoke(prompt, *, model, **_kw):
        return next(seq)

    verdict = judge_answer("q?", "e", "got", llm_invoke=_invoke)
    assert verdict.score == 1.0
    # H8 (2026-05-29): backoff base was bumped from 1s to 4s after the
    # bench-100 run hit z.ai outage windows that exhausted the old
    # 1-2-4-8 schedule (15s worst case) before upstream recovered.
    # New schedule: 4, 8, 16, 32 — 60s worst case.
    assert sleeps == [4.0, 8.0]


def test_exponential_backoff_schedule(monkeypatch) -> None:
    """Backoff doubles each retry: 4, 8, 16, 32 seconds (H8)."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        _judge_mod.time, "sleep",
        lambda s: sleeps.append(s),
    )

    def _invoke(prompt, *, model, **_kw):
        return ""  # all 5 attempts fail

    with pytest.raises(JudgeError):
        judge_answer("q?", "e", "got", llm_invoke=_invoke)
    # 4 sleeps between 5 attempts.
    assert sleeps == [4.0, 8.0, 16.0, 32.0]


def test_temperature_jitter_increases_across_attempts(monkeypatch) -> None:
    """H2: each retry uses a higher temperature to break LLM wedge."""
    # Skip backoff sleeps.
    monkeypatch.setattr(
        _judge_mod.time, "sleep", lambda _: None,
    )

    temps: list[float | None] = []

    def _invoke(prompt, *, model, temperature=None, **_kw):
        temps.append(temperature)
        return ""

    with pytest.raises(JudgeError):
        judge_answer("q?", "e", "got", llm_invoke=_invoke)
    # 5 attempts; first at 0.0, then each step +0.2.
    assert temps == [0.0, 0.2, 0.4, 0.6, 0.8]


def test_tolerates_invoke_without_temperature_kwarg(monkeypatch) -> None:
    """Custom LLMInvoke impls that don't accept ``temperature`` must
    still work — judge degrades gracefully to a temp-less call."""
    monkeypatch.setattr(
        _judge_mod.time, "sleep", lambda _: None,
    )

    saw_temperature_kw: list[bool] = []

    def _invoke(prompt, *, model):  # no **kwargs — strict signature
        saw_temperature_kw.append(False)
        return _GOOD_VERDICT

    verdict = judge_answer("q?", "e", "got", llm_invoke=_invoke)
    assert verdict.score == 1.0
    assert saw_temperature_kw == [False]


def test_unwraps_llm_response_dataclass(monkeypatch) -> None:
    """Audit H2 (2026-05-29): production ``default_llm_invoke``
    returns ``LLMResponse(text=…)``, not a bare string. The judge
    must unwrap it; without this the 2026-05-29 bench reported 5/5
    QAs as ``judge_error_possible`` because every parse hit the
    ``isinstance(raw, str)`` guard."""
    monkeypatch.setattr(
        _judge_mod.time, "sleep", lambda _: None,
    )

    class _LLMResponse:
        def __init__(self, text: str) -> None:
            self.text = text
            self.prompt_tokens = 0
            self.completion_tokens = 0

    def _invoke(prompt, *, model, **_kw):
        return _LLMResponse(_GOOD_VERDICT)

    verdict = judge_answer("q?", "e", "got", llm_invoke=_invoke)
    assert verdict.score == 1.0


def test_explicit_max_retries_override_honoured(monkeypatch) -> None:
    """Callers can still pin max_retries=2 to reproduce v1 behaviour."""
    monkeypatch.setattr(
        _judge_mod.time, "sleep", lambda _: None,
    )
    attempts: list[int] = []

    def _invoke(prompt, *, model, **_kw):
        attempts.append(1)
        return ""

    with pytest.raises(JudgeError):
        judge_answer(
            "q?", "e", "got", llm_invoke=_invoke, max_retries=2,
        )
    assert len(attempts) == 3
