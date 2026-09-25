"""``is_under`` compares filesystem IDENTITY, not path text.

A case-insensitive filesystem (macOS APFS by default, most Windows volumes)
resolves a differently-cased path segment (``CONFIG.JSON``, ``Config.json.d``,
``.DURIN``) to the exact same on-disk entry as the canonical spelling. A guard
written as a plain text/``relative_to`` comparison never sees that collision —
which is exactly how ``config.json`` and every other denied directory
(``skills/``, ``workflows/``, ``automations/``, ``.approvals/``, the import
quarantine) could be written straight through the write guard on such a
filesystem. These tests pin the fix at the identity-helper level directly
(deterministic, regardless of the host filesystem's own case sensitivity —
either a hard link, the cross-platform way to force two differently-named
paths onto one inode, or a monkeypatched ``os.path.samefile``), plus through
the real tool stack when the tmp filesystem actually is case-insensitive.
"""
from __future__ import annotations

import os

import pytest

from durin.agent.tools import path_utils
from durin.agent.tools.filesystem import WriteFileTool
from durin.agent.tools.path_utils import _same_entry, is_under


def _case_insensitive_fs(tmp_path) -> bool:
    """True when writing/reading a differently-cased path resolves to the
    same file on this filesystem — i.e. the host actually exercises the bug
    this fix closes, rather than the (harmless, unrelated-file) behavior of
    a case-sensitive one."""
    probe = tmp_path / "case_probe_XyZ"
    probe.write_text("x", encoding="utf-8")
    try:
        return (tmp_path / "case_probe_xyz").exists()
    finally:
        probe.unlink()


# --- the identity primitive, deterministic on any host --------------------


def test_same_entry_trusts_a_monkeypatched_samefile(tmp_path, monkeypatch):
    """``_same_entry`` defers to ``os.path.samefile``'s verdict — the seam a
    test uses to simulate case-insensitive-filesystem identity
    deterministically on any host, including a case-sensitive CI runner,
    without needing two paths that actually collide on this machine."""
    a = tmp_path / "config.json.d"
    a.mkdir()
    b = tmp_path / "other-dir"  # genuinely unrelated on THIS host
    b.mkdir()
    monkeypatch.setattr(os.path, "samefile", lambda x, y: True)
    assert _same_entry(a, b) is True


def test_same_entry_false_for_unrelated_paths(tmp_path):
    a = tmp_path / "config.json"
    a.write_text("{}", encoding="utf-8")
    b = tmp_path / "config.jsonx"
    b.write_text("{}", encoding="utf-8")
    assert _same_entry(a, b) is False


def test_is_under_true_for_an_alias_of_the_guarded_file(tmp_path):
    """On a case-insensitive host, ``CONFIG.JSON`` already IS ``config.json``
    the moment the latter exists — no setup needed, that's the bug this fix
    closes. On a case-sensitive host the two are genuinely unrelated, so a
    hard link forces the same identity a case-insensitive filesystem would
    have given for free — either way this is deterministic, real filesystem
    identity, no mocking."""
    guarded = tmp_path / "config.json"
    guarded.write_text("{}", encoding="utf-8")
    variant = tmp_path / "CONFIG.JSON"
    if not variant.exists():  # case-sensitive host: not aliased already
        os.link(guarded, variant)

    assert is_under(variant, guarded) is True


def test_is_under_not_yet_existing_target_matched_by_parent_and_casefold(tmp_path, monkeypatch):
    """A guarded FILE that doesn't exist yet at all (a brand-new instance,
    before durin has ever written config.json): both sides are compared by
    their common existing parent (identity) plus the missing name, which a
    case-insensitive filesystem folds. The verdict is pinned so this runs
    the same on a case-sensitive CI runner."""
    monkeypatch.setattr(path_utils, "_fs_case_insensitive", lambda _p: True)
    guarded = tmp_path / "config.json"  # deliberately never created
    variant = tmp_path / "CONFIG.JSON"  # also never created

    assert is_under(variant, guarded) is True
    assert is_under(tmp_path / "config.jsonx", guarded) is False  # different name


def test_is_under_ordinary_containment_still_works(tmp_path):
    """No case games at all — the ordinary "is this path inside this
    directory" question must still work exactly as before."""
    d = tmp_path / "skills"
    d.mkdir()
    (d / "x").mkdir()
    assert is_under(d / "x" / "SKILL.md", d) is True
    assert is_under(tmp_path / "other" / "f.txt", d) is False


# --- through the real tool stack, only when the filesystem actually collides


@pytest.mark.asyncio
async def test_write_file_refuses_a_real_case_variant_on_this_filesystem(
    tmp_path, monkeypatch,
):
    if not _case_insensitive_fs(tmp_path):
        pytest.skip("tmp filesystem is case-sensitive; a case variant is a "
                    "genuinely different, unrelated file here")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("durin.config.loader._current_config_path", home / "config.json")
    ws = tmp_path / "ws"
    ws.mkdir()

    out = await WriteFileTool(workspace=ws).execute(
        path=str(home / "CONFIG.JSON"), content="pwned")

    assert "durin's configuration is changed by the person" in out
    assert (home / "config.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_write_file_ordinary_workspace_write_unaffected(tmp_path):
    """The identity rewrite must not make an everyday write any pickier."""
    ws = tmp_path / "ws"
    ws.mkdir()
    out = await WriteFileTool(workspace=ws).execute(path="notes.txt", content="hi")
    assert "Successfully wrote" in out


# --- absent guards: ancestor-walk identity, no directory ever created ------
#
# A guarded path that does not exist yet (a fresh workspace's .approvals/, a
# never-split config.json.d/, a config.json not written yet) is compared by
# its nearest EXISTING ancestor (filesystem identity) plus the remaining
# component names — casefolded only where that filesystem folds case. The
# guard must never create the directory to have something to compare: an
# empty config.json.d/ flips every "is the config split?" reader, and empty
# .approvals/.durin/automations/ dirs clutter the workspace.


def _fold(monkeypatch, value: bool) -> None:
    """Pin the case-sensitivity verdict so a test is deterministic on any
    host (APFS folds case, a Linux CI runner does not)."""
    monkeypatch.setattr(path_utils, "_fs_case_insensitive", lambda _p: value)


def test_is_under_both_absent_under_a_common_existing_ancestor(tmp_path, monkeypatch):
    _fold(monkeypatch, True)
    assert is_under(tmp_path / ".APPROVALS" / "x.json", tmp_path / ".approvals") is True
    assert is_under(tmp_path / "CONFIG.JSON", tmp_path / "config.json") is True
    assert is_under(tmp_path / "Config.Json.D" / "tools.json", tmp_path / "config.json.d") is True
    assert is_under(tmp_path / "config.jsonx", tmp_path / "config.json") is False
    assert is_under(tmp_path / "config.json.legacy", tmp_path / "config.json") is False
    assert list(tmp_path.iterdir()) == []  # the check created nothing


def test_is_under_same_name_variant_is_distinct_on_a_case_sensitive_fs(tmp_path, monkeypatch):
    """No casefold where the filesystem keeps case: `.APPROVALS/` and
    `.approvals/` are two unrelated directories there."""
    _fold(monkeypatch, False)
    assert is_under(tmp_path / ".APPROVALS" / "x.json", tmp_path / ".approvals") is False
    assert is_under(tmp_path / "CONFIG.JSON", tmp_path / "config.json") is False
    # the exact spelling is still caught with nothing on disk
    assert is_under(tmp_path / ".approvals" / "x.json", tmp_path / ".approvals") is True
    assert is_under(tmp_path / "config.json", tmp_path / "config.json") is True


def test_a_differently_cased_workspace_is_outside_it_on_a_case_sensitive_fs(tmp_path):
    """restrict_to_workspace: where the filesystem keeps case,
    ``<home>/WORKSPACE`` is not ``<home>/workspace``, existing or not."""
    if _case_insensitive_fs(tmp_path):
        pytest.skip("on a case-insensitive filesystem the two names are one directory")
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert is_under(tmp_path / "WORKSPACE" / "x.txt", ws) is False
    (tmp_path / "WORKSPACE").mkdir()
    assert is_under(tmp_path / "WORKSPACE" / "x.txt", ws) is False


def test_is_under_deep_absent_paths(tmp_path, monkeypatch):
    _fold(monkeypatch, True)
    guard = tmp_path / "a" / "b" / "c"  # nothing below tmp_path exists
    assert is_under(tmp_path / "a" / "b" / "c" / "d" / "e" / "f.txt", guard) is True
    assert is_under(tmp_path / "A" / "b" / "C" / "d" / "f.txt", guard) is True
    assert is_under(tmp_path / "a" / "b" / "cx" / "f.txt", guard) is False
    assert is_under(tmp_path / "a" / "b", guard) is False  # the guard's parent
    (tmp_path / "a").mkdir()  # part of the guard exists, the rest does not
    _fold(monkeypatch, False)
    assert is_under(tmp_path / "a" / "B" / "c" / "x", guard) is False
    assert is_under(tmp_path / "a" / "b" / "c" / "x", guard) is True
    monkeypatch.undo()
    # `A` names the existing `a` only where the filesystem folds case: the
    # real detector decides, so the answer follows the host.
    assert is_under(tmp_path / "A" / "B" / "c" / "x", guard) is _case_insensitive_fs(tmp_path)


def test_is_under_through_a_symlinked_ancestor(tmp_path, monkeypatch):
    _fold(monkeypatch, True)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # guard spelled through the link, target through the real dir — and back
    assert is_under(real / ".approvals" / "x.json", link / ".approvals") is True
    assert is_under(link / "skills" / "evil" / "SKILL.md", real / "skills") is True
    assert is_under(link / ".APPROVALS" / "x.json", real / ".approvals") is True
    assert is_under(link / "notes" / "x.md", real / ".approvals") is False
    assert sorted(p.name for p in real.iterdir()) == []


def test_is_under_existing_guard_still_matched_by_identity(tmp_path, monkeypatch):
    _fold(monkeypatch, False)  # identity, not names, decides for existing entries
    guard = tmp_path / "skills"
    guard.mkdir()
    variant = tmp_path / "SKILLS"
    if not variant.exists():  # case-sensitive host: alias it explicitly
        variant.symlink_to(guard, target_is_directory=True)
    assert is_under(variant / "x" / "SKILL.md", guard) is True


def test_protected_store_paths_create_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("durin.config.loader._current_config_path", home / "config.json")
    path_utils.protected_durin_store_paths()
    assert list(home.iterdir()) == []


# --- case-sensitivity detection -------------------------------------------


def _case_folding_samefile(monkeypatch) -> None:
    """Make ``os.path.samefile`` answer like a case-insensitive volume, so
    the detector's walk is tested the same way on any host."""
    real_samefile = os.path.samefile

    def samefile(a, b):
        if os.fspath(a).casefold() == os.fspath(b).casefold():
            return os.path.exists(a) or os.path.exists(b)
        return real_samefile(a, b)

    monkeypatch.setattr(os.path, "samefile", samefile)


def test_fs_case_insensitive_walks_up_past_a_letterless_name(tmp_path, monkeypatch):
    """A DURIN_HOME named ``12345`` has no case to swap; the detector must
    walk up to the nearest ancestor whose name has letters instead of
    answering "case-sensitive"."""
    home = tmp_path / "Abc" / "12345" / "678"
    home.mkdir(parents=True)
    _case_folding_samefile(monkeypatch)
    assert path_utils._fs_case_insensitive(home) is True
    assert path_utils._fs_case_insensitive(home / "absent" / "deeper") is True


def test_fs_case_insensitive_letterless_name_on_this_host(tmp_path):
    """No mocking: the verdict for a letterless directory matches what this
    host's filesystem actually does."""
    home = tmp_path / "12345"
    home.mkdir()
    assert path_utils._fs_case_insensitive(home) is _case_insensitive_fs(tmp_path)


# --- through the real tool stack: nothing created, variants still refused --


@pytest.fixture()
def fresh_instance(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("durin.config.loader._current_config_path", home / "config.json")
    ws = tmp_path / "ws"
    ws.mkdir()
    return home, ws


@pytest.mark.asyncio
async def test_an_ordinary_write_creates_no_guard_dirs(fresh_instance):
    home, ws = fresh_instance
    out = await WriteFileTool(workspace=ws).execute(path="notes.txt", content="hi")
    assert "Successfully wrote" in out
    assert sorted(p.name for p in ws.iterdir()) == ["notes.txt"]
    assert list(home.iterdir()) == []  # no empty config.json.d/


@pytest.mark.asyncio
@pytest.mark.parametrize("where,rel", [
    ("ws", ".APPROVALS/forged.json"),
    ("ws", "SKILLS/evil/SKILL.md"),
    ("ws", "Workflows/w.json"),
    ("ws", "AUTOMATIONS/a.json"),
    ("ws", ".durin/IMPORT-QUARANTINE/x/.scan.json"),
    ("home", "CONFIG.JSON.D/tools.json"),
    ("home", "CONFIG.JSON"),
    ("home", "Secrets.JSON"),
])
async def test_a_case_variant_of_an_absent_guard_is_refused(
    fresh_instance, monkeypatch, where, rel,
):
    home, ws = fresh_instance
    _fold(monkeypatch, True)
    base = ws if where == "ws" else home
    out = await WriteFileTool(workspace=ws).execute(path=str(base / rel), content="{}")
    assert out.startswith("Error"), out
    assert list(home.iterdir()) == []
    assert list(ws.iterdir()) == []
