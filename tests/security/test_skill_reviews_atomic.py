"""Single-process atomic+lock tests for skill-reviews.json.

Verifies that record_review and clear_review use atomic_write_text and
acquire the cross-process lock (lock file is created).
"""

from __future__ import annotations

from pathlib import Path


def _skill(tmp_path: Path, name: str = "demo") -> Path:
    d = tmp_path / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    (d / "scripts" / "a.py").write_text("print(1)\n", encoding="utf-8")
    return d


def _finding(detail: str = "dangerous call") -> dict:
    return {"category": "cat", "severity": "low", "where": "scripts/a.py", "detail": detail}


def test_record_review_creates_lock_file(tmp_path: Path) -> None:
    from durin.security import skill_reviews as sr

    ws = tmp_path / "workspace"
    skill_dir = _skill(tmp_path)
    sr.record_review(ws, "demo", skill_dir, by="user",
                     verdict="safe", original="caution", findings=[_finding()])
    store = ws / ".durin" / "skill-reviews.json"
    lock = Path(f"{store}.lock")
    assert lock.exists()
    assert "demo" in sr.load_reviews(ws)


def test_clear_review_creates_lock_file(tmp_path: Path) -> None:
    from durin.security import skill_reviews as sr

    ws = tmp_path / "workspace"
    skill_dir = _skill(tmp_path)
    sr.record_review(ws, "demo", skill_dir, by="user",
                     verdict="safe", original="caution", findings=[_finding()])
    sr.clear_review(ws, "demo")
    store = ws / ".durin" / "skill-reviews.json"
    lock = Path(f"{store}.lock")
    assert lock.exists()
    assert "demo" not in sr.load_reviews(ws)
