"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass

from durin import __version__
from durin.bus.events import OutboundMessage
from durin.command.router import CommandContext, CommandRouter
from durin.utils.helpers import build_status_content
from durin.utils.restart import set_restart_notice_to_env


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Stop the current task and start a fresh conversation.",
        "square-pen",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
    ),
    BuiltinCommandSpec(
        "/restart",
        "Restart durin",
        "Restart the bot process in place.",
        "rotate-cw",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
    ),
    BuiltinCommandSpec(
        "/dream",
        "Run Dream",
        "Manually trigger memory consolidation.",
        "sparkles",
    ),
    BuiltinCommandSpec(
        "/dream-log",
        "Show Dream log",
        "Show what the last Dream consolidation changed.",
        "book-open",
    ),
    BuiltinCommandSpec(
        "/dream-restore",
        "Restore memory",
        "Revert memory to a previous Dream snapshot.",
        "undo-2",
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
    BuiltinCommandSpec(
        "/pairing",
        "Manage pairing",
        "List, approve, deny or revoke pairing requests.",
        "shield",
        "[list|approve <code>|deny <code>|revoke <user_id>]",
    ),
    BuiltinCommandSpec(
        "/plan",
        "Enter plan mode",
        "Switch the agent into read-only planning mode. The agent will "
        "investigate and propose a plan but not modify the workspace. "
        "Use /build to resume execution.",
        "lightbulb",
    ),
    BuiltinCommandSpec(
        "/build",
        "Exit plan mode",
        "Restore the previous mode (typically build) so the agent can "
        "execute the plan.",
        "play",
    ),
    BuiltinCommandSpec(
        "/mode",
        "Show or set agent mode",
        "Without arguments, shows the active mode. With one of "
        "build/plan/explore, switches to that mode.",
        "settings-2",
        "[build|plan|explore]",
    ),
    BuiltinCommandSpec(
        "/sessions",
        "List sessions",
        "List saved sessions in this workspace, sorted by most recent. "
        "Optional substring filter.",
        "list",
        "[filter]",
    ),
    BuiltinCommandSpec(
        "/resume",
        "Switch to a session",
        "Switch the active chat to a different saved session. Substring match.",
        "log-in",
        "<key>",
    ),
    BuiltinCommandSpec(
        "/compact",
        "Manual compaction",
        "Force the consolidator to summarise older messages in this session. "
        "Optional hint passed to the consolidator.",
        "package",
        "[hint]",
    ),
    BuiltinCommandSpec(
        "/copy",
        "Copy last response",
        "Copy the last assistant message to the system clipboard.",
        "copy",
    ),
    BuiltinCommandSpec(
        "/name",
        "Name this session",
        "Set or show the display name of the current session.",
        "tag",
        "[name]",
    ),
    BuiltinCommandSpec(
        "/hotkeys",
        "Keyboard shortcuts",
        "List the keyboard shortcuts available in interactive mode.",
        "keyboard",
    ),
    BuiltinCommandSpec(
        "/memory",
        "Memory operations",
        "Subcommands: list [class], show <id>, search <query>, drill <uri>.",
        "brain",
        "<list|show|search|drill> [args]",
    ),
    BuiltinCommandSpec(
        "/remember",
        "Remember a fact",
        "Store a fact in episodic memory tagged as user-authored (curator never touches).",
        "bookmark-plus",
        "<fact>",
    ),
    BuiltinCommandSpec(
        "/forget",
        "Delete a memory entry",
        "Remove a memory entry by id (substring match). Confirm prompt.",
        "trash-2",
        "<id>",
    ),
    BuiltinCommandSpec(
        "/sources",
        "Ingested artifacts",
        "List ingested documents, or ingest a new one with `/sources ingest <path>`.",
        "files",
        "[ingest <path>]",
    ),
    BuiltinCommandSpec(
        "/audit",
        "What the agent believes",
        "Show the agent's stable memory entries — what it 'knows' about you.",
        "shield-check",
    ),
    BuiltinCommandSpec(
        "/why",
        "Trace claim provenance",
        "Search memory for a claim and surface the source links it came from.",
        "search-check",
        "<claim>",
    ),
)


def builtin_command_palette() -> list[dict[str, str]]:
    """Return structured command metadata for UI command palettes."""
    return [spec.as_dict() for spec in BUILTIN_COMMAND_SPECS]


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(msg.session_key)
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=dict(msg.metadata or {}),
    )

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "durin"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)

    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    # Never let usage fetch break /status
    with suppress(Exception):
        from durin.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    active_tasks = loop._active_tasks.get(ctx.key, [])
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    composition_payload = None
    with suppress(Exception):
        composition_payload = getattr(loop.context, "last_composition", None)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
            max_completion_tokens=getattr(
                getattr(loop.provider, "generation", None), "max_tokens", 8192
            ),
            composition_payload=composition_payload,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and start a fresh session."""
    loop = ctx.loop
    await loop._cancel_active_tasks(ctx.key)
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.consolidator.archive(snapshot))
    # Doc 25 §2.A.1 β.2 — session-close trigger fires once per /new
    # regardless of whether the snapshot above triggered compaction.
    # Independent config knob (memory.dream.on_session_close).
    # getattr keeps test scaffolds (SimpleNamespace loops) working —
    # production AgentLoop always has the attribute.
    _on_close = getattr(loop, "on_session_close", None)
    if _on_close is not None:
        try:
            _on_close(ctx.key)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "session_close hook raised for %s", ctx.key,
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop) -> list[str]:
    names = set(loop.model_presets)
    names.add("default")
    return ["default", *sorted(name for name in names if name != "default")]


def _active_model_preset_name(loop) -> str:
    return loop.model_preset or "default"


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _model_command_status(loop) -> str:
    names = _model_preset_names(loop)
    active = _active_model_preset_name(loop)
    return "\n".join([
        "## Model",
        f"- Current model: `{loop.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=_model_command_status(loop),
            metadata=metadata,
        )

    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/model [preset]`",
            metadata=metadata,
        )

    name = parts[0]
    try:
        loop.set_model_preset(name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = getattr(getattr(loop.provider, "generation", None), "max_tokens", None)
    lines = [
        f"Switched model preset to `{loop.model_preset}`.",
        f"- Model: `{loop.model}`",
        f"- Context window: {loop.context_window_tokens}",
    ]
    if max_tokens is not None:
        lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"Dream completed in {elapsed:.1f}s."
            else:
                content = "Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest commit's diff
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent commits for the user to pick
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


_GOAL_PROMPT_TEMPLATE = """The user declared a sustained objective for this thread.

Inspect or clarify if needed, then call `long_task` with the refined objective (and optional short ui_summary). Work proceeds as normal assistant turns using your usual tools. When the objective is fully done and verified, call `complete_goal` with a brief recap. If the user later cancels or changes direction, still call `complete_goal` with an honest recap (then `long_task` again only after there is no active goal). Do not use `long_task` / `complete_goal` for trivial one-shot answers.

Goal:
{goal}
"""


async def cmd_goal(ctx: CommandContext) -> OutboundMessage | None:
    """Rewrite /goal into a normal agent turn that nudges long_task use."""
    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if ctx.session is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "A task is already running for this chat. "
                "Use `/stop` first, then send `/goal <long-running task description>` again."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    ctx.msg.metadata = {
        **dict(ctx.msg.metadata or {}),
        "original_command": "/goal",
        "original_content": ctx.raw,
        "goal_started_at": time.time(),
    }
    ctx.msg.content = _GOAL_PROMPT_TEMPLATE.format(goal=goal)
    return None


async def cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    """List, approve, deny or revoke pairing requests."""
    from durin.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command

    reply = handle_pairing_command(ctx.msg.channel, ctx.args)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=reply,
        metadata={PAIRING_COMMAND_META_KEY: True},
    )


# ---------------------------------------------------------------------------
# Sprint B / L3 — Agent mode slash commands
# ---------------------------------------------------------------------------


def _emit_mode_telemetry(event_type: str, data: dict) -> None:
    """Best-effort telemetry emit — never raises if logger unbound or broken."""
    from contextlib import suppress

    from durin.telemetry.logger import current_telemetry

    logger_obj = current_telemetry()
    if logger_obj is None:
        return
    with suppress(Exception):
        logger_obj.log(event_type, data)


def _resolve_session_meta_path(ctx: CommandContext):
    """Locate the .meta.json file for the session in the command context."""
    from contextlib import suppress

    from durin.session.session_meta import meta_path_for

    if ctx.session is None or ctx.loop is None:
        return None
    with suppress(Exception):
        sessions_dir = ctx.loop.sessions.sessions_dir
        return meta_path_for(ctx.session.key, sessions_dir)
    return None


def _supersede_executing_plan(ctx: CommandContext, plan_path: str) -> None:
    """Mark the prior executing plan in session meta as superseded.

    Best-effort: any failure here must not break the slash command.
    """
    from contextlib import suppress
    from pathlib import Path

    from durin.session.session_meta import mark_plan_superseded

    mp = _resolve_session_meta_path(ctx)
    if mp is None:
        return
    with suppress(Exception):
        plan_id = Path(plan_path).stem
        msg_index = len(ctx.session.messages)
        mark_plan_superseded(mp, plan_id, msg_index)


def _approve_executing_plan(ctx: CommandContext, plan_path: str) -> None:
    """Mark the active plan in session meta as approved/executing."""
    from contextlib import suppress
    from pathlib import Path

    from durin.session.session_meta import mark_plan_approved

    mp = _resolve_session_meta_path(ctx)
    if mp is None:
        return
    with suppress(Exception):
        plan_id = Path(plan_path).stem
        msg_index = len(ctx.session.messages)
        mark_plan_approved(mp, plan_id, msg_index)


async def cmd_plan(ctx: CommandContext) -> OutboundMessage:
    """Enter plan mode for the current session.

    Supports both forms:
    - ``/plan`` (exact) — just activate the mode; user types the task in the
      next message.
    - ``/plan <task>`` (prefix) — activate the mode AND forward ``<task>``
      as a regular user message so the agent processes it in plan mode in
      the same turn. Mirrors Claude Code's UX.
    """
    from dataclasses import replace as dataclass_replace

    from durin.agent.agent_mode import (
        PLAN_MODE,
        enter_plan_mode,
        get_active_mode_name,
    )

    if ctx.session is None or ctx.session.metadata is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Cannot enter plan mode: no active session.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    task = (ctx.args or "").strip()
    current = get_active_mode_name(ctx.session)
    if current == PLAN_MODE.name:
        if task:
            content = (
                "🧠 Already in PLAN MODE — passing your new task to the agent. "
                "It will refine the current plan or propose a new one.\n\n"
                "Tip: if you want to discard the in-progress plan and start "
                "fresh, run `/build` first, then `/plan <new task>`."
            )
        else:
            content = "🧠 Already in PLAN MODE."
    else:
        previous = enter_plan_mode(ctx.session)
        # Entering a new plan supersedes any prior executing plan — clear
        # the runtime marker so prompts don't keep re-injecting stale plan
        # content. Also close the executing-plan event in the session meta.
        superseded = ctx.session.metadata.pop("executing_plan_path", None)
        _emit_mode_telemetry("agent_mode.switch", {
            "from": previous,
            "to": PLAN_MODE.name,
            "trigger": "slash_command",
        })
        if superseded:
            _supersede_executing_plan(ctx, superseded)
        content = (
            f"🧠 **PLAN MODE** activated (was: {previous}).\n\n"
            "The agent will investigate and propose a plan. Modifications "
            "are disabled until you run `/build` to resume execution."
        )

    # If the user typed `/plan <task>`, re-publish the task as a regular
    # inbound message so the agent processes it in plan mode in the same
    # logical turn. The CommandRouter strips the prefix into ``ctx.args``.
    expects_followup = False
    if task and ctx.loop is not None and hasattr(ctx.loop, "bus"):
        try:
            follow_up = dataclass_replace(ctx.msg, content=task)
            await ctx.loop.bus.publish_inbound(follow_up)
            expects_followup = True
        except Exception:
            # If forwarding fails, fall back to just announcing the mode
            # switch — the user can re-type the task manually.
            pass

    out_metadata = dict(ctx.msg.metadata or {})
    if expects_followup:
        out_metadata["_block_input_until_response"] = True
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=out_metadata,
    )


async def cmd_build(ctx: CommandContext) -> OutboundMessage:
    """Exit plan mode, restoring the previous mode (typically build)."""
    from durin.agent.agent_mode import (
        PLAN_MODE,
        exit_plan_mode,
        get_active_mode_name,
        set_mode,
    )

    if ctx.session is None or ctx.session.metadata is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Cannot switch mode: no active session.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    current = get_active_mode_name(ctx.session)
    expects_followup = False
    if current == PLAN_MODE.name:
        # Capture the active plan path BEFORE exiting so /build can
        # surface it to the model on resume. The plan file lives in
        # <workspace>/.durin/plans/ and was written by exit_plan_mode tool.
        from durin.agent.tools.plan_mode import _ACTIVE_PLAN_PATH_KEY

        plan_path = ctx.session.metadata.get(_ACTIVE_PLAN_PATH_KEY)
        restored = exit_plan_mode(ctx.session)
        _emit_mode_telemetry("agent_mode.switch", {
            "from": current,
            "to": restored,
            "trigger": "slash_command",
        })
        if plan_path:
            content = (
                f"▶️ Exited plan mode → **{restored}**.\n\n"
                f"Approved plan: `{plan_path}`. Proceeding with execution now."
            )
            # Stash the approved path for two purposes:
            # 1. approved_plan_path → one-shot system reminder on the next
            #    turn (consumed by ContextBuilder.build_messages)
            # 2. executing_plan_path → persistent; lets us splice the plan
            #    content into the consolidation summary so it survives
            #    cursor advance
            ctx.session.metadata["approved_plan_path"] = plan_path
            ctx.session.metadata["executing_plan_path"] = plan_path
            ctx.session.metadata.pop(_ACTIVE_PLAN_PATH_KEY, None)
            # Update session meta: mark plan as approved (executing).
            _approve_executing_plan(ctx, plan_path)
            # Wake the agent: without an inbound message the runner stays
            # idle until the user types something. Publish a synthetic
            # "proceed" message so the model picks up the approved plan
            # path from runtime context and executes immediately. This is
            # the UX equivalent of /plan <task> forwarding its args.
            from dataclasses import replace as dataclass_replace

            if ctx.loop is not None and hasattr(ctx.loop, "bus"):
                try:
                    trigger = dataclass_replace(
                        ctx.msg,
                        content="Proceed with the approved plan.",
                    )
                    await ctx.loop.bus.publish_inbound(trigger)
                    expects_followup = True
                except Exception:
                    pass
        else:
            content = (
                f"▶️ Exited plan mode → **{restored}**. "
                "No plan file was recorded — the agent will proceed based "
                "on the conversation context."
            )
    elif current == "build":
        content = "▶️ Already in **build** mode."
    else:
        # Coming from a non-plan mode (e.g. explore for a subagent) —
        # explicit set to build, no restore semantics.
        set_mode(ctx.session, "build")
        _emit_mode_telemetry("agent_mode.switch", {
            "from": current,
            "to": "build",
            "trigger": "slash_command",
        })
        content = f"▶️ Switched **{current}** → **build**."
    out_metadata = dict(ctx.msg.metadata or {})
    if expects_followup:
        # Signals the interactive CLI to keep the spinner running and not
        # return to the input prompt until the agent's response to the
        # synthetic trigger published above arrives. Without this, the user
        # sees the input prompt with no indication that work is in flight.
        out_metadata["_block_input_until_response"] = True
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=out_metadata,
    )


async def cmd_mode(ctx: CommandContext) -> OutboundMessage:
    """Show or set the active agent mode."""
    from durin.agent.agent_mode import (
        get_active_mode,
        list_modes,
        set_mode,
    )

    if ctx.session is None or ctx.session.metadata is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Cannot access mode: no active session.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    target = (ctx.args or "").strip().lower()
    if not target:
        active = get_active_mode(ctx.session)
        lines = [f"Active mode: **{active.name}**", "", "Available modes:"]
        for mode in list_modes():
            marker = "→" if mode.name == active.name else " "
            lines.append(f"{marker} **{mode.name}** — {mode.description}")
        content = "\n".join(lines)
    else:
        known = {m.name for m in list_modes()}
        if target not in known:
            content = (
                f"Unknown mode: `{target}`. Available: {', '.join(sorted(known))}"
            )
        else:
            previous = get_active_mode(ctx.session).name
            if previous == target:
                content = f"Already in **{target}** mode."
            else:
                set_mode(ctx.session, target)
                _emit_mode_telemetry("agent_mode.switch", {
                    "from": previous,
                    "to": target,
                    "trigger": "slash_command",
                })
                content = f"Mode: **{previous}** → **{target}**."
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )


def _read_session_metadata(path) -> dict | None:
    """Read line 0 of a session.jsonl and return the metadata dict (or None)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    first = text.split("\n", 1)[0] if text else ""
    if not first:
        return None
    try:
        meta = json.loads(first)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict) or meta.get("_type") != "metadata":
        return None
    # Cheap msg count: total lines minus the metadata line.
    meta["_msg_count"] = max(0, text.count("\n") - 1)
    return meta


async def cmd_sessions(ctx: CommandContext) -> OutboundMessage:
    """List saved sessions, sorted by updated_at desc."""
    loop = ctx.loop
    needle = ctx.args.strip().lower()
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    entries: list[dict] = []
    sessions_dir = loop.sessions.sessions_dir
    for path in sessions_dir.glob("*.jsonl"):
        meta = _read_session_metadata(path)
        if not meta:
            continue
        key = meta.get("key", path.stem)
        if needle and needle not in key.lower():
            identity = (meta.get("metadata") or {}).get("display_name") or ""
            if needle not in identity.lower():
                continue
        entries.append({
            "key": key,
            "display_name": (meta.get("metadata") or {}).get("display_name") or "",
            "updated_at": meta.get("updated_at", ""),
            "msg_count": meta["_msg_count"],
        })

    if not entries:
        content = (
            f"No sessions match `{needle}`." if needle
            else "No sessions in this workspace yet."
        )
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=content, metadata=metadata_text,
        )

    entries.sort(key=lambda e: e["updated_at"], reverse=True)
    lines = ["## Sessions", ""]
    for i, e in enumerate(entries, 1):
        marker = " ← current" if e["key"] == ctx.key else ""
        name_part = f" — {e['display_name']}" if e["display_name"] else ""
        when = e["updated_at"][:16].replace("T", " ") if e["updated_at"] else ""
        lines.append(
            f"{i}. `{e['key']}`{name_part} · {e['msg_count']} msgs · {when}{marker}"
        )
    lines += ["", "Use `/resume <key>` to switch (substring match)."]
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="\n".join(lines), metadata=metadata_text,
    )


async def cmd_resume(ctx: CommandContext) -> OutboundMessage:
    """Switch the active chat to a different saved session."""
    loop = ctx.loop
    needle = ctx.args.strip()
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not needle:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Usage: `/resume <key>`. Run `/sessions` to list.",
            metadata=metadata_text,
        )

    needle_low = needle.lower()
    candidates: list[str] = []
    sessions_dir = loop.sessions.sessions_dir
    for path in sessions_dir.glob("*.jsonl"):
        meta = _read_session_metadata(path)
        if not meta:
            continue
        key = meta.get("key", "")
        if needle_low in key.lower():
            candidates.append(key)

    if not candidates:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"No session matches `{needle}`. Run `/sessions` to list.",
            metadata=metadata_text,
        )
    if len(candidates) > 1:
        listed = ", ".join(f"`{c}`" for c in candidates)
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"`{needle}` is ambiguous: {listed}. Be more specific.",
            metadata=metadata_text,
        )

    target_key = candidates[0]
    if target_key == ctx.key:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Already in session `{target_key}`.",
            metadata=metadata_text,
        )

    # The CLI loop watches metadata['_switch_chat_id'] in outbound messages
    # and updates its `cli_chat_id` for subsequent inbound publishes.
    target_chat_id = target_key.split(":", 1)[-1] if ":" in target_key else target_key
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=f"Switched to session `{target_key}`.",
        metadata={**metadata_text, "_switch_chat_id": target_chat_id},
    )


async def cmd_compact(ctx: CommandContext) -> OutboundMessage:
    """Manually run the consolidator over unconsolidated messages."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    chunk = session.messages[session.last_consolidated:]
    if not chunk:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Nothing to compact — session is already consolidated.",
            metadata=metadata_text,
        )

    try:
        summary, tags = await loop.consolidator.archive(chunk)
    except Exception as exc:  # noqa: BLE001
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Compaction failed: {exc}",
            metadata=metadata_text,
        )

    session.last_consolidated = len(session.messages)
    if summary:
        loop.consolidator._merge_session_tags(session, tags)
        loop.consolidator._persist_last_summary(session, summary)
    loop.sessions.save(session)

    if summary:
        content = (
            f"Compacted {len(chunk)} messages into summary "
            f"(total: {len(session.messages)})."
        )
    else:
        content = (
            f"Consolidation LLM degraded — raw-archived {len(chunk)} messages "
            "as a breadcrumb. Cursor still advanced."
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata=metadata_text,
    )


def _copy_to_clipboard(text: str) -> str:
    """Copy text via the shared clipboard helper. Returns the tool name used."""
    from durin.utils.clipboard import copy_text

    return copy_text(text)


def _last_assistant_content(session) -> str | None:
    for msg in reversed(session.messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not content:
            continue
        if isinstance(content, list):
            content = "\n".join(
                str(b.get("text", "")) if isinstance(b, dict) else str(b)
                for b in content
            ).strip()
        text = str(content).strip()
        if text:
            return text
    return None


async def cmd_copy(ctx: CommandContext) -> OutboundMessage:
    """Copy the last assistant message to the system clipboard."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    text = _last_assistant_content(session)
    if not text:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No assistant message to copy in this session yet.",
            metadata=metadata_text,
        )

    try:
        tool = _copy_to_clipboard(text)
    except RuntimeError as exc:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"{exc}. (Last response was {len(text)} chars.)",
            metadata=metadata_text,
        )

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=f"Copied {len(text)} chars to clipboard via `{tool}`.",
        metadata=metadata_text,
    )


_DISPLAY_NAME_KEY = "display_name"
_DISPLAY_NAME_MAX = 80


async def cmd_name(ctx: CommandContext) -> OutboundMessage:
    """Set or show the display name of the current session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    name = ctx.args.strip()
    if not name:
        current = (session.metadata or {}).get(_DISPLAY_NAME_KEY) or ""
        if current:
            content = f"Current display name: `{current}`. Use `/name <new>` to change."
        else:
            content = "No display name set. Use `/name <name>` to set one."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=content, metadata=metadata_text,
        )

    if len(name) > _DISPLAY_NAME_MAX:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Name too long (max {_DISPLAY_NAME_MAX} chars).",
            metadata=metadata_text,
        )

    session.metadata[_DISPLAY_NAME_KEY] = name
    loop.sessions.save(session)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=f"Display name set to `{name}`.",
        metadata=metadata_text,
    )


async def cmd_hotkeys(ctx: CommandContext) -> OutboundMessage:
    """List keyboard shortcuts available in interactive mode."""
    text = (
        "## Keyboard shortcuts (interactive CLI)\n"
        "\n"
        "| Key | Action |\n"
        "|---|---|\n"
        "| `Enter` | Send message |\n"
        "| `Ctrl+C` | Cancel input |\n"
        "| `Ctrl+D` / `exit` / `:q` / `/quit` | Quit |\n"
        "\n"
        "Slash commands: type `/help` for the full list."
    )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=text,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _resolve_workspace(loop) -> "Path":
    """Resolve the workspace Path for memory operations."""
    from pathlib import Path

    workspace = getattr(loop, "workspace", None)
    if workspace is None:
        return Path.cwd()
    return Path(workspace) if not isinstance(workspace, Path) else workspace


def _find_memory_entry(workspace, id_needle: str):
    """Walk memory/<class>/*.md; return list of (class_name, path) matching the id."""
    from pathlib import Path

    from durin.memory.paths import MEMORY_CLASSES

    needle = id_needle.lower().strip()
    if not needle:
        return []
    memory_root = workspace / "memory"
    if not memory_root.is_dir():
        return []
    matches = []
    for class_name in MEMORY_CLASSES:
        class_dir = memory_root / class_name
        if not class_dir.is_dir():
            continue
        for path in class_dir.glob("*.md"):
            if needle in path.stem.lower():
                matches.append((class_name, path))
    return matches


async def cmd_memory(ctx: CommandContext) -> OutboundMessage:
    """Memory operations dispatcher: list, show, search, drill."""
    from durin.memory.paths import MEMORY_CLASSES
    from durin.memory.search import search_memory
    from durin.memory.drill import DrillError, drill
    from durin.memory.storage import load_entry, FrontmatterError

    loop = ctx.loop
    workspace = _resolve_workspace(loop)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    parts = (ctx.args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if not sub:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=(
                "Usage: `/memory <list|show|search|drill|reindex> [args]`.\n"
                "- `/memory list [class]` — list entries.\n"
                "- `/memory show <id>` — render one entry.\n"
                "- `/memory search <query>` — search dreamed + undreamed.\n"
                "- `/memory drill <uri>` — fetch a specific section.\n"
                "- `/memory reindex` — rebuild the vector index from disk."
            ),
            metadata=metadata_text,
        )

    if sub == "list":
        class_filter = rest.strip().lower()
        memory_root = workspace / "memory"
        entries: list[tuple[str, str, str]] = []  # (class, id, headline)
        if memory_root.is_dir():
            for class_name in MEMORY_CLASSES:
                if class_filter and class_name != class_filter:
                    continue
                class_dir = memory_root / class_name
                if not class_dir.is_dir():
                    continue
                for path in sorted(class_dir.glob("*.md")):
                    try:
                        entry = load_entry(path)
                    except (FrontmatterError, Exception):
                        continue
                    entries.append((class_name, path.stem, entry.headline))
        if not entries:
            scope = f"`{class_filter}`" if class_filter else "any class"
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"No memory entries found in {scope}.",
                metadata=metadata_text,
            )
        lines = ["## Memory entries", ""]
        for class_name, entry_id, headline in entries:
            lines.append(f"- `{class_name}/{entry_id}` — {headline}")
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="\n".join(lines), metadata=metadata_text,
        )

    if sub == "show":
        if not rest:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: `/memory show <id>`.",
                metadata=metadata_text,
            )
        matches = _find_memory_entry(workspace, rest)
        if not matches:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"No memory entry matches `{rest}`.",
                metadata=metadata_text,
            )
        if len(matches) > 1:
            listed = ", ".join(f"`{c}/{p.stem}`" for c, p in matches)
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"`{rest}` is ambiguous: {listed}. Be more specific.",
                metadata=metadata_text,
            )
        class_name, path = matches[0]
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            content = f"Cannot read entry: {exc}"
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=content,
            metadata=dict(ctx.msg.metadata or {}),
        )

    if sub == "search":
        if not rest:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: `/memory search <query>`.",
                metadata=metadata_text,
            )
        results = search_memory(workspace, rest, scope="all", level="warm")
        if not results:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"No memory hits for `{rest}`.",
                metadata=metadata_text,
            )
        lines = [f"## Memory search · {len(results)} hits for `{rest}`", ""]
        for r in results[:20]:
            lines.append(f"- [{r.source}] `{r.uri}` — {r.headline}")
            if r.snippet:
                lines.append(f"  > {r.snippet}")
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="\n".join(lines), metadata=metadata_text,
        )

    if sub == "drill":
        if not rest:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: `/memory drill <uri>` (e.g. `sessions/abc.md#turn-42`).",
                metadata=metadata_text,
            )
        try:
            text = drill(workspace, rest)
        except DrillError as exc:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"drill error: {exc}",
                metadata=metadata_text,
            )
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=text,
            metadata=dict(ctx.msg.metadata or {}),
        )

    if sub == "reindex":
        return await _memory_reindex(ctx, workspace, metadata_text)

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=(
            f"Unknown `/memory` subcommand `{sub}`. "
            f"Try `list`, `show`, `search`, `drill`, `reindex`."
        ),
        metadata=metadata_text,
    )


async def _memory_reindex(
    ctx: CommandContext, workspace: "Path", metadata_text: dict
) -> OutboundMessage:
    """Rebuild the vector index from the markdown source of truth.

    Covers three real-world cases:

    1. The user changed ``memory.embedding.model`` and the on-disk
       table has a stale dim. ``rebuild_from_workspace`` drops the
       table and creates a new one with the current provider's dim.
    2. The user edited memory entries by hand — those edits aren't
       seen by the index until a rebuild.
    3. The on-disk index got corrupted somehow and the user wants a
       clean slate without losing the markdown.
    """
    cfg = ctx.loop.config
    if not getattr(cfg.memory, "enabled", False):
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=(
                "Vector memory is off. Enable it first with "
                "`durin onboard memory` or by setting `memory.enabled=true`."
            ),
            metadata=metadata_text,
        )
    from durin.memory.vector_index import VectorIndex, vector_index_available
    if not vector_index_available():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=(
                "Vector backend (lancedb) is not installed. "
                "Install with `pip install durin-agent[memory]` or "
                "`durin doctor --install-missing -y`."
            ),
            metadata=metadata_text,
        )
    from durin.memory.embedding import FastembedProvider
    try:
        provider = FastembedProvider(model=cfg.memory.embedding.model)
    except Exception as exc:  # noqa: BLE001
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Could not load embedding model: {exc}",
            metadata=metadata_text,
        )
    index = VectorIndex(workspace, provider)
    try:
        count = index.rebuild_from_workspace()
    except Exception as exc:  # noqa: BLE001
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Rebuild failed: {exc}",
            metadata=metadata_text,
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=(
            f"Vector index rebuilt — {count} entries indexed with "
            f"{provider.model_name} ({provider.dimensions}-dim)."
        ),
        metadata=metadata_text,
    )


async def cmd_remember(ctx: CommandContext) -> OutboundMessage:
    """Store a fact in episodic memory as ``user_authored``.

    The curator + dream never touch user_authored entries. We wrap
    the write in an explicit ``author_scope("user_authored")`` —
    per ``durin/memory/provenance.py`` there is no implicit default;
    every write declares its author.
    """
    from durin.memory.provenance import author_scope
    from durin.memory.store import StoreError, store_memory

    fact = (ctx.args or "").strip()
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    if not fact:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Usage: `/remember <fact>`.", metadata=metadata_text,
        )
    workspace = _resolve_workspace(ctx.loop)
    try:
        with author_scope("user_authored"):
            result = store_memory(
                workspace, content=fact, class_name="episodic",
            )
    except (StoreError, OSError) as exc:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"`/remember` failed: {exc}", metadata=metadata_text,
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=(
            f"Remembered as `{result['class']}/{result['id']}` "
            f"(author: {result['author']}).\n> {result['headline']}"
        ),
        metadata=metadata_text,
    )


async def cmd_forget(ctx: CommandContext) -> OutboundMessage:
    """Delete a memory entry by id substring; also drop its vector index row."""
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    needle = (ctx.args or "").strip()
    if not needle:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Usage: `/forget <id>`. Run `/memory list` for available ids.",
            metadata=metadata_text,
        )
    workspace = _resolve_workspace(ctx.loop)
    matches = _find_memory_entry(workspace, needle)
    if not matches:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"No memory entry matches `{needle}`.",
            metadata=metadata_text,
        )
    if len(matches) > 1:
        listed = ", ".join(f"`{c}/{p.stem}`" for c, p in matches)
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"`{needle}` is ambiguous: {listed}. Be more specific.",
            metadata=metadata_text,
        )
    class_name, path = matches[0]
    entry_id = path.stem
    try:
        path.unlink()
    except OSError as exc:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"Cannot delete `{class_name}/{entry_id}`: {exc}",
            metadata=metadata_text,
        )
    # Best-effort vector index cleanup.
    try:
        from durin.memory.vector_index import VectorIndex, vector_index_available

        if vector_index_available():
            embedding_model = None
            try:
                embedding_model = ctx.loop.config.memory.embedding.model  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
            if embedding_model:
                from durin.memory.embedding import FastembedProvider

                provider = FastembedProvider(model=embedding_model)
                index = VectorIndex(workspace, provider)
                try:
                    import lancedb  # noqa: F401

                    db = index._connect()  # type: ignore[attr-defined]
                    if "memory_entries" in db.list_tables().tables:
                        table = db.open_table("memory_entries")
                        table.delete(f"id = '{entry_id}'")
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=f"Forgot `{class_name}/{entry_id}`.",
        metadata=metadata_text,
    )


async def cmd_sources(ctx: CommandContext) -> OutboundMessage:
    """List ingested artifacts, or ingest a new one with `/sources ingest <path>`."""
    import json
    from pathlib import Path

    from durin.memory.ingestion import IngestError, ingest_artifact

    workspace = _resolve_workspace(ctx.loop)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    args = (ctx.args or "").strip()
    parts = args.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "ingest":
        if not rest:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: `/sources ingest <path>`.",
                metadata=metadata_text,
            )
        source = Path(rest).expanduser()
        if not source.is_absolute():
            source = (workspace / source).resolve()
        try:
            result = ingest_artifact(workspace, source)
        except IngestError as exc:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=f"Ingest failed: {exc}", metadata=metadata_text,
            )
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=(
                f"Ingested as `{result['id']}` ({result['size_bytes']} bytes).\n"
                f"Source: `{result['source']}`"
            ),
            metadata=metadata_text,
        )

    ingested_dir = workspace / "ingested"
    if not ingested_dir.is_dir():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=(
                "No ingested artifacts yet. "
                "Use `/sources ingest <path>` to add one."
            ),
            metadata=metadata_text,
        )

    entries: list[tuple[str, str, str]] = []
    for entry_dir in sorted(ingested_dir.iterdir()):
        if not entry_dir.is_dir():
            continue
        meta_path = entry_dir / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        derived = meta.get("derived") or {}
        source_path = derived.get("source_path") or "?"
        size = derived.get("size_bytes") or 0
        entries.append((entry_dir.name, source_path, str(size)))

    if not entries:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No ingested artifacts yet.",
            metadata=metadata_text,
        )

    lines = [f"## Ingested sources · {len(entries)}", ""]
    for entry_id, source_path, size in entries:
        lines.append(f"- `{entry_id}` ({size} bytes) ← `{source_path}`")
    lines.append("")
    lines.append("Use `/sources ingest <path>` to add another.")
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="\n".join(lines), metadata=metadata_text,
    )


async def cmd_audit(ctx: CommandContext) -> OutboundMessage:
    """Show the agent's stable memory — what it 'knows' about the user."""
    from durin.memory.storage import FrontmatterError, load_entry

    workspace = _resolve_workspace(ctx.loop)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    stable_dir = workspace / "memory" / "stable"
    if not stable_dir.is_dir():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No stable memory yet — the agent hasn't accumulated identity-level entries.",
            metadata=metadata_text,
        )
    entries = []
    for path in sorted(stable_dir.glob("*.md")):
        try:
            entry = load_entry(path)
        except (FrontmatterError, Exception):
            continue
        entries.append({
            "id": path.stem,
            "headline": entry.headline,
            "valid_from": entry.valid_from.isoformat() if entry.valid_from else "?",
            "author": entry.author,
            "source_refs": len(entry.source_refs),
        })
    if not entries:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No stable memory entries.",
            metadata=metadata_text,
        )
    lines = ["## Agent's stable memory (audit)", ""]
    for e in entries:
        lines.append(
            f"- `{e['id']}` · {e['valid_from']} · {e['author']} · "
            f"{e['source_refs']} source ref(s)\n"
            f"  > {e['headline']}"
        )
    lines.append("")
    lines.append("Use `/forget <id>` to remove an entry the agent shouldn't keep.")
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="\n".join(lines), metadata=metadata_text,
    )


async def cmd_why(ctx: CommandContext) -> OutboundMessage:
    """Search memory for a claim; surface source_refs as navigable links."""
    from durin.memory.search import search_memory
    from durin.memory.storage import FrontmatterError, load_entry

    workspace = _resolve_workspace(ctx.loop)
    metadata_text = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    claim = (ctx.args or "").strip()
    if not claim:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Usage: `/why <claim>` — searches memory and shows the source links.",
            metadata=metadata_text,
        )
    results = search_memory(workspace, claim, scope="dreamed", level="warm")
    if not results:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=f"No memory supports `{claim}` yet.",
            metadata=metadata_text,
        )
    lines = [f"## Provenance for `{claim}`", ""]
    for r in results[:10]:
        lines.append(f"### {r.headline}")
        lines.append(f"*from* `{r.uri}`")
        if r.summary:
            lines.append("")
            lines.append(f"> {r.summary}")
        # If the result is a memory entry, surface its source_refs.
        if r.source == "memory":
            try:
                rel = r.uri[len("memory/"):] if r.uri.startswith("memory/") else r.uri
                path = workspace / "memory" / f"{rel}.md"
                if path.is_file():
                    entry = load_entry(path)
                    if entry.source_refs:
                        lines.append("")
                        lines.append("**Sources:**")
                        for ref in entry.source_refs:
                            lines.append(f"- {ref}")
            except (FrontmatterError, Exception):
                pass
        lines.append("")
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="\n".join(lines), metadata=metadata_text,
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["⚒️ durin commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/help", cmd_help)
    router.exact("/pairing", cmd_pairing)
    router.prefix("/pairing ", cmd_pairing)
    router.exact("/plan", cmd_plan)
    router.prefix("/plan ", cmd_plan)
    router.exact("/build", cmd_build)
    router.exact("/mode", cmd_mode)
    router.prefix("/mode ", cmd_mode)
    router.exact("/sessions", cmd_sessions)
    router.prefix("/sessions ", cmd_sessions)
    router.exact("/resume", cmd_resume)
    router.prefix("/resume ", cmd_resume)
    router.exact("/compact", cmd_compact)
    router.prefix("/compact ", cmd_compact)
    router.exact("/copy", cmd_copy)
    router.exact("/name", cmd_name)
    router.prefix("/name ", cmd_name)
    router.exact("/hotkeys", cmd_hotkeys)
    router.exact("/memory", cmd_memory)
    router.prefix("/memory ", cmd_memory)
    router.exact("/remember", cmd_remember)
    router.prefix("/remember ", cmd_remember)
    router.exact("/forget", cmd_forget)
    router.prefix("/forget ", cmd_forget)
    router.exact("/sources", cmd_sources)
    router.prefix("/sources ", cmd_sources)
    router.exact("/audit", cmd_audit)
    router.exact("/why", cmd_why)
    router.prefix("/why ", cmd_why)
