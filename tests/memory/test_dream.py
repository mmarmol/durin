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
