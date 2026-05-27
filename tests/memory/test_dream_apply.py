"""Apply pipeline for the v2 Dream output.

Per `docs/memory/05_dream_cold_path.md` §6: take a parsed
:class:`ParsedDreamOutput` and apply its patch + body delta to the
entity page on disk, atomically. Steps:

  1. Validate patch ops (allowed roots, provenance present, paths
     well-formed).
  2. Copy current page to `<path>.md.bak`.
  3. Apply ops to the frontmatter dict via :mod:`jsonpatch`.
  4. Append body delta to the page body if non-empty.
  5. Re-render markdown, verify it round-trips, write atomically.
  6. On any step 1-5 failure, restore from `.md.bak` and surface a
     typed failure to the caller.
  7. On success, delete the `.md.bak` and return the new path's mtime
     plus diagnostic counters.

Cursor advance is the caller's responsibility — the applier reports
success but does not write `dream_processed_through`. That keeps the
G2 invariant (doc 05 §6.1) in one place (the runner) instead of two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.dream_apply import (
    DreamApplyError,
    DreamApplyFailureKind,
    DreamApplyResult,
    apply_dream_output,
)
from durin.memory.dream_patch_parser import ParsedDreamOutput
from durin.memory.entity_page import EntityPage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Fresh workspace with a single seeded entity page (v2 schema)."""
    page = EntityPage(
        type="person", name="Marcelo", aliases=["Marcelo Marmol"],
        body="Existing prose about Marcelo.",
    )
    path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    page.save(path)
    return tmp_path


def _page_path(workspace: Path) -> Path:
    return workspace / "memory" / "entities" / "person" / "marcelo.md"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_add_new_attribute(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/email",
             "value": "m@x.com", "provenance": "episodic/foo.md"},
        ],
        body_delta="",
        commit_message="subject\n\nSources: foo",
    )
    result = apply_dream_output(
        workspace=workspace,
        entity_ref="person:marcelo",
        parsed=output,
    )
    assert isinstance(result, DreamApplyResult)
    assert result.failure_kind is None
    assert result.ops_applied == 1

    reloaded = EntityPage.from_file(_page_path(workspace))
    assert reloaded is not None
    assert reloaded.attributes == {"email": "m@x.com"}
    # Provenance is recorded by the applier (not by the LLM-emitted patch).
    assert reloaded.provenance["attributes"]["email"] == {
        "source_ref": "episodic/foo.md",
    }


def test_replace_existing_attribute(workspace: Path) -> None:
    page = EntityPage.from_file(_page_path(workspace))
    assert page is not None
    page.attributes = {"email": "old@x.com"}
    page.save(_page_path(workspace))

    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "replace", "path": "/attributes/email",
             "value": "new@x.com", "provenance": "episodic/bar.md"},
        ],
        body_delta="",
        commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind is None
    reloaded = EntityPage.from_file(_page_path(workspace))
    assert reloaded is not None
    assert reloaded.attributes == {"email": "new@x.com"}


def test_add_relation_via_dash(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/relations/-",
             "value": {"to": "person:susana", "type": "spouse",
                       "since": 2010},
             "provenance": "episodic/foo.md"},
        ],
        body_delta="",
        commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind is None
    reloaded = EntityPage.from_file(_page_path(workspace))
    assert reloaded is not None
    assert reloaded.relations == [
        {"to": "person:susana", "type": "spouse", "since": 2010},
    ]


def test_body_delta_appended(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[],
        body_delta="An extra paragraph.",
        commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind is None
    reloaded = EntityPage.from_file(_page_path(workspace))
    assert reloaded is not None
    assert "Existing prose about Marcelo." in reloaded.body
    assert "An extra paragraph." in reloaded.body


def test_empty_patch_and_body_is_noop_success(workspace: Path) -> None:
    """Rule 8: an empty output is a successful no-op. The runner
    advances the cursor; the file is unchanged."""
    before = _page_path(workspace).read_text(encoding="utf-8")
    output = ParsedDreamOutput(
        patch_ops=[], body_delta="", commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind is None
    assert result.ops_applied == 0
    after = _page_path(workspace).read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# Validation failures — patch rejected before any disk write
# ---------------------------------------------------------------------------


def test_missing_provenance_rejected(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1},
        ],
        body_delta="", commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind == DreamApplyFailureKind.VALIDATION
    assert "provenance" in (result.error_message or "").lower()


def test_forbidden_root_rejected(workspace: Path) -> None:
    """Ops targeting `/dream_processed_through` (an internal field)
    must be rejected."""
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "replace", "path": "/dream_processed_through",
             "value": "spoofed", "provenance": "p"},
        ],
        body_delta="", commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind == DreamApplyFailureKind.VALIDATION


def test_unknown_op_rejected(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "frobnicate", "path": "/attributes/x",
             "value": 1, "provenance": "p"},
        ],
        body_delta="", commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind == DreamApplyFailureKind.VALIDATION


def test_validation_failure_leaves_file_untouched(workspace: Path) -> None:
    before = _page_path(workspace).read_text(encoding="utf-8")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1},  # no provenance
        ],
        body_delta="", commit_message="subject",
    )
    apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    after = _page_path(workspace).read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# Round-trip + rollback
# ---------------------------------------------------------------------------


def test_jsonpatch_runtime_error_rolls_back(workspace: Path) -> None:
    """`replace` on a non-existent path is a JSON Patch runtime error
    (the lib raises). Apply rolls back to the original file."""
    before = _page_path(workspace).read_text(encoding="utf-8")
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "replace", "path": "/attributes/missing_key",
             "value": "x", "provenance": "p"},
        ],
        body_delta="", commit_message="subject",
    )
    result = apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    assert result.failure_kind in {
        DreamApplyFailureKind.PATCH_RUNTIME,
        DreamApplyFailureKind.VALIDATION,
    }
    after = _page_path(workspace).read_text(encoding="utf-8")
    assert before == after
    # `.md.bak` cleaned up — failure should not leave sidecars.
    bak = _page_path(workspace).with_suffix(".md.bak")
    assert not bak.exists()


def test_successful_apply_cleans_up_bak(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/email",
             "value": "m@x.com", "provenance": "p"},
        ],
        body_delta="", commit_message="subject",
    )
    apply_dream_output(
        workspace=workspace, entity_ref="person:marcelo", parsed=output,
    )
    bak = _page_path(workspace).with_suffix(".md.bak")
    assert not bak.exists()


# ---------------------------------------------------------------------------
# Missing target page
# ---------------------------------------------------------------------------


def test_missing_entity_page_raises(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[
            {"op": "add", "path": "/attributes/x", "value": 1,
             "provenance": "p"},
        ],
        body_delta="", commit_message="subject",
    )
    with pytest.raises(DreamApplyError):
        apply_dream_output(
            workspace=workspace,
            entity_ref="person:nobody",
            parsed=output,
        )


def test_malformed_entity_ref_raises(workspace: Path) -> None:
    output = ParsedDreamOutput(
        patch_ops=[], body_delta="", commit_message="subject",
    )
    with pytest.raises(DreamApplyError):
        apply_dream_output(
            workspace=workspace,
            entity_ref="no_colon_here",
            parsed=output,
        )
