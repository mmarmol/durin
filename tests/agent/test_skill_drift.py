import shutil
from pathlib import Path

from durin.agent import skill_drift
from durin.agent.skill_resolve import ResolveResult, SkillCandidate
from durin.agent.skills_import import _content_hash


def _install(ws: Path, name: str, source: str, content_hash: str, body: str = "old body\n") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: a demo\n"
        "metadata:\n"
        "  durin:\n"
        "    provenance:\n"
        f'      source: "{source}"\n'
        f'      content_hash: "{content_hash}"\n'
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def _upstream(tmp: Path, body: str) -> Path:
    u = tmp / "upstream" / "x"
    u.mkdir(parents=True)
    (u / "SKILL.md").write_text(
        f"---\nname: x\ndescription: a demo\n---\n{body}", encoding="utf-8")
    return u


def _patch(monkeypatch, upstream_dir: Path, source: str):
    monkeypatch.setattr(
        "durin.agent.skill_resolve.resolve_candidates",
        lambda s: ResolveResult([SkillCandidate("x", source, "github")]))

    def fake_fetch(cand, *, quarantine_root, **kw):
        q = Path(quarantine_root) / "x"
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(upstream_dir, q)
        return q
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", fake_fetch)


def test_no_drift_returns_none(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    up = _upstream(tmp_path, "same\n")
    _install(ws, "x", "github:o/r/x", _content_hash(up))  # stored hash == upstream
    _patch(monkeypatch, up, "github:o/r/x")
    assert skill_drift.check_upstream_drift(ws, "x", allowlist=[]) is None


def test_drift_safe_allowlisted_returns_allow(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    up = _upstream(tmp_path, "NEW prose, no code\n")
    _install(ws, "x", "github:acme/r/x", "STALEHASH")  # won't match → drift
    _patch(monkeypatch, up, "github:acme/r/x")
    rep = skill_drift.check_upstream_drift(ws, "x", allowlist=["github:acme/"])
    assert rep is not None
    assert rep.action == "allow"        # safe, no code, allowlisted
    assert "NEW prose" in rep.upstream_md
    assert rep.verdict == "safe"


def test_drift_out_of_allowlist_needs_confirm(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; ws.mkdir()
    up = _upstream(tmp_path, "NEW prose\n")
    _install(ws, "x", "github:stranger/r/x", "STALEHASH")
    _patch(monkeypatch, up, "github:stranger/r/x")
    rep = skill_drift.check_upstream_drift(ws, "x", allowlist=[])  # not allowlisted
    assert rep is not None and rep.action == "confirm"   # §8.D: not auto-incorporable


def test_local_source_is_skipped(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    _install(ws, "y", "/abs/local/disk/path", "H")
    assert skill_drift.check_upstream_drift(ws, "y", allowlist=[]) is None


def test_no_provenance_is_skipped(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    d = ws / "skills" / "z"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: z\ndescription: d\n---\nbody\n", encoding="utf-8")
    assert skill_drift.check_upstream_drift(ws, "z", allowlist=[]) is None
