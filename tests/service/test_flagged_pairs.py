"""MemoryService flagged-pairs endpoints — unit tests (TDD).

Tests call the service directly (no HTTP), matching the pattern used by
test_memory_dream_digest.py and test_memory.py.  EntityAbsorption.absorb is
monkeypatched so no git or disk I/O for entity files is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.refine_dream import add_flagged, read_flagged
from durin.service.memory import (
    FlaggedPairs,
    MemoryService,
    ResolveFlaggedRequest,
)
from durin.service.principal import Principal
from durin.service.types import ConflictError, ForbiddenError, ValidationFailedError

LOCAL = Principal.local()
MEMORY_READ = Principal.remote("tok", frozenset({"memory:read"}))
MEMORY_WRITE = Principal.remote("tok", frozenset({"memory:read", "memory:write"}))
NO_SCOPE = Principal.remote("tok", frozenset())


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(workspace_resolver=lambda: tmp_path)


# ---------------------------------------------------------------------------
# GET /api/v1/memory/flagged-pairs
# ---------------------------------------------------------------------------


async def test_flagged_pairs_requires_memory_read(tmp_path: Path):
    from durin.service.memory import FlaggedPairsQuery
    with pytest.raises(ForbiddenError):
        await _service(tmp_path).flagged_pairs(FlaggedPairsQuery(), NO_SCOPE)


async def test_flagged_pairs_empty(tmp_path: Path):
    from durin.service.memory import FlaggedPairsQuery
    result = await _service(tmp_path).flagged_pairs(FlaggedPairsQuery(), LOCAL)
    assert isinstance(result, FlaggedPairs)
    assert result.pairs == []


async def test_flagged_pairs_returns_seeded_pairs(tmp_path: Path):
    from durin.service.memory import FlaggedPairsQuery
    add_flagged(
        tmp_path, "person:alice", "person:alice-v2",
        verdict="unclear", confidence=72, reasoning="too similar",
    )
    add_flagged(
        tmp_path, "company:acme", "company:acme-corp",
        verdict="different", confidence=85, reasoning="different entities",
    )
    result = await _service(tmp_path).flagged_pairs(FlaggedPairsQuery(), LOCAL)
    assert len(result.pairs) == 2
    # Check that the right fields exist
    refs_a = {p.ref_a for p in result.pairs}
    refs_b = {p.ref_b for p in result.pairs}
    # sorted pairs: refs are stored sorted so alice < alice-v2 and acme < acme-corp
    assert "person:alice" in refs_a or "person:alice" in refs_b
    for p in result.pairs:
        assert p.verdict in ("unclear", "different")
        assert isinstance(p.confidence, int)
        assert isinstance(p.reasoning, str)


# ---------------------------------------------------------------------------
# POST /api/v1/memory/flagged-pairs/resolve — scope
# ---------------------------------------------------------------------------


async def test_resolve_requires_memory_write(tmp_path: Path):
    cmd = ResolveFlaggedRequest(ref_a="person:a", ref_b="person:b", action="separate")
    with pytest.raises(ForbiddenError):
        await _service(tmp_path).resolve_flagged(cmd, MEMORY_READ)


async def test_resolve_rejects_unknown_action(tmp_path: Path):
    add_flagged(
        tmp_path, "person:a", "person:b",
        verdict="unclear", confidence=70, reasoning="r",
    )
    cmd = ResolveFlaggedRequest(ref_a="person:a", ref_b="person:b", action="explode")
    with pytest.raises(ValidationFailedError):
        await _service(tmp_path).resolve_flagged(cmd, LOCAL)


# ---------------------------------------------------------------------------
# POST resolve action=separate
# ---------------------------------------------------------------------------


async def test_resolve_separate_adds_tombstone_and_removes_flag(tmp_path: Path):
    from durin.memory.refine_dream import is_tombstoned

    add_flagged(
        tmp_path, "person:alice", "person:alice-v2",
        verdict="unclear", confidence=72, reasoning="r",
    )
    assert len(read_flagged(tmp_path)) == 1

    cmd = ResolveFlaggedRequest(
        ref_a="person:alice", ref_b="person:alice-v2", action="separate",
    )
    result = await _service(tmp_path).resolve_flagged(cmd, LOCAL)

    assert result.ok is True
    assert result.action == "separate"
    assert is_tombstoned(tmp_path, "person:alice", "person:alice-v2")
    assert read_flagged(tmp_path) == []


async def test_resolve_separate_leaves_other_flags_intact(tmp_path: Path):
    add_flagged(tmp_path, "person:alice", "person:alice-v2",
                verdict="unclear", confidence=72, reasoning="r1")
    add_flagged(tmp_path, "company:acme", "company:corp",
                verdict="different", confidence=85, reasoning="r2")

    cmd = ResolveFlaggedRequest(
        ref_a="person:alice", ref_b="person:alice-v2", action="separate",
    )
    await _service(tmp_path).resolve_flagged(cmd, LOCAL)

    remaining = read_flagged(tmp_path)
    assert len(remaining) == 1
    assert sorted(remaining[0]["pair"]) == ["company:acme", "company:corp"]


# ---------------------------------------------------------------------------
# POST resolve action=merge
# ---------------------------------------------------------------------------


async def test_resolve_merge_calls_absorb_and_removes_flag(tmp_path: Path, monkeypatch):
    add_flagged(
        tmp_path, "person:alice", "person:alice-v2",
        verdict="unclear", confidence=72, reasoning="r",
    )
    assert len(read_flagged(tmp_path)) == 1

    absorb_calls: list[dict] = []

    def _fake_absorb(self, canonical, absorbed, *, reason="", **kw):
        absorb_calls.append({"canonical": canonical, "absorbed": absorbed, "reason": reason})
        return "deadbeef"

    monkeypatch.setattr(
        "durin.memory.absorption.EntityAbsorption.absorb",
        _fake_absorb,
    )

    cmd = ResolveFlaggedRequest(
        ref_a="person:alice", ref_b="person:alice-v2", action="merge",
    )
    result = await _service(tmp_path).resolve_flagged(cmd, LOCAL)

    assert result.ok is True
    assert result.action == "merge"
    assert len(absorb_calls) == 1
    assert absorb_calls[0]["canonical"] == "person:alice"
    assert absorb_calls[0]["absorbed"] == "person:alice-v2"
    assert absorb_calls[0]["reason"] == "manual_review"
    # flag removed after merge
    assert read_flagged(tmp_path) == []


async def test_resolve_merge_stale_pair_raises_conflict(tmp_path: Path, monkeypatch):
    """absorb() raises AbsorptionError when entity pages are gone (already resolved).

    The service must catch it and re-raise as ConflictError (409), not let the
    raw exception escape as an unhandled 500.
    """
    from durin.memory.absorption import AbsorptionError

    add_flagged(
        tmp_path, "person:alice", "person:alice-v2",
        verdict="unclear", confidence=72, reasoning="r",
    )

    def _fake_absorb_missing(self, canonical, absorbed, **kw):
        raise AbsorptionError(f"canonical page missing: {canonical}")

    monkeypatch.setattr(
        "durin.memory.absorption.EntityAbsorption.absorb",
        _fake_absorb_missing,
    )

    cmd = ResolveFlaggedRequest(
        ref_a="person:alice", ref_b="person:alice-v2", action="merge",
    )
    with pytest.raises(ConflictError):
        await _service(tmp_path).resolve_flagged(cmd, LOCAL)


async def test_resolve_merge_leaves_other_flags_intact(tmp_path: Path, monkeypatch):
    add_flagged(tmp_path, "person:alice", "person:alice-v2",
                verdict="unclear", confidence=72, reasoning="r1")
    add_flagged(tmp_path, "company:acme", "company:corp",
                verdict="different", confidence=85, reasoning="r2")

    monkeypatch.setattr(
        "durin.memory.absorption.EntityAbsorption.absorb",
        lambda self, canonical, absorbed, **kw: "sha",
    )

    cmd = ResolveFlaggedRequest(
        ref_a="person:alice", ref_b="person:alice-v2", action="merge",
    )
    await _service(tmp_path).resolve_flagged(cmd, LOCAL)

    remaining = read_flagged(tmp_path)
    assert len(remaining) == 1
    assert sorted(remaining[0]["pair"]) == ["company:acme", "company:corp"]
