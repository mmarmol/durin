"""Unit tests for node_label and its constituent helpers."""

import pytest

from durin.workflow.spec import (
    ParallelNode,
    SubworkflowNode,
    WorkNode,
    _first_sentence,
    _prettify_id,
    node_description,
    node_label,
)


# ---------------------------------------------------------------------------
# _first_sentence
# ---------------------------------------------------------------------------

def test_first_sentence_simple():
    assert _first_sentence("Break the question into research angles.") == "Break the question into research angles"


def test_first_sentence_splits_on_period_space():
    assert _first_sentence("Do X. Then do Y.") == "Do X"


def test_first_sentence_splits_on_newline():
    assert _first_sentence("Do X\nThen do Y") == "Do X"


def test_first_sentence_truncates_long():
    long = "A " * 50  # 100 chars of "A "
    result = _first_sentence(long)
    assert len(result) <= 81  # max_chars + ellipsis character
    assert result.endswith("…")


def test_first_sentence_empty():
    assert _first_sentence("") == ""
    assert _first_sentence("   ") == ""


# ---------------------------------------------------------------------------
# _prettify_id
# ---------------------------------------------------------------------------

def test_prettify_id_underscores():
    assert _prettify_id("research_phase") == "Research phase"


def test_prettify_id_hyphens():
    assert _prettify_id("gather-results") == "Gather results"


def test_prettify_id_already_nice():
    assert _prettify_id("plan") == "Plan"


def test_prettify_id_empty_returns_original():
    assert _prettify_id("") == ""


# ---------------------------------------------------------------------------
# node_label
# ---------------------------------------------------------------------------

def test_node_label_title_wins():
    node = WorkNode(id="plan", title="Break the question into angles", prompt="Do research.")
    assert node_label(node) == "Break the question into angles"


def test_node_label_ignores_the_prompt_when_no_title():
    # node_label does not fall back to the prompt — a WorkNode with no title or
    # command/script reads as its prettified id, not prose from its prompt.
    node = WorkNode(id="plan", prompt="Research the topic carefully. Then synthesize.")
    assert node_label(node) == "Plan"


def test_node_description_is_the_prompt_first_sentence():
    # The prompt-derived text still exists for hover text: node_description
    # (not node_label, which prefers the node id) returns it.
    node = WorkNode(id="plan", prompt="Research the topic carefully. Then synthesize.")
    assert node_description(node) == "Research the topic carefully"


def test_node_label_prettified_id_when_no_title_no_prompt():
    node = WorkNode(id="gather_results")
    assert node_label(node) == "Gather results"


def test_node_label_subworkflow_no_prompt_has_prettified_id():
    node = SubworkflowNode(id="run_sub", workflow="inner")
    assert node_label(node) == "Run sub"


def test_node_label_subworkflow_with_title():
    node = SubworkflowNode(id="run_sub", title="Execute the sub-workflow", workflow="inner")
    assert node_label(node) == "Execute the sub-workflow"


def test_node_label_parallel_prettified_id():
    node = ParallelNode(id="gather_parallel", branches=("b1", "b2"))
    assert node_label(node) == "Gather parallel"


def test_node_label_parallel_with_title():
    node = ParallelNode(id="gather_parallel", title="Gather from multiple sources", branches=("b1", "b2"))
    assert node_label(node) == "Gather from multiple sources"


def test_node_label_title_blank_falls_through_to_prettified_id():
    node = WorkNode(id="plan", title="   ", prompt="Plan the work.")
    assert node_label(node) == "Plan"


def test_node_label_title_parsed_from_workflow():
    """title is parsed from a workflow definition and used over the prompt."""
    from durin.workflow.spec import parse_workflow
    wf = parse_workflow({
        "name": "test", "start": "a",
        "nodes": [
            {"id": "a", "title": "Research angles", "prompt": "Do research.", "kind": "work", "next": None},
        ],
    })
    assert node_label(wf.nodes["a"]) == "Research angles"
