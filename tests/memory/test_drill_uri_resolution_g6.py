"""G6 (audit fourth pass, 2026-05-28): the URIs that `memory_search`
emits must be resolvable by `memory_drill`.

Two bugs discovered while investigating Item 6 (summary slot):

**Bug 1 — entity pages.** `memory_search` emits
`memory/entity_page/<type>:<slug>` (`memory_search.py:720-722`) but
entity pages live at `memory/entities/<type>/<slug>.md` on disk.
`drill()` resolves the URI literally and fails with "file not
found". The agent receives a canonical hit `=== CANONICAL:
person:marcelo ===` and cannot drill it.

**Bug 2 — archive scope (F2).** `_run_archive_scope` emits
`uri = front.get('uri', '') or path.stem` (`memory_search.py:606`)
which yields a bare id like `arch1` with no path prefix. `drill()`
cannot resolve it to any file under `memory/archive/`.

G6 fixes both:
- `drill()` learns to translate the `memory/entity_page/<type>:<slug>`
  shape to `memory/entities/<type>/<slug>.md` before resolving.
- `_run_archive_scope` emits the relative path under `memory/archive/`
  so the URI is directly drillable.

Entries, sessions, ingested were already correct — no regression test
needed for them but a few are kept here as a guardrail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _seed_workspace(tmp_path: Path) -> dict[str, str]:
    """Populate every memory class so we can exercise each URI shape.
    Returns a dict of `kind -> uri_as_emitted_by_memory_search`."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.provenance import author_scope
    from durin.memory.store import store_memory

    EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="entity body for drill test",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

    with author_scope("agent_created"):
        ep = store_memory(
            tmp_path, content="episodic content G6",
            class_name="episodic", entities=[],
        )

    # Archived episodic with explicit id frontmatter.
    arch_dir = tmp_path / "memory" / "archive" / "episodic"
    arch_dir.mkdir(parents=True)
    (arch_dir / "old-ep-001.md").write_text(
        "---\n"
        "id: old-ep-001\n"
        "headline: 'archived headline'\n"
        "summary: 'archived summary G6'\n"
        "archived_at: '2024-04-10T10:00:00Z'\n"
        "archived_into: 'person:marcelo'\n"
        "---\n"
        "archived body content\n",
        encoding="utf-8",
    )

    return {
        "entity_canonical": "memory/entity_page/person:marcelo",
        "episodic": f"memory/episodic/{ep['id']}",
        "archive_episodic_target": "memory/archive/episodic/old-ep-001.md",
    }


def test_drill_resolves_entity_page_canonical_uri(tmp_path: Path) -> None:
    """Bug 1 (G6): the canonical-shape URI that memory_search emits
    for entity pages must resolve via drill."""
    from durin.memory.drill import drill

    uris = _seed_workspace(tmp_path)
    text = drill(tmp_path, uris["entity_canonical"])
    assert "Marcelo" in text
    assert "entity body for drill test" in text


def test_drill_resolves_entity_page_with_md_suffix(tmp_path: Path) -> None:
    """Defensive: agent or tool that accidentally appended `.md` to
    the canonical URI should still resolve. We do not want a brittle
    surface that fails on irrelevant string suffix variation."""
    from durin.memory.drill import drill

    _seed_workspace(tmp_path)
    text = drill(tmp_path, "memory/entity_page/person:marcelo.md")
    assert "Marcelo" in text


def test_drill_legacy_path_form_still_works(tmp_path: Path) -> None:
    """The pre-G6 escape hatch — passing the on-disk path directly —
    keeps working. Some callers and tests use it."""
    from durin.memory.drill import drill

    _seed_workspace(tmp_path)
    text = drill(tmp_path, "memory/entities/person/marcelo.md")
    assert "Marcelo" in text


def test_drill_returns_clear_error_for_unknown_entity(
    tmp_path: Path,
) -> None:
    """`memory/entity_page/<type>:<slug>` where the file does not exist
    raises DrillError with a message that includes the canonical URI
    (not just the failed disk path) so the agent can act on it."""
    from durin.memory.drill import DrillError, drill

    _seed_workspace(tmp_path)
    with pytest.raises(DrillError) as exc:
        drill(tmp_path, "memory/entity_page/person:ghost")
    assert "person:ghost" in str(exc.value) or "ghost.md" in str(exc.value)


def test_memory_search_archive_scope_emits_drillable_uri(
    tmp_path: Path,
) -> None:
    """Bug 2 (G6): `memory_search(scope='archive')` must emit URIs
    that drill can resolve. Pre-G6 the URI was `front.uri or
    path.stem` — a bare id with no path prefix that drill could not
    map back to a file."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.drill import drill

    _seed_workspace(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="archived", scope="archive"),
    )
    assert out["results"], "archive scope should return at least one hit"

    for hit in out["results"]:
        uri = hit["uri"]
        # The emitted URI must point under memory/archive/ so the
        # operator/agent can drill it directly.
        assert uri.startswith("memory/archive/"), uri
        text = drill(tmp_path, uri)
        assert "archived body content" in text


def test_memory_search_archive_entity_emits_drillable_uri(
    tmp_path: Path,
) -> None:
    """Same as above but for archived entity pages (under
    `memory/archive/entities/<type>/<slug>.md`)."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.drill import drill

    # Seed an archived entity page.
    arch_ent = (
        tmp_path / "memory" / "archive" / "entities" / "person"
    )
    arch_ent.mkdir(parents=True)
    (arch_ent / "marcelo_legacy.md").write_text(
        "---\n"
        "type: person\n"
        "name: 'Marcelo (legacy)'\n"
        "aliases: ['legacy']\n"
        "archived_at: '2024-11-05T09:00:00Z'\n"
        "archived_into: 'person:marcelo'\n"
        "archived_reason: 'absorbed into canonical'\n"
        "---\n"
        "archived entity body content for G6\n",
        encoding="utf-8",
    )

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="legacy", scope="archive"),
    )
    assert out["results"]
    for hit in out["results"]:
        uri = hit["uri"]
        assert uri.startswith("memory/archive/"), uri
        text = drill(tmp_path, uri)
        assert "archived entity body content for G6" in text
