"""The consolidator template's own example must survive the tag parser.

Pins the prompt<->parser contract: if the template teaches the LLM a
format the parser rejects, entities silently die (regression of the
bare-names bug found 2026-07-17).
"""
from __future__ import annotations

import re

from durin.memory.consolidator_tags import parse_consolidator_response
from durin.utils.prompt_templates import render_template


def _example_block(template: str) -> str:
    """Return everything from 'Example output:' to the end of the template."""
    marker = "Example output:"
    assert marker in template, "template lost its example section"
    return template[template.index(marker) + len(marker):]


def test_template_example_entities_parse_non_empty():
    template = render_template("agent/consolidator_archive.md", strip=True)
    example = _example_block(template).strip()
    summary, tags = parse_consolidator_response(example)
    assert summary.strip(), "example summary side must be non-empty"
    assert tags["entities"], (
        "the template example emits entities the parser drops — "
        "prompt and parser have drifted apart again"
    )
    assert tags["topics"]


def test_template_instructs_typed_entity_refs():
    template = render_template("agent/consolidator_archive.md", strip=True)
    entities_instruction = re.search(r"`entities`:.*", template)
    assert entities_instruction is not None
    assert "<type>:<value>" in entities_instruction.group(0)


def test_template_has_locations_category_and_does_not_skip_paths():
    template = render_template("agent/consolidator_archive.md", strip=True)
    assert "- Locations:" in template
    # The old clause told the model to drop anything derivable from
    # source files — which included the paths themselves.
    assert "code patterns derivable from source" not in template
