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


def _load_run_module():
    """Load ``scripts/benchmark/locomo_run.py`` for H15 tests on
    ``_is_infra_fail``. Same importlib pattern as the judge loader."""
    import sys
    name = "scripts_benchmark_locomo_run_under_test"
    if name in sys.modules:
        return sys.modules[name]
    run_path = _REPO_ROOT / "scripts" / "benchmark" / "locomo_run.py"
    spec = importlib.util.spec_from_file_location(name, run_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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


def test_temperature_is_zero_across_all_attempts(monkeypatch) -> None:
    """H21 (2026-05-30): every attempt runs at temperature=0 for score
    reproducibility. The earlier H2 design varied temperature per
    attempt; that broke determinism (re-judge of the same trace
    could score differently) and the original "break LLM wedge"
    justification was speculation, not measurement."""
    monkeypatch.setattr(
        _judge_mod.time, "sleep", lambda _: None,
    )
    temps: list[float | None] = []

    def _invoke(prompt, *, model, temperature=None, **_kw):
        temps.append(temperature)
        return ""

    with pytest.raises(JudgeError):
        judge_answer("q?", "e", "got", llm_invoke=_invoke)
    assert temps == [0.0, 0.0, 0.0, 0.0, 0.0], (
        f"all 5 attempts must use temp=0; got {temps}"
    )


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


# ---------------------------------------------------------------------------
# Audit H15 (2026-05-29): iter-cap traces are NOT infra fails
# ---------------------------------------------------------------------------
#
# Pre-H15 a got starting with "I reached the maximum number of tool call
# iterations" routed to infra-retry. Bench-100 v8 analysis showed 4/5
# such traces were the agent falling back to grep/list_dir because
# memory_search didn't surface the answer — agent behaviour, not LLM
# transient. Retrying re-runs the same path against the same workspace
# and hits the same cap. H15 lets iter-cap fails count as real fails so
# the next durin change has signal to optimise.


def test_iter_cap_marker_not_treated_as_infra() -> None:
    _is_infra_fail = _load_run_module()._is_infra_fail
    from types import SimpleNamespace
    trace = SimpleNamespace(
        got=("I reached the maximum number of tool call iterations "
             "(8) without completing the task."),
        stop_reason="ok",
    )
    verdict = {"score": 0.0}
    assert _is_infra_fail(trace, verdict) is False, (
        "iter-cap fails are agent issues — they must NOT be queued for "
        "the infra retry pass"
    )


def test_llm_provider_error_still_infra() -> None:
    """Real infra markers (LLM connection / call errors) still route
    to retry — H15 didn't change that path."""
    _is_infra_fail = _load_run_module()._is_infra_fail
    from types import SimpleNamespace
    for marker in ("Error calling LLM: connection error",
                   "Connection error: timeout"):
        trace = SimpleNamespace(got=marker, stop_reason="ok")
        verdict = {"score": 0.0}
        assert _is_infra_fail(trace, verdict) is True, (
            f"{marker!r} must still be classified infra"
        )


def test_timeout_or_exception_still_infra() -> None:
    _is_infra_fail = _load_run_module()._is_infra_fail
    from types import SimpleNamespace
    for stop in ("timeout", "exception"):
        trace = SimpleNamespace(got="", stop_reason=stop)
        verdict = {"score": 0.0}
        assert _is_infra_fail(trace, verdict) is True


def test_max_iterations_default_is_twelve() -> None:
    """Audit H16: the bench CLI default rose from 8 to 12 so the
    agent has budget to finish the search→drill→synthesise pattern
    that memory-augmented agents use."""
    import argparse
    # Import the parser builder by replicating the relevant arg
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iterations", type=int, default=12)
    args = parser.parse_args([])
    # Re-fetch the real default from the script's argparse — direct
    # parse of the runner module is expensive, so check via help text.
    import subprocess
    out = subprocess.run(
        ["python", "-m", "scripts.benchmark.locomo_run", "--help"],
        capture_output=True, text=True, timeout=15,
    ).stdout
    assert "default 12" in out.lower() or "(default 12)" in out, (
        "max-iterations default must be 12 (H16)"
    )


# ---------------------------------------------------------------------------
# Audit H14 (2026-05-29): adversarial refusal rubric
# ---------------------------------------------------------------------------


def test_refuse_sentinel_matches_english_refusal() -> None:
    """H20: the LLM judge receives the agent answer and decides
    refused=true for plain English refusal patterns."""
    def _judge_llm(prompt, *, model, **_kw):
        return '{"refused": true, "reasoning": "agent says no info"}'
    verdict = judge_answer(
        question="What is X's secret password?",
        expected="__REFUSE__",
        got="I don't have information about X's password in memory.",
        llm_invoke=_judge_llm,
    )
    assert verdict.score == 1.0
    assert "refusal" in verdict.reasoning.lower()


def test_refuse_sentinel_matches_spanish_refusal() -> None:
    """H20: LLM judge handles refusal in any language naturally."""
    def _judge_llm(prompt, *, model, **_kw):
        return '{"refused": true, "reasoning": "agent refuses in Spanish"}'
    verdict = judge_answer(
        question="¿Cuál es la contraseña?",
        expected="__REFUSE__",
        got="No tengo información sobre la contraseña en la memoria.",
        llm_invoke=_judge_llm,
    )
    assert verdict.score == 1.0


def test_refuse_sentinel_penalises_hallucinated_answer() -> None:
    """H20: when the LLM judge identifies a positive (hallucinated)
    answer, score is 0.0 with hallucination reasoning."""
    def _judge_llm(prompt, *, model, **_kw):
        return '{"refused": false, "reasoning": "agent invented a password"}'
    verdict = judge_answer(
        question="What is X's password?",
        expected="__REFUSE__",
        got="X's password is 'hunter2'.",
        llm_invoke=_judge_llm,
    )
    assert verdict.score == 0.0
    assert "halluc" in verdict.reasoning.lower()


def test_refuse_sentinel_calls_llm_judge_for_semantic_evaluation(monkeypatch) -> None:
    """H20 (2026-05-30) reversed the H14 design — refusal scoring now
    INVOKES the LLM judge with a refusal-specific prompt because
    lexical matching missed stealth refusals ("it was actually X,
    not Y"). The previous "no LLM call" guarantee is gone by design.
    """
    monkeypatch.setattr(_judge_mod.time, "sleep", lambda _: None)
    calls = []
    def _track(prompt, *, model, **kw):
        calls.append(prompt)
        return '{"refused": true, "reasoning": "ok"}'
    judge_answer(
        question="q?", expected="__REFUSE__",
        got="I don't know.", llm_invoke=_track,
    )
    assert len(calls) == 1, "refusal scoring runs ONE LLM call (H20)"
    assert "REFUSED" in calls[0] or "refused" in calls[0], (
        "the refusal prompt must mention the refused/hallucinated decision"
    )


# ---------------------------------------------------------------------------
# Audit H20 (2026-05-30): adversarial refusal via LLM judge (replaces H14)
# ---------------------------------------------------------------------------
#
# Bench-100-prop analysis showed 3 of 11 adversarial fails were
# stealth refusals the H14 lexical matcher couldn't catch:
# "it was actually X, not Y" — semantically a refusal but no marker
# matched. H20 replaces lexical scoring with an LLM-judge call using
# a refusal-specific prompt. Same retry/backoff infrastructure as
# the main judge.


def test_h20_refusal_llm_judge_marks_explicit_refusal_correct() -> None:
    """Plain refusal: agent says "no record" → judge LLM returns
    refused=true → score 1.0."""
    invokes: list = []
    def _fake_llm(prompt, *, model, **_kw):
        invokes.append(prompt)
        # Mimic the LLM correctly identifying refusal
        return '{"refused": true, "reasoning": "agent says no record"}'

    verdict = judge_answer(
        question="What setback did Caroline face?",
        expected="__REFUSE__",
        got="I don't have any record of Caroline facing a setback.",
        llm_invoke=_fake_llm,
    )
    assert verdict.score == 1.0
    assert "refusal" in verdict.reasoning.lower()
    assert len(invokes) == 1, "exactly one LLM call for refusal scoring"


def test_h20_refusal_llm_judge_catches_stealth_refusal() -> None:
    """The bug H20 fixes: "it was actually X, not Y" pattern. LLM
    should mark refused=true; lexical H14 would have missed it."""
    def _fake_llm(prompt, *, model, **_kw):
        return '{"refused": true, "reasoning": "agent corrected the false premise"}'
    verdict = judge_answer(
        question="What schools did Tim play basketball at?",
        expected="__REFUSE__",
        got="It was actually John (not Tim) who played basketball.",
        llm_invoke=_fake_llm,
    )
    assert verdict.score == 1.0


def test_h20_refusal_llm_judge_penalises_hallucination() -> None:
    def _fake_llm(prompt, *, model, **_kw):
        return '{"refused": false, "reasoning": "agent invented a fact"}'
    verdict = judge_answer(
        question="What did John do to feel closer to community?",
        expected="__REFUSE__",
        got="John joined a fire-fighting brigade to give back.",
        llm_invoke=_fake_llm,
    )
    assert verdict.score == 0.0
    assert "halluc" in verdict.reasoning.lower()


def test_h20_refusal_llm_judge_tolerates_markdown_wrapped_json() -> None:
    """Robustness: LLMs often wrap JSON in ```json fences despite the
    prompt asking otherwise."""
    def _fake_llm(prompt, *, model, **_kw):
        return '```json\n{"refused": true, "reasoning": "OK"}\n```'
    verdict = judge_answer(
        question="q?", expected="__REFUSE__", got="no info",
        llm_invoke=_fake_llm,
    )
    assert verdict.score == 1.0


def test_h20_refusal_llm_judge_extracts_json_from_prose() -> None:
    """Robustness: LLMs sometimes prepend explanation before the JSON."""
    def _fake_llm(prompt, *, model, **_kw):
        return ('Here is the verdict:\n\n'
                '{"refused": false, "reasoning": "hallucinated"}\n\nDone.')
    verdict = judge_answer(
        question="q?", expected="__REFUSE__", got="X did Y in 2023",
        llm_invoke=_fake_llm,
    )
    assert verdict.score == 0.0


def test_h20_refusal_llm_judge_falls_back_to_lexical_on_failure(monkeypatch) -> None:
    """If the LLM judge call fails repeatedly, fall back to the legacy
    lexical matcher rather than losing signal on the QA."""
    monkeypatch.setattr(_judge_mod.time, "sleep", lambda _: None)
    def _broken_llm(prompt, *, model, **_kw):
        raise RuntimeError("z.ai down")
    # Got contains a lexical-detectable refusal marker so the fallback
    # produces a meaningful score
    verdict = judge_answer(
        question="q?", expected="__REFUSE__",
        got="I don't have any record of this.",
        llm_invoke=_broken_llm,
    )
    # The lexical fallback returns 1.0 because "i don't have" matches
    assert verdict.score == 1.0
    assert "lexical fallback" in verdict.reasoning.lower()


def test_h20_refusal_llm_judge_passes_question_to_prompt() -> None:
    """The refusal prompt MUST include the question — without it the
    judge can't tell whether the agent is correctly refusing or just
    avoiding the question."""
    captured: list[str] = []
    def _fake_llm(prompt, *, model, **_kw):
        captured.append(prompt)
        return '{"refused": true, "reasoning": "ok"}'
    judge_answer(
        question="Did Alice eat the cake?",
        expected="__REFUSE__",
        got="I don't know",
        llm_invoke=_fake_llm,
    )
    assert "Did Alice eat the cake?" in captured[0]
