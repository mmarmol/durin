"""Tests for the @file and /model completers."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.document import Document

from durin.cli.completers import FileReferenceCompleter, ModelPresetCompleter


def _doc(text: str) -> Document:
    return Document(text=text, cursor_position=len(text))


# ---------------------------------------------------------------------------
# FileReferenceCompleter
# ---------------------------------------------------------------------------


def test_file_completer_yields_paths_after_at(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "src").mkdir()
    (workspace / "src" / "foo.py").write_text("x", encoding="utf-8")
    (workspace / "README.md").write_text("x", encoding="utf-8")

    completer = FileReferenceCompleter(workspace)
    out = list(completer.get_completions(_doc("look at @"), None))
    texts = {c.text for c in out}
    assert "README.md" in texts
    assert "src/foo.py" in texts


def test_file_completer_filters_by_prefix(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "alpha.py").write_text("x", encoding="utf-8")
    (workspace / "beta.py").write_text("x", encoding="utf-8")

    completer = FileReferenceCompleter(workspace)
    out = list(completer.get_completions(_doc("@alp"), None))
    texts = {c.text for c in out}
    assert "alpha.py" in texts
    assert "beta.py" not in texts


def test_file_completer_no_at_no_completions(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "x.py").write_text("x", encoding="utf-8")
    completer = FileReferenceCompleter(workspace)
    assert list(completer.get_completions(_doc("just some text"), None)) == []


def test_file_completer_at_inside_word_not_triggered(tmp_path: Path) -> None:
    """`foo@bar` (e.g. an email) must not trigger completion."""
    workspace = tmp_path
    (workspace / "x.py").write_text("x", encoding="utf-8")
    completer = FileReferenceCompleter(workspace)
    assert list(completer.get_completions(_doc("send mail to foo@bar"), None)) == []


def test_file_completer_skips_excluded_dirs(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("x", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "blob.pyc").write_text("x", encoding="utf-8")
    (workspace / "real.py").write_text("x", encoding="utf-8")

    completer = FileReferenceCompleter(workspace)
    texts = {c.text for c in completer.get_completions(_doc("@"), None)}
    assert "real.py" in texts
    assert ".git/config" not in texts
    assert "__pycache__/blob.pyc" not in texts


def test_file_completer_substring_match_anywhere(tmp_path: Path) -> None:
    """Substring match, not just prefix — matches inside path components."""
    workspace = tmp_path
    (workspace / "src").mkdir()
    (workspace / "src" / "loop_manager.py").write_text("x", encoding="utf-8")

    completer = FileReferenceCompleter(workspace)
    texts = {c.text for c in completer.get_completions(_doc("@manager"), None)}
    assert "src/loop_manager.py" in texts


def test_file_completer_invalidate_picks_up_new_file(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "first.py").write_text("x", encoding="utf-8")

    completer = FileReferenceCompleter(workspace)
    list(completer.get_completions(_doc("@"), None))  # populate cache

    # Add a new file after cache populated.
    (workspace / "second.py").write_text("x", encoding="utf-8")
    texts_before = {c.text for c in completer.get_completions(_doc("@"), None)}
    assert "second.py" not in texts_before  # cache hit

    completer.invalidate()
    texts_after = {c.text for c in completer.get_completions(_doc("@"), None)}
    assert "second.py" in texts_after


# ---------------------------------------------------------------------------
# ModelPresetCompleter
# ---------------------------------------------------------------------------


def test_model_completer_yields_presets() -> None:
    completer = ModelPresetCompleter(lambda: ["default", "fast", "opus"])
    out = list(completer.get_completions(_doc("/model "), None))
    texts = {c.text for c in out}
    assert texts == {"default", "fast", "opus"}


def test_model_completer_filters_by_prefix() -> None:
    completer = ModelPresetCompleter(lambda: ["default", "fast", "opus"])
    texts = {c.text for c in completer.get_completions(_doc("/model fa"), None)}
    assert texts == {"fast"}


def test_model_completer_only_after_command() -> None:
    """No completion outside `/model ` prefix."""
    completer = ModelPresetCompleter(lambda: ["default", "fast"])
    assert list(completer.get_completions(_doc("/help"), None)) == []
    assert list(completer.get_completions(_doc("anything"), None)) == []


def test_model_completer_case_insensitive() -> None:
    completer = ModelPresetCompleter(lambda: ["Default", "Fast"])
    texts = {c.text for c in completer.get_completions(_doc("/model fa"), None)}
    assert texts == {"Fast"}
