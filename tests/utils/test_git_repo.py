"""Tests for `durin.utils.git_repo` — generic local-only git wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.utils.git_repo import (
    CommitInfo,
    GitRepo,
    GitRepoError,
    NothingToCommitError,
)
from durin.utils.git_repo import _compose_message, _split_message


# ---------------------------------------------------------------------------
# Message composition + parsing (pure helpers — fast feedback)
# ---------------------------------------------------------------------------


class TestComposeMessage:
    def test_subject_only(self) -> None:
        out = _compose_message("Initial commit")
        assert out == "Initial commit\n"

    def test_subject_and_body(self) -> None:
        out = _compose_message("Subj", body="Body line one.\nBody line two.")
        assert out == "Subj\n\nBody line one.\nBody line two.\n"

    def test_subject_and_trailers(self) -> None:
        out = _compose_message(
            "Subj", trailers={"Sources": ["a.md", "b.md"]}
        )
        assert out == "Subj\n\nSources: a.md, b.md\n"

    def test_full_layout(self) -> None:
        out = _compose_message(
            "Subj",
            body="prose explanation",
            trailers={
                "Sources": ["a.md"],
                "Entities-touched": "person:marcelo",
            },
        )
        # subject, blank, body, blank, trailers, trailing newline
        assert out == (
            "Subj\n"
            "\n"
            "prose explanation\n"
            "\n"
            "Sources: a.md\n"
            "Entities-touched: person:marcelo\n"
        )

    def test_single_value_str_vs_list_equivalent(self) -> None:
        a = _compose_message("Subj", trailers={"Key": "v"})
        b = _compose_message("Subj", trailers={"Key": ["v"]})
        assert a == b


class TestSplitMessage:
    def test_subject_only(self) -> None:
        subj, body, trailers = _split_message("Subj")
        assert subj == "Subj"
        assert body == ""
        assert trailers == {}

    def test_body_no_trailers(self) -> None:
        msg = "Subj\n\nBody only.\nMore body."
        subj, body, trailers = _split_message(msg)
        assert subj == "Subj"
        assert body == "Body only.\nMore body."
        assert trailers == {}

    def test_trailers_parsed(self) -> None:
        msg = (
            "Consolidate person:marcelo (rev 17)\n"
            "\n"
            "3 observaciones nuevas.\n"
            "\n"
            "Sources: a.md, b.md\n"
            "Entities-touched: person:marcelo\n"
            "Cursor-after: 4892\n"
        )
        subj, body, trailers = _split_message(msg)
        assert subj == "Consolidate person:marcelo (rev 17)"
        assert body == "3 observaciones nuevas."
        assert trailers == {
            "Sources": ["a.md", "b.md"],
            "Entities-touched": ["person:marcelo"],
            "Cursor-after": ["4892"],
        }

    def test_body_with_colons_not_mistaken_for_trailers(self) -> None:
        """Body that mentions colon-separated stuff but isn't all trailers."""
        msg = (
            "Subj\n"
            "\n"
            "Note: this body contains the word: foo bar.\n"
            "This is prose, not trailers.\n"
        )
        subj, body, trailers = _split_message(msg)
        assert subj == "Subj"
        # The trailing block looks like trailers ONLY if every line matches.
        # Mixed body should stay in body.
        assert "prose, not trailers" in body
        assert trailers == {}

    def test_repeated_key_merges_values(self) -> None:
        msg = (
            "Subj\n\n"
            "Sources: a.md\n"
            "Sources: b.md\n"
        )
        _, _, trailers = _split_message(msg)
        assert trailers == {"Sources": ["a.md", "b.md"]}


# ---------------------------------------------------------------------------
# GitRepo lifecycle
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_repo(self, tmp_path: Path) -> None:
        root = tmp_path / "memory"
        repo = GitRepo(root)
        assert not repo.is_initialized()
        created = repo.init(gitignore_patterns=["*.lance/", ".usage.json"])
        assert created is True
        assert repo.is_initialized()
        assert (root / ".gitignore").exists()
        assert "*.lance/" in (root / ".gitignore").read_text()

    def test_init_is_idempotent(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        assert repo.init(gitignore_patterns=["*.tmp"]) is True
        assert repo.init(gitignore_patterns=["*.tmp"]) is False  # noop

    def test_init_does_not_overwrite_existing_gitignore(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "memory"
        root.mkdir()
        (root / ".gitignore").write_text("custom-line\n")
        repo = GitRepo(root)
        repo.init(gitignore_patterns=["new-line"])
        # Existing .gitignore should NOT be overwritten when there's no repo
        # yet but the file is already there.
        content = (root / ".gitignore").read_text()
        assert "custom-line" in content
        # And "new-line" should NOT replace it.
        assert "new-line" not in content

    def test_initial_commit_exists(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        commits = repo.log()
        assert len(commits) == 1
        assert commits[0].subject == "Initialize repository"
        assert commits[0].author == "durin-system"


# ---------------------------------------------------------------------------
# commit / log
# ---------------------------------------------------------------------------


class TestCommit:
    def test_commit_returns_sha(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        (tmp_path / "memory" / "entities" / "person").mkdir(parents=True)
        (tmp_path / "memory" / "entities" / "person" / "marcelo.md").write_text(
            "---\ntype: person\n---\n# Marcelo\n"
        )
        sha = repo.commit(
            subject="Consolidate person:marcelo (rev 1)",
            body="Initial consolidation",
            trailers={
                "Sources": ["episodic/abc.md"],
                "Entities-touched": "person:marcelo",
            },
        )
        assert sha is not None
        assert len(sha) == 40  # full sha hex

    def test_commit_with_no_changes_raises(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        # No file changes; commit should be a no-op
        with pytest.raises(NothingToCommitError):
            repo.commit(subject="Nothing changed")

    def test_commit_with_explicit_author(self, tmp_path: Path) -> None:
        repo = GitRepo(
            tmp_path / "memory",
            default_author="durin-system",
            default_email="system@durin.local",
        )
        repo.init(gitignore_patterns=["*.tmp"])
        (tmp_path / "memory" / "file.md").write_text("hi")
        sha = repo.commit(
            subject="Add file",
            author="durin-dream",
            author_email="dream@durin.local",
        )
        commits = repo.log()
        # Newest first
        newest = commits[0]
        assert newest.sha == sha
        assert newest.author == "durin-dream"
        assert newest.author_email == "dream@durin.local"

    def test_log_parses_trailers(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        (tmp_path / "memory" / "a.md").write_text("a")
        repo.commit(
            subject="Add a",
            body="Reasoning here",
            trailers={
                "Sources": ["episodic/x.md", "episodic/y.md"],
                "Entities-touched": "person:marcelo",
            },
        )
        commits = repo.log()
        latest = commits[0]
        assert latest.subject == "Add a"
        assert latest.body == "Reasoning here"
        assert latest.trailers["Sources"] == ["episodic/x.md", "episodic/y.md"]
        assert latest.trailers["Entities-touched"] == ["person:marcelo"]


# ---------------------------------------------------------------------------
# show / diff / status
# ---------------------------------------------------------------------------


class TestShowDiffStatus:
    def test_show_file_at_commit(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        file_path = tmp_path / "memory" / "marcelo.md"
        file_path.write_text("version 1")
        sha1 = repo.commit(subject="v1")
        file_path.write_text("version 2")
        sha2 = repo.commit(subject="v2")
        # show file at sha1
        out1 = repo.show(sha1, file_path)
        assert out1 == "version 1"
        out2 = repo.show(sha2, file_path)
        assert out2 == "version 2"

    def test_show_missing_path_raises(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        (tmp_path / "memory" / "a.md").write_text("a")
        sha = repo.commit(subject="add a")
        with pytest.raises(GitRepoError):
            repo.show(sha, tmp_path / "memory" / "nonexistent.md")

    def test_diff_between_commits(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        file_path = tmp_path / "memory" / "marcelo.md"
        file_path.write_text("line1\nline2\n")
        sha1 = repo.commit(subject="v1")
        file_path.write_text("line1\nline2 modified\nline3\n")
        sha2 = repo.commit(subject="v2")
        diff = repo.diff(sha1, sha2)
        # diff should mention the changed lines
        assert "line2 modified" in diff
        assert "line3" in diff

    def test_status_reflects_untracked_modified(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        (tmp_path / "memory" / "tracked.md").write_text("first")
        repo.commit(subject="add tracked")
        # Modify tracked + create untracked
        (tmp_path / "memory" / "tracked.md").write_text("modified")
        (tmp_path / "memory" / "new.md").write_text("brand new")
        st = repo.status()
        assert "tracked.md" in st["modified"] or any(
            "tracked.md" in m for m in st["modified"]
        )
        assert any("new.md" in u for u in st["untracked"])


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_operations_on_uninitialized_repo_raise(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        with pytest.raises(GitRepoError):
            repo.commit(subject="anything")
        with pytest.raises(GitRepoError):
            repo.log()

    def test_show_invalid_sha_raises(self, tmp_path: Path) -> None:
        repo = GitRepo(tmp_path / "memory")
        repo.init(gitignore_patterns=["*.tmp"])
        with pytest.raises(GitRepoError):
            repo.show("0" * 40)
