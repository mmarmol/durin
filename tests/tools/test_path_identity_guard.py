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


def test_is_under_not_yet_existing_target_matched_by_parent_and_casefold(tmp_path):
    """A guarded FILE that doesn't exist yet at all (a brand-new instance,
    before durin has ever written config.json): no ancestor walk can stat a
    target that isn't there, so identity is proven by parent-directory
    identity (same real dir) plus a casefolded basename match instead."""
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
