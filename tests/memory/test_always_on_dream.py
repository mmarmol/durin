"""A4: always_on distillation pass — rank (drop contradictions) + fit a token
budget + mark always_on, never deleting an entity."""
from datetime import datetime, timezone

from durin.memory.always_on_dream import run_always_on_pass
from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity
from durin.memory.principal import _render_pinned_block, list_always_on
from durin.utils.helpers import estimate_text_tokens

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _feedback(ws, ref, text, name):
    write_entity(ws, ref, [FieldPatch(kind="body_append", value=text, author="agent",
                 source_ref="s", at=NOW)], create=True, name=name)


def _tok(ws, ref):
    t, s = ref.split(":", 1)
    page = EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")
    return estimate_text_tokens(_render_pinned_block(page))


def test_always_on_fits_budget_and_marks_without_data_loss(tmp_path):
    _feedback(tmp_path, "practice:verify", "Verify live before claiming done.", "Verify")
    _feedback(tmp_path, "stance:rigor", "Prefer rigor over speed.", "Rigor")
    _feedback(tmp_path, "stance:terse", "Be terse.", "Terse")
    budget = _tok(tmp_path, "practice:verify") + _tok(tmp_path, "stance:rigor")  # top 2 fit
    ranking = "practice:verify\nstance:rigor\nstance:terse"
    out = run_always_on_pass(tmp_path, token_budget=budget, llm_invoke=lambda p, **k: ranking)
    assert set(list_always_on(tmp_path)) == {"practice:verify", "stance:rigor"}
    assert out["selected"] == 2 and out["pruned"] == 1
    assert (tmp_path / "memory/entities/stance/terse.md").exists()  # pruned, NOT deleted


def test_always_on_drops_contradiction(tmp_path):
    _feedback(tmp_path, "stance:terse", "Always be terse.", "Terse")
    _feedback(tmp_path, "stance:verbose", "Always be detailed and verbose.", "Verbose")
    # the judge keeps terse and DROPS verbose (contradiction) → returns only terse
    out = run_always_on_pass(tmp_path, token_budget=10_000,
                             llm_invoke=lambda p, **k: "stance:terse")
    assert set(list_always_on(tmp_path)) == {"stance:terse"}
    assert out["dropped"] == 1
    assert (tmp_path / "memory/entities/stance/verbose.md").exists()  # dropped, NOT deleted


def test_always_on_fallback_without_llm(tmp_path):
    _feedback(tmp_path, "stance:a", "Guidance A.", "A")
    out = run_always_on_pass(tmp_path, token_budget=10_000, llm_invoke=None)
    assert list_always_on(tmp_path) == ["stance:a"]
    assert out["selected"] == 1


def test_always_on_unmarks_when_over_budget(tmp_path):
    # an item previously always_on but now pushed out of budget gets unmarked
    # (no data loss — the entity stays, only the flag flips).
    _feedback(tmp_path, "stance:old", "Old guidance.", "Old")
    from durin.memory.principal import mark_always_on
    mark_always_on(tmp_path, "stance:old", True)
    assert "stance:old" in list_always_on(tmp_path)
    out = run_always_on_pass(tmp_path, token_budget=0, llm_invoke=None)  # budget 0 → nothing fits
    assert list_always_on(tmp_path) == []
    assert out["selected"] == 0
    assert (tmp_path / "memory/entities/stance/old.md").exists()


# --- the pin is stable, and it holds rules, not document stubs ---------------


def _page(ws, ref):
    t, s = ref.split(":", 1)
    return EntityPage.from_file(ws / "memory" / "entities" / t / f"{s}.md")


def _hand_written(ws, ref, text, name):
    """A page the user wrote by hand: a file in the tree, no author stamp."""
    t, s = ref.split(":", 1)
    EntityPage(type=t, name=name, body=text).save(
        ws / "memory" / "entities" / t / f"{s}.md")


def _mined(ws, ref, text, name):
    """A rule the extract pass mined from a conversation, through the stage
    that writes it."""
    import json

    from durin.memory.extract_dream import mine_learnings
    from durin.memory.llm_invoke import LLMResponse

    reply = json.dumps([{"ref": ref, "name": name, "body": text}])
    mine_learnings(ws, "[turn-1] USER: " + text, source_ref="[[sessions/s1.md#turn-1]]",
                   llm_invoke=lambda prompt, **kw: LLMResponse(text=reply))


def _seed_from_document(ws, entities):
    """Ingest a document and run the document passes over it, the seed pass
    answering with *entities*."""
    import json

    from durin.memory.distill_dream import run_distill_reference_pass, run_seed_entities_pass
    from durin.memory.llm_invoke import LLMResponse
    from durin.memory.reference import ingest_reference

    slug = ingest_reference(
        ws, "handbook", "# Book\n\nintro.\n\n## Glossary\n\nterms.\n").ref.split(":", 1)[1]
    outline = json.dumps({"abstract": "A handbook.", "sections": {"Book": "i"}})
    run_distill_reference_pass(ws, llm_invoke=lambda prompt, **kw: LLMResponse(text=outline))
    out = run_seed_entities_pass(
        ws, llm_invoke=lambda prompt, **kw: LLMResponse(text=json.dumps(entities)))
    assert out["entities"] == len(entities) and out["errors"] == []
    return f"reference:{slug}"


def test_runs_with_no_content_change_flip_no_flag(tmp_path):
    # More rules than the budget holds: x is pinned, then two newer rules
    # push it out. Unpinning x must not make x the newest page, or the next
    # run pins it again and unpins another, every night.
    _feedback(tmp_path, "stance:x", "Guidance x.", "Rule X")
    budget = 2 * _tok(tmp_path, "stance:x")
    assert run_always_on_pass(tmp_path, token_budget=budget)["changed"] == 1
    _feedback(tmp_path, "stance:a", "Guidance a.", "Rule A")
    _feedback(tmp_path, "stance:b", "Guidance b.", "Rule B")

    first = run_always_on_pass(tmp_path, token_budget=budget)
    assert first["pruned"] == 1 and first["changed"] == 3
    pinned = set(list_always_on(tmp_path))
    assert pinned == {"stance:a", "stance:b"}

    for _ in range(2):
        again = run_always_on_pass(tmp_path, token_budget=budget)
        assert again["pruned"] == 1
        assert again["changed"] == 0
        assert set(list_always_on(tmp_path)) == pinned


def test_document_seeded_page_is_never_pinned(tmp_path):
    _seed_from_document(tmp_path, [
        {"ref": "practice:side-quests", "name": "Side quests",
         "significance": "A tool the book includes for creating side quests."},
        {"ref": "stance:credits", "name": "Credits"},  # no body at all
    ])
    out = run_always_on_pass(tmp_path, token_budget=10_000)
    assert list_always_on(tmp_path) == []
    assert out["selected"] == 0 and out["changed"] == 0


def test_document_seeded_page_pinned_earlier_is_unpinned(tmp_path):
    from durin.memory.principal import mark_always_on

    _seed_from_document(tmp_path, [
        {"ref": "practice:side-quests", "name": "Side quests",
         "significance": "A tool the book includes for creating side quests."},
    ])
    mark_always_on(tmp_path, "practice:side-quests", True)
    _feedback(tmp_path, "stance:terse", "Be terse.", "Terse")

    out = run_always_on_pass(tmp_path, token_budget=10_000)
    assert list_always_on(tmp_path) == ["stance:terse"]
    assert out["changed"] == 2  # the stub off, the rule on
    assert run_always_on_pass(tmp_path, token_budget=10_000)["changed"] == 0


def test_only_document_seeded_pages_still_unpins_them(tmp_path):
    from durin.memory.principal import mark_always_on

    _seed_from_document(tmp_path, [{"ref": "practice:side-quests", "name": "Side quests"}])
    mark_always_on(tmp_path, "practice:side-quests", True)
    out = run_always_on_pass(tmp_path, token_budget=10_000)
    assert list_always_on(tmp_path) == []
    assert out["selected"] == 0 and out["changed"] == 1


def test_rule_the_agent_wrote_stays_a_candidate_when_linked_to_a_document(tmp_path):
    # The repair pass links a page the agent wrote in a session to the
    # document read in that session. The page is still the agent's rule.
    import json

    from durin.memory.derived_from_dream import link_derived_from_for_session
    from durin.memory.llm_invoke import LLMResponse
    from durin.memory.reference import ingest_reference

    doc = ingest_reference(tmp_path, "style guide", "Write short sentences.").ref
    _feedback(tmp_path, "practice:short-sentences", "Write short sentences.", "Short sentences")
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    session = sdir / "s1.jsonl"
    session.write_text("\n".join(json.dumps(m) for m in [
        {"_type": "metadata", "key": "s1"},
        {"role": "tool", "content": json.dumps({"reference": doc})},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "memory_upsert_entity",
                         "arguments": json.dumps({"ref": "practice:short-sentences"})}}]},
    ]), encoding="utf-8")
    linked = link_derived_from_for_session(
        tmp_path, session, llm_invoke=lambda prompt, **kw: LLMResponse(
            text=json.dumps({"practice:short-sentences": [doc]})))
    assert linked["linked"][0]["committed"] is True

    run_always_on_pass(tmp_path, token_budget=10_000)
    assert list_always_on(tmp_path) == ["practice:short-sentences"]


def test_rule_a_document_also_covers_stays_a_candidate(tmp_path):
    # The seed pass reuses the ref of a page that already exists. The rule's
    # body came from a conversation; the document only added a source link.
    _feedback(tmp_path, "practice:session-zero", "Always run a session zero.", "Session zero")
    doc = _seed_from_document(
        tmp_path, [{"ref": "practice:session-zero", "name": "Session zero",
                    "significance": "A first meeting the book recommends."}])
    assert _page(tmp_path, "practice:session-zero").derived_from == [doc]

    run_always_on_pass(tmp_path, token_budget=10_000)
    assert list_always_on(tmp_path) == ["practice:session-zero"]


def test_user_written_rule_ranks_ahead_of_agent_and_dream_rules(tmp_path):
    _hand_written(tmp_path, "stance:mine", "Never invent what is not verified.", "Verified only")
    _feedback(tmp_path, "stance:agent", "Prefer rigor over speed today.", "Rigor")
    _mined(tmp_path, "feedback:dream", "Reply in the user's language.", "Language")
    budget = max(_tok(tmp_path, r) for r in ("stance:mine", "stance:agent", "feedback:dream"))

    run_always_on_pass(tmp_path, token_budget=budget)
    assert list_always_on(tmp_path) == ["stance:mine"]

    # Being pinned must not cost the page its place: newer agent and dream
    # content still ranks behind it.
    write_entity(tmp_path, "stance:agent", [FieldPatch(
        kind="body_replace", value="Prefer rigor over speed, always.", author="agent",
        source_ref="s2", at=NOW)])
    _mined(tmp_path, "feedback:dream", "Reply in the language the user writes.", "Language")
    out = run_always_on_pass(tmp_path, token_budget=budget)
    assert list_always_on(tmp_path) == ["stance:mine"]
    assert out["changed"] == 0


def test_flag_flip_moves_neither_part_of_the_ranking_key(tmp_path):
    _hand_written(tmp_path, "stance:mine", "Never invent what is not verified.", "Verified only")
    _feedback(tmp_path, "stance:agent", "Prefer rigor over speed.", "Rigor")
    # Commit the hand-written page as the writer's guard does on any write,
    # so "before" is the page as the pass will read it.
    _feedback(tmp_path, "stance:other", "Be terse.", "Terse")
    before = {r: _page(tmp_path, r) for r in ("stance:mine", "stance:agent")}

    out = run_always_on_pass(tmp_path, token_budget=10_000)
    assert out["changed"] == 3
    for ref, was in before.items():
        now = _page(tmp_path, ref)
        assert now.attributes.get("always_on") is True
        assert now.updated_at == was.updated_at
        assert now.author == was.author
    assert before["stance:mine"].author == "user_authored"

    out = run_always_on_pass(tmp_path, token_budget=0)  # every flag off again
    assert out["changed"] == 3
    for ref, was in before.items():
        now = _page(tmp_path, ref)
        assert now.updated_at == was.updated_at
        assert now.author == was.author


def test_flag_flips_do_not_reopen_cached_refine_verdicts(tmp_path):
    from durin.memory.refine_dream import run_refine

    # two rules sharing an alias: one candidate pair for the dedup pass
    for ref, name in (("stance:terse", "Terse"), ("stance:brief", "Brief")):
        write_entity(tmp_path, ref, [
            FieldPatch(kind="alias", value="short answers", author="agent",
                       source_ref="s", at=NOW),
            FieldPatch(kind="body_append", value=f"Be {name.lower()}.", author="agent",
                       source_ref="s", at=NOW)], create=True, name=name)
    judged = {"n": 0}

    def judge(prompt, **kw):
        judged["n"] += 1
        return ("===VERDICT===\ndifferent\n===CONFIDENCE===\n90\n"
                "===REASONING===\ntwo rules\n===END===")

    run_refine(tmp_path, llm_invoke=judge)
    assert judged["n"] == 1
    assert run_always_on_pass(tmp_path, token_budget=10_000)["changed"] == 2
    run_refine(tmp_path, llm_invoke=judge)
    assert run_always_on_pass(tmp_path, token_budget=0)["changed"] == 2
    run_refine(tmp_path, llm_invoke=judge)
    assert judged["n"] == 1


def test_event_reports_the_number_of_flags_changed(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel

    events = []
    monkeypatch.setattr(tel, "emit_tool_event", lambda name, data: events.append((name, data)))
    _feedback(tmp_path, "stance:a", "Guidance a.", "Rule A")
    _feedback(tmp_path, "stance:b", "Guidance b.", "Rule B")

    run_always_on_pass(tmp_path, token_budget=10_000)
    run_always_on_pass(tmp_path, token_budget=10_000)
    changed = [d["changed"] for n, d in events if n == "memory.dream.always_on"]
    assert changed == [2, 0]
