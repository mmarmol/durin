"""§6.C — acquire_safe_seed: only a risk-free hit becomes a seed."""
import asyncio
from pathlib import Path

import pytest

from durin.agent import skill_acquire
from durin.agent.skill_registry import SkillSearchHit


class _Cand:
    def __init__(self, name, ref):
        self.name, self.ref, self.kind = name, ref, "github"


class _Resolve:
    def __init__(self, cands):
        self.candidates = cands


class _Scan:
    def __init__(self, verdict):
        self._v = verdict

    @property
    def verdict(self):
        return self._v


class _Valid:
    def __init__(self, carries_code):
        self.carries_code = carries_code


def _wire(monkeypatch, *, hits, verdict, carries_code, tmp_path):
    """Patch the network/fetch/scan deps so the test is offline + deterministic."""
    async def _search(query, *, adapters, allowlist, limit):
        return hits

    def _resolve(ref):
        return _Resolve([_Cand(ref.split("/")[-1], ref)])

    def _fetch(cand, *, quarantine_root, allowlist=None):
        d = Path(quarantine_root) / cand.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")
        return d

    monkeypatch.setattr("durin.agent.skill_registry.search_registries", _search)
    monkeypatch.setattr("durin.agent.skill_registry.build_adapters", lambda r: [])
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates", _resolve)
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", _fetch)
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: _Valid(carries_code))
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: _Scan(verdict))


def test_safe_allowlisted_hit_returns_seed(monkeypatch, tmp_path):
    hits = [SkillSearchHit(name="pdf", ref="github:acme/pdf", registry="skills.sh")]
    _wire(monkeypatch, hits=hits, verdict="safe", carries_code=False, tmp_path=tmp_path)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "pdf", registries=[], allowlist=["github:acme"], limit=5))
    assert out is not None
    assert out["source"] == "github:acme/pdf"
    assert "body" in out["content"]


def test_risky_hit_is_not_seeded(monkeypatch, tmp_path):
    hits = [SkillSearchHit(name="pdf", ref="github:acme/pdf", registry="skills.sh")]
    _wire(monkeypatch, hits=hits, verdict="safe", carries_code=True, tmp_path=tmp_path)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "pdf", registries=[], allowlist=["github:acme"], limit=5))
    assert out is None


def test_not_allowlisted_is_not_seeded(monkeypatch, tmp_path):
    hits = [SkillSearchHit(name="pdf", ref="github:acme/pdf", registry="skills.sh")]
    _wire(monkeypatch, hits=hits, verdict="safe", carries_code=False, tmp_path=tmp_path)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "pdf", registries=[], allowlist=[], limit=5))
    assert out is None


def test_no_hits_returns_none(monkeypatch, tmp_path):
    _wire(monkeypatch, hits=[], verdict="safe", carries_code=False, tmp_path=tmp_path)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "pdf", registries=[], allowlist=["github:acme"], limit=5))
    assert out is None
