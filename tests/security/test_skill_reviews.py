"""Tests for the active-skill review store (durin/security/skill_reviews.py)."""
from pathlib import Path

from durin.security import skill_reviews as sr


def _skill(tmp_path: Path, body="x", script="print(1)\n") -> Path:
    d = tmp_path / "demo"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: demo\n---\n{body}\n", encoding="utf-8")
    (d / "scripts" / "a.py").write_text(script, encoding="utf-8")
    return d


def _finding(detail="dangerous call compile"):
    return {"category": "dangerous_code", "severity": "caution",
            "where": "scripts/a.py", "detail": detail}


def test_content_hash_changes_on_edit(tmp_path):
    d = _skill(tmp_path)
    h1 = sr.content_hash(d)
    (d / "scripts" / "a.py").write_text("print(2)\n", encoding="utf-8")
    assert sr.content_hash(d) != h1


def test_fingerprint_format():
    assert sr.fingerprint(_finding()) == "dangerous_code|scripts/a.py|dangerous call compile"


def test_record_and_get_roundtrip(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[_finding()], note="ok")
    got = sr.get_review(ws, "demo", d, [_finding()])
    assert got and got["by"] == "user" and got["verdict"] == "safe"
    assert got["original"] == "caution" and got["note"] == "ok"


def test_get_review_invalid_after_content_edit(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[_finding()])
    (d / "scripts" / "a.py").write_text("print(2)\n", encoding="utf-8")
    assert sr.get_review(ws, "demo", d, [_finding()]) is None


def test_get_review_invalid_on_new_finding(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[_finding()])
    new = [_finding(), _finding("reverse shell")]
    assert sr.get_review(ws, "demo", d, new) is None


def test_get_review_valid_with_fewer_findings(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution",
                     findings=[_finding(), _finding("env access")])
    assert sr.get_review(ws, "demo", d, [_finding()]) is not None


def test_corrupt_store_is_empty(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".durin").mkdir(parents=True)
    (ws / ".durin" / "skill-reviews.json").write_text("{not json", encoding="utf-8")
    assert sr.load_reviews(ws) == {}


def test_clear_review(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[_finding()])
    assert sr.clear_review(ws, "demo") is True
    assert sr.get_review(ws, "demo", d, [_finding()]) is None
    assert sr.clear_review(ws, "demo") is False
