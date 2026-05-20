"""Tests for `durin doctor` checks + orchestrator."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.cli.doctor import (
    CheckResult,
    DoctorReport,
    apply_safe_fixes,
    check_at_least_one_provider,
    check_cache_size,
    check_config_file,
    check_config_parses,
    check_default_model_resolvable,
    check_executable,
    check_optional_extra,
    check_python_version,
    check_state_dirs_writable,
    check_workspace,
    run_checks,
    run_doctor,
)
from durin.config.schema import Config

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def valid_config(fake_home: Path) -> Path:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    data = Config().model_dump(mode="json", by_alias=True)
    # Plant an api_key so check_at_least_one_provider passes.
    data["providers"]["zhipu"]["apiKey"] = "sk-test"
    # `Path.expanduser()` reads $HOME directly, ignoring our monkeypatch of
    # `Path.home()`. Force the workspace to an absolute path under fake_home.
    data["agents"]["defaults"]["workspace"] = str(fake_home / ".durin" / "workspace")
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with patch("durin.cli.doctor.get_config_path", return_value=cfg), \
         patch("durin.config.loader.get_config_path", return_value=cfg):
        yield cfg


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_check_python_version_ok() -> None:
    r = check_python_version()
    assert r.status == "ok"
    assert "Python" in r.message


def test_check_config_file_missing(fake_home: Path) -> None:
    cfg = fake_home / ".durin" / "config.json"
    with patch("durin.cli.doctor.get_config_path", return_value=cfg):
        r = check_config_file()
    assert r.status == "fail"
    assert "Missing" in r.message
    assert r.fix and "onboard" in r.fix


def test_check_config_file_ok(valid_config: Path) -> None:
    r = check_config_file()
    assert r.status == "ok"


def test_check_config_parses_rejects_invalid_json(fake_home: Path) -> None:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not json", encoding="utf-8")
    with patch("durin.cli.doctor.get_config_path", return_value=cfg):
        r = check_config_parses()
    assert r.status == "fail"
    assert "JSON parse error" in r.message


def test_check_config_parses_accepts_valid(valid_config: Path) -> None:
    r = check_config_parses()
    assert r.status == "ok"


def test_check_workspace_missing(valid_config: Path, fake_home: Path) -> None:
    # workspace_path defaults to ~/.durin/workspace which doesn't exist yet.
    r = check_workspace()
    assert r.status == "warn"
    assert "Missing" in r.message


def test_check_workspace_exists(valid_config: Path, fake_home: Path) -> None:
    (fake_home / ".durin" / "workspace").mkdir(parents=True)
    r = check_workspace()
    assert r.status == "ok"


def test_check_state_dirs_writable(fake_home: Path) -> None:
    r = check_state_dirs_writable()
    assert r.status == "ok"


def test_check_at_least_one_provider_when_none_configured(fake_home: Path) -> None:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(Config().model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    with patch("durin.cli.doctor.get_config_path", return_value=cfg), \
         patch("durin.config.loader.get_config_path", return_value=cfg):
        r = check_at_least_one_provider()
    assert r.status == "fail"
    assert "No provider" in r.message


def test_check_at_least_one_provider_with_api_key(valid_config: Path) -> None:
    r = check_at_least_one_provider()
    assert r.status == "ok"
    assert "Zhipu" in r.message


def test_check_default_model_resolvable_ok(valid_config: Path) -> None:
    r = check_default_model_resolvable()
    assert r.status == "ok"


def test_check_executable_returns_ok_for_known_binary() -> None:
    # `python` is guaranteed to exist when tests run.
    py = sys.executable
    name = Path(py).name
    r = check_executable(name, required=True, hint="install it")
    assert r.status == "ok"
    # The reported path should be a string.
    assert isinstance(r.message, str)


def test_check_executable_warn_for_missing_optional() -> None:
    r = check_executable("definitely-not-installed-zzz", required=False, hint="optional thing")
    assert r.status == "warn"
    assert "PATH" in r.message


def test_check_executable_fail_for_missing_required() -> None:
    r = check_executable("definitely-not-installed-zzz", required=True, hint="required thing")
    assert r.status == "fail"


def test_check_optional_extra_present() -> None:
    # `json` is in the stdlib so importable; it stands in for a present extra.
    r = check_optional_extra("json", extra="memory", purpose="testing")
    assert r.status == "ok"


def test_check_optional_extra_missing_is_warn() -> None:
    r = check_optional_extra("definitely_no_such_module_zzz", extra="memory", purpose="testing")
    assert r.status == "warn"
    assert r.fix and "pip install" in r.fix
    # The hint must reference the actual PyPI distribution name, not the legacy `durin`.
    assert "durin-agent" in r.fix or ".[memory]" in r.fix  # editable mode uses `.[memory]`
    assert "'durin[" not in r.fix
    # The result must carry the extra name so `--install-missing` can group it.
    assert r.extra == "memory"


def test_collect_missing_extras_groups_unique() -> None:
    from durin.cli.doctor import collect_missing_extras

    report = DoctorReport()
    report.add(CheckResult("fastembed", "warn", "", extra="memory", category="extras"))
    report.add(CheckResult("lancedb", "warn", "", extra="memory", category="extras"))
    report.add(CheckResult("mcp", "warn", "", extra="mcp", category="extras"))
    # ok results don't get included
    report.add(CheckResult("anyio", "ok", "", extra="memory", category="extras"))
    # non-extras-category warns don't get included
    report.add(CheckResult("git", "warn", "", category="tools"))
    assert collect_missing_extras(report) == ["memory", "mcp"]


def test_install_missing_extras_no_op_when_empty() -> None:
    from durin.cli.doctor import install_missing_extras

    assert install_missing_extras([], assume_yes=True) == 0


def test_install_missing_extras_editable_prints_command_no_op() -> None:
    """Editable mode should print the command but NOT run it."""
    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="editable", source_root=Path("/tmp/x"), version="0.1.0a2")
    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run") as mock_run:
        rc = install_missing_extras(["memory"], assume_yes=True)
    assert rc == 0
    mock_run.assert_not_called()


def test_install_missing_extras_pipx_runs_pipx_install_force() -> None:
    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="pipx", source_root=None, version="0.1.0a2")
    captured: list[list[str]] = []

    def _fake_run(cmd):
        captured.append(list(cmd))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run", side_effect=_fake_run):
        rc = install_missing_extras(["memory", "mcp"], assume_yes=True)
    assert rc == 0
    assert captured == [["pipx", "install", "--force", "durin-agent[memory,mcp]"]]


def test_install_missing_extras_wheel_runs_pip_install() -> None:
    import sys

    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="wheel", source_root=None, version="0.1.0a2")
    captured: list[list[str]] = []

    def _fake_run(cmd):
        captured.append(list(cmd))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run", side_effect=_fake_run):
        rc = install_missing_extras(["memory"], assume_yes=True)
    assert rc == 0
    assert captured == [[sys.executable, "-m", "pip", "install", "--upgrade", "durin-agent[memory]"]]


def test_install_missing_extras_unknown_mode_returns_one() -> None:
    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="unknown", source_root=None, version="0.1.0a2")
    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run") as mock_run:
        rc = install_missing_extras(["memory"], assume_yes=True)
    assert rc == 1
    mock_run.assert_not_called()


def test_check_cache_size_no_cache(fake_home: Path) -> None:
    r = check_cache_size()
    assert r.status == "ok"


# ---------------------------------------------------------------------------
# Orchestrator + report
# ---------------------------------------------------------------------------


def test_run_checks_returns_report_with_many_results(valid_config: Path) -> None:
    report = run_checks()
    assert isinstance(report, DoctorReport)
    assert len(report.results) >= 8
    names = {r.name for r in report.results}
    assert {"python", "config file", "config valid", "workspace", "providers"}.issubset(names)


def test_doctor_report_worst_picks_highest_severity() -> None:
    report = DoctorReport()
    report.add(CheckResult("a", "ok", ""))
    report.add(CheckResult("b", "warn", ""))
    report.add(CheckResult("c", "ok", ""))
    assert report.worst == "warn"
    report.add(CheckResult("d", "fail", ""))
    assert report.worst == "fail"


def test_doctor_report_counts() -> None:
    report = DoctorReport()
    report.add(CheckResult("a", "ok", ""))
    report.add(CheckResult("b", "warn", ""))
    report.add(CheckResult("c", "fail", ""))
    report.add(CheckResult("d", "ok", ""))
    assert report.counts == {"ok": 2, "warn": 1, "fail": 1}


def test_apply_safe_fixes_creates_workspace(valid_config: Path, fake_home: Path) -> None:
    ws = fake_home / ".durin" / "workspace"
    assert not ws.exists()
    applied = apply_safe_fixes()
    assert ws.exists()
    assert any("workspace" in m for m in applied)


def test_run_doctor_returns_zero_when_clean(valid_config: Path, fake_home: Path) -> None:
    (fake_home / ".durin" / "workspace").mkdir(parents=True)
    rc = run_doctor()
    assert rc == 0


def test_run_doctor_returns_one_on_fail(fake_home: Path) -> None:
    # No config → check_config_file fails.
    rc = run_doctor()
    assert rc == 1


def test_run_doctor_json_output(valid_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_doctor(as_json=True)
    captured = capsys.readouterr().out
    # Output is one or more JSON objects (Rich may wrap); the first character of
    # the JSON section should be `{`. We trim any preceding whitespace/lines.
    start = captured.find("{")
    payload = json.loads(captured[start:])
    assert "worst" in payload and "counts" in payload and "results" in payload
    assert isinstance(rc, int)


# ---------------------------------------------------------------------------
# CLI via Typer runner
# ---------------------------------------------------------------------------


def test_cli_doctor_json_emits_parseable_payload(valid_config: Path, fake_home: Path) -> None:
    (fake_home / ".durin" / "workspace").mkdir(parents=True)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    start = result.output.find("{")
    data = json.loads(result.output[start:])
    assert data["worst"] in ("ok", "warn")


def test_cli_doctor_fix_creates_missing_workspace(valid_config: Path, fake_home: Path) -> None:
    ws = fake_home / ".durin" / "workspace"
    assert not ws.exists()
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    assert ws.exists()


def test_cli_doctor_exits_one_when_config_invalid(fake_home: Path) -> None:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not valid json", encoding="utf-8")
    with patch("durin.cli.doctor.get_config_path", return_value=cfg), \
         patch("durin.config.loader.get_config_path", return_value=cfg):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
