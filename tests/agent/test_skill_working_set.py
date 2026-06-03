import durin.agent.skill_usage as su
from durin.agent.skill_usage import compute_working_set


def _patch_calls(monkeypatch, by_window):
    """by_window: {window_hours: {skill: total_count}} → fake collect_recent_skill_calls."""
    def fake(workspace, within_hours=None):
        counts = by_window.get(within_hours, {})
        return {s: {"read": c} for s, c in counts.items()}
    monkeypatch.setattr(su, "collect_recent_skill_calls", fake)


def test_frequent_ranked_then_recent_dedup(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {
        168.0: {"deploy": 9, "rebase": 5, "lint": 1},
        24.0: {"hotfix": 3, "deploy": 2},
    })
    cands = ["deploy", "rebase", "lint", "hotfix", "docs"]
    ws = compute_working_set(tmp_path, cands, recent=2, frequent=2,
                             frequent_window_hours=168.0, recent_window_hours=24.0)
    # frequent top-2 = deploy, rebase ; recent top-2 = hotfix, deploy(dup) → +hotfix
    # budget 4 → fill one more from candidate order skipping selected → lint
    assert ws == ["deploy", "rebase", "hotfix", "lint"]


def test_small_catalog_injects_everything(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {}, 24.0: {}})
    cands = ["a", "b", "c"]
    ws = compute_working_set(tmp_path, cands, recent=15, frequent=30)
    assert ws == ["a", "b", "c"]


def test_usage_for_unknown_skill_ignored(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {"ghost": 99}, 24.0: {}})
    cands = ["a", "b"]
    ws = compute_working_set(tmp_path, cands, recent=1, frequent=1)
    assert "ghost" not in ws and ws == ["a", "b"]


def test_budget_caps_large_cold_catalog(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {}, 24.0: {}})
    cands = [f"s{i}" for i in range(50)]
    ws = compute_working_set(tmp_path, cands, recent=5, frequent=10)
    assert ws == cands[:15]


def test_fill_prefers_used_over_unused(monkeypatch, tmp_path):
    # 4 used skills but each window's top-2 only selects {deploy, rebase};
    # budget=4 must fill the 2 remaining slots with the OTHER used skills
    # (lint, docs), never the unused tail (live-verify regression).
    _patch_calls(monkeypatch, {
        168.0: {"deploy": 10, "rebase": 8, "lint": 6, "docs": 4},
        24.0: {"deploy": 10, "rebase": 8, "lint": 6, "docs": 4},
    })
    # Unused skills come FIRST in candidate order, so a naive fill (pure
    # candidate order) would pick obscure1/obscure2 over the still-used
    # lint/docs. Only the used-over-unused preference yields the right set.
    cands = ["deploy", "rebase", "obscure1", "obscure2", "lint", "docs"]
    ws = compute_working_set(tmp_path, cands, recent=2, frequent=2)
    assert set(ws) == {"deploy", "rebase", "lint", "docs"}
    assert "obscure1" not in ws and "obscure2" not in ws
