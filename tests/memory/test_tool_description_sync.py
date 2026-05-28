"""Audit: tool descriptions in code must match doc 06 §3 verbatim.

Per `docs/memory/06_prompts_and_instructions.md` §3.5: the canonical
text the LLM sees lives in the doc. Code drift silently changes
agent behaviour. This test parses the doc + compares against the
4 memory tools' descriptions.

Whitespace normalization: doc markdown is indentation-sensitive
inside code blocks; we collapse repeated whitespace before
comparison so cosmetic line-wrapping differences don't fail the
audit while semantic edits (added/removed sentence) still do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from durin.agent.tools.memory_drill import _PARAMETERS as DRILL_PARAMS
from durin.agent.tools.memory_ingest import _PARAMETERS as INGEST_PARAMS
from durin.agent.tools.memory_search import _PARAMETERS as SEARCH_PARAMS
from durin.agent.tools.memory_store import _PARAMETERS as STORE_PARAMS


_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "memory" / "06_prompts_and_instructions.md"
)


def _extract_section_block(doc: str, section_heading: str) -> str:
    """Return the first fenced ``` block under the given H3 heading."""
    # Find the heading line.
    lines = doc.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip() == section_heading:
            start_idx = i
            break
    if start_idx is None:
        raise AssertionError(
            f"section heading not found in doc: {section_heading!r}"
        )
    # Find the next ``` opening + closing.
    in_block = False
    block_lines: list[str] = []
    for line in lines[start_idx + 1:]:
        if not in_block:
            if line.strip() == "```":
                in_block = True
            elif line.startswith("###"):
                break  # heading change before any block — bail
            continue
        if line.strip() == "```":
            break
        block_lines.append(line)
    if not block_lines:
        raise AssertionError(
            f"no fenced block found under {section_heading!r}"
        )
    return "\n".join(block_lines).strip()


def _normalise(text: str) -> str:
    """Collapse whitespace + strip — semantic comparison only."""
    return re.sub(r"\s+", " ", text).strip()


@pytest.fixture(scope="module")
def doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def test_memory_search_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.1 `memory_search`")
    actual = SEARCH_PARAMS["description"]
    assert _normalise(actual) == _normalise(expected), (
        "memory_search description drifted from doc 06 §3.1 — sync "
        "either the spec or the code."
    )


def test_memory_store_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.2 `memory_store`")
    actual = STORE_PARAMS["description"]
    assert _normalise(actual) == _normalise(expected), (
        "memory_store description drifted from doc 06 §3.2."
    )


def test_memory_ingest_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.3 `memory_ingest`")
    actual = INGEST_PARAMS["description"]
    assert _normalise(actual) == _normalise(expected), (
        "memory_ingest description drifted from doc 06 §3.3."
    )


def test_memory_drill_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.4 `memory_drill`")
    actual = DRILL_PARAMS["description"]
    assert _normalise(actual) == _normalise(expected), (
        "memory_drill description drifted from doc 06 §3.4."
    )
