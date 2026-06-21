"""Cross-process safety tests for skill-reviews.json.

Two processes each record_review for a DISTINCT skill concurrently to
the same skill-reviews.json. Without cross_process_lock wrapping the
load→mutate→save, one write overwrites the other. With the lock, both
reviews must survive.

See docs/architecture/concurrency.md for lock-ordering invariants.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path


def _make_skill(base: Path, name: str) -> Path:
    d = base / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    (d / "scripts" / "a.py").write_text("print(1)\n", encoding="utf-8")
    return d


def _record(workspace: str, skill_base: str, name: str) -> None:
    from durin.security import skill_reviews as sr  # noqa: PLC0415

    skill_dir = Path(skill_base) / name
    finding = {"category": "cat", "severity": "low", "where": "scripts/a.py", "detail": "d"}
    sr.record_review(workspace, name, skill_dir, by="user",
                     verdict="safe", original="caution", findings=[finding])


def test_concurrent_record_both_reviews_survive(tmp_path: Path) -> None:
    """Two concurrent record_review calls for distinct skills must not lose either."""
    skills_base = tmp_path / "skills"
    for name in ("skill-A", "skill-B"):
        _make_skill(skills_base, name)

    ws = str(tmp_path / "workspace")
    ctx = mp.get_context("spawn")
    ps = [
        ctx.Process(target=_record, args=(ws, str(skills_base), "skill-A")),
        ctx.Process(target=_record, args=(ws, str(skills_base), "skill-B")),
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(20)

    from durin.security import skill_reviews as sr  # noqa: PLC0415

    reviews = sr.load_reviews(ws)
    assert "skill-A" in reviews, f"skill-A lost — got {list(reviews)}"
    assert "skill-B" in reviews, f"skill-B lost — got {list(reviews)}"
