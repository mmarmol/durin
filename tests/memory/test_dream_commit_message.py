"""Hybrid commit-message post-processor for Dream apply.

Per `docs/memory/05_dream_cold_path.md` §11 + `docs/memory/06_prompts_and_instructions.md` §4.5:

The LLM emits a `===COMMIT===` section with subject + optional body +
`Sources:` / `Cursor-after:` / `Entities-touched:` trailers. The
runner is responsible for:

  - Always appending `Trigger:` and `Run-id:` (known by runner only).
  - Verifying the three LLM-emitted trailers are present; filling
    them from runner state when missing, logging a warning.
  - Never blocking the commit on missing trailers — auto-fill is the
    safety net.

This module is pure text: takes the LLM commit string + runner state,
returns the final message ready for ``git commit -m``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from durin.memory.dream_commit_message import (
    CommitTrailers,
    finalize_commit_message,
)


# ---------------------------------------------------------------------------
# Happy path — LLM supplied all trailers
# ---------------------------------------------------------------------------


def test_appends_trigger_and_runid_when_llm_supplied_all() -> None:
    llm_message = (
        "Update Marcelo's email\n"
        "\n"
        "The May 26 standup confirmed the new work email.\n"
        "\n"
        "Sources: episodic/foo.md\n"
        "Cursor-after: 2026-05-26T08:45:00Z\n"
        "Entities-touched: person:marcelo\n"
    )
    out = finalize_commit_message(
        llm_message,
        trailers=CommitTrailers(
            sources=["episodic/foo.md"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:marcelo",
            trigger="threshold",
            run_id="abc-123",
        ),
    )
    assert "Update Marcelo's email" in out
    assert "Sources: episodic/foo.md" in out
    assert "Cursor-after: 2026-05-26T08:45:00Z" in out
    assert "Entities-touched: person:marcelo" in out
    assert "Trigger: threshold" in out
    assert "Run-id: abc-123" in out


# ---------------------------------------------------------------------------
# Auto-fill when LLM omits one or more of its trailers
# ---------------------------------------------------------------------------


def test_fills_missing_sources_trailer() -> None:
    llm_message = (
        "Update foo\n"
        "\n"
        "Cursor-after: 2026-05-26T08:45:00Z\n"
        "Entities-touched: person:m\n"
    )
    out = finalize_commit_message(
        llm_message,
        trailers=CommitTrailers(
            sources=["episodic/a.md", "episodic/b.md"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    assert "Sources: episodic/a.md, episodic/b.md" in out


def test_fills_missing_cursor_after() -> None:
    llm_message = (
        "Subject\n\nSources: x\nEntities-touched: person:m\n"
    )
    out = finalize_commit_message(
        llm_message,
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    assert "Cursor-after: 2026-05-26T08:45:00Z" in out


def test_fills_missing_entities_touched() -> None:
    llm_message = (
        "Subject\n\nSources: x\nCursor-after: 2026-05-26T08:45:00Z\n"
    )
    out = finalize_commit_message(
        llm_message,
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    assert "Entities-touched: person:m" in out


def test_appends_trigger_and_runid_even_when_llm_added_them() -> None:
    """The LLM is instructed not to emit Trigger / Run-id, but if it
    sneaks them in we override with runner values (auth source)."""
    llm_message = (
        "Subject\n\n"
        "Sources: x\n"
        "Cursor-after: 2026-05-26T08:45:00Z\n"
        "Entities-touched: person:m\n"
        "Trigger: hallucinated_value\n"
        "Run-id: hallucinated_run\n"
    )
    out = finalize_commit_message(
        llm_message,
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="threshold",
            run_id="real-run-id",
        ),
    )
    assert "Trigger: threshold" in out
    assert "Run-id: real-run-id" in out
    assert "Trigger: hallucinated_value" not in out
    assert "Run-id: hallucinated_run" not in out


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_message_still_produces_minimum_trailers() -> None:
    out = finalize_commit_message(
        "",
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    # Required trailers must surface even with an empty subject.
    assert "Sources: x" in out
    assert "Trigger: manual" in out
    assert "Run-id: r1" in out


def test_subject_only_message() -> None:
    out = finalize_commit_message(
        "Just a subject",
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    assert out.startswith("Just a subject")
    assert "Sources: x" in out


# ---------------------------------------------------------------------------
# Order — trailers should be grep-able and appear in a stable block
# ---------------------------------------------------------------------------


def test_trailers_appear_in_canonical_order() -> None:
    out = finalize_commit_message(
        "Subject\n",
        trailers=CommitTrailers(
            sources=["x"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    # Trailers block: Sources → Cursor-after → Entities-touched →
    # Trigger → Run-id. Stable order so `git log --grep` recipes
    # work consistently.
    idx_sources = out.index("Sources:")
    idx_cursor = out.index("Cursor-after:")
    idx_ents = out.index("Entities-touched:")
    idx_trigger = out.index("Trigger:")
    idx_runid = out.index("Run-id:")
    assert idx_sources < idx_cursor < idx_ents < idx_trigger < idx_runid


def test_sources_list_serialized_comma_separated() -> None:
    out = finalize_commit_message(
        "Subject\n",
        trailers=CommitTrailers(
            sources=["episodic/a.md", "episodic/b.md", "episodic/c.md"],
            cursor_after="2026-05-26T08:45:00Z",
            entities_touched="person:m",
            trigger="manual",
            run_id="r1",
        ),
    )
    assert "Sources: episodic/a.md, episodic/b.md, episodic/c.md" in out
