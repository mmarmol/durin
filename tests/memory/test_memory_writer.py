import threading
from datetime import datetime, timezone

from durin.memory.entity_page import EntityPage
from durin.memory.field_patch import FieldPatch
from durin.memory.git_plumbing import (
    build_commit_with_file,
    head_sha,
    read_blob_at_head,
)
from durin.memory.memory_writer import write_entity
from durin.memory.provenance import author_scope
from durin.utils.git_repo import GitRepo

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 6, tzinfo=timezone.utc)


def _page_path(ws, ref):
    type_, _, slug = ref.partition(":")
    return ws / "memory" / "entities" / type_ / f"{slug}.md"


# ---- plumbing helpers (Task 4) -------------------------------------------

def _init_repo(tmp_path):
    from dulwich import porcelain
    root = tmp_path / "memory"
    root.mkdir()
    porcelain.init(str(root))
    (root / "seed.md").write_text("seed", encoding="utf-8")
    porcelain.add(str(root), paths=[str(root / "seed.md")])
    porcelain.commit(str(root), message=b"init", author=b"t <t@t>",
                     committer=b"t <t@t>")
    return root


def test_plumbing_roundtrip(tmp_path):
    root = _init_repo(tmp_path)
    base = head_sha(root)
    assert read_blob_at_head(root, "seed.md") == b"seed"
    assert read_blob_at_head(root, "missing.md") is None
    new = build_commit_with_file(root, base, "entities/company/x.md", b"hello",
                                 author=b"a <a@a>", message=b"add x")
    assert new is not None
    assert head_sha(root) == base                    # ref NOT moved by plumbing


# ---- writer (Task 5) ------------------------------------------------------

def test_two_writers_different_fields_both_land(tmp_path):
    ws = tmp_path
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="relation",
                             value=dict(to="company:carahsoft", type="partner"),
                             author="agent", source_ref="a", at=NOW)])
    write_entity(ws, "company:mxhero",
                 [FieldPatch(kind="alias", value="mxHERO Inc.", author="agent",
                             source_ref="b", at=NOW)])
    page = EntityPage.from_file(_page_path(ws, "company:mxhero"))
    assert any(r["to"] == "company:carahsoft" for r in page.relations)
    assert "mxHERO Inc." in page.aliases


def test_body_replace_precedence_survives_persistence(tmp_path):
    ws = tmp_path
    # A user authors the body; prov["body"]=user must persist via git round-trip.
    write_entity(ws, "topic:t",
                 [FieldPatch(kind="body_append", value="User's careful notes.",
                             author="user", source_ref="manual", at=NOW)], create=True)
    # In a SEPARATE write, the agent tries to replace the whole body. It must
    # read prov["body"]=user back from disk and degrade to a lossless append.
    write_entity(ws, "topic:t",
                 [FieldPatch(kind="body_replace", value="Agent rewrite.",
                             author="agent", source_ref="s#t1", at=LATER)])
    page = EntityPage.from_file(_page_path(ws, "topic:t"))
    assert "User's careful notes." in page.body     # not clobbered across writes
    assert "Agent rewrite." in page.body            # appended, not lost
    assert page.provenance["body"]["author"] == "user"


def test_body_replace_overwrites_agent_body_across_writes(tmp_path):
    ws = tmp_path
    write_entity(ws, "topic:t",
                 [FieldPatch(kind="body_append", value="Old agent prose.",
                             author="agent", source_ref="s#t1", at=NOW)], create=True)
    write_entity(ws, "topic:t",
                 [FieldPatch(kind="body_replace", value="Fresh canonical prose.",
                             author="agent", source_ref="s#t2", at=LATER)])
    page = EntityPage.from_file(_page_path(ws, "topic:t"))
    assert "Old agent prose." not in page.body      # replaced cleanly
    assert page.body.strip() == "Fresh canonical prose."


def test_write_entity_sets_display_name(tmp_path):
    write_entity(tmp_path, "company:mxhero",
                 [FieldPatch(kind="body_append", value="x", author="agent",
                             source_ref="s", at=NOW)], create=True, name="mxHERO Inc.")
    page = EntityPage.from_file(_page_path(tmp_path, "company:mxhero"))
    assert page.name == "mxHERO Inc."


def test_missing_without_create_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        write_entity(tmp_path, "company:nope",
                     [FieldPatch(kind="alias", value="x", author="agent",
                                 source_ref="s", at=NOW)])


# ---- concurrency + idempotency (Task 6) -----------------------------------

def test_idempotent_attribute_reapply(tmp_path):
    ws = tmp_path
    write_entity(ws, "company:x",
                 [FieldPatch(kind="attribute", key="hq", value="SF",
                             author="dream", source_ref="s", at=NOW)], create=True)
    write_entity(ws, "company:x",
                 [FieldPatch(kind="attribute", key="hq", value="SF",
                             author="dream", source_ref="s", at=NOW)])
    page = EntityPage.from_file(_page_path(ws, "company:x"))
    assert page.attributes["hq"] == "SF"
    assert list(page.attributes.keys()) == ["hq"]


def _concurrent_relation_round(ws, n_threads):
    """Seed an entity, then have ``n_threads`` threads each append a distinct
    relation concurrently. Return (distinct_relations_landed_at_HEAD, errors)."""
    write_entity(ws, "company:x",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)

    errors = []

    def worker(i):
        try:
            write_entity(ws, "company:x",
                         [FieldPatch(kind="relation",
                                     value=dict(to=f"topic:t{i}", type="rel"),
                                     author="agent", source_ref=f"s{i}", at=NOW)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # The concurrency guarantee is about the COMMITTED state (HEAD), read via
    # plumbing — independent of working-tree ff timing.
    raw = read_blob_at_head(ws / "memory", "entities/company/x.md")
    page = EntityPage.from_text(raw.decode("utf-8"))
    return len({r["to"] for r in page.relations}), errors


def test_real_concurrency_threads(tmp_path):
    landed, errors = _concurrent_relation_round(tmp_path, 16)
    assert not errors, errors
    assert landed == 16   # all 16 landed via CAS


def test_concurrency_a_reset_racing_a_peer_commit_loses_no_write(tmp_path, monkeypatch):
    """Load-bearing regression for hazard #9 (in-process ref-CAS lost update).

    After each commit, write_entity fast-forwards the working tree with
    dulwich's ``reset --hard``, which resolves HEAD's commit, then re-reads
    HEAD and compare-and-sets it back to the commit it resolved. A peer
    thread's commit landing between those two reads is reverted: its relation
    is lost with no exception and no CAS retry. The per-repo in-process write
    lock keeps peers out of that window.

    The race is made deterministic: both writers read their base together,
    then the first reset is held inside its window until a peer commit has
    landed (bounded wait). With the lock no peer can commit meanwhile, the
    waits run out and both relations land; without it the peer commits in the
    window and its relation is lost every time.
    """
    import sys

    import dulwich.refs as dulwich_refs
    from dulwich import porcelain

    import durin.memory.memory_writer as memory_writer

    ws = tmp_path
    write_entity(ws, "company:x",
                 [FieldPatch(kind="body_append", value="seed", author="agent",
                             source_ref="s", at=NOW)], create=True)

    wait_s = 0.5
    start = threading.Barrier(2)
    started: set[int] = set()
    holder: list[int] = []
    peer_committed = threading.Event()

    real_head_sha = memory_writer.head_sha

    def head_sha_lined_up(root):
        me = threading.get_ident()
        if me not in started:
            started.add(me)
            try:
                start.wait(timeout=wait_s)
            except threading.BrokenBarrierError:
                pass
        return real_head_sha(root)

    real_parse_commit = porcelain.parse_commit

    def parse_commit_then_hold(*args, **kwargs):
        commit = real_parse_commit(*args, **kwargs)
        # Hold only the lookup reset makes, between resolving HEAD and
        # re-reading it for its compare-and-set.
        if not holder and sys._getframe(1).f_code.co_name == "reset":
            holder.append(threading.get_ident())
            peer_committed.wait(timeout=wait_s)
        return commit

    real_set_if_equals = dulwich_refs.DiskRefsContainer.set_if_equals

    def set_if_equals_and_signal(self, *args, **kwargs):
        ok = real_set_if_equals(self, *args, **kwargs)
        if ok and holder and threading.get_ident() != holder[0]:
            peer_committed.set()
        return ok

    monkeypatch.setattr(memory_writer, "head_sha", head_sha_lined_up)
    monkeypatch.setattr(porcelain, "parse_commit", parse_commit_then_hold)
    monkeypatch.setattr(dulwich_refs.DiskRefsContainer, "set_if_equals",
                        set_if_equals_and_signal)

    errors = []

    def worker(i):
        try:
            write_entity(ws, "company:x",
                         [FieldPatch(kind="relation",
                                     value=dict(to=f"topic:t{i}", type="rel"),
                                     author="agent", source_ref=f"s{i}", at=NOW)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert holder, "the hold never ran: dulwich's reset no longer calls parse_commit"
    assert not errors, errors
    raw = read_blob_at_head(ws / "memory", "entities/company/x.md")
    page = EntityPage.from_text(raw.decode("utf-8"))
    assert {r["to"] for r in page.relations} == {"topic:t0", "topic:t1"}, (
        "lost a write (hazard #9 in-process ref-CAS lost update)")


# ---- author scope bridge (Task 7) -----------------------------------------

def test_default_field_author_from_scope(tmp_path):
    ws = tmp_path
    with author_scope("agent_created"):
        write_entity(ws, "company:scoped",
                     [FieldPatch(kind="attribute", key="hq", value="SF",
                                 author=None, source_ref="s", at=NOW)], create=True)
    page = EntityPage.from_file(_page_path(ws, "company:scoped"))
    assert page.provenance["attributes"]["hq"]["author"] == "agent"


# ---- enriched commit messages (B1) ---------------------------------------

def _latest_commit(ws, ref):
    type_, _, slug = ref.partition(":")
    page = ws / "memory" / "entities" / type_ / f"{slug}.md"
    return GitRepo(ws / "memory").log(page)[0]


def test_create_commit_subject_says_create(tmp_path):
    write_entity(tmp_path, "person:marcelo",
                 [FieldPatch(kind="body_append", value="x", author="agent",
                             source_ref="[[sessions/s.md#turn-0]]", at=NOW)],
                 create=True, name="Marcelo")
    assert _latest_commit(tmp_path, "person:marcelo").subject.startswith(
        "create person:marcelo")


def test_update_commit_subject_and_trailers_are_enriched(tmp_path):
    ws = tmp_path
    write_entity(ws, "patient:drako",
                 [FieldPatch(kind="body_append", value="Stable patient.",
                             author="agent", source_ref="[[sessions/s.md#turn-0]]",
                             at=NOW)], create=True, name="Drako")
    write_entity(ws, "patient:drako",
                 [FieldPatch(kind="body_append", value="New finding.",
                             author="agent", source_ref="[[sessions/s.md#turn-4]]",
                             at=LATER),
                  FieldPatch(kind="relation",
                             value=dict(to="topic:cyst", type="has_condition"),
                             author="agent", source_ref="[[sessions/s.md#turn-4]]",
                             at=LATER)])

    latest = _latest_commit(ws, "patient:drako")
    # Subject names the entity and the kinds of change touched.
    assert latest.subject.startswith("update patient:drako")
    assert "body" in latest.subject
    assert "relation" in latest.subject
    # Trailers carry the provenance link and the field-author scope.
    assert latest.trailers.get("Source") == ["[[sessions/s.md#turn-4]]"]
    assert latest.trailers.get("Author") == ["agent"]
