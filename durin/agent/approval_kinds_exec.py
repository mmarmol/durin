"""Approval kind ``exec_command``: run one refused shell command, once.

A command the exec safety policy refused (a deny rule, or a configured
allowlist that does not list it) can be approved by the person in the chat
that asked for it. The request is bound to the exact command, its working
directory and the session (the hash below), and it lifts only the policy rules
that refused that command. The hard floor and every other guard (memory vault,
private URLs, the workspace boundary) still apply when it runs.

The record never stores the literal command. ``prepare`` redacts it first
(known secrets via ``redact_secrets``, plus a small pass for inline
credentials shell commands commonly carry: an ``Authorization: Bearer``
header, ``token=``, ``--password``, a mysql-style ``-p<password>``, and a
``user:pass@`` URL) — a pending or resolved record sits on disk for up to 30
days, and reviewing or matching an approval never needs the literal.

It runs only inside the turn that asked. ``ExecTool`` passes its own runner as
``ExecDeps.exec_run`` *and* the literal command as
``deps.extra["exec_command"]`` — the only place the literal exists once
``prepare`` has redacted it into the record. The executor re-redacts that
literal and checks it against the recorded (redacted) command: a match
confirms the turn is running the same command the person approved, without
the literal ever needing to be on disk. Approving the record from anywhere
else (``durin approvals``, a webui click after the turn stopped waiting) is
refused by ``approval.decide`` before the record changes: nothing outside the
turn holds the literal, and a shell command replayed outside the run that
needed it has no defined meaning anyway. The executor still refuses to run
with either handle missing, so no other path can run it either.

The command's output goes back to the turn the same way, in memory, as
``deps.extra["exec_output"]``; the record's result is only ``{"ran": True}``.
Output can echo the literal command (a background start message does).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from durin.agent.approval_executors import (
    ApprovalExecError,
    ExecDeps,
    Prepared,
    register,
)
from durin.security.secrets import redact_secrets

KIND = "exec_command"

_SUMMARY_MAX = 160

# Value characters for a bearer token, a `token=`/`--password` value, or a
# mysql-style `-p<password>` — deliberately narrow (no quotes, spaces or
# shell metacharacters) so a trailing quote or `)` around the value is left
# in place rather than swallowed into the mask.
_CRED_VALUE = r"[A-Za-z0-9._~+/=-]+"

# A small, explicit pass for inline-credential shapes that ``redact_secrets``
# (store-value and vendor-pattern based) does not cover on its own — most
# importantly, a credential this short-lived never made it into the secret
# store, so there is no stored value to match against. Order does not matter:
# the patterns do not overlap.
_INLINE_CRED_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>
    (re.compile(rf"(?i)(authorization:\s*bearer\s+){_CRED_VALUE}"), r"\1«redacted»"),
    # token=<value> (a query string, a CLI flag, a config-style assignment)
    (re.compile(rf"(?i)(\btoken=){_CRED_VALUE}"), r"\1«redacted»"),
    # --password <value> / --password=<value>
    (re.compile(rf"(?i)(--password[= ]){_CRED_VALUE}"), r"\1«redacted»"),
    # mysql-style -p<password>, no space — a bare "-p 8080" (space-separated)
    # or "--path" is untouched by the lookbehind excluding "-"/word chars.
    (re.compile(rf"(?<![\w-])-p{_CRED_VALUE}"), "-p«redacted»"),
    # user:pass@ inside a URL — the user stays, only the password is masked.
    (re.compile(r"(://[^\s/:@]+:)[^\s@]+(@)"), r"\1«redacted»\2"),
)


def _redact_command(command: str) -> str:
    """Mask secrets and common inline credentials before *command* ever
    reaches a record or disk. See the module docstring for why."""
    text = redact_secrets(command)
    for pattern, repl in _INLINE_CRED_RULES:
        text = pattern.sub(repl, text)
    return text


def exec_hash(command: str, cwd: str, session_key: str | None) -> str:
    """sha256 over the (already redacted) command, its working directory and
    the session."""
    blob = json.dumps([command, cwd, session_key or ""], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _short(command: str) -> str:
    first = command.strip().splitlines()[0] if command.strip() else ""
    if len(first) > _SUMMARY_MAX or first != command.strip():
        return first[:_SUMMARY_MAX].rstrip() + "…"
    return first


def prepare(*, command: str, cwd: str, rules: tuple[str, ...], session_key: str | None,
            timeout: int | None, background: bool) -> Prepared:
    """The approval request for running *command* once in *cwd* past *rules*.

    *command* is redacted before it touches the summary, detail, payload or
    hash — the record must never carry the literal (an inline credential
    would otherwise sit on disk for the resolved-record retention window).
    """
    redacted = _redact_command(command)
    return Prepared(
        kind=KIND,
        summary=f"run `{_short(redacted)}`",
        detail={"command": redacted, "cwd": cwd, "rule": ", ".join(rules)},
        payload={
            "command": redacted,
            "cwd": cwd,
            "rules": list(rules),
            "session_key": session_key,
            "timeout": timeout,
            "background": background,
        },
        change_hash=exec_hash(redacted, cwd, session_key),
    )


def _hash(workspace: Path, payload: dict) -> str:
    return exec_hash(str(payload.get("command") or ""), str(payload.get("cwd") or ""),
                     payload.get("session_key"))


async def _execute(workspace: Path, payload: dict, deps: ExecDeps) -> dict:
    literal = deps.extra.get("exec_command")
    if deps.exec_run is None or not literal:
        raise ApprovalExecError(
            "an approved shell command runs only inside the chat turn that asked "
            "for it; ask the agent to run it again")
    if _redact_command(literal) != payload.get("command"):
        # The turn handed us a different command than the one the person
        # reviewed (the redacted forms disagree) — never run it on a hunch.
        raise ApprovalExecError(
            "the command to run no longer matches the one that was approved; "
            "ask the agent to request it again")
    output = await deps.exec_run(
        command=literal,
        working_dir=payload["cwd"],
        timeout=payload.get("timeout"),
        background=bool(payload.get("background")),
        approved_rules=frozenset(payload.get("rules") or ()),
    )
    # Back to the turn in memory, never into the record's result: the output
    # can echo the literal command (a background start message does).
    deps.extra["exec_output"] = output
    return {"ran": True}


register(KIND, hash_fn=_hash, execute_fn=_execute)
