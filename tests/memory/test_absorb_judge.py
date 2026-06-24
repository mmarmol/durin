"""Unit tests for the absorb-judge LLM-judge module."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from durin.memory.absorb_judge import (
    JudgeError,
    JudgeResult,
    _build_prompt,
    _load_template,
    _parse_response,
    judge_pair,
)
from durin.memory.entity_page import EntityPage

# ---------------------------------------------------------------------------
# template loader
# ---------------------------------------------------------------------------


def test_template_has_all_placeholders() -> None:
    """The shipped prompt template must expose every variable the
    builder will substitute, or the format() call below will silently
    leave placeholders in the prompt."""
    tpl = _load_template()
    for marker in [
        "{shared_aliases}", "{ref_a}", "{ref_b}",
        "{page_a_block}", "{page_b_block}",
        "===VERDICT===", "===CONFIDENCE===", "===REASONING===", "===END===",
    ]:
        assert marker in tpl, f"template missing {marker!r}"


# ---------------------------------------------------------------------------
# _parse_response — happy path + every failure mode the judge can produce
# ---------------------------------------------------------------------------


def test_parse_clean_envelope() -> None:
    raw = (
        "===VERDICT===\nsame\n===CONFIDENCE===\n92\n"
        "===REASONING===\nShared email mmarmol@mxhero.com plus durin ownership.\n===END===\n"
    )
    r = _parse_response(raw)
    assert r.verdict == "same"
    assert r.confidence == 92
    assert "mmarmol" in r.reasoning


def test_parse_tolerates_extra_prose() -> None:
    raw = (
        "Sure, my answer:\n\n===VERDICT===\ndifferent\n===CONFIDENCE===\n10\n"
        "===REASONING===\nDistinct organisations.\n===END===\nThanks!\n"
    )
    r = _parse_response(raw)
    assert r.verdict == "different"


def test_parse_verdict_case_insensitive() -> None:
    raw = (
        "===VERDICT===\nSAME\n===CONFIDENCE===\n80\n"
        "===REASONING===\nok\n===END===\n"
    )
    assert _parse_response(raw).verdict == "same"


def test_parse_unclear_verdict_allowed() -> None:
    raw = (
        "===VERDICT===\nunclear\n===CONFIDENCE===\n50\n"
        "===REASONING===\nMixed signals.\n===END===\n"
    )
    assert _parse_response(raw).verdict == "unclear"


@pytest.mark.parametrize("bad", [
    "",
    "no markers",
    "===VERDICT===\nmaybe\n===CONFIDENCE===\n50\n===REASONING===\nx\n===END===\n",  # invalid verdict
    "===VERDICT===\nsame\n===CONFIDENCE===\nninety\n===REASONING===\nx\n===END===\n",  # non-int
    "===VERDICT===\nsame\n===CONFIDENCE===\n150\n===REASONING===\nx\n===END===\n",  # out of range
    "===VERDICT===\nsame\n===CONFIDENCE===\n80\n===REASONING===\n\n===END===\n",  # empty reasoning
    "===VERDICT===\nsame\n",  # truncated
])
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(JudgeError):
        _parse_response(bad)


# ---------------------------------------------------------------------------
# _build_prompt — temporal metadata + identifiers + body substitution
# ---------------------------------------------------------------------------


def test_build_prompt_substitutes_all_fields() -> None:
    a = EntityPage(
        type="person", name="Marcelo", aliases=["Marcelo", "marcelo"],
        body="## Current\nFounder of durin.\n",
        extra={"identifiers": {"email": ["mmarmol@mxhero.com"]}},
    )
    b = EntityPage(
        type="person", name="Marcelo Diaz", aliases=["Marcelo"],
        body="## Current\nRandom contact.\n",
    )
    prompt = _build_prompt(
        canonical=a, absorbed=b,
        shared_aliases=["Marcelo", "marcelo"],
        canonical_ref="person:marcelo",
        absorbed_ref="person:marcelo-d",
        canonical_mtime=datetime(2025, 1, 1, 12, tzinfo=timezone.utc),
        absorbed_mtime=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
    )
    # Refs + alias list + identifier + body all rendered.
    assert "person:marcelo" in prompt
    assert "person:marcelo-d" in prompt
    assert "Marcelo, marcelo" in prompt
    # Identifier value reaches the prompt via to_markdown() YAML serialization
    # (no longer hand-formatted as "Identifier (email): ...").
    assert "mmarmol@mxhero.com" in prompt
    assert "Founder of durin" in prompt
    assert "Random contact" in prompt
    # Temporal context per glm C2 mitigation.
    assert "2025-01-01" in prompt
    assert "2026-05-01" in prompt
    # No leftover format placeholders.
    for placeholder in ["{shared_aliases}", "{ref_a}", "{ref_b}",
                         "{page_a_block}", "{page_b_block}"]:
        assert placeholder not in prompt


def test_build_prompt_handles_missing_mtime() -> None:
    a = EntityPage(type="person", name="A", aliases=[])
    b = EntityPage(type="person", name="B", aliases=[])
    prompt = _build_prompt(
        canonical=a, absorbed=b, shared_aliases=[],
        canonical_ref="person:a", absorbed_ref="person:b",
        canonical_mtime=None, absorbed_mtime=None,
    )
    assert "(unknown)" in prompt
    assert "(none)" in prompt  # empty shared_aliases label


# ---------------------------------------------------------------------------
# judge_pair — happy path + retry on parse failure + LLM exception
# ---------------------------------------------------------------------------


def _make_pages():
    a = EntityPage(type="person", name="Marcelo", aliases=["Marcelo"])
    b = EntityPage(type="person", name="Marcelo M", aliases=["Marcelo"])
    return a, b


def test_judge_pair_happy_path() -> None:
    a, b = _make_pages()
    calls = []
    def stub(prompt: str, *, model: str) -> str:
        calls.append((prompt[:40], model))
        return (
            "===VERDICT===\nsame\n===CONFIDENCE===\n88\n"
            "===REASONING===\nSame email.\n===END===\n"
        )
    r = judge_pair(a, b, ["Marcelo"], llm_invoke=stub, model="test-model")
    assert isinstance(r, JudgeResult)
    assert r.verdict == "same" and r.confidence == 88
    assert len(calls) == 1
    assert calls[0][1] == "test-model"


def test_judge_pair_retries_on_parse_failure_then_succeeds() -> None:
    a, b = _make_pages()
    attempts: list[str] = []
    def stub(prompt: str, *, model: str) -> str:
        attempts.append("call")
        if len(attempts) < 2:
            return "garbage nope"
        return (
            "===VERDICT===\ndifferent\n===CONFIDENCE===\n40\n"
            "===REASONING===\nDistinct.\n===END===\n"
        )
    r = judge_pair(a, b, [], llm_invoke=stub, max_retries=2)
    assert r.verdict == "different"
    assert len(attempts) == 2


def test_judge_pair_exhausts_retries_and_raises() -> None:
    a, b = _make_pages()
    def stub(prompt: str, *, model: str) -> str:
        return "nope"
    with pytest.raises(JudgeError):
        judge_pair(a, b, [], llm_invoke=stub, max_retries=1)


def test_judge_pair_wraps_llm_exception_as_judge_error() -> None:
    a, b = _make_pages()
    def stub(prompt: str, *, model: str) -> str:
        raise RuntimeError("provider down")
    with pytest.raises(JudgeError) as exc_info:
        judge_pair(a, b, [], llm_invoke=stub, max_retries=1)
    assert "provider down" in str(exc_info.value)


# ---------------------------------------------------------------------------
# guard test: whole page (attributes / relations / provenance) reaches prompt
# ---------------------------------------------------------------------------


def test_build_prompt_renders_whole_page(tmp_path) -> None:
    a = EntityPage(
        type="place", name="Torrent",
        attributes={"warning_zone": "Litoral norte de Valencia"},
        relations=[{"to": "place:valencia", "type": "in"}],
        provenance={"attributes": {"warning_zone": {"author": "dream"}}},
    )
    b = EntityPage(type="place", name="Torrent", attributes={"country": "Spain"})
    prompt = _build_prompt(
        canonical=a, absorbed=b, shared_aliases=["torrent"],
        canonical_ref="place:torrent", absorbed_ref="place:torrent-valencia",
        canonical_mtime=None, absorbed_mtime=None,
    )
    assert "warning_zone" in prompt and "Litoral norte de Valencia" in prompt
    assert "place:valencia" in prompt          # relation
    assert "country" in prompt and "Spain" in prompt
