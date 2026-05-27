"""Tests for `durin memory` CLI subcommand."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.memory_cmd import memory_app
from durin.memory.dream import ConsolidationResult, DreamConsolidator, EntryRef


runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI escapes. Needed when CI runs with FORCE_COLOR=1 and
    rich/typer styles substrings like ``<type>`` and ``<slug>`` separately,
    breaking naive ``in`` checks against the rendered output.
    """
    return _ANSI_RE.sub("", text)


@pytest.fixture
def populated_workspace(tmp_path: Path) -> Path:
    """Workspace with one consolidated entity page + git history.

    Returns workspace root. Patches load_config so the CLI sees this
    workspace.
    """
    # Build a real page + history via DreamConsolidator with a stub LLM.
    consolidator = DreamConsolidator(
        workspace=tmp_path,
        llm_invoke=_make_stub_llm("person:marcelo", rev=1),
    )
    result = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e1", timestamp="2026-04-10", text="initial observation")],
    )
    consolidator.apply("person:marcelo", result)

    # Second consolidation — rev 2
    consolidator._llm_invoke = _make_stub_llm("person:marcelo", rev=2)
    result2 = consolidator.consolidate_entity(
        "person:marcelo",
        [EntryRef(id="e2", timestamp="2026-04-15", text="follow-up observation")],
    )
    consolidator.apply("person:marcelo", result2)
    return tmp_path


def _make_stub_llm(entity_ref: str, *, rev: int):
    type_, slug = entity_ref.split(":", 1)
    response = (
        "===PAGE===\n"
        "---\n"
        f"type: {type_}\n"
        "name: Marcelo Marmol\n"
        "aliases: [Marcelo, marcelo]\n"
        f"dream_processed_through: {rev * 10}\n"
        "---\n"
        "\n"
        "# Marcelo Marmol\n"
        "\n"
        f"## Current State (rev {rev})\n"
        f"Observation set {rev}.\n"
        "===COMMIT===\n"
        f"Consolidate {entity_ref} (rev {rev})\n"
        "\n"
        f"Consolidation pass {rev} merging recent observations.\n"
        "\n"
        f"Sources: e{rev}\n"
        f"Entities-touched: {entity_ref}\n"
        "Entities-referenced: project:durin\n"
        f"Cursor-after: {rev * 10}\n"
        "===END===\n"
    )

    def stub(prompt: str, *, model: str) -> str:
        return response

    return stub


def _patch_workspace(workspace: Path):
    """Patch _workspace_root() so the CLI sees `workspace` instead of ~/.durin/."""
    return patch("durin.cli.memory_cmd._workspace_root", return_value=workspace)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_lists_commits(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "person:marcelo"])
    assert result.exit_code == 0, result.output
    assert "person:marcelo" in result.output
    assert "rev 1" in result.output
    assert "rev 2" in result.output


def test_history_for_unknown_entity_shows_empty(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "person:nobody"])
    assert result.exit_code == 0
    assert "No history" in result.output


def test_history_rejects_bad_entity_format(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["history", "no-colon"])
    assert result.exit_code != 0
    assert "type" in result.output.lower() or "slug" in result.output.lower()


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_head_outputs_current_page(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["show", "person:marcelo"])
    assert result.exit_code == 0
    # rev 2 is HEAD, so we should see rev 2 body
    assert "rev 2" in result.output
    assert "Marcelo" in result.output


def _commit_sha_for(workspace: Path, subject_contains: str) -> str:
    """Helper: get commit SHA directly from git, not from CLI output."""
    from durin.utils.git_repo import GitRepo

    repo = GitRepo(workspace / "memory")
    for c in repo.log():
        if subject_contains in c.subject:
            return c.sha
    raise RuntimeError(f"no commit matching {subject_contains!r}")


def test_show_at_specific_revision(populated_workspace: Path) -> None:
    """Show the page at the first revision."""
    old_sha = _commit_sha_for(populated_workspace, "rev 1")
    with _patch_workspace(populated_workspace):
        result = runner.invoke(
            memory_app, ["show", "person:marcelo", "--rev", old_sha],
        )
    assert result.exit_code == 0, result.output
    # Should show rev 1 content, not rev 2
    assert "rev 1" in result.output
    assert "rev 2" not in result.output


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_rejects_bad_revs_format(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["diff", "person:marcelo", "not-a-range"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_shows_page_metadata_and_history(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["expand", "person:marcelo"])
    assert result.exit_code == 0, result.output
    assert "Marcelo Marmol" in result.output
    assert "History" in result.output
    assert "Sources" in result.output
    assert "Related entities" in result.output
    assert "project:durin" in result.output  # from Entities-referenced trailer


def test_expand_missing_page_fails(populated_workspace: Path) -> None:
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["expand", "person:nobody"])
    assert result.exit_code != 0
    assert "No page" in result.output


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


def test_revert_with_yes_runs_git_revert(populated_workspace: Path) -> None:
    """Doc 25 §2.D: revert now actually invokes `git revert` via
    subprocess instead of printing guidance. The system `git` binary
    is required (durin doctor warns when missing); skip the test if
    not present rather than expanding scope of the test fixture."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git binary not in PATH")
    sha = _commit_sha_for(populated_workspace, "rev 2")
    with _patch_workspace(populated_workspace):
        result = runner.invoke(memory_app, ["revert", sha, "--yes"])
    assert result.exit_code == 0, result.output
    assert "Reverted" in result.output


# ---------------------------------------------------------------------------
# uninitialized repo handling
# ---------------------------------------------------------------------------


def test_history_on_uninitialized_repo(tmp_path: Path) -> None:
    """When memory/.git/ doesn't exist, history reports clean error."""
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["history", "person:marcelo"])
    assert result.exit_code != 0
    assert "not been initialized" in result.output


# ---------------------------------------------------------------------------
# dream — manual consolidation (Cluster D + G3 datetime cursor compare)
# ---------------------------------------------------------------------------


def test_dream_no_episodic_yet_clean_exit(tmp_path: Path) -> None:
    """Empty workspace: dream exits cleanly with informative message."""
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["dream"])
    assert result.exit_code == 0
    assert "nothing to dream" in result.output.lower()


def test_dream_no_pending_when_no_tags(tmp_path: Path) -> None:
    """Entries with no entity tags: nothing to consolidate."""
    from durin.memory.store import store_memory

    store_memory(tmp_path, content="untagged content", entities=[])
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["dream"])
    assert result.exit_code == 0
    assert "no pending" in result.output.lower()


def test_dream_dry_run_lists_pending(tmp_path: Path) -> None:
    """--dry-run shows what would be consolidated without writing."""
    from durin.memory.store import store_memory

    for i in range(3):
        store_memory(
            tmp_path,
            content=f"observation about marcelo #{i}",
            entities=["person:marcelo"],
        )

    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["dream", "--dry-run"])
    assert result.exit_code == 0
    assert "person:marcelo" in result.output
    assert "3 entries" in result.output
    # Verify nothing was actually consolidated (no entity page on disk)
    page_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    assert not page_path.exists()


def test_dream_filters_by_entity(tmp_path: Path) -> None:
    """Passing an entity argument filters discovery to that one entity."""
    from durin.memory.store import store_memory

    store_memory(tmp_path, content="about marcelo",
                 entities=["person:marcelo"])
    store_memory(tmp_path, content="about durin",
                 entities=["project:durin"])

    with _patch_workspace(tmp_path):
        result = runner.invoke(
            memory_app, ["dream", "person:marcelo", "--dry-run"],
        )
    assert result.exit_code == 0
    assert "person:marcelo" in result.output
    assert "project:durin" not in result.output


def test_dream_g3_datetime_cursor_filters_correctly(tmp_path: Path) -> None:
    """G3: pre-cursor entries (timestamp <= cursor) are excluded.

    Mixes date-only and datetime cursors to ensure datetime parsing
    is used, not string comparison.
    """
    from datetime import date
    from durin.memory.entity_page import EntityPage
    from durin.memory.store import store_memory

    # Build an existing page with cursor at 2026-04-15.
    page = EntityPage(
        type="person",
        name="Marcelo",
        aliases=["marcelo"],
        dream_processed_through="2026-04-15",
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

    # Entry on 2026-04-10 — BEFORE cursor → should be excluded.
    store_memory(
        tmp_path,
        content="old observation pre-cursor",
        entities=["person:marcelo"],
        valid_from=date(2026, 4, 10),
    )
    # Entry on 2026-04-20 — AFTER cursor → should be included.
    store_memory(
        tmp_path,
        content="fresh observation post-cursor",
        entities=["person:marcelo"],
        valid_from=date(2026, 4, 20),
    )

    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["dream", "--dry-run"])
    assert result.exit_code == 0
    # Only the post-cursor (1 entry) should be pending.
    assert "1 entries" in result.output
    assert "fresh observation" in result.output
    assert "old observation" not in result.output


# ---------------------------------------------------------------------------
# absorb — W4 (doc 24): expose EntityAbsorption via CLI
# ---------------------------------------------------------------------------


def _write_entity_page(
    workspace: Path,
    type_: str,
    slug: str,
    *,
    aliases: list[str],
    name: str | None = None,
) -> Path:
    from durin.memory.entity_page import EntityPage

    page = EntityPage(type=type_, name=name or slug, aliases=aliases)
    path = workspace / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def test_absorb_suggest_empty_when_no_overlap(tmp_path: Path) -> None:
    """No shared aliases across pages → no candidates reported."""
    _write_entity_page(tmp_path, "person", "marcelo", aliases=["Marcelo"])
    _write_entity_page(tmp_path, "project", "durin", aliases=["Durin"])
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["absorb-suggest"])
    assert result.exit_code == 0
    assert "No merge candidates" in result.output


def test_absorb_suggest_lists_overlapping_pairs(tmp_path: Path) -> None:
    """Pages sharing aliases appear as candidates."""
    _write_entity_page(
        tmp_path, "person", "marcelo_a",
        name="Marcelo A", aliases=["Marcelo"],
    )
    _write_entity_page(
        tmp_path, "person", "marcelo_b",
        name="Marcelo B", aliases=["Marcelo"],
    )
    with _patch_workspace(tmp_path):
        result = runner.invoke(memory_app, ["absorb-suggest"])
    assert result.exit_code == 0
    assert "person:marcelo_a" in result.output
    assert "person:marcelo_b" in result.output


def test_absorb_rejects_same_ref(tmp_path: Path) -> None:
    """canonical == absorbed must fail before touching disk."""
    with _patch_workspace(tmp_path):
        result = runner.invoke(
            memory_app,
            ["absorb", "person:marcelo", "person:marcelo", "--yes"],
        )
    assert result.exit_code != 0
    assert "must differ" in _plain(result.output)


def test_absorb_rejects_bad_format(tmp_path: Path) -> None:
    """Missing ':' rejected by validation, no side effects."""
    with _patch_workspace(tmp_path):
        result = runner.invoke(
            memory_app,
            ["absorb", "no-colon", "person:marcelo", "--yes"],
        )
    assert result.exit_code != 0
    assert "<type>:<slug>" in _plain(result.output)


def test_absorb_merges_pages_and_archives(tmp_path: Path) -> None:
    """End-to-end: canonical keeps merged aliases, absorbed moves to archive."""
    _write_entity_page(
        tmp_path, "person", "marcelo",
        name="Marcelo Marmol", aliases=["Marcelo", "mm"],
    )
    _write_entity_page(
        tmp_path, "person", "marcelo_m",
        name="Marcelo M", aliases=["Marcelo", "mmarmol"],
    )
    # Disable vector index to keep test hermetic (no fastembed model needed).
    with _patch_workspace(tmp_path), patch(
        "durin.cli.memory_cmd._build_vector_index_optional",
        return_value=None,
    ):
        result = runner.invoke(
            memory_app,
            ["absorb", "person:marcelo", "person:marcelo_m",
             "--reason", "same person", "--yes"],
        )
    assert result.exit_code == 0, result.output
    assert "Absorbed" in result.output

    # Canonical should still exist with merged aliases.
    from durin.memory.entity_page import EntityPage

    canonical_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    assert canonical_path.exists()
    merged = EntityPage.from_file(canonical_path)
    assert merged is not None
    assert "mmarmol" in merged.aliases  # unique alias from absorbed

    # Absorbed moved to archive.
    absorbed_orig = tmp_path / "memory" / "entities" / "person" / "marcelo_m.md"
    assert not absorbed_orig.exists()
    # Phase 0 deliverable 5: archive lives top-level at
    # memory/archive/entities/<type>/<slug>.md (no longer nested under
    # the canonical's slug folder).
    archived = (
        tmp_path / "memory" / "archive" / "entities" / "person"
        / "marcelo_m.md"
    )
    assert archived.exists()


def test_stats_empty_workspace_clean_output(tmp_path: Path) -> None:
    """Empty workspace + no telemetry directory: command exits 0 with
    all-zero tables (no error)."""
    with _patch_workspace(tmp_path), patch(
        "durin.memory.stats.DEFAULT_TELEMETRY_DIR",
        tmp_path / "nope",
    ):
        result = runner.invoke(memory_app, ["stats"])
    assert result.exit_code == 0, result.output
    plain = _plain(result.output)
    assert "Episodic entries on disk" in plain
    assert "Store writes" in plain
    assert "0" in plain  # at least one zero metric rendered


def test_stats_json_output(tmp_path: Path) -> None:
    """--json mode prints serialisable JSON instead of the rich tables."""
    with _patch_workspace(tmp_path), patch(
        "durin.memory.stats.DEFAULT_TELEMETRY_DIR",
        tmp_path / "nope",
    ):
        result = runner.invoke(memory_app, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(_plain(result.output))
    assert "filesystem" in payload
    assert "recall" in payload
    assert "store" in payload
    assert payload["filesystem"]["episodic_entries_on_disk"] == 0


def test_stats_aggregates_telemetry_from_real_jsonl(tmp_path: Path) -> None:
    """End-to-end: synthetic JSONL events + filesystem entries → stats table
    surfaces the gate-relevant counters."""
    # Workspace with one tagged episodic entry on disk.
    episodic = tmp_path / "memory" / "episodic"
    episodic.mkdir(parents=True)
    (episodic / "e1.md").write_text(
        "---\nid: e1\nclass_name: episodic\n"
        "entities: [person:marcelo]\n---\n\nbody\n",
        encoding="utf-8",
    )
    # Synthetic telemetry directory with a couple of memory.* events.
    tel = tmp_path / "tel"
    tel.mkdir()
    import time as _t
    now = _t.time()
    (tel / "s.jsonl").write_text(
        json.dumps({"ts": now, "type": "memory.store",
                    "data": {"entry_id": "e1", "class_name": "stable",
                             "author": "agent_created", "headline": ""}}) + "\n"
        + json.dumps({"ts": now, "type": "memory.store.blocked_near_duplicate",
                      "data": {"candidate_class_name": "stable",
                               "existing_id": "e1",
                               "existing_class_name": "stable",
                               "distance": 0.05, "threshold": 0.10}}) + "\n",
        encoding="utf-8",
    )

    with _patch_workspace(tmp_path), patch(
        "durin.memory.stats.DEFAULT_TELEMETRY_DIR", tel,
    ):
        result = runner.invoke(memory_app, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(_plain(result.output))
    assert payload["filesystem"]["episodic_entries_on_disk"] == 1
    assert payload["filesystem"]["episodic_entries_tagged"] == 1
    assert payload["store"]["total"] == 1
    assert payload["store"]["blocked_near_duplicate"] == 1


def test_absorb_idempotent_on_already_archived(tmp_path: Path) -> None:
    """Re-running absorb when the absorbed page is already gone is a clean no-op."""
    _write_entity_page(
        tmp_path, "person", "marcelo",
        name="Marcelo", aliases=["Marcelo"],
    )
    _write_entity_page(
        tmp_path, "person", "marcelo_m",
        name="Marcelo M", aliases=["Marcelo"],
    )
    with _patch_workspace(tmp_path), patch(
        "durin.cli.memory_cmd._build_vector_index_optional",
        return_value=None,
    ):
        first = runner.invoke(
            memory_app,
            ["absorb", "person:marcelo", "person:marcelo_m", "--yes"],
        )
        assert first.exit_code == 0
        second = runner.invoke(
            memory_app,
            ["absorb", "person:marcelo", "person:marcelo_m", "--yes"],
        )
    assert second.exit_code == 0
    assert "No-op" in second.output or "already archived" in second.output
