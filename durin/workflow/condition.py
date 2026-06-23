"""Objective decision condition: run a shell command; exit 0 means pass.

This is the only condition type in slice 1 — deterministic ground truth, no model
and no judgment involved. Judgment (an agent evaluates) is a later slice. A timeout
or any non-zero exit is a fail; the engine routes on `passed`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class CommandOutcome:
    passed: bool
    exit_code: int
    output: str        # merged stdout+stderr, for the trace and downstream context


def run_command(command: str, *, cwd: str | None = None, timeout: int = 30) -> CommandOutcome:
    """Run *command* in a shell; pass iff it exits 0. Never raises."""
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return CommandOutcome(passed=proc.returncode == 0, exit_code=proc.returncode, output=output)
    except subprocess.TimeoutExpired as exc:
        return CommandOutcome(passed=False, exit_code=124, output=f"timeout after {timeout}s: {exc}")
    except Exception as exc:  # noqa: BLE001 - a malformed command must not crash a run
        return CommandOutcome(passed=False, exit_code=1, output=f"command error: {exc}")
