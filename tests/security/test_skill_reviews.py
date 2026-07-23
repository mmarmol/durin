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


def test_review_survives_unrelated_edits(tmp_path):
    """Per-file acks: adding a new script or editing SKILL.md must NOT reopen
    a review whose acked findings live in an untouched file."""
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[_finding()])
    (d / "scripts" / "b.py").write_text("print('new')\n", encoding="utf-8")
    (d / "SKILL.md").write_text("---\nname: demo\n---\nedited\n", encoding="utf-8")
    assert sr.get_review(ws, "demo", d, [_finding()]) is not None


def test_synthetic_finding_acks_by_fingerprint(tmp_path):
    """A finding whose `where` is not a file (e.g. import_verdict anchors at
    metadata.durin.provenance) acks by fingerprint alone and survives edits."""
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    synth = {"category": "import_verdict", "severity": "caution",
             "where": "metadata.durin.provenance", "detail": "pinned"}
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[synth])
    (d / "SKILL.md").write_text("---\nname: demo\n---\nedited\n", encoding="utf-8")
    assert sr.get_review(ws, "demo", d, [synth]) is not None


def test_traversal_where_is_treated_as_synthetic(tmp_path):
    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    outside = {"category": "x", "severity": "caution",
               "where": "../../etc/passwd", "detail": "z"}
    sr.record_review(ws, "demo", d, by="user", verdict="safe",
                     original="caution", findings=[outside])
    assert sr.get_review(ws, "demo", d, [outside]) is not None


def test_legacy_v1_entry_keeps_whole_dir_semantics(tmp_path):
    """A store written before per-file acks (content_hash + fingerprint list)
    stays valid while the dir is unchanged and dies on ANY edit."""
    import json

    d = _skill(tmp_path)
    ws = tmp_path / "ws"
    (ws / ".durin").mkdir(parents=True)
    entry = {"content_hash": sr.content_hash(d),
             "acked": [sr.fingerprint(_finding())],
             "by": "user", "verdict": "safe", "original": "caution",
             "note": "", "at": "2026-07-01"}
    (ws / ".durin" / "skill-reviews.json").write_text(
        json.dumps({"version": 1, "reviews": {"demo": entry}}), encoding="utf-8")
    assert sr.get_review(ws, "demo", d, [_finding()]) is not None
    (d / "scripts" / "b.py").write_text("print('new')\n", encoding="utf-8")
    assert sr.get_review(ws, "demo", d, [_finding()]) is None


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
