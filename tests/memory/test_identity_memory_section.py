"""Lock the identity.md Memory section against doc 06 §2.

If a future edit drifts the prompt out of sync with the canonical
spec text, this test breaks loudly. Per doc 06 §2.2 the v2 wording
is what produced +12pp on single_hop and +3.9pp net on LoCoMo
(2026-05-25); changing it without bench evidence regresses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY_PATH = _REPO_ROOT / "durin" / "templates" / "agent" / "identity.md"


@pytest.fixture(scope="module")
def identity_text() -> str:
    return _IDENTITY_PATH.read_text(encoding="utf-8")


# Spec anchors taken verbatim from doc 06 §2. We don't anchor on the
# whole block (whitespace + Jinja interpolation make exact equality
# brittle) but we lock the load-bearing sentences.
_REQUIRED_PHRASES = (
    "You have access to four memory tools",
    # Tool list may wrap across lines in the file; normalise whitespace
    # before comparing.
    # (Checked separately below.)
    # §8a new model: entity pages + references replace canonical/fragments/
    # ingested-chunks. The bench-validated anchors below are PRESERVED.
    "Entity pages",
    "References",
    "Session summaries",
    "call memory_search rather than answering",
    "from cold recall",
    "State the source of any fact you cite",
    "For compound or multi-part questions, issue 2-3 searches",
    # H22 (2026-05-30) anti-hallucination bullets — these are
    # production defaults that ship with durin and are read by the
    # agent every turn. Drift = the production agent loses honesty
    # signal across reframing / multi-part / identifier-invention.
    "Don't reframe to fit the question",
    "Answer multi-part questions partially when needed",
    "Never invent identifiers",
)


def test_each_required_phrase_present(identity_text: str) -> None:
    """Drift breaks loudly: any spec-anchor phrase missing from
    identity.md fails the test with a precise message. Whitespace is
    normalised so a wrapped tool-name list still matches.
    """
    import re
    normalised = re.sub(r"\s+", " ", identity_text)
    missing = [p for p in _REQUIRED_PHRASES if p not in normalised]
    assert not missing, (
        "identity.md drifted from docs/architecture/memory/06_prompts_and_instructions.md "
        "§2. Missing spec phrases:\n  - " + "\n  - ".join(missing)
    )
    # The full tool name list must be present (in any whitespace
    # arrangement).
    assert (
        "memory_search, memory_upsert_entity, memory_ingest, memory_drill"
        in normalised
    )


def test_does_not_contain_dropped_v1_phrasing(identity_text: str) -> None:
    """Phrases that v2 explicitly dropped (per doc 06 §2.1) — drift in
    the wrong direction also breaks loudly."""
    forbidden = (
        "always call memory_search before answering",
        "trust X over Y",
    )
    found = [p for p in forbidden if p in identity_text]
    assert not found, (
        "identity.md re-introduced wording that v2 explicitly removed:\n"
        + "\n".join(f"  - {p}" for p in found)
    )
