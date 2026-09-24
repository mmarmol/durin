"""Approval kind ``exec_command``: run one refused shell command, once.

A command the exec safety policy refused (a deny rule, or a configured
allowlist that does not list it) can be approved by the person in the chat
that asked for it. The request is bound to the exact command, its working
directory and the session (the hash below), and it lifts only the policy rules
that refused that command. The hard floor and every other guard (memory vault,
private URLs, the workspace boundary) still apply when it runs.

It runs only inside the turn that asked: ``ExecTool`` passes its own runner as
``ExecDeps.exec_run``. Approving the record from anywhere else (Pending, the
CLI, the API) fails with a clear message, because a shell command replayed
outside the run that needed it has no defined meaning.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from durin.agent.approval_executors import (
    ApprovalExecError,
    ExecDeps,
    Prepared,
    register,
)

KIND = "exec_command"

_SUMMARY_MAX = 160


def exec_hash(command: str, cwd: str, session_key: str | None) -> str:
    """sha256 over the command, its working directory and the session."""
    blob = json.dumps([command, cwd, session_key or ""], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _short(command: str) -> str:
    first = command.strip().splitlines()[0] if command.strip() else ""
    if len(first) > _SUMMARY_MAX or first != command.strip():
        return first[:_SUMMARY_MAX].rstrip() + "…"
    return first


def prepare(*, command: str, cwd: str, rules: tuple[str, ...], session_key: str | None,
            timeout: int | None, background: bool) -> Prepared:
    """The approval request for running *command* once in *cwd* past *rules*."""
    return Prepared(
        kind=KIND,
        summary=f"run `{_short(command)}`",
        detail={"command": command, "cwd": cwd, "rule": ", ".join(rules)},
        payload={
            "command": command,
            "cwd": cwd,
            "rules": list(rules),
            "session_key": session_key,
            "timeout": timeout,
            "background": background,
        },
        change_hash=exec_hash(command, cwd, session_key),
    )


def _hash(workspace: Path, payload: dict) -> str:
    return exec_hash(str(payload.get("command") or ""), str(payload.get("cwd") or ""),
                     payload.get("session_key"))


async def _execute(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    if deps.exec_run is None:
        raise ApprovalExecError(
            "an approved shell command runs only inside the chat turn that asked "
            "for it; ask the agent to run it again")
    output = await deps.exec_run(
        command=payload["command"],
        working_dir=payload["cwd"],
        timeout=payload.get("timeout"),
        background=bool(payload.get("background")),
        approved_rules=frozenset(payload.get("rules") or ()),
    )
    return {"output": output}


register(KIND, hash_fn=_hash, execute_fn=_execute)
