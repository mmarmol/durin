"""`memory_search(scope='archive')` walks `memory/archive/**` on demand for
recovery / diagnostic queries. Before this fix, the scope enum rejected
`'archive'` with `{"error": "invalid scope 'archive'"}` because the
allowed set was `{'all', 'dreamed', 'undreamed'}`.

Design notes:
- Archive is intentionally NOT indexed (vector/lexical/grep over
  `memory/` exclude `memory/archive/**`). The `scope='archive'`
  path is a separate walk that loads each archived `.md`,
  substring-matches body+summary+headline, and returns hits.
- No re-ranking, no entity-aware: it's a recovery surface, not the
  hot path.
- CLI commands `durin archive show <uri>` and `durin archive list`
  remain deferred to backlog — file access already covers them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def _seed_archive(tmp_path: Path) -> None:
    """Place one archived episodic + one archived entity page so the
    walker has something to find."""
    arch_ep = tmp_path / "memory" / "archive" / "episodic"
    arch_ep.mkdir(parents=True)
    (arch_ep / "ep-001.md").write_text(
        "---\n"
        "headline: 'Trip to Paris'\n"
        "summary: 'Visited the Louvre with Marcelo.'\n"
        "valid_from: '2024-04-10'\n"
        "archived_at: '2024-09-12T10:00:00Z'\n"
        "archived_into: 'person:marcelo'\n"
        "---\n"
        "Body content about the Paris trip.\n",
        encoding="utf-8",
    )

    arch_ent = tmp_path / "memory" / "archive" / "entities" / "person"
    arch_ent.mkdir(parents=True)
    (arch_ent / "marcelo_old.md").write_text(
        "---\n"
        "type: person\n"
        "name: Marcelo (legacy)\n"
        "aliases: ['m.legacy']\n"
        "archived_at: '2024-11-05T09:00:00Z'\n"
        "archived_into: 'person:marcelo'\n"
        "archived_reason: 'absorbed into canonical'\n"
        "---\n"
        "Old entity page absorbed into the canonical one.\n",
        encoding="utf-8",
    )


def test_scope_archive_is_accepted_by_the_tool(tmp_path: Path) -> None:
    """Pre-F2 the enum rejected `'archive'` with an error string. The
    tool must accept it now."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed_archive(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Paris", scope="archive"))
    assert "error" not in out
    assert "results" in out


def test_scope_archive_finds_archived_episodic(tmp_path: Path) -> None:
    """Query that matches archived episodic body returns the hit
    with the correct class_name marker."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed_archive(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Louvre", scope="archive"))
    hits = out["results"]
    assert len(hits) >= 1
    classes = {h["class_name"] for h in hits}
    assert "episodic" in classes
    # The headline / summary should carry the original content so the
    # operator can identify what was archived.
    summaries = " ".join(h.get("summary", "") + h.get("headline", "") for h in hits)
    assert "Paris" in summaries or "Louvre" in summaries


def test_scope_archive_finds_archived_entity_page(tmp_path: Path) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed_archive(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="legacy", scope="archive"),
    )
    hits = out["results"]
    assert len(hits) >= 1
    classes = {h["class_name"] for h in hits}
    assert "entity_page" in classes or "entities" in classes


def test_scope_archive_returns_empty_when_no_archive(tmp_path: Path) -> None:
    """No `memory/archive/` directory → empty results, NOT an error."""
    from durin.agent.tools.memory_search import MemorySearchTool

    (tmp_path / "memory").mkdir()
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="anything", scope="archive"))
    assert "error" not in out
    assert out["results"] == []


def test_scope_archive_excludes_non_archived_content(tmp_path: Path) -> None:
    """Active (non-archived) memory MUST NOT appear in archive scope —
    the surface is for recovery, not a "search everything" mode."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index

    # Seed an active entity page AND an archived episodic.
    EntityPage(
        type="person", name="Marcelo (active)",
        aliases=["active_alias"], body="Active body content",
    ).save(
        tmp_path / "memory" / "entities" / "person" / "marcelo.md",
    )
    _seed_archive(tmp_path)
    rebuild_fts_index(tmp_path)

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="active", scope="archive"),
    )
    # Active entity page must NOT be in archive results.
    for hit in out["results"]:
        assert "active" not in (hit.get("summary", "") + hit.get("headline", "")).lower()


def test_scope_archive_respects_limit(tmp_path: Path) -> None:
    """Many archived entries → still respect the `limit` parameter."""
    arch_dir = tmp_path / "memory" / "archive" / "episodic"
    arch_dir.mkdir(parents=True)
    for i in range(15):
        (arch_dir / f"ep-{i:03d}.md").write_text(
            f"---\nheadline: 'archived item {i}'\n"
            f"summary: 'common token MATCHME'\n---\nBody {i}\n",
            encoding="utf-8",
        )

    from durin.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="MATCHME", scope="archive", limit=5),
    )
    assert len(out["results"]) == 5
