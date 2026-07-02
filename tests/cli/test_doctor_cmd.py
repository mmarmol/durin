"""Tests for `durin doctor` checks + orchestrator."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli import doctor
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
    # The default config flips `gateway.webui_enabled=True` which makes
    # doctor try to reach the dashboard. We're testing doctor itself
    # here, not the gateway, so disable both service-level checks.
    data.setdefault("gateway", {}).update({
        "daemon": False,
        "webuiEnabled": False,
    })
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with patch("durin.cli.doctor.get_config_path", return_value=cfg), \
         patch("durin.config.loader.get_config_path", return_value=cfg):
        yield cfg


@pytest.fixture
def hermetic_state_probes(monkeypatch: pytest.MonkeyPatch):
    """Make the aggregate exit-code tests deterministic across machines.

    These probes depend on host state that is irrelevant to the exit-code
    aggregation those tests assert, and make a "clean" run non-deterministic:

    - ``check_embedding_model_loads`` does a real model load+embed. On a box
      with the ``[memory]`` extra installed but the ~0.45 GB model not yet
      downloaded it returns ``fail`` (CI runs *without* the extra, so it skips
      there — the failure only surfaces on dev machines).
    - ``check_durin_on_path`` warns when more than one ``durin`` executable is
      on PATH (a dev box with e.g. a pipx + venv install).
    - ``check_gateway_version`` probes the websocket port over HTTP. On a dev
      box a REAL gateway may be serving there (possibly an older version),
      which would leak into the test and even trigger the --fix restart path.

    All probes have their own dedicated unit tests; here we stub them to
    their clean result so these tests exercise the aggregation, not the host.
    """
    monkeypatch.setattr(
        "durin.cli.doctor.check_embedding_model_loads",
        lambda: CheckResult("embedding model load", "ok", "stubbed in test"),
    )
    monkeypatch.setattr(
        "durin.cli.doctor.check_durin_on_path",
        lambda: CheckResult("durin on PATH", "ok", "stubbed in test"),
    )
    monkeypatch.setattr(
        "durin.cli.doctor.check_gateway_version",
        lambda: CheckResult("gateway version", "ok", "stubbed in test", category="services"),
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def test_check_python_version_ok() -> None:
    r = check_python_version()
    assert r.status == "ok"
    assert "Python" in r.message


def test_check_durin_on_path_warns_on_multiple(tmp_path, monkeypatch) -> None:
    from durin.cli.doctor import check_durin_on_path

    # Two PATH dirs each holding an executable `durin`.
    one, two = tmp_path / "a", tmp_path / "b"
    for d in (one, two):
        d.mkdir()
        exe = d / "durin"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{one}:{two}")
    r = check_durin_on_path()
    assert r.status == "warn"
    assert "2 durin executables" in r.message

    monkeypatch.setenv("PATH", str(one))
    assert check_durin_on_path().status == "ok"


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
    # The interpreter's *basename* is not reliably on PATH (macOS ships only
    # python3), but shutil.which accepts a full path — and the running
    # interpreter is guaranteed to exist and be executable.
    r = check_executable(sys.executable, required=True, hint="install it")
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


def test_install_missing_extras_pipx_uses_inject() -> None:
    """`pipx inject` is the non-destructive way to add extras to an
    existing pipx install. No uninstall, no venv recreation."""
    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="pipx", source_root=None, version="0.1.0a4")
    captured: list[tuple[list[str], dict | None]] = []

    def _fake_run(cmd, env=None):
        captured.append((list(cmd), env))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run", side_effect=_fake_run):
        rc = install_missing_extras(["memory", "mcp"], assume_yes=True)
    assert rc == 0
    assert len(captured) == 1, captured
    cmd, env = captured[0]
    # Must be `pipx inject durin-agent <pkgs...>` — no uninstall step.
    assert cmd[:3] == ["pipx", "inject", "durin-agent"]
    # All extras' packages present.
    pkg_args = cmd[3:]
    assert "fastembed" in pkg_args
    assert "lancedb" in pkg_args
    assert "mcp" in pkg_args
    # Bypass uv's index cache so freshly-published versions are visible.
    assert env is not None and env.get("UV_NO_CACHE") == "1"


def test_install_missing_extras_wheel_runs_pip_install() -> None:
    import sys

    from durin.cli.doctor import install_missing_extras
    from durin.cli.upgrade import InstallInfo

    info = InstallInfo(mode="wheel", source_root=None, version="0.1.0a2")
    captured: list[tuple[list[str], dict | None]] = []

    def _fake_run(cmd, env=None):
        captured.append((list(cmd), env))
        class _R:
            returncode = 0
        return _R()

    with patch("durin.cli.upgrade.detect_install_mode", return_value=info), \
         patch("durin.cli.doctor.subprocess.run", side_effect=_fake_run):
        rc = install_missing_extras(["memory"], assume_yes=True)
    assert rc == 0
    assert len(captured) == 1
    cmd, env = captured[0]
    assert cmd == [sys.executable, "-m", "pip", "install", "--upgrade", "durin-agent[memory]"]
    # Wheel mode doesn't need the uv-cache bypass.
    assert env is None


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


def test_run_doctor_returns_zero_when_clean(
    valid_config: Path, fake_home: Path, hermetic_state_probes
) -> None:
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


def test_cli_doctor_json_emits_parseable_payload(
    valid_config: Path, fake_home: Path, hermetic_state_probes
) -> None:
    (fake_home / ".durin" / "workspace").mkdir(parents=True)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    start = result.output.find("{")
    data = json.loads(result.output[start:])
    assert data["worst"] in ("ok", "warn")


def test_cli_doctor_fix_creates_missing_workspace(
    valid_config: Path, fake_home: Path, hermetic_state_probes
) -> None:
    ws = fake_home / ".durin" / "workspace"
    assert not ws.exists()
    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    assert ws.exists()


def test_embedding_model_load_skips_when_fastembed_absent(
    valid_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI runs without the ``[memory]`` extra. A missing fastembed must be a
    *skip*, not a ``fail`` — otherwise ``durin doctor`` exits 1 (and the whole
    CI suite goes red) for every install that didn't opt into memory."""
    import builtins

    from durin.cli.doctor import check_embedding_model_loads

    _real_import = builtins.__import__

    def _no_fastembed(name: str, *args: object, **kwargs: object):
        if name.split(".")[0] == "fastembed":
            raise ImportError("simulated: fastembed not installed")
        return _real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)
    result = check_embedding_model_loads()
    assert result.status != "fail", result.message


def test_cli_doctor_exits_one_when_config_invalid(fake_home: Path) -> None:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not valid json", encoding="utf-8")
    with patch("durin.cli.doctor.get_config_path", return_value=cfg), \
         patch("durin.config.loader.get_config_path", return_value=cfg):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# check_gateway_daemon — PID file is ground truth
# ---------------------------------------------------------------------------


class _FakeDaemonStatus:
    def __init__(self, state: str, pid: int | None = None) -> None:
        self.state = state
        self.pid = pid


def test_gateway_daemon_running_is_ok(valid_config: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_status",
        lambda: _FakeDaemonStatus("running", 4242),
    )
    r = doctor.check_gateway_daemon()
    assert r.status == "ok"
    assert "4242" in r.message


def test_gateway_daemon_stale_pid_fails_even_with_daemon_flag_off(
    valid_config: Path, monkeypatch
) -> None:
    """A crashed daemon must be reported regardless of config.gateway.daemon —
    the daemon is routinely started with the flag off (`durin gateway start`)."""
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_status",
        lambda: _FakeDaemonStatus("stale_pid", 4242),
    )
    r = doctor.check_gateway_daemon()
    assert r.status == "fail"


def test_gateway_daemon_not_running_without_request_is_ok(
    valid_config: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_status",
        lambda: _FakeDaemonStatus("not_running"),
    )
    r = doctor.check_gateway_daemon()
    assert r.status == "ok"


def test_gateway_daemon_not_running_with_daemon_requested_fails(
    fake_home: Path, monkeypatch
) -> None:
    cfg = fake_home / ".durin" / "config.json"
    cfg.parent.mkdir(parents=True)
    data = Config().model_dump(mode="json", by_alias=True)
    data.setdefault("gateway", {}).update({"daemon": True})
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.daemon_status",
        lambda: _FakeDaemonStatus("not_running"),
    )
    with patch("durin.config.loader.get_config_path", return_value=cfg):
        r = doctor.check_gateway_daemon()
    assert r.status == "fail"


# ---------------------------------------------------------------------------
# check_gateway_version — stale-install detection
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self, response: _FakeHttpResponse | Exception) -> None:
        self._response = response

    def __call__(self, *a, **k):  # stands in for httpx.Client(...)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url: str):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_httpx_client(monkeypatch, response) -> None:
    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeHttpClient(response))


def test_gateway_version_match_is_ok(valid_config: Path, monkeypatch) -> None:
    from durin import __version__

    _patch_httpx_client(
        monkeypatch, _FakeHttpResponse(200, {"status": "ok", "version": __version__})
    )
    r = doctor.check_gateway_version()
    assert r.status == "ok"


def test_gateway_version_mismatch_warns_with_restart_fix(
    valid_config: Path, monkeypatch
) -> None:
    _patch_httpx_client(
        monkeypatch, _FakeHttpResponse(200, {"status": "ok", "version": "0.0.0-old"})
    )
    r = doctor.check_gateway_version()
    assert r.status == "warn"
    assert "0.0.0-old" in r.message
    assert "restart" in (r.fix or "")


def test_gateway_version_unreachable_is_skipped(valid_config: Path, monkeypatch) -> None:
    _patch_httpx_client(monkeypatch, ConnectionError("refused"))
    r = doctor.check_gateway_version()
    assert r.status == "ok"
    assert "skipped" in r.message


def test_gateway_version_missing_field_warns(valid_config: Path, monkeypatch) -> None:
    """An older gateway whose /health predates version reporting IS stale."""
    _patch_httpx_client(monkeypatch, _FakeHttpResponse(200, {"status": "ok"}))
    r = doctor.check_gateway_version()
    assert r.status == "warn"


# ---------------------------------------------------------------------------
# apply_service_repairs
# ---------------------------------------------------------------------------


def _report_with(name: str, status: str) -> doctor.DoctorReport:
    report = doctor.DoctorReport()
    report.add(CheckResult(name, status, "x", category="services"))
    return report


def test_repairs_relaunch_dead_daemon(valid_config: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.start_daemon", lambda *a, **k: calls.append("start") or 4242
    )
    monkeypatch.setattr("durin.cli.doctor._wait_for_gateway_health", lambda **k: True)
    msgs = doctor.apply_service_repairs(_report_with("gateway daemon", "fail"))
    assert calls == ["start"]
    assert any("Relaunched" in m for m in msgs)


def test_repairs_restart_stale_gateway_with_yes(valid_config: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.stop_daemon", lambda *a, **k: calls.append("stop")
    )
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.start_daemon", lambda *a, **k: calls.append("start") or 4242
    )
    monkeypatch.setattr("durin.cli.doctor._wait_for_gateway_health", lambda **k: True)
    msgs = doctor.apply_service_repairs(
        _report_with("gateway version", "warn"), assume_yes=True
    )
    assert calls == ["stop", "start"]
    assert any("Restarted" in m for m in msgs)


def test_repairs_stale_gateway_declined_does_nothing(
    valid_config: Path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.stop_daemon", lambda *a, **k: calls.append("stop")
    )
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    msgs = doctor.apply_service_repairs(_report_with("gateway version", "warn"))
    assert calls == []
    assert msgs == []


def test_repairs_clean_report_does_nothing(valid_config: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "durin.cli.gateway_daemon.start_daemon", lambda *a, **k: calls.append("start") or 1
    )
    msgs = doctor.apply_service_repairs(_report_with("gateway version", "ok"))
    assert calls == []
    assert msgs == []
