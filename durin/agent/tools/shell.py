"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field

from durin.agent.approval_prompt import ChatHandles
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext, RequestContextVar
from durin.agent.tools.sandbox import wrap_command
from durin.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.config.paths import get_media_dir
from durin.config.schema import Base
from durin.utils.subprocess_cleanup import aclose_subprocess

_IS_WINDOWS = sys.platform == "win32"


# Policy note appended to recoverable workspace-boundary guard errors.
_WORKSPACE_BOUNDARY_NOTE = (
    "\n\nNote: this is a hard policy boundary, not a transient failure. "
    "Do NOT retry with shell tricks (symlinks, base64 piping, alternative "
    "tools, working_dir overrides). If the user genuinely needs this "
    "resource, tell them you cannot reach it under the current "
    "restrict_to_workspace policy and ask how to proceed."
)

# Policy note appended when the deny list or a configured allowlist refuses a
# command. The same effect is always reachable another way (python, find
# -delete, a script), so the model is told to stop and ask, not to reword.
_COMMAND_POLICY_NOTE = (
    "\n\nNote: this is the exec safety policy, not a transient failure. "
    "Do NOT get the same result another way (a reworded command, python or "
    "perl, find -delete, a script you write and run, or another tool). Stop, "
    "tell the user what you wanted to run and why, and ask the user how to "
    "proceed: they can run it themselves, or allow it for you via "
    "tools.exec.allow_patterns."
)

# Policy note appended when the hard floor refuses a command. These commands
# never run through durin, not even with the user's approval, so the note
# offers neither allow_patterns nor an approval.
_HARD_FLOOR_NOTE = (
    "\n\nNote: this command is on the exec hard floor: durin never runs it, "
    "not even with the user's approval. Do NOT get the same result another "
    "way (a reworded command, python or perl, a script you write and run, or "
    "another tool). Tell the user what you wanted to run and why; if it is "
    "really needed, they must run it themselves outside durin."
)

# Appended to a deny/allowlist refusal after the person in the chat was asked
# to approve that exact command and declined.
_APPROVAL_DECLINED_NOTE = (
    "\n\nThe user was asked to approve this exact command and declined. Do "
    "NOT retry it, and do NOT get the same result another way (a reworded "
    "command, python or perl, find -delete, a script you write and run, or "
    "another tool). Continue without it, or ask the user what they want instead."
)

# Appended when the approval request got no answer during the turn. The
# request is closed, not left waiting: approving it later would run a shell
# command outside the turn that needed it.
_APPROVAL_UNANSWERED_NOTE = (
    "\n\nThe user was asked to approve this exact command and did not answer, "
    "so it did not run and the request was dropped. Do NOT retry it, and do "
    "NOT get the same result another way (a reworded command, python or perl, "
    "find -delete, a script you write and run, or another tool). Continue "
    "without it, or ask the user."
)

# Appended to the output of a command that ran after the person approved it.
_APPROVAL_APPLIED_NOTE = "\n\n(The user approved this exact command, once.)"

# Rule name recorded when a configured allowlist refuses a command: no pattern
# matched, so the refusal names the setting the command is missing from.
_ALLOWLIST_RULE = "tools.exec.allow_patterns"

# The longest command the guard checks; a longer one is refused unchecked
# (fail closed, and not approvable) with a pointer to write_file, the tool for
# long content. Every hardcoded deny/hard-floor/vault pattern below is either
# inherently linear or, where it isn't, gated by a cheap literal pre-check
# (``_cheap_prefilter_ok``'s tables, ``_guard_memory_mutation``'s own
# "memory/" check) that rules it out in one linear pass when the literal its
# match requires is absent — the case that used to be quadratic: an
# adversarial repeat of an anchor word ("rm "/"cp "/"sudo -x "/... thousands
# of times with no trigger literal anywhere, where each occurrence made the
# pattern rescan the rest of the command before failing. A caller-configured
# ``tools.exec.deny_patterns``/``allow_patterns`` entry is arbitrary regex, not
# analyzed this way, so this bound still limits its own worst case. At this
# size the whole adversarial check (every pattern, off the loop so other
# chats still run meanwhile) stays well under a tenth of a second; a
# realistic long command (a heredoc, a long script, a large JSON argument)
# checks just as fast, since none of this cost was ever about typical input.
MAX_CHECKED_COMMAND_CHARS = 200_000

# A command position: the start of the command, right after a separator
# (including a backtick or an opening brace, for `` `cmd` `` and `{ cmd; }`),
# after a wrapper that runs its argument as a command (sudo, env, nice, time,
# systemctl…), or after a `sh`/`bash`/`dash`/`zsh`/`ksh -c '...'` shell
# wrapper (with its own flags, e.g. `bash -lc`), with an optional path prefix.
# Anchoring there keeps a word such as "shutdown" inside an argument (a grep
# pattern, a test file name, a path component with more path after it) off
# the floor, while still catching the command inside a shell wrapper.
_CMD_START = (
    r"(?:^|[;&|(\n`{]\s*|\b(?:sudo|doas|exec|nohup|command|systemctl|env|nice|time)\s+"
    r"(?:-\S+\s+(?:[^-\s]\S*\s+)?)*|\b(?:ba|da|z|k)?sh\s+(?:-\w+\s+)*-\w*c\w*\s+[\"']?\s*)"
    r"(?:\S*/)?"
)
# Whole-disk block devices (Linux, macOS).
_RAW_DISK = r"/dev/(?:sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|disk\d|rdisk\d)"

# Commands that never run, not even when the user approves them: removing the
# filesystem root or the home directory recursively, formatting or overwriting
# a whole disk, a fork bomb, powering the host off, and approving or rejecting
# a pending approval request through the shell (which would let the model
# authorize its own privileged action — the "durin approvals" CLI itself also
# refuses to run without a TTY, as a second, independent layer). Matched
# against the lowercased command before allow_patterns, so no configuration
# exempts them.
_HARD_FLOOR_PATTERNS: tuple[str, ...] = (
    # rm with a recursive flag whose target is exactly /, /*, ~, ~/, ~/*,
    # $HOME or ${HOME} (optionally quoted around the whole target, or with
    # the closing quote right after "HOME"/"}", options before or after).
    r"\brm\b(?=[^;&|\n]*\s(?:-[a-z]*r[a-z]*|--recursive)(?:\s|$))"
    r"[^;&|\n]*\s[\"']?(?:/\*?|~/?\*?|\$\{?home\}?[\"']?/?\*?)[\"']?(?=\s|$|[;&|])",
    # (?![\w./-]) rejects a directory or file name that merely starts with
    # this word (a "shutdown" or "mkfs" worktree/directory, a ".sh" suffix):
    # the word must end the path, not continue into more of it.
    _CMD_START + r"(?:mkfs(?:\.[a-z0-9]+)?|diskpart)(?![\w./-])",
    r"\bdd\b[^;&|\n]*\bof=" + _RAW_DISK,
    r">\s*" + _RAW_DISK,
    r"\\\\\.\\physicaldrive\d",  # Windows raw disk: \\.\PhysicalDriveN
    # Fork bomb: a function that pipes itself into itself in the background.
    # (?<![\w:]) keeps the function-name group from starting mid-word, so it
    # cannot backtrack character-by-character over a long unrelated word
    # (quadratic time on a long argument that merely contains no fork bomb).
    r"(?<![\w:])(?P<fn>[\w:]+)\s*\(\)\s*\{\s*(?P=fn)\s*\|\s*(?P=fn)\s*&\s*;?\s*\}\s*;\s*(?P=fn)",
    _CMD_START + r"(?:shutdown|reboot|poweroff|halt)(?![\w./-])",
    # SysV runlevel 0 ("init 0") halts/powers off the host, same as the
    # commands above. Command position only, so "npm init 0" (a made-up
    # package version, not a runlevel) is unaffected.
    _CMD_START + r"init\s+0\b",
    # "durin approvals approve/reject" at command position — a bare
    # invocation, "python -m durin approvals ...", or nested inside a shell
    # wrapper (`` `...` ``, `{ ...; }`, `bash -c '...'`, `sh -c "..."`) — so
    # the model cannot approve its own pending request through a shell. The
    # CLI's own TTY requirement (approve/reject refuse to run without one) is
    # a second, independent layer behind this one. Tolerant of the CLI's own
    # options (-w/--workspace/--all/-c/...) and of a quoted "approvals" or
    # "approve"/"reject" token, and of extra whitespace between the words.
    # Anchored at command position so "grep 'durin approvals approve' docs/"
    # or a commit message mentioning the phrase is not refused.
    _CMD_START + r"(?:\S*python\S*\s+-m\s+)?durin\b\s+[\"']?approvals[\"']?"
    r"(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+[\"']?(?:approve|reject)\b",
)

# Cheap pre-filter for a pattern above keyed by its exact text: a tuple of
# clauses, ANDed together, each clause a tuple of literal substrings ORed
# together. Every pattern here requires ALL of its clauses' literals to
# appear (in lowercase) SOMEWHERE in the command before it can possibly
# match — verified by inspection against the pattern it gates, not derived
# mechanically — so when a clause's literals are all absent, the regex is
# skipped instead of run: it could not have matched anyway. This is what
# keeps each of these patterns fast on an adversarial command that repeats
# an anchor word (e.g. "sudo -x " thousands of times) without ever supplying
# the pattern's own required literal: instead of the regex engine failing
# only after an expensive scan at every anchor occurrence (quadratic over
# the whole command), the single substring scan below rules the whole
# pattern out in one linear pass. A pattern not listed here is always run —
# skipping it was not proven safe, so it is not skipped.
_HARD_FLOOR_PRECHECKS: dict[str, tuple[tuple[str, ...], ...]] = {
    _HARD_FLOOR_PATTERNS[0]: (("-",),),                                        # rm: needs a flag
    _HARD_FLOOR_PATTERNS[1]: (("mkfs", "diskpart"),),
    _HARD_FLOOR_PATTERNS[2]: (("/dev/",),),                                     # dd of=<raw disk>
    _HARD_FLOOR_PATTERNS[6]: (("shutdown", "reboot", "poweroff", "halt"),),
    _HARD_FLOOR_PATTERNS[7]: (("init",),),
    _HARD_FLOOR_PATTERNS[8]: (("durin",), ("approv",)),                        # "approvals"/"approve"
}


def _cheap_prefilter_ok(lower: str, clauses: tuple[tuple[str, ...], ...] | None) -> bool:
    """Whether the regex a *clauses* entry gates might still match — see
    the precheck tables' own comment for what a clause means and why this
    is safe. ``None`` (no entry) always allows the regex to run."""
    return clauses is None or all(
        any(lit in lower for lit in alternatives) for alternatives in clauses
    )


# The exec tool's own hardcoded deny patterns (a config-supplied deny_patterns
# list is prepended to these in ExecTool.__init__, never mixed into this
# constant, since only THESE fixed strings are analyzed for a safe precheck
# below — an arbitrary user-supplied pattern is not).
_DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
    r"\bdel\s+/[fq]\b",              # del /f, del /q
    r"\brmdir\s+/s\b",               # rmdir /s
    r"(?:^|[;&|]\s*)format(?!=)\b",   # format (as standalone command only)
    r"\b(mkfs|diskpart)\b",          # disk operations
    r"\bdd\s+if=",                   # dd
    r">\s*/dev/sd",                  # write to disk
    r"\b(shutdown|reboot|poweroff)\b",  # system power
    r":\(\)\s*\{.*\};\s*:",          # fork bomb
    # Block writes to durin internal state files. history.jsonl is
    # append-only and owned by append_history(); a direct write
    # corrupts the cursor format and breaks every later append.
    r">>?\s*\S*history\.jsonl",                       # > / >> redirect
    r"\btee\b[^|;&<>]*history\.jsonl",                 # tee / tee -a
    r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*history\.jsonl",  # cp/mv target
    r"\bdd\b[^|;&<>]*\bof=\S*history\.jsonl",        # dd of=
    r"\bsed\s+-i[^|;&<>]*history\.jsonl",              # sed -i
)

# Same idea as _HARD_FLOOR_PRECHECKS, for the history.jsonl guards above: all
# five require the literal "history.jsonl" somewhere in the command, so an
# adversarial repeat of "cp "/"mv "/"tee "/"dd "/"sed " with no such target
# anywhere is ruled out in one linear scan instead of failing expensively at
# every occurrence.
_DENY_PRECHECKS: dict[str, tuple[tuple[str, ...], ...]] = {
    p: (("history.jsonl",),) for p in _DEFAULT_DENY_PATTERNS[9:14]
}


@dataclass(frozen=True)
class CommandRefusal:
    """Why the exec guard refused a command.

    ``kind`` is ``hard_floor``, ``deny`` or ``allowlist`` for the exec safety
    policy, and ``guard`` for every other refusal (memory vault, private URL,
    workspace boundary). ``rules`` are the policy rules that matched: a person
    may approve running the command once past exactly those rules, except on
    the hard floor. ``headline`` is the first line of the refusal text and
    ``message`` the whole text the model receives.
    """

    kind: str
    headline: str
    note: str = ""
    rules: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        return self.headline + self.note

    @property
    def approvable(self) -> bool:
        return self.kind in ("deny", "allowlist")


class ExecToolConfig(Base):
    """Shell exec tool configuration."""
    enable: bool = Field(default=True, description="Enable the shell exec tool")
    timeout: int = Field(default=60, description="Default command timeout in seconds")
    path_append: str = Field(default="", description="Directories appended to PATH for executed commands")
    sandbox: str = Field(default="", description="Optional sandbox wrapper command that executed commands run through; empty = no sandbox")
    allowed_env_keys: list[str] = Field(default_factory=list, description="Env var names passed into the subprocess in addition to the safe defaults")
    allow_patterns: list[str] = Field(default_factory=list, description="Command patterns allowed to run")
    deny_patterns: list[str] = Field(default_factory=list, description="Command patterns refused; deny wins over allow")


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema("The shell command to execute"),
        working_dir=StringSchema("Optional working directory for the command"),
        timeout=IntegerSchema(
            60,
            description=(
                "Timeout in seconds. Increase for long-running commands "
                "like compilation or installation (default 60, max 600)."
            ),
            minimum=1,
            maximum=600,
        ),
        background=BooleanSchema(
            description=(
                "Run the command in the background and return a process id "
                "immediately (for dev servers, builds, watchers). Poll or "
                "stop it with the process tool; timeout does not apply."
            ),
        ),
        required=["command"],
    )
)
class ExecTool(Tool, ContextAware):
    """Tool to execute shell commands."""
    _scopes = {"core", "subagent"}

    config_key = "exec"

    @classmethod
    def config_cls(cls):
        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.exec.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = ctx.config.exec
        return cls(
            working_dir=ctx.workspace,
            timeout=cfg.timeout,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            sandbox=cfg.sandbox,
            path_append=cfg.path_append,
            allowed_env_keys=cfg.allowed_env_keys,
            allow_patterns=cfg.allow_patterns,
            deny_patterns=cfg.deny_patterns,
            process_config=getattr(ctx.config, "process", None),
            chat=ChatHandles.from_tool_context(ctx),
        )

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        sandbox: str = "",
        path_append: str = "",
        allowed_env_keys: list[str] | None = None,
        process_config: Any = None,
        chat: ChatHandles | None = None,
    ):
        self.timeout = timeout
        # What asking the person in the chat to approve a refused command
        # needs; without sessions nobody is ever asked.
        self._chat = chat or ChatHandles()
        self._process_config = process_config
        self.working_dir = working_dir
        # This turn's context: the instance is shared by concurrent turns.
        self._ctx = RequestContextVar("exec_request_ctx")
        self.sandbox = sandbox
        self.deny_patterns = (deny_patterns or []) + list(_DEFAULT_DENY_PATTERNS)
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.allowed_env_keys = allowed_env_keys or []

    def set_context(self, ctx: RequestContext) -> None:
        self._ctx.set(ctx)

    def _work_dir(self) -> Path | None:
        """Return the per-session work directory, creating it if necessary.

        A subprocess cannot start in a non-existent directory, so this method
        creates the directory before returning it. Returns None when no session
        context is set or no workspace is configured.
        """
        ctx = self._ctx.get()
        sk = ctx.session_key if ctx else None
        if not sk or not self.working_dir:
            return None
        from durin.agent.tools.work_area import session_work_dir
        work = session_work_dir(Path(self.working_dir), sk)
        work.mkdir(parents=True, exist_ok=True)
        return work

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    # Kernel device files are safe as stdio redirect targets.
    _BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    })

    # Mutations of the `memory/` vault must go through the memory_upsert_entity /
    # memory_forget tools — a raw rm/mv/redirect/sed leaves the FTS +
    # vector index pointing at a missing file (orphan rows the auto-repair
    # can't reconstruct). Reads (cat/ls/grep) stay allowed. `memory/` is
    # matched as a path segment (absolute `/…/memory/` or relative
    # `memory/`); the boundary char before it keeps `inmemory/` and the
    # like from tripping. The gap excludes command separators so a read of
    # memory/ piped to an unrelated write isn't flagged.
    _MEMREF = r"[^|;&\n]*[\s'\"=/(]memory/"
    _MEMORY_MUTATION_PATTERNS: tuple[str, ...] = (
        rf"\brm\b{_MEMREF}",
        rf"\bmv\b{_MEMREF}",
        rf"\bcp\b{_MEMREF}",
        rf"\btruncate\b{_MEMREF}",
        rf"\btee\b{_MEMREF}",
        # The atomic groups commit to the first "-i" / "of=" after the command
        # word. A later one only sees less of the same segment, so the match is
        # the same, but the check stays quadratic instead of cubic on a long
        # command (two open-ended scans back to back).
        rf"\bsed\b(?>[^|;&\n]*?-i){_MEMREF}",
        r"\bdd\b(?>[^|;&\n]*?\bof=)[^|;&\n]*memory/",
        r">>?\s*(?:[^\s'\"|;&<>]*/)?memory/",
    )

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output. "
            "Prefer read_file/write_file/edit_file over cat/echo/sed, "
            "and grep/glob over shell find/grep. "
            "Use -y or --yes flags to avoid interactive prompts. "
            "Output is truncated at 10 000 chars; timeout defaults to 60s. "
            "Destructive commands (rm -rf, dd, mkfs, format, shutdown) and "
            "writes into memory/ or history files are blocked. In a chat, the "
            "tool itself shows a blocked command to the user, who can approve "
            "running it once; wiping / or home, formatting a disk and shutdown "
            "never run. When a command is refused, do not reach the result "
            "another way. "
            "Set background=true for long-lived commands (servers, builds) "
            "and manage them with the process tool."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, background: bool = False, **kwargs: Any,
    ) -> str:
        # The model-facing entry, and the only caller that may ask the person
        # to approve a refused command. Nothing else in kwargs reaches the
        # runner, so the model cannot lift a rule by passing one.
        return await self._run(command, working_dir, timeout, background, ask=True)

    async def _run(
        self, command: str, working_dir: str | None = None,
        timeout: int | None = None, background: bool = False, *,
        approved_rules: frozenset[str] = frozenset(), ask: bool = False,
    ) -> str:
        """Guard and run *command*.

        Never opens an approval unless ``ask`` is set, and only ``execute``
        sets it. This is the runner handed to approval executors and to the
        skill tools: a command they run that the policy refuses fails with
        the refusal text instead of opening a second approval in the middle
        of the one being carried out.

        ``approved_rules`` is set only when a person approved this exact
        command: the policy rules it lifts are skipped, nothing else is.
        """
        cwd = working_dir or str(self._work_dir() or self.working_dir or os.getcwd())

        # Prevent an LLM-supplied working_dir from escaping the configured
        # workspace when restrict_to_workspace is enabled. Without this
        # check, a caller could pass working_dir="/etc" and bypass the
        # cwd-anchored guard.
        if self.restrict_to_workspace and self.working_dir:
            try:
                requested = Path(cwd).expanduser().resolve()
                workspace_root = Path(self.working_dir).expanduser().resolve()
            except Exception:
                return (
                    "Error: working_dir could not be resolved"
                    + _WORKSPACE_BOUNDARY_NOTE
                )
            if requested != workspace_root and workspace_root not in requested.parents:
                return (
                    "Error: working_dir is outside the configured workspace"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

        refusal = await self._check_off_loop(command, cwd, approved_rules=approved_rules)
        if refusal is not None:
            if ask and refusal.approvable:
                return await self._ask_to_run(command, cwd, refusal, timeout, background)
            return refusal.message

        if self.sandbox:
            if _IS_WINDOWS:
                logger.warning(
                    "Sandbox '{}' is not supported on Windows; running unsandboxed",
                    self.sandbox,
                )
            else:
                workspace = self.working_dir or cwd
                command = wrap_command(self.sandbox, command, workspace, cwd)
                cwd = str(Path(workspace).resolve())

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)
        env = self._build_env()

        if self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append
            else:
                env["DURIN_PATH_APPEND"] = self.path_append
                command = f'export PATH="$PATH{os.pathsep}$DURIN_PATH_APPEND"; {command}'

        if background:
            # Full guard pipeline (deny patterns, memory-vault guard,
            # workspace boundary, sandbox wrap, env curation) already ran
            # above — background mode adds no new privileges.
            from durin.agent.tools.process_registry import get_process_registry
            try:
                registry = get_process_registry(self._process_config)
                sess = await registry.spawn(command, cwd=cwd, env=env)
            except RuntimeError as e:
                return f"Error: {e}"
            return (
                f"Started background process {sess.id} (pid {sess.pid}).\n"
                f"Command: {command[:200]}\n"
                "It keeps running across turns. Check it with "
                f"process(action='poll', id='{sess.id}'); stop it with "
                f"process(action='kill', id='{sess.id}'). Use sleep between "
                "polls instead of busy-looping."
            )

        try:
            process = await self._spawn(command, cwd, env)

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return f"Error: Command timed out after {effective_timeout} seconds"
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            # Close the subprocess transport inside the loop so its GC
            # ``__del__`` doesn't fire post-loop (durin/utils/subprocess_cleanup.py).
            await aclose_subprocess(process)

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # T4 — Tool output cap with spill-to-disk for recovery.
            # The model gets head+tail of the output plus a reference to a
            # spill file under <workspace>/.durin/spills/ that it can read
            # with read_file when it needs the omitted middle.
            from durin.agent.tools.output_spill import truncate_with_spill
            from durin.security.secrets import redact_secrets
            from durin.telemetry.logger import current_telemetry

            workspace_path = Path(self.working_dir) if self.working_dir else None
            rendered, spill_meta = truncate_with_spill(
                result,
                tool_name="exec",
                workspace=workspace_path,
                max_chars=self._MAX_OUTPUT,
                redact=redact_secrets,
            )
            if spill_meta.get("original_chars", 0) != spill_meta.get("rendered_chars", 0):
                logger_obj = current_telemetry()
                if logger_obj is not None:
                    with suppress(Exception):
                        logger_obj.log("tool.exec.spill", spill_meta)
            return rendered

        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _ask_to_run(
        self, command: str, cwd: str, refusal: CommandRefusal,
        timeout: int | None, background: bool,
    ) -> str:
        """Ask the person in this chat to approve one refused command.

        Approved, it runs once, past only the policy rules that refused it.
        With nobody to ask (cron, workflow, sub-agent, a chat with no live
        consumer: the asker is None unless ``approval.human_reachable``; and in
        a turn with input from an API token, which never asks in the chat), the
        refusal stands and nothing is filed: a shell command replayed outside
        the run that needed it has no defined meaning. A filed request is
        never left pending either: declined, it is rejected; unanswered, or
        cancelled with the turn while it waits, it is closed as expired.
        """
        from durin.agent import approval, approval_store
        from durin.agent.approval_executors import ExecDeps
        from durin.agent.approval_kinds_exec import prepare

        ctx = self._ctx.get()
        ask = self._chat.asker(ctx) if self.working_dir else None
        if ask is None:
            return refusal.message

        # Ask only for a command an approval would actually let run. With the
        # refusing rules lifted, a further policy rule (an allowlist behind a
        # deny match) joins the request; any other refusal (the memory vault,
        # a private URL, the workspace boundary) stands and nobody is asked.
        rules = refusal.rules
        while (further := await self._check_off_loop(
                command, cwd, approved_rules=frozenset(rules))) is not None:
            if not further.approvable:
                return further.message
            rules += further.rules

        session_key = ctx.session_key
        filed: list[str] = []

        async def ask_once(record: dict) -> str | None:
            filed.append(record["id"])
            return await ask(record)

        # The literal command exists only here, in memory: the record holds a
        # redacted copy, and the executor runs this literal after checking that
        # it redacts to the recorded one. Its output comes back the same way.
        deps = ExecDeps(exec_run=self._run, extra={"exec_command": command})
        try:
            outcome = await approval.request(
                self.working_dir,
                prepare(command=command, cwd=cwd, rules=rules, session_key=session_key,
                        timeout=timeout, background=background),
                session_key=session_key, deps=deps, ask=ask_once)
        finally:
            # An exec request never waits for `durin approvals`: approving it
            # later would run a shell command outside the turn that needed it.
            # Close it when no answer came back, including when the turn is
            # cancelled (/stop, shutdown) while it waits. An answered request,
            # or one decided from outside meanwhile, is no longer pending, so
            # this leaves it alone.
            for approval_id in filed:
                approval_store.transition(
                    self.working_dir, approval_id, expect=("pending",), to="expired",
                    result={"reason": "not answered during the turn"})

        if outcome.status == "applied":
            return str(deps.extra.get("exec_output", "")) + _APPROVAL_APPLIED_NOTE
        if outcome.status == "rejected":
            return refusal.headline + _APPROVAL_DECLINED_NOTE
        if outcome.status == "pending":
            return refusal.headline + _APPROVAL_UNANSWERED_NOTE
        if outcome.status == "failed":
            return f"Error: {outcome.message}"
        return f"{refusal.headline}\n\n{outcome.message}"

    @staticmethod
    async def _spawn(
        command: str, cwd: str, env: dict[str, str],
    ) -> asyncio.subprocess.Process:
        """Launch *command* in a platform-appropriate shell."""
        if _IS_WINDOWS:
            # create_subprocess_exec re-quotes args via list2cmdline, which
            # breaks commands containing paths with spaces (e.g. "D:\Program
            # Files\python.exe" "script.py"). create_subprocess_shell passes
            # the raw command string to COMSPEC without re-quoting.
            return await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        bash = shutil.which("bash") or "/bin/bash"
        return await asyncio.create_subprocess_exec(
            bash, "-l", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own process group → group kill works
        )

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """Kill a subprocess and everything it started, then reap it.

        On POSIX the command leads its own process group (``_spawn``), so the
        group kill also reaches children the shell forked — ``a && b`` runs
        ``a`` as a child. Killing only the shell would orphan them, and an
        orphan holding the output pipes stalls the reap below until its
        timeout.
        """
        if _IS_WINDOWS:
            process.kill()
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            await aclose_subprocess(process)
            if not _IS_WINDOWS:
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError) as e:
                    logger.debug("Process already reaped or not found: {}", e)

    @staticmethod
    def _exec_scoped_secrets() -> dict[str, str]:
        """Stored secrets whose ``scope`` authorizes the ``exec`` consumer.

        These are injected into the subprocess env so scripts can read
        them — the agent issues the command but never sees the values.
        """
        try:
            from durin.security.secrets import get_secret_store

            return get_secret_store().collect_for("exec")
        except Exception:  # noqa: BLE001
            return {}

    def _build_env(self) -> dict[str, str]:
        """Build a minimal environment for subprocess execution.

        On Unix, only HOME/LANG/TERM are passed; ``bash -l`` sources the
        user's profile which sets PATH and other essentials.

        On Windows, ``cmd.exe`` has no login-profile mechanism, so a curated
        set of system variables (including PATH) is forwarded.

        Ambient API keys are NOT inherited. The only credentials present
        are stored secrets explicitly granted the ``exec`` scope.
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PYTHONUNBUFFERED": "1",
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            for key in self.allowed_env_keys:
                val = os.environ.get(key)
                if val is not None:
                    env[key] = val
            env.update(self._exec_scoped_secrets())
            return env
        home = os.environ.get("HOME", "/tmp")
        env = {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONUNBUFFERED": "1",
        }
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        env.update(self._exec_scoped_secrets())
        return env

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands.

        The refusal text, or None when the command may run.
        """
        refusal = self._check(command, cwd)
        return refusal.message if refusal is not None else None

    async def _check_off_loop(
        self, command: str, cwd: str, *,
        approved_rules: frozenset[str] = frozenset(),
    ) -> CommandRefusal | None:
        """``_check`` in a worker thread, so a slow check (a long command, the
        DNS lookups of the private-URL guard) delays only this call, and the
        event loop keeps serving other chats between the guard's steps.
        ``_check`` reads only this tool's fixed configuration and the
        filesystem, never loop-bound state, so it is safe off the loop."""
        return await asyncio.to_thread(self._check, command, cwd,
                                       approved_rules=approved_rules)

    def _check(
        self, command: str, cwd: str, *,
        approved_rules: frozenset[str] = frozenset(),
    ) -> CommandRefusal | None:
        """Run the guard pipeline: the first refusal, or None when it may run.

        ``approved_rules`` are policy rules a person approved this exact
        command past; only those are skipped. The hard floor and the other
        guards (memory vault, private URL, workspace boundary) always apply.
        """
        cmd = command.strip()
        if len(cmd) > MAX_CHECKED_COMMAND_CHARS:
            # Refused before any pattern runs, and not approvable: nothing
            # checked it, so nobody could know what an approval would let run.
            return CommandRefusal(
                "guard",
                f"Error: Command blocked by safety guard (it is {len(cmd)} characters; "
                f"the exec guard checks at most {MAX_CHECKED_COMMAND_CHARS}). Write a "
                "long script or data to a file with write_file, then run that file.",
            )
        lower = cmd.lower()

        floor = tuple(
            p for p in _HARD_FLOOR_PATTERNS
            if _cheap_prefilter_ok(lower, _HARD_FLOOR_PRECHECKS.get(p)) and re.search(p, lower)
        )
        if floor:
            return CommandRefusal(
                "hard_floor",
                f"Error: Command blocked by the exec hard floor (rule: {floor[0]})",
                _HARD_FLOOR_NOTE, floor,
            )

        # allow_patterns take priority over deny_patterns so that users can
        # exempt specific commands (e.g. "rm -rf" inside a build directory)
        # from the hardcoded deny list via configuration.
        explicitly_allowed = bool(self.allow_patterns) and any(
            re.search(p, lower) for p in self.allow_patterns
        )
        if not explicitly_allowed:
            denied = tuple(
                p for p in self.deny_patterns
                if p not in approved_rules
                and _cheap_prefilter_ok(lower, _DENY_PRECHECKS.get(p))
                and re.search(p, lower)
            )
            if denied:
                named = ", ".join(f"rule: {p}" for p in denied)
                return CommandRefusal(
                    "deny",
                    f"Error: Command blocked by deny pattern filter ({named})",
                    _COMMAND_POLICY_NOTE, denied,
                )

            mem_block = self._guard_memory_mutation(lower)
            if mem_block:
                return CommandRefusal("guard", mem_block)

            if self.allow_patterns and _ALLOWLIST_RULE not in approved_rules:
                return CommandRefusal(
                    "allowlist",
                    "Error: Command blocked by allowlist filter (not in allowlist)",
                    _COMMAND_POLICY_NOTE, (_ALLOWLIST_RULE,),
                )

        from durin.security.network import contains_internal_url
        if contains_internal_url(cmd):
            # The runner turns this marker into a non-retryable security hint.
            return CommandRefusal(
                "guard",
                "Error: Command blocked by safety guard (internal/private URL detected)",
            )

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return CommandRefusal(
                    "guard",
                    "Error: Command blocked by safety guard (path traversal detected)",
                    _WORKSPACE_BOUNDARY_NOTE,
                )

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    # Match against the un-resolved path first.  On Linux,
                    # /dev/stderr is a symlink to /proc/self/fd/2 and
                    # ``Path.resolve()`` would mask the device-file intent.
                    if self._is_benign_device_path(expanded):
                        continue
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                if self._is_benign_device_path(str(p)):
                    continue

                media_path = get_media_dir().resolve()
                if (p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                    and media_path not in p.parents
                    and p != media_path
                ):
                    return CommandRefusal(
                        "guard",
                        "Error: Command blocked by safety guard (path outside working dir)",
                        _WORKSPACE_BOUNDARY_NOTE,
                    )

        return None

    @classmethod
    def _guard_memory_mutation(cls, lowered_cmd: str) -> str | None:
        """Block shell mutations of the ``memory/`` vault.

        Returns an actionable error (pointing at the memory tools) when the
        command would rm/mv/cp/truncate/tee/sed -i/dd/redirect into a path
        under ``memory/``; ``None`` otherwise. Reads are never matched.

        Every pattern below ends in the literal ``memory/`` (``_MEMREF``), so
        it is a necessary condition for any of them to match — checked once,
        cheaply, before the loop, instead of each pattern separately failing
        only after an expensive scan on a command that repeats an anchor
        word ("rm "/"cp "/... thousands of times) with no ``memory/`` in it
        at all.
        """
        if "memory/" not in lowered_cmd:
            return None
        for pattern in cls._MEMORY_MUTATION_PATTERNS:
            if re.search(pattern, lowered_cmd):
                return (
                    "Error: refusing to mutate the memory/ vault from the "
                    "shell — use the memory_upsert_entity / memory_forget tools. A "
                    "raw rm/mv/redirect leaves the search index pointing at "
                    "a missing file."
                )
        return None

    @classmethod
    def _is_benign_device_path(cls, path: str) -> bool:
        """Return True for kernel device files that should never be workspace-blocked."""
        if path in cls._BENIGN_DEVICE_PATHS:
            return True
        return path.startswith("/dev/fd/")

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`, and UNC paths like `\\server\share`
        # NOTE: `*` is required so `C:\` (nothing after the slash) is still extracted.
        win_paths = re.findall(
            r"(?:[A-Za-z]:[^\s\"'|><;]*|\\\\[^\s\"'|><;]+(?:\\[^\s\"'|><;]+)*)",
            command
        )
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
