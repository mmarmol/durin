"""``memory.index_skills=False`` is a clean no-op for skills.

When the toggle is off, skills must never be written to the index AND
never surfaced by search, even though ``skills/<slug>/SKILL.md`` files
exist on disk. The gates live in the memory layer (FTS rebuild, vector
rebuild, drift detection, search grep-fallback) and all consult the same
helper, :func:`durin.memory.index_meta.skills_indexing_enabled`.

The flag is flipped by monkeypatching ``durin.config.loader.load_config``
(the import the helper performs internally), so these tests run against a
real on-disk skill without needing a config file.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import rebuild_fts_index
from durin.memory.search import search_dreamed

_SKILL_MD = (
    "---\nname: deploy-flow\ndescription: deploy the service uniquetokenzz\n---\n"
    "run the deploy-flow playbook uniquetokenzz\n"
)


def _force_flag(monkeypatch, value: bool) -> None:
    """Pin ``memory.index_skills`` to *value* for every ``load_config``."""
    from durin.config.schema import Config

    cfg = Config()
    cfg.memory.index_skills = value
    monkeypatch.setattr(
        "durin.config.loader.load_config", lambda *a, **k: cfg,
    )


def _write_skill_on_disk(ws: Path, name: str = "deploy-flow") -> Path:
    """Write ``skills/<name>/SKILL.md`` directly (no index side effects)."""
    d = ws / "skills" / name
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text(_SKILL_MD, encoding="utf-8")
    return md


def test_reindex_skips_skills_when_disabled(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    _write_skill_on_disk(ws)
    _force_flag(monkeypatch, False)

    rebuild_fts_index(ws)

    with FTSIndex.open(ws) as idx:
        hits = idx.search("uniquetokenzz")
    assert all(h.type != "skill" for h in hits), (
        f"skills must not be indexed when index_skills=False; got {hits}"
    )


def test_reindex_indexes_skills_when_enabled(tmp_path: Path, monkeypatch) -> None:
    """Positive control: with the flag ON, the skill IS retrievable."""
    ws = tmp_path / "ws"
    _write_skill_on_disk(ws)
    _force_flag(monkeypatch, True)

    rebuild_fts_index(ws)

    with FTSIndex.open(ws) as idx:
        hits = idx.search("uniquetokenzz")
    assert any(
        h.uri == "skill/deploy-flow" and h.type == "skill" for h in hits
    ), f"skill should be indexed when index_skills=True; got {hits}"


def test_search_grep_fallback_skipped_when_disabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """The grep-fallback reads SKILL.md off disk, bypassing the index — it
    must be gated too. SKILL.md on disk, NO index built."""
    ws = tmp_path / "ws"
    _write_skill_on_disk(ws)
    # search_dreamed returns early unless memory/ exists; create it empty so
    # the function proceeds to the skill `extend` we are gating.
    (ws / "memory").mkdir()
    _force_flag(monkeypatch, False)

    results = search_dreamed(ws, "uniquetokenzz", level="warm")

    assert all(r.class_name != "skill" for r in results), (
        f"grep-fallback must not surface skills when disabled; got {results}"
    )


def test_search_grep_fallback_surfaces_skill_when_enabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """Positive control for the grep-fallback gate."""
    ws = tmp_path / "ws"
    _write_skill_on_disk(ws)
    (ws / "memory").mkdir()
    _force_flag(monkeypatch, True)

    results = search_dreamed(ws, "uniquetokenzz", level="warm")

    assert any(r.class_name == "skill" for r in results), (
        f"grep-fallback should surface the skill when enabled; got {results}"
    )
