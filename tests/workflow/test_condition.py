"""Tests for evaluating a decision node's command condition."""

from durin.workflow.condition import CommandOutcome, run_command


def test_exit_zero_passes():
    out = run_command("exit 0")
    assert isinstance(out, CommandOutcome)
    assert out.passed is True
    assert out.exit_code == 0


def test_nonzero_exit_fails():
    out = run_command("exit 3")
    assert out.passed is False
    assert out.exit_code == 3


def test_captures_output():
    out = run_command("echo hello; echo oops 1>&2")
    assert "hello" in out.output
    assert "oops" in out.output


def test_runs_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    out = run_command("test -f marker.txt", cwd=str(tmp_path))
    assert out.passed is True


def test_timeout_fails_without_raising():
    out = run_command("sleep 5", timeout=1)
    assert out.passed is False
    assert out.exit_code != 0
