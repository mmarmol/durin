"""Audit: tool descriptions in code must match the prompts doc verbatim.

The canonical text the LLM sees lives in the doc. Code drift silently changes
agent behaviour. This test parses the doc + compares against the LIVE memory
tools' descriptions (search, upsert_entity, ingest, drill, forget) plus the
disabled memory_store (kept in sync while it still exists).

**What we compare**: the `Tool.description` property — the field that
`Tool.to_schema()` emits as `function.description` in the OpenAI
function-calling spec. This IS what the LLM reads to decide whether to invoke
the tool.

Whitespace normalization: doc markdown is indentation-sensitive inside code
blocks; we collapse repeated whitespace before comparison so cosmetic
line-wrapping differences don't fail the audit while semantic edits still do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from durin.agent.tools.memory_drill import MemoryDrillTool
from durin.agent.tools.memory_forget import MemoryForgetTool
from durin.agent.tools.memory_ingest import MemoryIngestTool
from durin.agent.tools.memory_search import MemorySearchTool
from durin.agent.tools.memory_store import MemoryStoreTool
from durin.agent.tools.memory_upsert_entity import MemoryUpsertEntityTool

_DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs" / "architecture" / "memory" / "06_prompts_and_instructions.md"
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


def _tool_description(tool_cls) -> str:
    """Return the LLM-visible description that ``to_schema()`` emits.

    We instantiate with a workspace placeholder — `description` is a
    property that doesn't touch workspace state, so the value is
    deterministic.
    """
    tool = tool_cls(workspace="/tmp")  # type: ignore[arg-type]
    return tool.description


def test_memory_search_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.1 `memory_search`")
    actual = _tool_description(MemorySearchTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_search `.description` property drifted from doc 06 "
        "§3.1 — sync either the spec or the code. Note: this is "
        "the field `Tool.to_schema()` emits as "
        "`function.description` — what the LLM actually reads."
    )


def test_memory_store_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.2 `memory_store`")
    actual = _tool_description(MemoryStoreTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_store `.description` property drifted from doc 06 §3.2."
    )


def test_memory_ingest_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.3 `memory_ingest`")
    actual = _tool_description(MemoryIngestTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_ingest `.description` property drifted from doc 06 §3.3."
    )


def test_memory_drill_description_matches_doc(doc_text: str) -> None:
    expected = _extract_section_block(doc_text, "### 3.4 `memory_drill`")
    actual = _tool_description(MemoryDrillTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_drill `.description` property drifted from doc 06 §3.4."
    )


def test_memory_upsert_entity_description_matches_doc(doc_text: str) -> None:
    # N6: the live write tool MUST be doc-governed (was uncovered).
    expected = _extract_section_block(doc_text, "### 3.5 `memory_upsert_entity`")
    actual = _tool_description(MemoryUpsertEntityTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_upsert_entity `.description` property drifted from doc 06 §3.5."
    )


def test_memory_forget_description_matches_doc(doc_text: str) -> None:
    # N6: the live delete tool MUST be doc-governed (was uncovered).
    expected = _extract_section_block(doc_text, "### 3.6 `memory_forget`")
    actual = _tool_description(MemoryForgetTool)
    assert _normalise(actual) == _normalise(expected), (
        "memory_forget `.description` property drifted from doc 06 §3.6."
    )


# H9 (audit 2026-05-29): the standalone ``memory_drill_batch`` tool was
# folded into ``memory_drill``. The consolidated drill description must
# document both the single-``uri`` and list-``uris`` shapes; the dedicated
# ``test_memory_drill_description_matches_doc`` above covers it.


def test_description_property_is_what_to_schema_emits() -> None:
    """B1 invariant: the field the test guards (`.description`) IS
    the one OpenAI function-calling consumers read. If
    `Tool.to_schema()` ever pivots to using
    `_PARAMETERS["description"]` instead, this test must be updated
    in lock-step so the sync still covers the LLM-visible surface."""
    tool = MemorySearchTool(workspace="/tmp")  # type: ignore[arg-type]
    schema = tool.to_schema()
    assert schema["function"]["description"] == tool.description, (
        "Tool.to_schema() must emit `self.description` as "
        "`function.description` — otherwise this audit guards the "
        "wrong field. See durin/agent/tools/base.py."
    )
