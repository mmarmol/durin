"""Regression guard: the pipx subprocess invocations must stay
non-destructive.

Two paths in durin shell out to pipx:

- ``durin upgrade`` (pipx mode) → ``_upgrade_pipx()`` in
  ``durin/cli/upgrade.py``. Must use ``pipx upgrade``, never
  ``pipx install --force`` or ``pipx reinstall``.
- ``durin doctor --install-missing -y`` (pipx mode) →
  ``install_missing_extras`` in ``durin/cli/doctor.py``. Must use
  ``pipx inject``, never ``pipx install --extras`` or ``reinstall``.

Both forbidden alternatives have surprising failure modes on a uv
backend (the install-with-force one is a silent no-op; reinstall
drops injected extras). The full rationale lives in the docstring of
``_upgrade_pipx`` — those comments tell you WHY, this file tells you
when someone breaks the rule.
"""

from __future__ import annotations

import subprocess

import pytest

from durin.cli import doctor, upgrade


def _capture_args(monkeypatch, target, attr: str = "_run"):
    """Replace ``target.<attr>`` with a recorder; return the list of
    argv lists captured. Caller drives the function under test."""
    calls: list[list[str]] = []
    monkeypatch.setattr(target, attr, lambda argv, **kw: calls.append(list(argv)) or 0)
    return calls


def _capture_subprocess_run(monkeypatch):
    """Capture ``subprocess.run`` calls module-wide (doctor.py uses
    bare ``subprocess.run`` in the pipx branch instead of going through
    upgrade._run)."""
    calls: list[list[str]] = []

    class _FakeProc:
        returncode = 0

    def _fake_run(argv, **kw):
        calls.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def test_upgrade_pipx_uses_upgrade_not_force_or_reinstall(monkeypatch):
    """`pipx upgrade durin-agent` is the only safe in-place op:
    preserves the venv, preserves every `pipx inject`-ed extra,
    preserves config. `pipx install --force` is a silent no-op on the
    uv backend (verified 2026-05-31). `pipx reinstall` would drop
    injected extras."""
    calls = _capture_args(monkeypatch, upgrade)
    upgrade._upgrade_pipx()

    assert calls == [["pipx", "upgrade", "durin-agent"]], (
        f"unexpected pipx invocation: {calls!r}"
    )
    flat = " ".join(calls[0])
    assert "--force" not in flat, "pipx install --force is broken on uv backend"
    assert "reinstall" not in flat, "pipx reinstall drops injected extras"
    assert "uninstall" not in flat, "uninstall + install drops injected extras + PATH gap"


def test_install_missing_extras_uses_inject_not_force_or_reinstall(monkeypatch):
    """`pipx inject durin-agent <pkgs>` is the only safe path to add
    optional extras to an existing pipx venv. The alternatives
    (`pipx install --force --extras`, `pipx reinstall --extras`) are
    either silently broken or drop pre-existing injections."""
    # Force pipx mode regardless of where the test runs from.
    from durin.cli import upgrade as _upgrade_mod

    class _FakeMode:
        mode = "pipx"

    monkeypatch.setattr(_upgrade_mod, "detect_install_mode", lambda: _FakeMode())
    monkeypatch.setattr(
        _upgrade_mod, "install_hint", lambda extras, mode: f"pipx inject ... ({mode})",
    )
    # Real extras_to_packages would hit importlib metadata; stub it.
    monkeypatch.setattr(
        _upgrade_mod, "extras_to_packages", lambda extras: ["fastembed", "lancedb"],
    )

    calls = _capture_subprocess_run(monkeypatch)
    rc = doctor.install_missing_extras(["memory"], assume_yes=True)
    assert rc == 0
    assert calls, "expected at least one pipx subprocess invocation"

    pipx_calls = [c for c in calls if c and c[0] == "pipx"]
    assert pipx_calls, f"no pipx subprocess invoked in {calls!r}"

    for argv in pipx_calls:
        assert argv[:2] == ["pipx", "inject"], (
            f"pipx must be invoked with 'inject', got: {argv!r}"
        )
        assert argv[2] == "durin-agent"
        flat = " ".join(argv)
        assert "--force" not in flat, "pipx install --force is broken on uv backend"
        assert "reinstall" not in flat, "pipx reinstall drops injected extras"
        # Specifically `pipx install ...` (positional install subcommand)
        # is also forbidden here.
        assert argv[1] != "install", (
            f"install_missing_extras must use inject, not install: {argv!r}"
        )
