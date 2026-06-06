"""N1: human-edit guard — a user's in-progress hand edit (e.g. editing a page in
Obsidian) must not be clobbered by the next system write's hard-reset ff. The
guard commits dirty working-tree .md edits with author:user first. No data loss."""
from datetime import datetime, timezone

from dulwich.repo import Repo

from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _body(text):
    return [FieldPatch(kind="body_append", value=text, author="agent", source_ref="s", at=NOW)]


def _commit_count(tmp_path):
    repo = Repo(str(tmp_path / "memory"))
    try:
        return sum(1 for _ in repo.get_walker())
    finally:
        repo.close()


def test_user_edit_survives_system_write(tmp_path):
    write_entity(tmp_path, "company:a", _body("v1"), create=True, name="A")
    p = tmp_path / "memory/entities/company/a.md"
    p.write_text(p.read_text() + "\nUSER-HAND-EDIT-LINE\n", encoding="utf-8")
    # a system write to a DIFFERENT entity → would hard-reset the working tree
    write_entity(tmp_path, "company:b", _body("v1"), create=True, name="B")
    assert "USER-HAND-EDIT-LINE" in p.read_text(encoding="utf-8")  # not clobbered


def test_user_edit_committed_with_author_user(tmp_path):
    write_entity(tmp_path, "company:a", _body("v1"), create=True, name="A")
    p = tmp_path / "memory/entities/company/a.md"
    p.write_text(p.read_text() + "\nUSER-HAND-EDIT-LINE\n", encoding="utf-8")
    write_entity(tmp_path, "company:b", _body("v1"), create=True, name="B")
    repo = Repo(str(tmp_path / "memory"))
    try:
        authors = [e.commit.author for e in repo.get_walker()]
    finally:
        repo.close()
    assert any(b"user@durin.local" in a for a in authors)  # the guard committed as user


def test_guard_is_noop_on_clean_tree(tmp_path):
    write_entity(tmp_path, "company:a", _body("v1"), create=True, name="A")
    before = _commit_count(tmp_path)
    # no user edit pending → the next system write must NOT add a spurious commit
    write_entity(tmp_path, "company:b", _body("v1"), create=True, name="B")
    assert _commit_count(tmp_path) == before + 1  # only b's commit, no phantom "manual edit"
