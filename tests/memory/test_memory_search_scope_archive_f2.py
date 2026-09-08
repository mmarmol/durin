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

import pytest


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


def test_scope_archive_surfaces_entities(tmp_path: Path) -> None:
    """Archived hits carry their own `entities` frontmatter, same as the
    main search path — the archive walk must not hardcode an empty tuple
    and silently drop the `Entities:` tail and the `results[].entities`
    field."""
    from durin.agent.tools.memory_search import MemorySearchTool

    arch_ep = tmp_path / "memory" / "archive" / "episodic"
    arch_ep.mkdir(parents=True)
    (arch_ep / "ep-001.md").write_text(
        "---\n"
        "headline: 'Trip to Paris'\n"
        "summary: 'Visited the Louvre with Marcelo.'\n"
        "entities: ['person:marcelo', 'city:paris']\n"
        "valid_from: '2024-04-10'\n"
        "archived_at: '2024-09-12T10:00:00Z'\n"
        "archived_into: 'person:marcelo'\n"
        "---\n"
        "Body content about the Paris trip.\n",
        encoding="utf-8",
    )

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Louvre", scope="archive"))
    hits = out["results"]
    assert len(hits) >= 1
    assert hits[0]["entities"] == ["person:marcelo", "city:paris"]
    assert "Entities: person:marcelo, city:paris" in out["sectioned_rendered"]


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


def test_scope_archive_applies_warm_max_chars_and_emits_rendered_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive used to render fully unbounded and skip `rendered_chars`
    entirely — the same per-response budget the main path honours at warm
    level (archive results always render at that level) must also bound
    this path, and its `memory.recall` row must carry `rendered_chars` for
    parity with every other scope.

    Asserted by comparison (a tight budget renders strictly smaller than a
    generous one for the same hits) rather than a hand-computed byte count,
    which would be brittle against the snippet-window/marker-format details
    `_run_archive_scope` and `render_sectioned` own."""
    from durin.config.schema import Config

    arch_dir = tmp_path / "memory" / "archive" / "episodic"
    arch_dir.mkdir(parents=True)
    for i in range(10):
        (arch_dir / f"ep-{i:03d}.md").write_text(
            f"---\nheadline: 'archived item {i}'\n"
            f"summary: 'common token MATCHME {'x' * 200}'\n---\nBody {i}\n",
            encoding="utf-8",
        )

    from durin.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(workspace=tmp_path)

    loose_cfg = Config()
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: loose_cfg,
    )
    loose = asyncio.run(
        tool.execute(query="MATCHME", scope="archive", limit=10),
    )["sectioned_rendered"]

    tight_cfg = Config()
    tight_cfg.memory.search.warm_max_chars = 400
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: tight_cfg,
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools.memory_search.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    tight = asyncio.run(
        tool.execute(query="MATCHME", scope="archive", limit=10),
    )["sectioned_rendered"]

    assert len(tight) < len(loose)
    assert "drill for the body" in tight
    assert "drill for the body" not in loose

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload["rendered_chars"] == len(tight)


def test_scope_archive_a_hit_over_budget_never_forces_pointers_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group A final review, Important 1: a hit whose block alone exceeds
    `warm_max_chars` used to trip `render_sectioned`'s one-way ratchet
    before the first block ever rendered, degrading every hit — including
    small ones that would have fit on their own — to a pointer line, with
    `total` still reporting both. The top-ranked (first) hit must now
    render whole; the rendering must never be pointers-only. A tiny
    `warm_max_chars` isolates the floor guarantee itself, decoupled from
    the separate per-hit excerpt cut covered by
    `test_scope_archive_cuts_per_hit_bodies_to_warm_excerpt_chars`.
    `level='cold'` is passed because the archive path renders at (the
    effective) warm level regardless of the caller's `level` — this
    reproduces the review's probe exactly."""
    from durin.config.schema import Config

    arch_dir = tmp_path / "memory" / "archive" / "episodic"
    arch_dir.mkdir(parents=True)
    (arch_dir / "incident.md").write_text(
        "---\nheadline: 'the incident report'\n---\n"
        "the incident report.\n",
        encoding="utf-8",
    )
    (arch_dir / "note.md").write_text(
        "---\nheadline: 'a short note'\n---\n"
        "a short note about the incident\n",
        encoding="utf-8",
    )

    cfg = Config()
    cfg.memory.search.warm_max_chars = 1
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: cfg,
    )

    from durin.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="incident", scope="archive", level="cold"),
    )

    assert out["total"] == 2
    rendered = out["sectioned_rendered"]
    # Not pointers-only: the top-ranked hit (glob-sorted first: incident
    # before note) rendered its full block despite the 1-char budget.
    assert "=== FRAGMENT:" in rendered
    assert "the incident report." in rendered
    # The ratchet still applies from the second block on.
    assert "drill for the body" in rendered


def test_scope_archive_cuts_per_hit_bodies_to_warm_excerpt_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive path used to carry each hit's raw file body straight
    into `SectionedHit`, unbounded — so the `max_chars` budget bounded
    raw file dumps rather than a set of comparable blocks the way the
    main path's `warm_excerpt_chars` cut does. A single large archived
    entry with no frontmatter `summary` must render an excerpt no longer
    than `warm_excerpt_chars`, not the whole file."""
    from durin.config.schema import Config

    arch_dir = tmp_path / "memory" / "archive" / "episodic"
    arch_dir.mkdir(parents=True)
    (arch_dir / "huge.md").write_text(
        "---\nheadline: 'a huge archived entry'\n---\n"
        + ("filler content about the incident. " * 500) + "\n",
        encoding="utf-8",
    )

    cfg = Config()
    cfg.memory.search.warm_excerpt_chars = 300
    cfg.memory.search.warm_max_chars = 100_000
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: cfg,
    )

    from durin.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="incident", scope="archive"),
    )

    assert out["total"] == 1
    rendered = out["sectioned_rendered"]
    assert "drill for the body" not in rendered
    # The rendered block must be far shorter than the raw file (~18000
    # chars of filler) — bounded to roughly warm_excerpt_chars, not a
    # raw file dump.
    assert len(rendered) < 1000
