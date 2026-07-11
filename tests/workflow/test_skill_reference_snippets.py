"""The workflows skill's patterns reference promises "one small, parseable snippet
per capability". This test holds it to that: every fenced JSON block in patterns.md
that looks like a full workflow definition (has "name" and "start") must parse
through the real parser, so the LLM-facing examples can never drift from the schema.
Non-workflow fences (e.g. a single-node fragment) only need to be valid JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from durin.workflow.spec import parse_workflow

PATTERNS = (
    Path(__file__).resolve().parents[2]
    / "durin" / "skills" / "workflows" / "references" / "patterns.md"
)


def _json_fences(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"```json\s*([\s\S]*?)```", text)]


def test_patterns_reference_snippets_parse():
    fences = _json_fences(PATTERNS.read_text())
    assert fences, "patterns.md has no ```json fences — did the file move?"
    workflows = 0
    for block in fences:
        data = json.loads(block)  # every fence must at least be valid JSON
        if isinstance(data, dict) and "name" in data and "start" in data:
            parse_workflow(data)  # full definitions must satisfy the real parser
            workflows += 1
    assert workflows >= 5, f"expected several full workflow snippets, found {workflows}"


def test_patterns_cover_every_node_kind():
    # The reference must document every node kind the parser accepts.
    text = PATTERNS.read_text()
    for kind in ('"kind": "work"', '"kind": "script"', '"kind": "parallel"', '"kind": "subworkflow"'):
        assert kind in text, f"patterns.md lacks a snippet using {kind}"
