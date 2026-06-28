"""Lock the identity.md Memory section against prompt drift.

If a future edit drifts the prompt out of sync with the expected wording,
this test breaks loudly. Update the phrases here when the block is
intentionally rewritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY_PATH = _REPO_ROOT / "durin" / "templates" / "agent" / "identity.md"


@pytest.fixture(scope="module")
def identity_text() -> str:
    return _IDENTITY_PATH.read_text(encoding="utf-8")


# Spec anchors taken verbatim from identity.md. We don't anchor on the
# whole block (whitespace + Jinja interpolation make exact equality
# brittle) but we lock the load-bearing sentences.
_REQUIRED_PHRASES = (
    "Memory is how you persist what matters",
    "Entity pages",
    "References",
    "Session summaries",
    "Fragments",
    "search — don't answer from cold recall",
    "issue 2-3 searches with different phrasings",
    "reconcile disagreements by timestamp",
    "enumerate every distinct item",
    "State the source",
    "never claim what isn't in the results",
    "invent identifiers",
    "capture as you go",
    "Standard types: person, place, project, topic, event, artifact",
    "Known types",
    "Don't save",
    "Correct in place",
    "Say what you saved",
)

_ACTIVE_TOOLS = (
    "memory_search", "memory_drill", "memory_read_entity",
    "memory_entity_lineage", "memory_source_session",
    "memory_upsert_entity", "memory_ingest", "memory_forget",
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
        "identity.md drifted from its expected content. "
        "Missing phrases:\n  - " + "\n  - ".join(missing)
    )


def test_identity_names_all_active_memory_tools(identity_text: str) -> None:
    """Every active memory tool is named in the identity Memory block."""
    missing = [t for t in _ACTIVE_TOOLS if t not in identity_text]
    assert not missing, f"identity.md no longer names: {missing}"


def test_does_not_contain_dropped_v1_phrasing(identity_text: str) -> None:
    """Phrases that v2 explicitly dropped — drift in the wrong direction
    also breaks loudly."""
    forbidden = (
        "always call memory_search before answering",
        "trust X over Y",
    )
    found = [p for p in forbidden if p in identity_text]
    assert not found, (
        "identity.md re-introduced wording that v2 explicitly removed:\n"
        + "\n".join(f"  - {p}" for p in found)
    )
