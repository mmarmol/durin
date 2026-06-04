"""§6.C — acquire_safe_seed gates ONE ref; only a risk-free allowlisted ref seeds."""
import asyncio
from pathlib import Path

from durin.agent import skill_acquire


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


def _wire(monkeypatch, *, verdict, carries_code, fetch_spy=None):
    """Patch resolve/fetch/scan so the test is offline + deterministic.
    decide_action is REAL (pure) — the allowlist gating is exercised for real."""
    def _resolve(ref):
        return _Resolve([_Cand(ref.split("/")[-1], ref)])

    def _fetch(cand, *, quarantine_root, allowlist=None, judge_trigger="off"):
        if fetch_spy is not None:
            fetch_spy.append(cand.ref)
        d = Path(quarantine_root) / cand.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")
        return d

    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates", _resolve)
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", _fetch)
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: _Valid(carries_code))
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: _Scan(verdict))


def test_allowlisted_clean_ref_returns_seed(monkeypatch, tmp_path):
    _wire(monkeypatch, verdict="safe", carries_code=False)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is not None
    assert out["source"] == "github:acme/pdf"
    assert "body" in out["content"]


def test_not_allowlisted_rejected_without_download(monkeypatch, tmp_path):
    spy: list[str] = []
    _wire(monkeypatch, verdict="safe", carries_code=False, fetch_spy=spy)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=[]))
    assert out is None
    assert spy == []  # fast reject — fetch_candidate must NOT be called


def test_allowlisted_but_carries_code_refused(monkeypatch, tmp_path):
    _wire(monkeypatch, verdict="safe", carries_code=True)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is None


def test_unresolvable_source_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda ref: _Resolve([]))
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is None
