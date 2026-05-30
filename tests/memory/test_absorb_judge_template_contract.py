"""Lock the absorb-judge prompt ↔ code vocabulary alignment.

Verifies that ``durin/templates/dream/absorb_judge.md`` mentions the
exact verdict labels that ``durin/memory/absorb_judge.py`` accepts
(``same`` / ``different`` / ``unclear``) and the same markdown-marker
envelope (``===VERDICT===`` etc.). A drift between template and code
would make every judge call fail at parse time — so this test is the
canary.

Doc-level reference: `docs/architecture/memory/06_prompts_and_instructions.md` §5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.absorb_judge import _VALID_VERDICTS


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "durin" / "templates" / "dream" / "absorb_judge.md"
)


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def test_template_lists_every_valid_verdict(template_text: str) -> None:
    """Each verdict accepted by the parser must appear in the template
    so the LLM has a chance to emit it."""
    for verdict in _VALID_VERDICTS:
        assert verdict in template_text, (
            f"verdict {verdict!r} accepted by the parser but missing "
            f"from absorb_judge.md — the LLM won't know to emit it"
        )


def test_template_uses_v2_envelope(template_text: str) -> None:
    """The runner's regex matches `===VERDICT===` / `===CONFIDENCE===`
    / `===REASONING===` / `===END===`. The template must instruct the
    LLM to emit that exact envelope."""
    for marker in (
        "===VERDICT===",
        "===CONFIDENCE===",
        "===REASONING===",
        "===END===",
    ):
        assert marker in template_text, (
            f"marker {marker!r} missing from absorb_judge.md"
        )


def test_valid_verdicts_match_doc_06(template_text: str) -> None:
    """Doc 06 §5 names the three verdicts. Lock the set so a future
    refactor that adds/removes a verdict breaks this loudly."""
    assert _VALID_VERDICTS == frozenset({"same", "different", "unclear"})


def test_template_keeps_peer_review_framing(template_text: str) -> None:
    """Doc 06 §5 + the prompt's design note (`feedback_question_user_input`
    pattern) require a peer-review tone — the LLM should default to
    'different' when content evidence is thin, not confirm Dream's
    most recent decision (self-consistency bias mitigation, doc 05
    §8.7). Lock the safety net so a future rewrite can't drop it."""
    # Lenient match — the prompt's exact wording is in Spanish; we
    # check for one of two semantic anchors that must be present.
    assert (
        "Default a \"different\"" in template_text
        or "default to \"different\"" in template_text.lower()
    ), (
        "absorb_judge.md no longer instructs the LLM to default to "
        "'different' on weak evidence — peer-review framing removed?"
    )
