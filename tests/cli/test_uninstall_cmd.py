"""Tests for `durin uninstall` enumeration + deletion logic."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.cli.uninstall import (
    _format_bytes,
    _path_size,
    collect_targets,
    default_target_groups,
    run_uninstall,
)
from durin.cli.upgrade import PYPI_DIST_NAME

runner = CliRunner()


def _durin_tree(home: Path) -> Path:
    """A realistic durin home under *home*, and the default install's
    ~/.cache/durin beside it: its telemetry, the model files, the archive,
    and a file uninstall never lists."""
    (home / ".durin").mkdir()
    (home / ".durin" / "config.json").write_text('{"x":1}', encoding="utf-8")
    (home / ".durin" / "config.json.bak").write_text("{}", encoding="utf-8")
    (home / ".durin" / "workspace").mkdir()
    (home / ".durin" / "workspace" / "scratch.md").write_text("hi", encoding="utf-8")
    (home / ".durin" / "sessions").mkdir()
    (home / ".durin" / "history").mkdir()
    (home / ".durin" / "media").mkdir()
    cache = home / ".cache" / "durin"
    (cache / "telemetry").mkdir(parents=True)
    (cache / "telemetry" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    (cache / "models").mkdir()
    (cache / "models" / "weights.bin").write_bytes(b"\x00\x01")
    (cache / "archive").mkdir()
    (cache / "archive" / "payload.json").write_text("{}", encoding="utf-8")
    (cache / "locomo10.json").write_text("[]", encoding="utf-8")
    return home


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An instance selected with DURIN_HOME, run as a throwaway user: HOME is
    a temp directory too, holding the default install's ~/.cache/durin, so
    nothing a run removes can be the real home's."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DURIN_HOME", str(home / ".durin"))
    return _durin_tree(home)


@pytest.fixture
def default_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The default install, no DURIN_HOME, run as a throwaway user under a
    temp HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DURIN_HOME", raising=False)
    return _durin_tree(home)


def test_default_target_groups_lists_expected_paths() -> None:
    groups = default_target_groups()
    names = {g.name for g in groups}
    assert {"Config", "Workspace", "Cache", "Other state"}.issubset(names)


def test_default_target_groups_includes_workspace_when_passed(tmp_path: Path) -> None:
    ws = tmp_path / "myproj"
    groups = default_target_groups(workspace=ws)
    assert any("Per-workspace" in g.name for g in groups)


def test_collect_targets_skips_missing_paths(fake_home: Path) -> None:
    # Cron dir does not exist in the fixture; it should not appear in targets.
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = [str(p) for _g, p, _s in targets]
    assert not any(p.endswith(".durin/cron") for p in str_paths)


def test_collect_targets_includes_existing_paths(default_install: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(default_install / ".durin" / "config.json") in str_paths
    assert str(default_install / ".durin" / "workspace") in str_paths
    assert str(default_install / ".cache" / "durin" / "telemetry") in str_paths


def test_collect_targets_honors_keep_config(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=True, keep_workspace=False, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".durin" / "config.json") not in str_paths
    # Workspace still slated for removal.
    assert str(fake_home / ".durin" / "workspace") in str_paths


def test_collect_targets_honors_keep_workspace(fake_home: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=True, keep_cache=False, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(fake_home / ".durin" / "workspace") not in str_paths


def test_collect_targets_honors_keep_cache(default_install: Path) -> None:
    targets = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=True, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in targets}
    assert str(default_install / ".cache" / "durin" / "telemetry") not in str_paths


def test_run_uninstall_yes_actually_deletes(default_install: Path) -> None:
    rc = run_uninstall(
        assume_yes=True,
        purge=False,
        keep_config=False,
        keep_workspace=False,
        keep_cache=False,
        workspace=None,
    )
    assert rc == 0
    assert not (default_install / ".durin" / "config.json").exists()
    assert not (default_install / ".durin" / "workspace").exists()
    assert not (default_install / ".cache" / "durin" / "telemetry").exists()


def test_run_uninstall_keep_config_preserves_file(fake_home: Path) -> None:
    rc = run_uninstall(
        assume_yes=True,
        purge=False,
        keep_config=True,
        keep_workspace=False,
        keep_cache=False,
        workspace=None,
    )
    assert rc == 0
    assert (fake_home / ".durin" / "config.json").exists()
    assert not (fake_home / ".durin" / "workspace").exists()


def test_run_uninstall_purge_spawns_pip(fake_home: Path) -> None:
    with patch("durin.cli.uninstall.subprocess.Popen") as mock_popen:
        rc = run_uninstall(
            assume_yes=True,
            purge=True,
            keep_config=False,
            keep_workspace=False,
            keep_cache=False,
            workspace=None,
        )
    assert rc == 0
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args.args[0]
    # Must target the real PyPI distribution name (`durin-agent`), not the
    # bare import/CLI name `durin`: `durin` isn't an installed distribution,
    # so `pip uninstall -y durin` is a silent no-op and --purge leaves the
    # package on disk.
    assert cmd[:5] == [sys.executable, "-m", "pip", "uninstall", "-y"]
    assert cmd[-1] == PYPI_DIST_NAME
    assert PYPI_DIST_NAME == "durin-agent"
    assert "durin" not in cmd


def test_run_uninstall_aborts_when_prompt_declined(fake_home: Path) -> None:
    with patch("typer.confirm", return_value=False):
        rc = run_uninstall(
            assume_yes=False,
            purge=False,
            keep_config=False,
            keep_workspace=False,
            keep_cache=False,
            workspace=None,
        )
    assert rc == 1
    # State untouched.
    assert (fake_home / ".durin" / "config.json").exists()


def test_cli_uninstall_dry_run_prints_plan(fake_home: Path) -> None:
    """Without --yes, the runner sees the prompt and (no input) declines."""
    result = runner.invoke(app, ["uninstall"], input="n\n")
    # Aborted by the user → exit 1 + state untouched.
    assert result.exit_code == 1
    assert (fake_home / ".durin" / "config.json").exists()
    assert "Path" in result.output  # rendered as part of the plan table


def test_cli_uninstall_yes_deletes(fake_home: Path) -> None:
    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (fake_home / ".durin" / "config.json").exists()


def test_format_bytes() -> None:
    assert _format_bytes(0) == "0 B"
    assert _format_bytes(1023) == "1023 B"
    assert _format_bytes(2048).startswith("2.0 KB")
    assert _format_bytes(5 * 1024 * 1024).startswith("5.0 MB")


def test_path_size_returns_zero_for_missing(tmp_path: Path) -> None:
    assert _path_size(tmp_path / "nope") == 0


def test_path_size_counts_directory_contents(tmp_path: Path) -> None:
    d = tmp_path / "x"
    d.mkdir()
    (d / "a.txt").write_text("hello", encoding="utf-8")
    (d / "b.txt").write_text("world!", encoding="utf-8")
    assert _path_size(d) == 11


def test_instance_telemetry_is_collected_with_the_cache_group(fake_home: Path) -> None:
    """An instance selected with DURIN_HOME keeps its telemetry under the
    instance home; uninstall treats it like the cache copy — removed by
    default, kept by ``--keep-cache``."""
    instance_telemetry = fake_home / ".durin" / "telemetry"
    instance_telemetry.mkdir()
    (instance_telemetry / "cli_x_2026-09-09.jsonl").write_text("{}\n", encoding="utf-8")

    removed = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None
    )
    assert str(instance_telemetry) in {str(p) for _g, p, _s in removed}

    kept = collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=True, workspace=None
    )
    str_paths = {str(p) for _g, p, _s in kept}
    assert str(instance_telemetry) not in str_paths
    assert str(fake_home / ".cache" / "durin" / "telemetry") not in str_paths


def test_an_instance_never_touches_the_default_installs_cache(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninstalling an instance selected with DURIN_HOME also removed
    ~/.cache/durin's telemetry, model files and archive, which are the
    default install's whatever DURIN_HOME says. Now it removes the
    instance's own telemetry and leaves ~/.cache/durin as it was, naming
    what it leaves there and why."""
    cache = fake_home / ".cache" / "durin"
    own = fake_home / ".durin" / "telemetry"
    own.mkdir()
    (own / "cli_x_2026-09-09.jsonl").write_text("{}\n", encoding="utf-8")
    before = _tree(cache)
    monkeypatch.setattr("durin.cli.uninstall.console", Console(width=400))

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert _tree(cache) == before
    assert not own.exists()
    left = result.output[result.output.index("Left in place"):]
    assert f"{cache / 'telemetry'} — the default install's telemetry" in left
    assert f"{cache / 'models'} — model files shared by every install on this machine" in left
    assert f"{cache / 'archive'} — the default install's archive" in left


def test_the_default_install_removes_its_caches(default_install: Path) -> None:
    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    # Its telemetry, the model files and the archive; nothing else there.
    assert sorted(p.name for p in (default_install / ".cache" / "durin").iterdir()) == ["locomo10.json"]


@pytest.mark.parametrize("install", ["default_install", "fake_home"])
def test_keep_cache_keeps_the_caches(install: str, request: pytest.FixtureRequest) -> None:
    home = request.getfixturevalue(install)
    own = home / ".durin" / "telemetry"
    own.mkdir()
    (own / "cli_x_2026-09-09.jsonl").write_text("{}\n", encoding="utf-8")
    cache = home / ".cache" / "durin"
    before = (_tree(cache), _tree(own))

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=True, workspace=None,
    )

    assert rc == 0
    assert (_tree(cache), _tree(own)) == before
    assert not (home / ".durin" / "sessions").exists()


def _split_config(fake_home: Path) -> dict[str, bytes]:
    """The split layout durin writes: config.json as its marker, a file per
    section under config.json.d/. Returns every file's bytes."""
    root = fake_home / ".durin"
    (root / "config.json").write_text('{"_layout": "split"}', encoding="utf-8")
    split = root / "config.json.d"
    split.mkdir()
    (split / "agents.json").write_text('{"defaults": {"model": "openai/gpt-4.1"}}', encoding="utf-8")
    (split / "providers.json").write_text('{"openai": {"apiKey": "sk-test"}}', encoding="utf-8")
    return {p.name: p.read_bytes() for p in (root / "config.json", *sorted(split.iterdir()))}


def test_run_uninstall_removes_the_split_config_too(fake_home: Path) -> None:
    """Uninstall removed config.json, which on the split layout is only its
    marker, and left every setting behind in config.json.d/."""
    _split_config(fake_home)

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    assert not (fake_home / ".durin" / "config.json").exists()
    assert not (fake_home / ".durin" / "config.json.d").exists()


def test_run_uninstall_keep_config_keeps_the_split_config(fake_home: Path) -> None:
    before = _split_config(fake_home)

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=True, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    root = fake_home / ".durin"
    split = root / "config.json.d"
    assert {p.name: p.read_bytes() for p in (root / "config.json", *sorted(split.iterdir()))} == before


# The config, its backups and the credentials its ${secret:…} references and
# sign-ins resolve against: --keep-config keeps exactly these.
_CONFIG_GROUP = (
    "api_tokens.json",
    "config.json",
    "config.json.bak",
    "config.json.bak.1780253978",
    "config.json.bak.20260610_215409",
    "config.json.d",
    "config.json.d.bak.20260610_215409",
    "config.json.legacy",
    "oauth",
    "pairing.json",
    "secrets.json",
)


def _tree(root: Path) -> dict[str, bytes]:
    """Every entry under *root* with its bytes, symlinks as their target and
    never followed: what a run must leave untouched, compared whole."""
    out: dict[str, bytes] = {}
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = b"-> " + os.readlink(path).encode()
            elif path.is_dir():
                out[rel + "/"] = b""
            else:
                out[rel] = path.read_bytes()
    return out


def _full_home(fake_home: Path) -> tuple[Path, Path]:
    """A durin home holding every kind of entry uninstall meets: the config
    with its sections, its timestamped backups and the pre-split monolith;
    the credentials; the workspace; state under names a fixed list never
    had, and lock files; and symlinks to a directory outside the home, one
    at the top and one inside a directory. Returns the home and that
    outside directory."""
    outside = fake_home / "projects" / "notes"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("not durin's", encoding="utf-8")

    root = fake_home / ".durin"
    (root / "config.json").write_text('{"_layout": "split"}', encoding="utf-8")
    (root / "config.json.d").mkdir()
    (root / "config.json.d" / "providers.json").write_text(
        '{"openai": {"apiKey": "${secret:OPENAI_API_KEY}"}}', encoding="utf-8",
    )
    (root / "config.json.bak.20260610_215409").write_text('{"x": 1}', encoding="utf-8")
    (root / "config.json.bak.1780253978").write_text('{"x": 0}', encoding="utf-8")
    (root / "config.json.d.bak.20260610_215409").mkdir()
    (root / "config.json.d.bak.20260610_215409" / "providers.json").write_text(
        '{"openai": {"apiKey": "sk-plaintext"}}', encoding="utf-8",
    )
    (root / "config.json.legacy").write_text('{"providers": {}}', encoding="utf-8")
    (root / "pairing.json").write_text("{}", encoding="utf-8")
    (root / "secrets.json").write_text('{"OPENAI_API_KEY": {"value": "sk-test"}}', encoding="utf-8")
    (root / "api_tokens.json").write_text("{}", encoding="utf-8")
    (root / "oauth").mkdir()
    (root / "oauth" / "openrouter.json").write_text("{}", encoding="utf-8")
    for name in ("cron", "logs", "email", "jobs", "models"):
        (root / name).mkdir()
        (root / name / "state.json").write_text("{}", encoding="utf-8")
    for name in ("config.json.lock", "secrets.json.lock", "embed-cache.sqlite", "tui-state.json", "gateway.pid"):
        (root / name).write_text("x", encoding="utf-8")
    # A name with brackets, which a console reads as style markup.
    (root / "notes [old].txt").write_text("x", encoding="utf-8")
    (root / "telemetry").mkdir()
    (root / "notes").symlink_to(outside, target_is_directory=True)
    (root / "media" / "linked.txt").symlink_to(outside / "keep.txt")
    return root, outside


def test_uninstall_removes_everything_in_the_durin_home(fake_home: Path) -> None:
    """Uninstall removed a fixed list of names: the credentials (the secret
    store, the API tokens, the OAuth sign-ins), the config backups, and any
    state under a name the list lacked stayed behind. Everything goes now,
    and a symlink goes as the link: what it points at outside is untouched."""
    root, outside = _full_home(fake_home)
    before = _tree(outside)

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    assert _tree(root) == {}
    assert _tree(outside) == before


def test_keep_config_keeps_the_config_its_backups_and_the_credentials(fake_home: Path) -> None:
    root, outside = _full_home(fake_home)
    before = _tree(outside)
    kept = {rel: data for rel, data in _tree(root).items() if rel.split("/")[0].rstrip("/") in _CONFIG_GROUP}
    assert {rel.split("/")[0].rstrip("/") for rel in kept} == set(_CONFIG_GROUP)

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=True, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    assert _tree(root) == kept
    assert _tree(outside) == before


def test_yes_lists_exactly_what_it_removes_before_removing_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--yes skips the question, not the list: the plan names every path
    (a symlink as the link, its target left alone), and what goes is what
    it named."""
    root, outside = _full_home(fake_home)
    monkeypatch.setattr("durin.cli.uninstall.console", Console(width=400))
    entries = set(root.iterdir())
    plan = [path for _group, path, _size in collect_targets(
        keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )]

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert entries - set(root.iterdir()) == {path for path in plan if path.parent == root}
    listing = result.output[: result.output.index("Removed")]
    for path in plan:
        assert str(path) in listing
    assert f"{root / 'notes'} -> {outside}" in listing


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes the entries of a read-only directory")
def test_a_path_it_cannot_remove_is_reported(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path uninstall cannot remove is named, the run exits 1, and the rest
    still goes. Before, a directory under a name the fixed list lacked was
    never even tried: it stayed behind with nothing said."""
    root = fake_home / ".durin"
    stuck = root / "email"
    stuck.mkdir()
    (stuck / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("durin.cli.uninstall.console", Console(width=400))
    stuck.chmod(0o500)
    try:
        result = runner.invoke(app, ["uninstall", "--yes"])
    finally:
        stuck.chmod(0o700)

    assert result.exit_code == 1, result.output
    failures = result.output[result.output.index("could not be deleted"):]
    assert str(stuck) in failures
    assert not (root / "sessions").exists()


def test_a_path_gone_before_its_turn_is_not_a_failure(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping the gateway removes its pid file, which the plan listed: the
    path is gone, as asked, and is not reported as one uninstall failed on."""
    pid = fake_home / ".durin" / "gateway.pid"
    pid.write_text("12345", encoding="utf-8")
    monkeypatch.setattr("durin.cli.uninstall._stop_gateway_daemon", pid.unlink)

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    assert not pid.exists()


def test_a_full_uninstall_leaves_the_durin_home_empty(fake_home: Path) -> None:
    """Uninstall stops the daemon after it lists what it removes, and checking
    the daemon's status created a logs/ folder the list never had: it stayed
    behind, empty."""
    root = fake_home / ".durin"
    assert not (root / "logs").exists()

    rc = run_uninstall(
        assume_yes=True, purge=False, keep_config=False, keep_workspace=False, keep_cache=False, workspace=None,
    )

    assert rc == 0
    assert sorted(p.name for p in root.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory without its read permission")
def test_a_durin_home_it_cannot_list_removes_nothing(fake_home: Path) -> None:
    """Without the home's entries there is no complete plan to show: the run
    says why, exits 1, and removes nothing."""
    root = fake_home / ".durin"
    root.chmod(0o300)
    try:
        result = runner.invoke(app, ["uninstall", "--yes"])
    finally:
        root.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert "Nothing was removed" in result.output
    assert (root / "config.json").exists()
    assert (fake_home / ".cache" / "durin" / "telemetry").exists()
