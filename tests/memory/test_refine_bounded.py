"""The refine pass is bounded, incremental and cheap where it can be.

A wall-clock budget like every other dream pass, a verdict cache flushed as
it grows (an interrupted run keeps what it judged), a recheck cooldown for
pairs without a settled verdict (the scan advances instead of re-answering
them every night), provider failures that stop the run instead of poisoning
the cache, a name signal that orders embedding-near candidates, and
concurrent judge calls — in the caller's telemetry context, chunks free of
shared pages — with merges applied in order.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from durin.memory import refine_dream
from durin.memory.field_patch import FieldPatch
from durin.memory.llm_invoke import LLMResponse
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
    stub = _Stub(sleep=0.3)

    out = run_refine(tmp_path, llm_invoke=stub, max_seconds=0.1)

    assert out["yielded"] is True and out["stop_reason"] == "max_seconds"
    assert out["judged"] == 1  # the chunk in flight completes; no new chunk starts
    reached = [d for e, d in events if e == "memory.dream.max_seconds_reached"]
    assert reached and reached[0]["remaining"] == out["candidates"] - out["judged"] - len(out["skipped"])
    assert out["candidates"] == 4
    reached = [d for e, d in events if e == "memory.dream.max_seconds_reached"]
    assert reached and reached[0]["kind"] == "refine" and reached[0]["remaining"] >= 1


def test_the_budget_covers_candidate_generation(tmp_path, monkeypatch):
    """The clock starts before the candidate walk (the phase that scales
    with the workspace), and is checked before every pair, so a run whose
    generation alone exhausted the budget judges nothing and yields."""
    from durin.memory import absorption
    _alias_pairs(tmp_path, 3)
    original = absorption.EntityAbsorption.find_candidates

    def slow_find(self):
        time.sleep(0.3)
        return original(self)

    monkeypatch.setattr(absorption.EntityAbsorption, "find_candidates", slow_find)
    stub = _Stub()
    out = run_refine(tmp_path, llm_invoke=stub, max_seconds=0.1)
    assert out["yielded"] is True and out["judged"] == 0 and stub.prompts == []
    assert out["stop_reason"] == "max_seconds"


def test_the_verdict_cache_is_flushed_as_the_pass_goes(tmp_path, monkeypatch):
    """After a kill or a budget stop the verdicts already judged survive:
    the cache is flushed every few pairs (here: every pair) and on exit."""
    monkeypatch.setattr(refine_dream, "_CACHE_FLUSH_EVERY", 1)
    _alias_pairs(tmp_path, 3)
    seen: dict[str, int] = {}

    def on_call(i, prompt):
        if i == 3:
            p = _verdicts_path(tmp_path)
            seen["entries"] = len(json.loads(p.read_text())) if p.exists() else 0

    run_refine(tmp_path, llm_invoke=_Stub(on_call=on_call), judge_concurrency=1)

    assert seen["entries"] == 2


def test_the_cache_is_flushed_on_exit_even_when_the_pass_raises(tmp_path, monkeypatch):
    _alias_pairs(tmp_path, 2)
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("absorb exploded")
        return "===VERDICT===\ndifferent\n===CONFIDENCE===\n90\n===REASONING===\nok\n===END==="

    # Make the second pair's application raise from a step outside the judge.
    monkeypatch.setattr(refine_dream, "_emit", lambda event, **d: (_ for _ in ()).throw(RuntimeError("emit exploded")) if event == "memory.absorb.judged" and calls["n"] == 2 else None)
    try:
        run_refine(tmp_path, llm_invoke=boom, judge_concurrency=1)
    except RuntimeError:
        pass
    assert _verdicts_path(tmp_path).exists()
    assert len(json.loads(_verdicts_path(tmp_path).read_text())) >= 1


def test_an_unparseable_reply_is_remembered_with_a_recheck_cooldown(tmp_path, monkeypatch):
    _alias_pairs(tmp_path, 1)
    first = _Stub(fail_first=99)
    out = run_refine(tmp_path, llm_invoke=first, recheck_cooldown_s=3600)
    assert [s["reason"].split(":")[0] for s in out["skipped"]] == ["judge_error"]
    assert len(first.prompts) == 3  # the judge's own retries, with feedback
    entry = next(iter(json.loads(_verdicts_path(tmp_path).read_text()).values()))
    assert entry["verdict"] == "error" and entry["until"] > time.time()

    second = _Stub()
    out = run_refine(tmp_path, llm_invoke=second, recheck_cooldown_s=3600)
    assert [s["reason"] for s in out["skipped"]] == ["cached_error"]
    assert second.prompts == []

    third = _Stub()
    monkeypatch.setattr(refine_dream, "_now", lambda: time.time() + 7200)
    out = run_refine(tmp_path, llm_invoke=third, recheck_cooldown_s=3600)
    assert out["judged"] == 1 and third.prompts


def test_a_zero_recheck_cooldown_ignores_remembered_errors(tmp_path):
    """An operator recovering from a bad night sets the cooldown to 0: entries
    already on disk must not keep the pairs skipped."""
    _alias_pairs(tmp_path, 1)
    run_refine(tmp_path, llm_invoke=_Stub(fail_first=99), recheck_cooldown_s=3600)
    second = _Stub()
    out = run_refine(tmp_path, llm_invoke=second, recheck_cooldown_s=0)
    assert out["judged"] == 1 and second.prompts


def test_lowering_the_recheck_cooldown_shortens_remembered_entries(tmp_path):
    """The expiry stored on disk is clamped by the cooldown configured now,
    so an operator who lowers the knob is not stuck with the old window."""
    _alias_pairs(tmp_path, 1)
    run_refine(tmp_path, llm_invoke=_Stub("unclear", 50), recheck_cooldown_s=30 * 86400)
    again = _Stub("unclear", 50)
    out = run_refine(tmp_path, llm_invoke=again, recheck_cooldown_s=30 * 86400)
    assert again.prompts == [] and out["skipped"][0]["reason"] == "cached_verdict"
    lowered = _Stub("unclear", 50)
    real_now = refine_dream._now
    try:
        refine_dream._now = lambda: real_now() + 2  # two seconds later, cooldown now 1 s
        out = run_refine(tmp_path, llm_invoke=lowered, recheck_cooldown_s=1)
    finally:
        refine_dream._now = real_now
    assert out["judged"] == 1 and lowered.prompts


def test_a_corrupt_until_value_does_not_abort_the_pass(tmp_path):
    _alias_pairs(tmp_path, 1)
    run_refine(tmp_path, llm_invoke=_Stub(fail_first=99), recheck_cooldown_s=3600)
    p = _verdicts_path(tmp_path)
    data = json.loads(p.read_text())
    for v in data.values():
        v["until"] = "2026-09-20"
    p.write_text(json.dumps(data))
    out = run_refine(tmp_path, llm_invoke=_Stub(), recheck_cooldown_s=3600)
    assert out["judged"] == 1


def test_unsettled_verdicts_are_remembered_so_the_scan_advances(tmp_path):
    """`unclear` and a below-threshold `same` are not settled, but re-judging
    them every run would starve the tail under a budget; they are remembered
    with the recheck cooldown and skipped until it expires."""
    _alias_pairs(tmp_path, 2)
    out = run_refine(tmp_path, llm_invoke=_Stub("unclear", 50), recheck_cooldown_s=3600)
    assert out["judged"] == 2
    again = _Stub("unclear", 50)
    out = run_refine(tmp_path, llm_invoke=again, recheck_cooldown_s=3600)
    assert again.prompts == [] and [s["reason"] for s in out["skipped"]] == ["cached_verdict"] * 2


def test_a_provider_failure_is_not_remembered_and_stops_the_run(tmp_path, monkeypatch):
    """A dead key or an outage is a property of the moment: nothing is cached
    against the pairs, and after a few failures in a row the pass stops so
    the remaining candidates are judged on a later run."""
    _alias_pairs(tmp_path, 6)
    events = _events(monkeypatch)
    calls = {"n": 0}

    def provider_down(prompt, **kw):
        calls["n"] += 1
        return LLMResponse(text="Error calling LLM: 401 invalid api key", finish_reason="error")

    out = run_refine(tmp_path, llm_invoke=provider_down, recheck_cooldown_s=3600, judge_concurrency=1)

    assert out["yielded"] is True and out["stop_reason"] == "judge_unavailable"
    assert calls["n"] == 3  # one attempt per pair, three pairs, then stop
    assert not _verdicts_path(tmp_path).exists()
    assert any(e == "memory.absorb.judge_unavailable" for e, _ in events)


def test_a_transport_exception_counts_as_a_provider_failure(tmp_path):
    _alias_pairs(tmp_path, 1)

    def raises(prompt, **kw):
        raise ConnectionError("provider down")

    out = run_refine(tmp_path, llm_invoke=raises, recheck_cooldown_s=3600)
    assert [s["reason"].split(":")[0] for s in out["skipped"]] == ["judge_error"]
    assert not _verdicts_path(tmp_path).exists()


class _FakeVI:
    """The index surface the semantic walk reads: every stored entity vector
    in one call. Pages missing from the index would be embedded; none are."""

    def __init__(self, vectors):
        self.vectors = dict(vectors)

    def entity_page_vectors(self):
        return dict(self.vectors)

    def embed_passages(self, texts):
        raise AssertionError(f"walk tried to embed {len(texts)} page(s); all were indexed")

    def delete_by_id(self, ref):
        pass

    def upsert_entity_page(self, **kw):
        pass


def _pair(axis: int, dist: float, dim: int = 12) -> tuple[list[float], list[float]]:
    """Two vectors ``dist`` apart in squared L2, alone on their own axes so
    they are far (2.0) from every other pair."""
    a = [0.0] * dim
    a[axis] = 1.0
    b = list(a)
    b[axis + 1] = dist ** 0.5
    return a, b


def _semantic_workspace(ws: Path) -> _FakeVI:
    _entity(ws, "topic:email-flow", "Email Flow", "u1")
    _entity(ws, "topic:emailflow", "EmailFlow", "u2")
    _entity(ws, "project:auto-filling", "Auto Filling", "u3")
    _entity(ws, "project:mxhero-autofilling-system", "mxHERO Autofilling System", "u4")
    _entity(ws, "topic:kinesis-events", "Kinesis Events", "u5")
    _entity(ws, "topic:onedrive-share", "OneDrive Share", "u6")
    _entity(ws, "company:hp", "HP", "u7")
    _entity(ws, "company:hp-inc", "HP Inc", "u8")
    _entity(ws, "topic:configuracion", "Configuración", "u9")
    _entity(ws, "topic:configuracion-avanzada", "Configuracion avanzada", "u10")
    vectors = {}
    for axis, (a, b, dist) in enumerate((
        ("topic:email-flow", "topic:emailflow", 0.24),
        ("project:auto-filling", "project:mxhero-autofilling-system", 0.23),
        ("topic:kinesis-events", "topic:onedrive-share", 0.10),   # nearest of all, no name overlap
        ("company:hp", "company:hp-inc", 0.22),
        ("topic:configuracion", "topic:configuracion-avanzada", 0.21),
    )):
        vectors[a], vectors[b] = _pair(axis * 2, dist)
    return _FakeVI(vectors)


def test_the_name_signal_orders_embedding_near_pairs(tmp_path):
    """`prioritize` (the default) judges pairs whose names overlap first —
    accents, hyphenation and acronyms included — and the rest after, by
    distance; nothing is excluded."""
    from durin.memory.absorption import EntityAbsorption
    ws = tmp_path / "prio"
    vi = _semantic_workspace(ws)
    cands = EntityAbsorption(workspace=ws, vector_index=vi).find_semantic_candidates(
        vi, distance_threshold=0.30, name_gate="prioritize")
    order = [tuple(sorted(c.refs)) for c in cands]
    assert order[-1] == ("topic:kinesis-events", "topic:onedrive-share")  # closest, but last
    assert all(c.name_overlap for c in cands[:-1]) and cands[-1].name_overlap is False
    assert ("company:hp", "company:hp-inc") in order
    assert ("topic:configuracion", "topic:configuracion-avanzada") in order[:-1]


def test_names_overlap_is_structural_not_a_substring_or_a_stopword():
    from durin.memory.absorption import names_overlap
    ok = lambda a, b: names_overlap(a, b)  # noqa: E731
    assert ok(["email_flow", "email flow"], ["emailflow"])            # hyphenation
    assert ok(["auto_filling"], ["mxhero_autofilling_system"])         # a run of tokens
    assert ok(["hp"], ["hp_inc"]) and ok(["s3"], ["aws_s3"])          # acronym = whole name
    assert ok(["configuracion"], ["configuracion_avanzada"])          # shared long token
    assert not ok(["configuracion_de_correo"], ["reglas_de_flujo"])   # a two-letter word is not evidence
    assert not ok(["ana"], ["banana_bread"])                           # substring across a boundary
    assert not ok(["kinesis_events"], ["onedrive_share"])


def test_name_forms_drop_the_unnamed_sentinel():
    from durin.memory.absorption import name_forms
    from durin.memory.entity_page import EntityPage
    page = EntityPage(type="topic", name="🚀", aliases=["—"])
    assert name_forms("topic:rocket", page) == ["rocket"]


def test_the_name_signal_can_require_or_be_switched_off(tmp_path):
    from durin.memory.absorption import EntityAbsorption
    ws = tmp_path / "modes"
    vi = _semantic_workspace(ws)
    ab = EntityAbsorption(workspace=ws, vector_index=vi)
    required = ab.find_semantic_candidates(vi, distance_threshold=0.30, name_gate="require")
    assert ("topic:kinesis-events", "topic:onedrive-share") not in [tuple(sorted(c.refs)) for c in required]
    assert len(required) == 4
    off = ab.find_semantic_candidates(vi, distance_threshold=0.30, name_gate="off")
    assert [tuple(sorted(c.refs)) for c in off][0] == ("topic:kinesis-events", "topic:onedrive-share")
    assert all(c.name_overlap is None for c in off)


def test_run_refine_reports_the_name_signal_on_judged_pairs(tmp_path, monkeypatch):
    ws = tmp_path / "judged"
    vi = _semantic_workspace(ws)
    events = _events(monkeypatch)
    out = run_refine(ws, llm_invoke=_Stub(), vector_index=vi, semantic_name_gate="prioritize")
    assert out["judged"] == 5
    judged = [d for e, d in events if e == "memory.absorb.judged"]
    assert sorted(d["name_overlap"] for d in judged) == [False, True, True, True, True]


def test_alias_pairs_are_never_gated_by_name_overlap(tmp_path):
    _entity(tmp_path, "company:acme", "Acme", "the-firm")
    _entity(tmp_path, "company:zeta", "Zeta", "the-firm")
    out = run_refine(tmp_path, llm_invoke=_Stub(), semantic_name_gate="require")
    assert out["judged"] == 1


def test_judge_calls_run_concurrently_while_merges_stay_serial(tmp_path):
    _alias_pairs(tmp_path, 4)
    stub = _Stub(sleep=0.1)
    out = run_refine(tmp_path, llm_invoke=stub, judge_concurrency=4)
    assert out["judged"] == 4
    assert stub.max_inflight == 4


def test_judge_workers_keep_the_callers_telemetry_context(tmp_path):
    """The provider emits its cost rows through a ContextVar-bound sink; a
    bare thread pool would run the judge without it and the refine pass's
    token accounting would vanish."""
    from durin.telemetry.logger import bind_telemetry, current_telemetry, reset_telemetry

    class _Sink:
        session_key = "test-refine"

    _alias_pairs(tmp_path, 3)
    seen: list = []

    def inv(prompt, **kw):
        seen.append(current_telemetry())
        time.sleep(0.02)
        return VERDICT.format(v="different", c=90)

    token = bind_telemetry(_Sink())
    try:
        run_refine(tmp_path, llm_invoke=inv, judge_concurrency=3)
    finally:
        reset_telemetry(token)
    assert len(seen) == 3 and all(isinstance(s, _Sink) for s in seen)


def test_two_pairs_sharing_an_entity_are_never_judged_in_one_chunk(tmp_path):
    """x~y and y~z: the second pair waits for the next chunk, so the merge of
    the first is visible (y absorbed) before the second is even judged."""
    _entity(tmp_path, "company:x", "X", "s1")
    _entity(tmp_path, "company:y", "Y", "s1", "s2")
    _entity(tmp_path, "company:z", "Z", "s2")
    stub = _Stub("same", 99, sleep=0.05)
    out = run_refine(tmp_path, llm_invoke=stub, judge_concurrency=2, confidence_threshold=95)
    assert len(out["merged"]) == 1
    assert stub.max_inflight == 1
    assert out["judged"] == 1 and any(s["reason"] in ("load_failed", "tombstoned") for s in out["skipped"])
    remaining = sorted(p.stem for p in (tmp_path / "memory" / "entities" / "company").glob("*.md"))
    assert len(remaining) == 2


def test_a_page_that_cannot_be_rendered_costs_one_pair_not_the_pass(tmp_path):
    _alias_pairs(tmp_path, 3)
    calls = {"n": 0}

    def inv(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyError("template placeholder")  # not a JudgeError, not a provider failure
        return VERDICT.format(v="different", c=90)

    out = run_refine(tmp_path, llm_invoke=inv, judge_concurrency=1)
    assert out["judged"] == 2 and out["yielded"] is False
    assert sum(1 for s in out["skipped"] if s["reason"].startswith("judge_error")) == 1


def test_run_refine_pass_forwards_the_bounds(tmp_path, monkeypatch):
    from durin.memory import dream_passes
    seen: dict = {}

    def fake_run_refine(ws, **kw):
        seen.update(kw)
        return {"merged": [], "kept_separate": [], "skipped": [], "candidates": 0,
                "judged": 0, "yielded": False, "stop_reason": None}

    monkeypatch.setattr(dream_passes, "run_refine", fake_run_refine)
    dream_passes.run_refine_pass(tmp_path, max_seconds=5, judge_concurrency=2, recheck_days=3, semantic_name_gate="off")
    assert seen["max_seconds"] == 5 and seen["judge_concurrency"] == 2
    assert seen["recheck_cooldown_s"] == 3 * 86400 and seen["semantic_name_gate"] == "off"


def test_auto_absorb_config_carries_the_new_knobs():
    from durin.config.schema import AutoAbsorbConfig
    c = AutoAbsorbConfig()
    assert c.judge_concurrency == 3 and c.recheck_days == 7 and c.semantic_name_gate == "prioritize"


def test_aux_retry_mode_reads_the_agent_defaults():
    from durin.config.schema import Config
    from durin.memory.llm_invoke import _retry_mode
    cfg = Config()
    assert _retry_mode(cfg) == cfg.agents.defaults.provider_retry_mode
