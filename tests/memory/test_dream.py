"""Tests for `durin.memory.dream` — DreamConsolidator vertical slice."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from durin.memory.aliases_index import AliasIndex
from durin.memory.dream import (
    ConsolidationResult,
    DreamConsolidator,
    DreamError,
    EntryRef,
)
from durin.memory.entity_page import EntityPage
from durin.utils.git_repo import GitRepo


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


def _llm_with_response(text: str):
    """Build a stub llm_invoke that returns *text* verbatim."""

    def stub(prompt: str, *, model: str) -> str:
        # Record last prompt for tests that want to inspect it.
        stub.last_prompt = prompt
        stub.last_model = model
        return text

    return stub


def _well_formed_response(
    *,
    entity_ref: str = "person:marcelo",
    page_body: str = "## Current State\nMarcelo is the user.\n",
    cursor_after: int = 100,
    sources: list[str] | None = None,
) -> str:
    """Produce a syntactically correct LLM response for tests.

    Built via concatenation (NOT textwrap.dedent) because dedent breaks
    when interpolated multi-line variables have their own indentation
    that differs from the f-string template.
    """
    sources = sources or ["episodic/abc.md", "episodic/def.md"]
    type_, slug = entity_ref.split(":", 1)
    sources_str = ", ".join(sources)
    return (
        "===PAGE===\n"
        "---\n"
        f"type: {type_}\n"
        "name: Marcelo Marmol\n"
        "aliases: [Marcelo, marcelo]\n"
        f"dream_processed_through: {cursor_after}\n"
        "---\n"
        "\n"
        "# Marcelo Marmol\n"
        "\n"
        f"{page_body}\n"
        "===COMMIT===\n"
        f"Consolidate {entity_ref} (rev 1)\n"
        "\n"
        "Initial consolidation from N observations.\n"
        "\n"
        f"Sources: {sources_str}\n"
        f"Entities-touched: {entity_ref}\n"
        f"Cursor-after: {cursor_after}\n"
        "===END===\n"
    )


@pytest.fixture
def consolidator(tmp_path: Path) -> DreamConsolidator:
    """A DreamConsolidator wired to tmp_path with a stub LLM."""
    return DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_llm_with_response(_well_formed_response()),
    )


# ---------------------------------------------------------------------------
# consolidate_entity — prompt building + response parsing
# ---------------------------------------------------------------------------


class TestConsolidateEntity:
    def test_happy_path(self, consolidator: DreamConsolidator) -> None:
        entries = [
            EntryRef(id="e1", timestamp="2026-04-10", text="Marcelo said X"),
            EntryRef(id="e2", timestamp="2026-04-11", text="Marcelo prefers Y"),
        ]
        result = consolidator.consolidate_entity("person:marcelo", entries)
        assert isinstance(result, ConsolidationResult)
        assert "---" in result.page_text  # frontmatter present
        assert "name: Marcelo Marmol" in result.page_text
        assert result.commit_subject == "Consolidate person:marcelo (rev 1)"
        assert result.commit_body
        assert result.commit_trailers["Cursor-after"] == ["100"]

    def test_empty_entries_raises(self, consolidator: DreamConsolidator) -> None:
        with pytest.raises(DreamError, match="no entries"):
            consolidator.consolidate_entity("person:marcelo", [])

    def test_bad_entity_ref_raises(self, consolidator: DreamConsolidator) -> None:
        with pytest.raises(DreamError, match="entity_ref"):
            consolidator.consolidate_entity(
                "marcelo",  # missing type:
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_prompt_includes_entries(self, tmp_path: Path) -> None:
        stub = _llm_with_response(_well_formed_response())
        c = DreamConsolidator(workspace=tmp_path, llm_invoke=stub)
        entries = [
            EntryRef(id="e1", timestamp="2026-04-10",
                     text="Marcelo prefers pytest"),
            EntryRef(id="e2", timestamp="2026-04-11",
                     text="Marcelo said unittest is too verbose"),
        ]
        c.consolidate_entity("person:marcelo", entries)
        # The stub captured the prompt
        assert "Marcelo prefers pytest" in stub.last_prompt
        assert "Marcelo said unittest" in stub.last_prompt
        assert "person:marcelo" in stub.last_prompt

    def test_prompt_includes_existing_page_when_present(
        self, tmp_path: Path
    ) -> None:
        # Pre-existing page on disk
        page = EntityPage(
            type="person", name="Marcelo", aliases=["marcelo"],
            body="## Prior state\nPrior fact about Marcelo.\n",
            dream_processed_through=50,
        )
        page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
        stub = _llm_with_response(_well_formed_response())
        c = DreamConsolidator(workspace=tmp_path, llm_invoke=stub)
        c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-12", text="new fact")],
        )
        # Prior page contents should land in the prompt
        assert "Prior fact about Marcelo" in stub.last_prompt


# ---------------------------------------------------------------------------
# Response parsing edge cases
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_missing_markers_raises(self, consolidator: DreamConsolidator) -> None:
        # Override the stub to return malformed text
        consolidator._llm_invoke = _llm_with_response("just prose without markers")
        with pytest.raises(DreamError, match="markers"):
            consolidator.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_strips_outer_code_fence(self, tmp_path: Path) -> None:
        """LLMs often wrap the whole output in ```...``` — must be tolerated."""
        wrapped = "```\n" + _well_formed_response() + "\n```\n"
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=_llm_with_response(wrapped),
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        assert "name: Marcelo Marmol" in result.page_text


# ---------------------------------------------------------------------------
# apply — disk + git + alias index integration
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_writes_page_and_commits(
        self, tmp_path: Path, consolidator: DreamConsolidator
    ) -> None:
        result = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        sha = consolidator.apply("person:marcelo", result)
        # File written
        page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        assert page_path.exists()
        # Commit exists in git
        assert sha is not None and len(sha) == 40
        repo = GitRepo(tmp_path / "memory")
        commits = repo.log()
        # Newest commit should match — older = initial empty commit from init
        latest = commits[0]
        assert latest.sha == sha
        assert latest.subject == "Consolidate person:marcelo (rev 1)"
        assert latest.trailers["Cursor-after"] == ["100"]

    def test_apply_idempotent_on_identical_content(
        self, tmp_path: Path, consolidator: DreamConsolidator
    ) -> None:
        result = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        sha1 = consolidator.apply("person:marcelo", result)
        assert sha1 is not None
        # Second apply with same content → no new commit
        sha2 = consolidator.apply("person:marcelo", result)
        assert sha2 is None

    def test_apply_updates_alias_index(
        self, tmp_path: Path, consolidator: DreamConsolidator
    ) -> None:
        result = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        consolidator.apply("person:marcelo", result)
        # Alias index should now contain marcelo's aliases mapping to the ref.
        # Rebuild from disk (per doc 23 T1.4: no persistent sidecar).
        idx = AliasIndex(tmp_path / "memory")
        idx.build()
        assert idx.lookup("Marcelo") == ["person:marcelo"]
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.lookup("Marcelo Marmol") == ["person:marcelo"]

    def test_apply_creates_git_repo_if_missing(
        self, tmp_path: Path, consolidator: DreamConsolidator
    ) -> None:
        result = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        consolidator.apply("person:marcelo", result)
        assert (tmp_path / "memory" / ".git").exists()


# ---------------------------------------------------------------------------
# C.2 safety nets: retry, context budget, cursor force, body shrink check
# ---------------------------------------------------------------------------


class TestSafetyNets:
    def test_retry_on_parse_failure_then_succeeds(self, tmp_path: Path) -> None:
        """G10: parse failure → retry; second attempt succeeds."""
        responses = [
            "garbage not a valid response",  # first attempt fails
            _well_formed_response(),         # second succeeds
        ]
        call_count = [0]

        def stub(prompt: str, *, model: str) -> str:
            i = call_count[0]
            call_count[0] += 1
            return responses[i] if i < len(responses) else responses[-1]

        c = DreamConsolidator(workspace=tmp_path, llm_invoke=stub)
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        assert result.page_text
        assert call_count[0] == 2  # one failure + one success

    def test_retry_exhausts_after_max_attempts(self, tmp_path: Path) -> None:
        """After MAX_RETRIES (3) failed attempts, raise DreamError."""
        def always_fail(prompt: str, *, model: str) -> str:
            return "no valid response ever"

        c = DreamConsolidator(workspace=tmp_path, llm_invoke=always_fail)
        with pytest.raises(DreamError, match="3 attempts"):
            c.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_context_budget_caps_entries(self, tmp_path: Path) -> None:
        """G11: when N > MAX_ENTRIES_PER_CALL, take newest by timestamp."""
        captured_prompt = []

        def capturing(prompt: str, *, model: str) -> str:
            captured_prompt.append(prompt)
            return _well_formed_response()

        c = DreamConsolidator(workspace=tmp_path, llm_invoke=capturing)
        # Build 60 entries with zero-padded ids so substring match is reliable.
        # Newest first by timestamp — e59 is the latest.
        entries = [
            EntryRef(id=f"entry-{i:03d}", timestamp=f"2026-04-{(i%28)+1:02d}",
                     text=f"obs {i}")
            for i in range(60)
        ]
        c.consolidate_entity("person:marcelo", entries)
        prompt = captured_prompt[0]
        ids_in_prompt = [f"entry-{i:03d}" for i in range(60)
                         if f"entry-{i:03d}" in prompt]
        assert len(ids_in_prompt) == DreamConsolidator.MAX_ENTRIES_PER_CALL

    def test_cursor_force_set_overrides_llm(self, tmp_path: Path) -> None:
        """G2: apply() forces cursor to batch_last_ts regardless of LLM output."""
        # LLM puts Cursor-after: 999 in trailers, but our batch ends at 100.
        # apply() should override to 100.
        response_with_high_cursor = (
            "===PAGE===\n"
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: [marcelo]\n"
            "dream_processed_through: 999\n"
            "---\n"
            "# Marcelo\n"
            "body\n"
            "===COMMIT===\n"
            "Consolidate person:marcelo (rev 1)\n"
            "\n"
            "body\n"
            "\n"
            "Sources: e1\n"
            "Entities-touched: person:marcelo\n"
            "Cursor-after: 999\n"
            "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: response_with_high_cursor,
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        assert result.batch_last_ts == "2026-04-10"
        c.apply("person:marcelo", result)
        # The on-disk page must have the batch ts as cursor, NOT 999.
        page = EntityPage.from_file(
            tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        )
        assert page.dream_processed_through == "2026-04-10"

    def test_body_shrink_rejected(self, tmp_path: Path) -> None:
        """G7: if new body shrinks >50% vs current page, raise DreamError."""
        # Create a page with substantial body.
        existing = EntityPage(
            type="person",
            name="Marcelo",
            aliases=["marcelo"],
            body=(
                "## Background\n" + "x" * 500 + "\n\n"
                "## Notes\n" + "y" * 500
            ),
        )
        existing.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

        # LLM returns a tiny body (shrunk way more than 50%).
        shrunk_response = (
            "===PAGE===\n"
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: [marcelo]\n"
            "---\n"
            "# Marcelo\n"
            "tiny\n"
            "===COMMIT===\n"
            "Consolidate (rev 2)\n\nbody\n\nSources: e1\n"
            "Entities-touched: person:marcelo\nCursor-after: 100\n"
            "===END===\n"
        )

        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: shrunk_response,
        )
        with pytest.raises(DreamError, match="shrank"):
            c.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_oversized_page_rejected(self, tmp_path: Path) -> None:
        """Page exceeding PAGE_MAX_BYTES raises DreamError."""
        huge_body = "x" * (DreamConsolidator.PAGE_MAX_BYTES + 100)
        oversized = (
            "===PAGE===\n"
            "---\n"
            "type: person\n"
            "name: Marcelo\n"
            "aliases: []\n"
            "---\n"
            f"# Marcelo\n{huge_body}\n"
            "===COMMIT===\n"
            "Consolidate (rev 1)\n\nbody\n\nSources: e1\n"
            "Entities-touched: person:marcelo\n"
            "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: oversized,
        )
        with pytest.raises(DreamError, match="exceeds"):
            c.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_invalid_yaml_rejected(self, tmp_path: Path) -> None:
        """Page that doesn't parse as EntityPage raises DreamError after retries."""
        invalid_yaml = (
            "===PAGE===\n"
            "---\n"
            "type: not-a-valid-page  # no name field\n"
            "---\n"
            "body\n"
            "===COMMIT===\n"
            "Consolidate (rev 1)\n\nbody\n\nSources: e1\n"
            "Entities-touched: person:marcelo\n"
            "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: invalid_yaml,
        )
        with pytest.raises(DreamError):
            c.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )


# ---------------------------------------------------------------------------
# Real end-to-end with glm-5.1 (skipped unless ZHIPU key configured)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("DURIN_E2E_DREAM", "").lower() not in {"1", "true", "yes"},
    reason="set DURIN_E2E_DREAM=1 to run live LLM end-to-end test (~30s, costs credits)",
)
def test_end_to_end_with_real_llm(tmp_path: Path) -> None:
    """Real LLM call. Exercises the whole pipeline.

    Setup:
    - Seed 3 synthetic entries about person:marcelo.
    - Run consolidate_entity + apply.
    - Verify on disk: entity page, git commit with trailers, alias index.

    Run with::

        DURIN_E2E_DREAM=1 .venv/bin/python -m pytest \\
            tests/memory/test_dream.py::test_end_to_end_with_real_llm -v -s
    """
    entries = [
        EntryRef(
            id="2026-04-10-001",
            timestamp="2026-04-10",
            text="Marcelo Marmol pidió que el agente envíe emails desde aule@mxhero.com",
            entities=["person:marcelo", "project:mxhero"],
        ),
        EntryRef(
            id="2026-04-11-002",
            timestamp="2026-04-11",
            text="Marcelo confirmó preferencia por pytest sobre unittest en una sesión técnica",
            entities=["person:marcelo", "topic:testing"],
        ),
        EntryRef(
            id="2026-04-12-003",
            timestamp="2026-04-12",
            text="Marcelo (mmarmol@mxhero.com) escribió desde el Slack #forge-work",
            entities=["person:marcelo"],
        ),
    ]
    consolidator = DreamConsolidator(workspace=tmp_path, model="glm-5.1")
    result = consolidator.consolidate_entity("person:marcelo", entries)

    # Sanity checks on parsed output
    assert "type: person" in result.page_text
    assert "Marcelo" in result.page_text
    assert "Consolidate person:marcelo" in result.commit_subject

    # Apply and verify disk state
    sha = consolidator.apply("person:marcelo", result)
    assert sha is not None

    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    assert page_path.exists()
    parsed = EntityPage.from_file(page_path)
    assert parsed is not None
    assert parsed.type == "person"
    assert any("Marcelo" in a for a in parsed.aliases) or parsed.name.startswith("Marcelo")

    # Git log carries the structured trailers
    repo = GitRepo(tmp_path / "memory")
    commits = repo.log()
    latest = commits[0]
    assert "Sources" in latest.trailers
    assert "Entities-touched" in latest.trailers

    # Alias index populated (rebuild from disk per doc 23 T1.4)
    idx = AliasIndex(tmp_path / "memory")
    idx.build()
    candidates = idx.lookup("Marcelo")
    assert "person:marcelo" in candidates

    print()
    print(f"=== Real E2E output (page) ===\n{result.page_text}\n")
    print(f"=== Commit message ===\n{result.commit_subject}\n\n{result.commit_body}\n")
    print(f"=== Trailers ===\n{result.commit_trailers}\n")
