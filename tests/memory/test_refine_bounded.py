"""The refine pass is bounded, incremental and cheap where it can be.

A wall-clock budget like every other dream pass, a verdict cache saved as it
grows (an interrupted run keeps what it judged), a cooldown for pairs whose
judge call failed (they are not re-judged every night), a name-overlap gate on
embedding-near candidates (the judge is spent on pairs that can plausibly be
the same thing), and concurrent judge calls with merges applied in order.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from durin.memory import refine_dream
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import _verdicts_path, run_refine

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
VERDICT = "===VERDICT===\n{v}\n===CONFIDENCE===\n{c}\n===REASONING===\nstub\n===END==="


def _entity(ws: Path, ref: str, name: str, *aliases: str) -> None:
    patches = [FieldPatch(kind="alias", value=a, author="agent", source_ref="s", at=NOW) for a in aliases]
    write_entity(ws, ref, patches, create=True, name=name)


def _alias_pairs(ws: Path, n: int) -> list[tuple[str, str]]:
    """n alias-overlap candidate pairs over 2n distinct entities."""
    pairs = []
    for i in range(n):
        a, b = f"topic:p{i}a", f"topic:p{i}b"
        _entity(ws, a, f"P{i} a", f"key{i}")
        _entity(ws, b, f"P{i} b", f"key{i}")
        pairs.append((a, b))
    return pairs


class _Stub:
    """Thread-safe judge stub: records prompts, can sleep, fail the first N
    calls with an unparseable reply, and track how many calls run at once."""

    def __init__(self, verdict="different", conf=90, *, sleep=0.0, fail_first=0, on_call=None):
        self.verdict, self.conf, self.sleep, self.fail_first, self.on_call = verdict, conf, sleep, fail_first, on_call
        self.prompts: list[str] = []
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()

    def __call__(self, prompt, **kw):
        with self._lock:
            self.prompts.append(prompt)
            i = len(self.prompts)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.on_call:
                self.on_call(i, prompt)
            if self.sleep:
                time.sleep(self.sleep)
            if i <= self.fail_first:
                return "no envelope here"
            return VERDICT.format(v=self.verdict, c=self.conf)
        finally:
            with self._lock:
                self.inflight -= 1


def _events(monkeypatch) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(refine_dream, "_emit", lambda event, **data: out.append((event, data)))
    return out


def test_the_budget_stops_the_pass_and_the_rest_waits_for_the_next_run(tmp_path, monkeypatch):
    _alias_pairs(tmp_path, 4)
    events = _events(monkeypatch)
    stub = _Stub(sleep=0.05)

    out = run_refine(tmp_path, llm_invoke=stub, max_seconds=0.01)

    assert out["budget_hit"] is True
    assert 1 <= out["judged"] < 4
    assert out["candidates"] == 4
    reached = [d for e, d in events if e == "memory.dream.max_seconds_reached"]
    assert reached and reached[0]["kind"] == "refine" and reached[0]["remaining"] >= 1


def test_the_verdict_cache_is_saved_as_the_pass_goes(tmp_path):
    """After a kill or a budget stop the verdicts already judged survive."""
    _alias_pairs(tmp_path, 3)
    seen: dict[str, int] = {}

    def on_call(i, prompt):
        if i == 3:
            p = _verdicts_path(tmp_path)
            seen["entries"] = len(json.loads(p.read_text())) if p.exists() else 0

    run_refine(tmp_path, llm_invoke=_Stub(on_call=on_call), judge_concurrency=1)

    assert seen["entries"] == 2


def test_a_failed_judge_call_is_cached_with_a_cooldown(tmp_path, monkeypatch):
    _alias_pairs(tmp_path, 1)
    first = _Stub(fail_first=99)
    out = run_refine(tmp_path, llm_invoke=first, error_cooldown_s=3600)
    assert [s["reason"].split(":")[0] for s in out["skipped"]] == ["judge_error"]
    assert len(first.prompts) == 3  # the judge's own retries
    entry = next(iter(json.loads(_verdicts_path(tmp_path).read_text()).values()))
    assert entry["verdict"] == "error" and entry["until"] > time.time()

    second = _Stub()
    out = run_refine(tmp_path, llm_invoke=second, error_cooldown_s=3600)
    assert [s["reason"] for s in out["skipped"]] == ["cached_error"]
    assert second.prompts == []

    third = _Stub()
    monkeypatch.setattr(refine_dream, "_now", lambda: time.time() + 7200)
    out = run_refine(tmp_path, llm_invoke=third, error_cooldown_s=3600)
    assert out["judged"] == 1 and third.prompts


def test_a_zero_cooldown_retries_failed_pairs_every_run(tmp_path):
    _alias_pairs(tmp_path, 1)
    run_refine(tmp_path, llm_invoke=_Stub(fail_first=99), error_cooldown_s=0)
    second = _Stub()
    out = run_refine(tmp_path, llm_invoke=second, error_cooldown_s=0)
    assert out["judged"] == 1 and second.prompts


class _FakeVI:
    """Neighbours keyed by a substring of the composed entity text."""

    def __init__(self, rows_by_substr):
        self._rows = rows_by_substr

    def search(self, query, *, top_k=10, where=None):
        for substr, rows in self._rows.items():
            if substr.lower() in query.lower():
                return rows[:top_k]
        return []

    def delete_by_id(self, ref):
        pass

    def upsert_entity_page(self, **kw):
        pass


def _near(ref, dist=0.2):
    t, s = ref.split(":", 1)
    return {"id": ref, "class_name": "entity_page", "_distance": dist, "path": f"memory/entities/{t}/{s}.md"}


def _semantic_workspace(ws: Path) -> _FakeVI:
    _entity(ws, "topic:email-flow", "Email Flow", "u1")
    _entity(ws, "topic:emailflow", "EmailFlow", "u2")
    _entity(ws, "project:auto-filling", "Auto Filling", "u3")
    _entity(ws, "project:mxhero-autofilling-system", "mxHERO Autofilling System", "u4")
    _entity(ws, "topic:kinesis-events", "Kinesis Events", "u5")
    _entity(ws, "topic:onedrive-share", "OneDrive Share", "u6")
    return _FakeVI({
        "Email Flow": [_near("topic:emailflow")],
        "Auto Filling": [_near("project:mxhero-autofilling-system")],
        "Kinesis Events": [_near("topic:onedrive-share")],
    })


def test_embedding_near_pairs_are_judged_only_with_a_name_overlap(tmp_path):
    ws = tmp_path / "gated"
    vi = _semantic_workspace(ws)
    out = run_refine(ws, llm_invoke=_Stub(), vector_index=vi, require_name_overlap=True)
    judged = {tuple(sorted(k["pair"])) for k in out["kept_separate"]}
    assert judged == {
        ("topic:email-flow", "topic:emailflow"),                        # hyphenation variant
        ("project:auto-filling", "project:mxhero-autofilling-system"),  # one name inside the other
    }
    assert [s["reason"] for s in out["skipped"]] == ["no_name_overlap"]


def test_the_name_gate_can_be_switched_off(tmp_path):
    ws = tmp_path / "open"
    vi = _semantic_workspace(ws)
    out = run_refine(ws, llm_invoke=_Stub(), vector_index=vi, require_name_overlap=False)
    assert out["judged"] == 3 and out["skipped"] == []


def test_alias_pairs_are_never_gated_by_name_overlap(tmp_path):
    _entity(tmp_path, "company:acme", "Acme", "the-firm")
    _entity(tmp_path, "company:zeta", "Zeta", "the-firm")
    out = run_refine(tmp_path, llm_invoke=_Stub(), require_name_overlap=True)
    assert out["judged"] == 1


def test_judge_calls_run_concurrently_while_merges_stay_serial(tmp_path):
    _alias_pairs(tmp_path, 4)
    stub = _Stub(sleep=0.1)
    out = run_refine(tmp_path, llm_invoke=stub, judge_concurrency=4)
    assert out["judged"] == 4
    assert stub.max_inflight == 4


def test_two_pairs_sharing_an_entity_in_one_chunk_merge_once(tmp_path):
    """x~y and y~z judged together, both 'same': the first merge absorbs y;
    the second must see that and skip instead of merging a vanished page."""
    _entity(tmp_path, "company:x", "X", "s1")
    _entity(tmp_path, "company:y", "Y", "s1", "s2")
    _entity(tmp_path, "company:z", "Z", "s2")
    out = run_refine(tmp_path, llm_invoke=_Stub("same", 99), judge_concurrency=2, confidence_threshold=95)
    assert len(out["merged"]) == 1
    assert any(s["reason"] == "merged_earlier" for s in out["skipped"])
    remaining = sorted(p.stem for p in (tmp_path / "memory" / "entities" / "company").glob("*.md"))
    assert len(remaining) == 2


def test_run_refine_pass_forwards_the_bounds(tmp_path, monkeypatch):
    from durin.memory import dream_passes
    seen: dict = {}
    monkeypatch.setattr(dream_passes, "run_refine", lambda ws, **kw: seen.update(kw) or {"merged": [], "kept_separate": [], "skipped": [], "candidates": 0, "judged": 0, "budget_hit": False})
    dream_passes.run_refine_pass(tmp_path, max_seconds=5, judge_concurrency=2, error_cooldown_days=3, require_name_overlap=False)
    assert seen["max_seconds"] == 5 and seen["judge_concurrency"] == 2
    assert seen["error_cooldown_s"] == 3 * 86400 and seen["require_name_overlap"] is False


def test_auto_absorb_config_carries_the_new_knobs():
    from durin.config.schema import AutoAbsorbConfig
    c = AutoAbsorbConfig()
    assert c.judge_concurrency == 3 and c.error_cooldown_days == 7 and c.require_name_overlap is True
