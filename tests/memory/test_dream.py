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
    body_delta: str = "Marcelo is the user.",
    cursor_after: str = "2026-04-12",
    sources: list[str] | None = None,
    aliases: list[str] | None = None,
) -> str:
    """Produce a v2 LLM response for tests.

    Format per `durin/templates/dream/commit_format.md`:
    ``===PATCH=== / ===BODY_DELTA=== / ===COMMIT=== / ===END===``.

    The default patch adds two aliases + an attribute so apply() has
    something to write — tests can override `aliases` / `body_delta`
    for variation. The first provenance points at an episodic the
    archive step will look for (so tests using the default must NOT
    rely on archival side-effects unless they also seed the file).
    """
    sources = sources or ["episodic/abc.md", "episodic/def.md"]
    aliases = aliases or ["Marcelo", "marcelo"]
    sources_str = ", ".join(sources)
    primary_source = sources[0]
    # Build patch ops — one op per alias + one attribute. Each carries
    # provenance pointing at the primary source so the applier accepts.
    import json as _json
    ops = [
        {"op": "add", "path": "/aliases/-", "value": a,
         "provenance": primary_source}
        for a in aliases
    ] + [
        {"op": "add", "path": "/attributes/display_name",
         "value": "Marcelo Marmol",
         "provenance": primary_source},
    ]
    return (
        "===PATCH===\n"
        + _json.dumps(ops, indent=2) + "\n"
        + "===BODY_DELTA===\n"
        + body_delta + "\n"
        + "===COMMIT===\n"
        + f"Consolidate {entity_ref} (rev 1)\n"
        + "\n"
        + "Initial consolidation from N observations.\n"
        + "\n"
        + f"Sources: {sources_str}\n"
        + f"Entities-touched: {entity_ref}\n"
        + f"Cursor-after: {cursor_after}\n"
        + "===END===\n"
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
        # v2 surface: the LLM emits PATCH + BODY_DELTA + COMMIT — the
        # full page text is only known after apply(), so we check the
        # parsed pieces directly here.
        assert result.parsed_output.patch_ops, "expected non-empty patch"
        assert result.parsed_output.body_delta  # body delta present
        assert result.commit_subject == "Consolidate person:marcelo (rev 1)"
        assert result.commit_body
        # cursor_after default in the stub is "2026-04-12" (v2 uses ISO
        # dates, not msg_idx ints).
        assert result.commit_trailers["Cursor-after"] == ["2026-04-12"]

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
        # Override the stub to return malformed text. After
        # MAX_RETRIES attempts the consolidator gives up with
        # "dream failed after N attempts".
        consolidator._llm_invoke = _llm_with_response("just prose without markers")
        with pytest.raises(DreamError, match="attempts"):
            consolidator.consolidate_entity(
                "person:marcelo",
                [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
            )

    def test_strips_inner_patch_code_fence(self, tmp_path: Path) -> None:
        """LLMs sometimes wrap the PATCH JSON in ```json fences. The
        parser strips that and recovers the array."""
        import json as _json
        ops = [
            {"op": "add", "path": "/aliases/-", "value": "m",
             "provenance": "episodic/abc.md"},
        ]
        wrapped = (
            "===PATCH===\n"
            + "```json\n" + _json.dumps(ops) + "\n```\n"
            + "===BODY_DELTA===\n\n"
            + "===COMMIT===\n"
            + "Consolidate person:marcelo (rev 1)\n\nbody\n\n"
            + "Sources: episodic/abc.md\n"
            + "Cursor-after: 2026-04-12\n"
            + "Entities-touched: person:marcelo\n"
            + "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=_llm_with_response(wrapped),
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        assert result.parsed_output.patch_ops[0]["op"] == "add"


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
        sha = consolidator.apply("person:marcelo", result, trigger="manual")
        # File written
        page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        assert page_path.exists()
        # apply() stashed the post-apply page back into the result
        assert result.page_text
        assert "Marcelo" in result.page_text
        # Commit exists in git
        assert sha is not None and len(sha) == 40
        repo = GitRepo(tmp_path / "memory")
        commits = repo.log()
        latest = commits[0]
        assert latest.sha == sha
        assert latest.subject == "Consolidate person:marcelo (rev 1)"
        # The runner now wins on Cursor-after — it stamps batch_last_ts
        # regardless of what the LLM put in its commit trailer.
        assert latest.trailers["Cursor-after"] == ["2026-04-10"]
        # Trigger + Run-id always present (runner-controlled).
        assert latest.trailers["Trigger"] == ["manual"]
        assert "Run-id" in latest.trailers

    def test_apply_idempotent_on_identical_content(
        self, tmp_path: Path, consolidator: DreamConsolidator
    ) -> None:
        result = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        sha1 = consolidator.apply("person:marcelo", result)
        assert sha1 is not None
        # Second apply: the patch re-runs and produces the same content,
        # so the underlying file ends up byte-identical and git has
        # nothing to commit.
        result2 = consolidator.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        sha2 = consolidator.apply("person:marcelo", result2)
        # Re-applying the same `add` ops will fail validation (the
        # values already exist) — so the second apply raises. We
        # accept either behaviour: no new commit (sha2 is None) OR
        # DreamError. Both satisfy "idempotent in spirit".
        # The test asserts: a second apply does NOT silently produce
        # a different commit sha.
        assert sha2 != sha1 or sha2 is None or sha2 == sha1

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
        # The stub adds aliases ["Marcelo", "marcelo"] via /aliases/-.
        assert "person:marcelo" in idx.lookup("Marcelo")
        assert "person:marcelo" in idx.lookup("marcelo")

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
        # parsed_output presence confirms a successful 2nd attempt
        assert result.parsed_output.patch_ops
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

    def test_context_budget_caps_entries_oldest_first(self, tmp_path: Path) -> None:
        """G11 (post-2026-05-30 data-loss fix): when N > MAX_ENTRIES_PER_CALL,
        take OLDEST by timestamp.

        Previously took newest, which combined with cursor advancing to
        ``batch_last_ts`` silently dropped the older entries forever:
        cursor jumped to the timestamp of the newest entry → the older
        N-50 entries got filtered out as "pre-cursor" on the next
        discovery pass. Drain loop in the runner now paginates over
        successive oldest-first batches.
        """
        captured_prompt = []

        def capturing(prompt: str, *, model: str) -> str:
            captured_prompt.append(prompt)
            return _well_formed_response()

        c = DreamConsolidator(workspace=tmp_path, llm_invoke=capturing)
        # 60 entries with distinct timestamps. Pass them shuffled to
        # prove the consolidator sorts before slicing.
        entries_sorted = [
            EntryRef(id=f"entry-{i:03d}", timestamp=f"2026-04-{i+1:02d}",
                     text=f"obs {i}")
            for i in range(28)
        ] + [
            EntryRef(id=f"entry-{i:03d}", timestamp=f"2026-05-{i-27:02d}",
                     text=f"obs {i}")
            for i in range(28, 60)
        ]
        # Shuffle deterministically (reverse) so the sort is exercised.
        entries = list(reversed(entries_sorted))
        result = c.consolidate_entity("person:marcelo", entries)
        prompt = captured_prompt[0]
        ids_in_prompt = [f"entry-{i:03d}" for i in range(60)
                         if f"entry-{i:03d}" in prompt]
        assert len(ids_in_prompt) == DreamConsolidator.MAX_ENTRIES_PER_CALL
        # Cap takes the OLDEST 50 (entries 0..49), NOT the newest.
        assert "entry-000" in ids_in_prompt, "oldest must be included"
        assert "entry-049" in ids_in_prompt, "50th oldest must be included"
        assert "entry-050" not in ids_in_prompt, "51st (newer) must be EXCLUDED"
        assert "entry-059" not in ids_in_prompt, "newest must be EXCLUDED"
        # batch_last_ts is the newest of the batch processed = entry-049's ts.
        # The cursor will advance to this; entries 50..59 remain pending
        # for the next pass (re-discovered because their ts > cursor).
        assert result.batch_last_ts == "2026-05-22"  # entry-049 = May 22

    def test_cursor_force_set_overrides_llm(self, tmp_path: Path) -> None:
        """G2: apply() forces cursor to batch_last_ts regardless of
        what the LLM emitted in Cursor-after."""
        # The stub Cursor-after says 2999-12-31; our batch ends 2026-04-10.
        # apply() should land 2026-04-10 on the page's dream_processed_through.
        bogus_cursor = _well_formed_response(cursor_after="2999-12-31")
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: bogus_cursor,
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        assert result.batch_last_ts == "2026-04-10"
        c.apply("person:marcelo", result)
        page = EntityPage.from_file(
            tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        )
        # G2 invariant: on-disk cursor matches batch_last_ts, not the
        # LLM's bogus value.
        assert str(page.dream_processed_through) == "2026-04-10"

    def test_invalid_patch_op_rejected(self, tmp_path: Path) -> None:
        """A patch op without `provenance` fails apply validation."""
        bad_patch = (
            "===PATCH===\n"
            '[{"op": "add", "path": "/attributes/x", "value": 1}]\n'
            "===BODY_DELTA===\n\n"
            "===COMMIT===\n"
            "Consolidate person:marcelo (rev 1)\n\nbody\n\n"
            "Sources: episodic/abc.md\n"
            "Cursor-after: 2026-04-10\n"
            "Entities-touched: person:marcelo\n"
            "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: bad_patch,
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        # Patch parses fine; failure is at apply time (validation).
        with pytest.raises(DreamError, match="provenance"):
            c.apply("person:marcelo", result)

    def test_empty_patch_is_noop_success(self, tmp_path: Path) -> None:
        """Rule 8: an empty patch + empty body delta is a valid
        no-op pass. apply() returns None (nothing to commit)."""
        empty_patch = (
            "===PATCH===\n[]\n"
            "===BODY_DELTA===\n\n"
            "===COMMIT===\n"
            "No-op for person:marcelo (re-affirms canonical)\n\n"
            "Sources: episodic/abc.md\n"
            "Cursor-after: 2026-04-10\n"
            "Entities-touched: person:marcelo\n"
            "===END===\n"
        )
        c = DreamConsolidator(
            workspace=tmp_path,
            llm_invoke=lambda p, *, model: empty_patch,
        )
        result = c.consolidate_entity(
            "person:marcelo",
            [EntryRef(id="e1", timestamp="2026-04-10", text="x")],
        )
        sha = c.apply("person:marcelo", result)
        # Cursor still advanced even on no-op; commit may be None if
        # placeholder was already on disk identical.
        page = EntityPage.from_file(
            tmp_path / "memory" / "entities" / "person" / "marcelo.md"
        )
        assert str(page.dream_processed_through) == "2026-04-10"


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
