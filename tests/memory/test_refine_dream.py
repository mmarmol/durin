from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.refine_dream import (
    add_flagged,
    add_tombstone,
    is_tombstoned,
    read_flagged,
    remove_flagged,
    run_refine,
)

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _judge_stub(verdict, conf, counter=None):
    def inv(prompt, **kw):
        if counter is not None:
            counter["n"] += 1
        return (f"===VERDICT===\n{verdict}\n===CONFIDENCE===\n{conf}\n"
                f"===REASONING===\nstub reasoning\n===END===")
    return inv


def _two_dupes(ws):
    # two entities sharing the alias "mxHERO" -> a merge candidate
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Inc.")
    write_entity(ws, "company:mxhero_inc",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Incorporated")


def test_tombstone_roundtrip(tmp_path):
    assert not is_tombstoned(tmp_path, "company:a", "company:b")
    add_tombstone(tmp_path, "company:b", "company:a")   # order-independent
    assert is_tombstoned(tmp_path, "company:a", "company:b")


def test_refine_merges_same(tmp_path):
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 96))
    assert out["merged"], out
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()  # absorbed
    assert (tmp_path / "memory/entities/company/mxhero.md").exists()          # canonical


def test_refine_respects_tombstone(tmp_path):
    _two_dupes(tmp_path)
    add_tombstone(tmp_path, "company:mxhero", "company:mxhero_inc")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99))
    assert not out["merged"]
    assert any(s["reason"] == "tombstoned" for s in out["skipped"])
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()      # NOT merged


def test_refine_keeps_different(tmp_path):
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("different", 90))
    assert not out["merged"]
    assert out["kept_separate"]
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_skips_user_managed(tmp_path):
    _two_dupes(tmp_path)
    p = tmp_path / "memory/entities/company/mxhero.md"
    page = EntityPage.from_file(p)
    page.author = "user_authored"                      # user opted to manage it
    p.write_text(page.to_markdown(), encoding="utf-8")
    counter = {"n": 0}
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99, counter))
    assert any(s["reason"] == "user_managed" for s in out["skipped"])
    assert counter["n"] == 0                            # judge not reached
    assert not out["merged"]


def test_refine_merge_preserves_relations_and_attributes(tmp_path):
    # the canonical's relations + attributes must survive the absorb — the new
    # model carries the entity graph + structured facts on the page, and
    # _merge_pages used to drop them (data loss / G1).
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="relation", value={"to": "person:alex", "type": "founded_by"},
                             author="agent", source_ref="s", at=NOW),
                  FieldPatch(kind="attribute", key="hq", value="US",
                             author="dream", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Inc.")
    write_entity(tmp_path, "company:mxhero_inc",
                 [FieldPatch(kind="alias", value="mxHERO", author="agent", source_ref="s", at=NOW)],
                 create=True, name="mxHERO Incorporated")
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 98))
    assert out["merged"]
    page = EntityPage.from_file(tmp_path / "memory/entities/company/mxhero.md")
    assert {"to": "person:alex", "type": "founded_by"} in page.relations
    assert page.attributes.get("hq") == "US"


def _set_created(ws, ref, dt):
    t, _, s = ref.partition(":")
    p = ws / "memory/entities" / t / f"{s}.md"
    page = EntityPage.from_file(p)
    page.created_at = dt
    p.write_text(page.to_markdown(), encoding="utf-8")


def test_refine_skips_pair_created_this_run(tmp_path):
    # Run-scoped quarantine: a pair created at/after the run start is the run's
    # own fresh output and is never merged this run.
    _two_dupes(tmp_path)
    _set_created(tmp_path, "company:mxhero", datetime(2026, 6, 10, tzinfo=timezone.utc))
    _set_created(tmp_path, "company:mxhero_inc", datetime(2026, 6, 10, tzinfo=timezone.utc))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99),
                     run_started_at=datetime(2026, 6, 9, tzinfo=timezone.utc))
    assert not out["merged"]
    assert any(s["reason"] == "quarantine" for s in out["skipped"])
    assert (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_merges_pair_predating_run(tmp_path):
    # Entities that existed before the run started are eligible immediately.
    _two_dupes(tmp_path)
    _set_created(tmp_path, "company:mxhero", datetime(2026, 6, 10, tzinfo=timezone.utc))
    _set_created(tmp_path, "company:mxhero_inc", datetime(2026, 6, 10, tzinfo=timezone.utc))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99),
                     run_started_at=datetime(2026, 6, 11, tzinfo=timezone.utc))
    assert out["merged"]
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_no_cutoff_merges(tmp_path):
    # run_started_at=None disables the quarantine (standalone refine).
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 99), run_started_at=None)
    assert out["merged"]


# ---------------------------------------------------------------------------
# Tier-2 routing tests (Task 5)
# ---------------------------------------------------------------------------

def test_refine_tier2_escalates_unclear_and_merges(tmp_path, monkeypatch):
    """Tier-1 unclear@80 → escalate → same@97 → merged."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("same", 97, "confirmed same"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert out["merged"], out
    assert not (tmp_path / "memory/entities/company/mxhero_inc.md").exists()


def test_refine_tier2_escalates_unclear_and_keeps(tmp_path, monkeypatch):
    """Tier-1 unclear@80 → escalate → different@85 → kept."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("different", 85, "not the same"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert not out["merged"], out
    assert out["kept_separate"]


def test_refine_tier2_escalates_same_in_borderline_window(tmp_path, monkeypatch):
    """Tier-1 same@75 (in [floor, threshold)) → escalate → same@97 → merged."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("same", 97, "confirmed"))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 75),
                     confidence_threshold=95, escalate_floor=70)
    assert out["merged"], out


def test_refine_tier2_no_escalation_when_floor_zero(tmp_path, monkeypatch):
    """escalate_floor=0 disables Tier-2; unclear@80 → kept (old behavior)."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    called = {"n": 0}

    def _fake_escalate(ws, a, b, **kw):
        called["n"] += 1
        from durin.memory.absorb_judge import JudgeResult
        return JudgeResult("same", 97, "should not be called")

    monkeypatch.setattr(rd, "_escalate_judge", _fake_escalate)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=0)
    assert called["n"] == 0
    assert not out["merged"]
    assert out["kept_separate"]


def test_refine_tier2_no_escalation_same_above_threshold(tmp_path, monkeypatch):
    """same@97 >= threshold → merges directly, no escalation."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    called = {"n": 0}

    def _fake_escalate(ws, a, b, **kw):
        called["n"] += 1
        from durin.memory.absorb_judge import JudgeResult
        return JudgeResult("same", 99, "should not be called")

    monkeypatch.setattr(rd, "_escalate_judge", _fake_escalate)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 97),
                     confidence_threshold=95, escalate_floor=70)
    assert called["n"] == 0
    assert out["merged"]


def test_refine_tier2_best_effort_on_error(tmp_path, monkeypatch):
    """Tier-2 exception → pair kept (best-effort; never aborts pass)."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)

    def _raise(ws, a, b, **kw):
        raise RuntimeError("agent timeout")

    monkeypatch.setattr(rd, "_escalate_judge", _raise)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert not out["merged"]
    assert any("tier2_error" in str(k.get("reason", "")) for k in out["kept_separate"])


# ---------------------------------------------------------------------------
# Task 6: flagged-pairs store + escalation routing
# ---------------------------------------------------------------------------

def test_flagged_roundtrip(tmp_path):
    """add_flagged stores a record; read_flagged returns it."""
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="different", confidence=85, reasoning="they differ")
    records = read_flagged(tmp_path)
    assert len(records) == 1
    r = records[0]
    assert sorted(r["pair"]) == ["company:a", "company:b"]
    assert r["verdict"] == "different"
    assert r["confidence"] == 85
    assert r["reasoning"] == "they differ"
    assert "at" in r


def test_flagged_dedup_keeps_newest(tmp_path):
    """Duplicate pair key (same sorted refs) keeps the newest record."""
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="different", confidence=80, reasoning="first")
    add_flagged(tmp_path, "company:b", "company:a",
                verdict="unclear", confidence=72, reasoning="second")
    records = read_flagged(tmp_path)
    assert len(records) == 1
    assert records[0]["reasoning"] == "second"


def test_flagged_multiple_pairs(tmp_path):
    """Two distinct pairs → two records."""
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="different", confidence=80, reasoning="r1")
    add_flagged(tmp_path, "person:x", "person:y",
                verdict="unclear", confidence=60, reasoning="r2")
    records = read_flagged(tmp_path)
    assert len(records) == 2


def test_refine_escalated_non_merge_writes_flagged(tmp_path, monkeypatch):
    """Escalated pair with Tier-2 verdict != same → flagged record written."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    from durin.memory.absorb_judge import JudgeResult
    monkeypatch.setattr(rd, "_escalate_judge",
                        lambda ws, a, b, **kw: JudgeResult("unclear", 70, "agent unsure"))
    run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
               confidence_threshold=95, escalate_floor=70)
    records = read_flagged(tmp_path)
    assert len(records) == 1
    assert records[0]["verdict"] == "unclear"
    assert "agent unsure" in records[0]["reasoning"]


def test_refine_non_escalated_kept_not_flagged(tmp_path):
    """Non-escalated kept pairs (floor=0) are NOT written to flagged store."""
    _two_dupes(tmp_path)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("different", 90),
                     confidence_threshold=95, escalate_floor=0)
    assert out["kept_separate"]
    records = read_flagged(tmp_path)
    assert records == []


def test_refine_judge_error_emits_skipped_event(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    _two_dupes(tmp_path)
    # An invoke that never yields a parseable verdict -> JudgeError after retries.
    out = run_refine(tmp_path, llm_invoke=lambda prompt, **kw: "no verdict markers")
    assert any(s["reason"].startswith("judge_error") for s in out["skipped"])
    skips = [d for n, d in events if n == "memory.absorb.skipped"]
    assert any(d.get("reason") == "judge_error" for d in skips)


def test_refine_load_failed_emits_skipped_event(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    import durin.memory.refine_dream as refine_dream
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    _two_dupes(tmp_path)
    real_load_page = refine_dream._load_page

    def _load_page_one_missing(ws, ref):
        if ref == "company:mxhero_inc":
            return None
        return real_load_page(ws, ref)

    monkeypatch.setattr(refine_dream, "_load_page", _load_page_one_missing)
    out = run_refine(tmp_path, llm_invoke=_judge_stub("same", 96))
    assert any(s["reason"] == "load_failed" for s in out["skipped"])
    skips = [d for n, d in events if n == "memory.absorb.skipped"]
    assert any(d.get("reason") == "load_failed" for d in skips)


def test_flagged_empty_workspace(tmp_path):
    """read_flagged on a workspace with no flagged file returns empty list."""
    assert read_flagged(tmp_path) == []


# ---------------------------------------------------------------------------
# remove_flagged
# ---------------------------------------------------------------------------


def test_remove_flagged_drops_matching_pair(tmp_path):
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="unclear", confidence=70, reasoning="r")
    remove_flagged(tmp_path, "company:a", "company:b")
    assert read_flagged(tmp_path) == []


def test_remove_flagged_order_independent(tmp_path):
    """Argument order (a, b) vs (b, a) resolves to the same sorted key."""
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="unclear", confidence=70, reasoning="r")
    remove_flagged(tmp_path, "company:b", "company:a")
    assert read_flagged(tmp_path) == []


def test_remove_flagged_leaves_other_pairs(tmp_path):
    add_flagged(tmp_path, "company:a", "company:b",
                verdict="unclear", confidence=70, reasoning="r1")
    add_flagged(tmp_path, "person:x", "person:y",
                verdict="different", confidence=85, reasoning="r2")
    remove_flagged(tmp_path, "company:a", "company:b")
    remaining = read_flagged(tmp_path)
    assert len(remaining) == 1
    assert sorted(remaining[0]["pair"]) == ["person:x", "person:y"]


def test_remove_flagged_noop_on_missing(tmp_path):
    """No error when pair is not in the store."""
    remove_flagged(tmp_path, "company:a", "company:b")  # file doesn't exist — no error

    add_flagged(tmp_path, "person:x", "person:y",
                verdict="unclear", confidence=70, reasoning="r")
    remove_flagged(tmp_path, "company:a", "company:b")  # different pair — no error
    assert len(read_flagged(tmp_path)) == 1


def test_refine_capped_escalation_flags_pair(tmp_path, monkeypatch):
    """Past the per-run escalation cap, a borderline pair is flagged for the
    Bandeja instead of silently keeping the cheap verdict."""
    import durin.memory.refine_dream as rd

    _two_dupes(tmp_path)
    monkeypatch.setattr(rd, "_MAX_ESCALATIONS_PER_RUN", 0)
    monkeypatch.setattr(
        rd, "_escalate_judge",
        lambda ws, a, b, **kw: (_ for _ in ()).throw(
            AssertionError("must not escalate past the cap")))
    out = run_refine(tmp_path, llm_invoke=_judge_stub("unclear", 80),
                     confidence_threshold=95, escalate_floor=70)
    assert not out["merged"]
    flagged = read_flagged(tmp_path)
    assert len(flagged) == 1
    assert flagged[0]["reasoning"].startswith("escalation cap")


def test_refine_absorb_events_carry_entity_type(tmp_path, monkeypatch):
    """judged/auto_merged events must carry the entity type so per-class
    duplicate churn (e.g. feedback/stance/practice) is measurable."""
    import durin.agent.tools._telemetry as tel

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    _two_dupes(tmp_path)
    run_refine(tmp_path, llm_invoke=_judge_stub("same", 96))
    judged = [d for n, d in events if n == "memory.absorb.judged"]
    merged = [d for n, d in events if n == "memory.absorb.auto_merged"]
    assert judged and judged[0]["entity_type"] == "company"
    assert merged and merged[0]["entity_type"] == "company"
