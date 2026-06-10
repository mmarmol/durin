"""identity.md carries the per-turn skill_observe trigger instructions.

The structural wiring for the observation queue: the tool description alone is
a weak signal, so the identity template must name the tool, the trigger
conditions (all four kinds), and the log-don't-act rule.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY = _ROOT / "durin" / "templates" / "agent" / "identity.md"


def _norm() -> str:
    return re.sub(r"\s+", " ", _IDENTITY.read_text(encoding="utf-8"))


def test_identity_names_skill_observe_tool():
    assert "skill_observe" in _norm()


def test_identity_lists_all_four_kinds():
    t = _norm().lower()
    for kind in ("correction", "gap", "improvement", "simplify"):
        assert kind in t, f"missing trigger kind: {kind}"


def test_identity_says_log_dont_act():
    t = _norm().lower()
    assert "log, don't act" in t
    # acting is curation's job, not the in-loop agent's
    assert "curation" in t


def test_identity_mentions_new_prefix_for_gaps():
    assert "new:" in _norm()
