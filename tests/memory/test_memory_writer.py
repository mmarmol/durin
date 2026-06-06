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

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


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


def test_real_concurrency_threads(tmp_path):
    ws = tmp_path
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

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    # The concurrency guarantee is about the COMMITTED state (HEAD), read via
    # plumbing — independent of working-tree ff timing.
    raw = read_blob_at_head(ws / "memory", "entities/company/x.md")
    page = EntityPage.from_text(raw.decode("utf-8"))
    assert len({r["to"] for r in page.relations}) == 8   # all 8 landed via CAS


# ---- author scope bridge (Task 7) -----------------------------------------

def test_default_field_author_from_scope(tmp_path):
    ws = tmp_path
    with author_scope("agent_created"):
        write_entity(ws, "company:scoped",
                     [FieldPatch(kind="attribute", key="hq", value="SF",
                                 author=None, source_ref="s", at=NOW)], create=True)
    page = EntityPage.from_file(_page_path(ws, "company:scoped"))
    assert page.provenance["attributes"]["hq"]["author"] == "agent"
