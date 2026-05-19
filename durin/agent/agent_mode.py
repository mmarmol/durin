"""Agent modes — Sprint B / L3 (docs/07_external_agents_review.md).

Permission-as-data agent modes. The loop doesn't have any conditional logic
about "what to do in plan mode vs build mode"; modes are pure data, applied by
filtering the available tool set at the start of each turn.

This avoids the V7/V8 PlanHook pitfall (refuted in 02_bitacora.md) — that
design forced behavior (verify-before-complete) via code. Here we only
restrict what tools the model can call. The model retains full agency within
the filtered surface.

Modes are stored on ``session.metadata["agent_mode"]``. The previous mode
gets stashed in ``session.metadata["pre_plan_mode"]`` when entering plan
mode, so ``exit_plan_mode`` restores the prior state (the ``prePlanMode``
pattern from OpenClaude).

Inspirations:
- OpenCode: declarative rulesets with wildcard matching (we simplified to
  explicit ``frozenset`` because Durin has ~15 tools, not 100s).
- OpenClaude: ``prePlanMode`` restore pattern.
- Hermes: ``set_thread_tool_whitelist`` per-thread filtering — same idea,
  ours is per-session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# AgentMode
# ---------------------------------------------------------------------------

DEFAULT_MODE = "build"
SESSION_MODE_KEY = "agent_mode"
SESSION_PRE_PLAN_KEY = "pre_plan_mode"


@dataclass(frozen=True, slots=True)
class AgentMode:
    """Declarative mode definition. Two filter knobs + optional prompt nudge.

    ``allowed`` of ``None`` means "no positive restriction" — every tool is
    allowed unless it appears in ``denied``. ``allowed`` of a frozenset means
    "ONLY these tools" — a tool not in the set is rejected, regardless of
    ``denied``. ``denied`` always wins over ``allowed`` (an explicit deny
    survives an allowlist).

    ``prompt_suffix`` is appended to the system prompt when this mode is
    active, so the model knows what posture it should adopt. Keep it short —
    the goal is to set expectations, not to embed rules.
    """

    name: str
    description: str
    allowed: frozenset[str] | None = None
    denied: frozenset[str] = frozenset()
    prompt_suffix: str = ""

    def is_tool_allowed(self, tool_name: str) -> bool:
        if tool_name in self.denied:
            return False
        if self.allowed is None:
            return True
        return tool_name in self.allowed


# ---------------------------------------------------------------------------
# Default mode definitions
# ---------------------------------------------------------------------------

BUILD_MODE = AgentMode(
    name="build",
    description="Default mode. Full access to all tools.",
)

# In plan mode the agent is read-only EXCEPT for `exit_plan_mode`, which
# the model uses to surface its plan and yield to the user. The model can
# also still call lookup tools (web fetch/search) so it can complete
# research that informs the plan.
PLAN_MODE_ALLOWED = frozenset({
    "read_file",
    "list_dir",
    "grep",
    "repo_overview",
    "web_fetch",
    "web_search",
    "exit_plan_mode",
    # Memory / state tools are read-only — safe in plan mode.
    "my",
    "long_task",
    "complete_goal",
    # Todo list lives on session metadata, not the workspace — safe to
    # maintain while planning. Useful for breaking a long investigation
    # into a checklist the user can see.
    "todo_write",
    # Asking the user for clarification mid-investigation is read-safe;
    # the tool just records the question and yields.
    "ask_user_question",
    # Searching the current session's prior messages is read-only.
    "session_search",
    # Subagent lifecycle observation (and stop) is read-only with respect
    # to the workspace — only touches the manager's in-memory state.
    "subagent_list",
    "subagent_status",
    "subagent_stop",
    "subagent_output",
    # Subagent spawning is allowed; subagents inherit `explore` mode (see
    # spawn integration) which is also read-only.
    "spawn",
})

PLAN_MODE = AgentMode(
    name="plan",
    description=(
        "Read-only planning mode. The agent investigates and proposes a "
        "plan but does not modify the workspace. Use /build (or the "
        "`exit_plan_mode` tool followed by user approval) to resume execution."
    ),
    allowed=PLAN_MODE_ALLOWED,
    prompt_suffix=(
        "\n\n## PLAN MODE — READ-ONLY (strict)\n"
        "You are currently in PLAN MODE. Your ONLY goal is to investigate "
        "and produce a plan, then call `exit_plan_mode(plan=...)` to yield "
        "to the user for approval. You CANNOT modify state in this mode.\n\n"
        "Hard constraints (the tool surface enforces them):\n"
        "- You cannot call `edit_file`, `write_file`, `exec`, or any other "
        "state-changing tool. They are not available.\n"
        "- You **cannot delegate modifications via `spawn`**. Subagents you "
        "spawn ALSO run read-only — they cannot edit, write, or execute "
        "shell on your behalf. Trying to work around plan mode by "
        "delegating will simply produce another read-only assistant that "
        "can investigate but not modify.\n\n"
        "Workflow:\n"
        "1. Read code, search, and gather what you need to understand the "
        "task. You may use `spawn` for parallel investigation if helpful.\n"
        "2. When you have a concrete plan, call `exit_plan_mode` with the "
        "plan as a markdown string in the `plan` argument. The plan is "
        "written to disk but **the user does not see it yet**.\n"
        "3. **IMMEDIATELY AFTER** calling `exit_plan_mode`, your next "
        "assistant message MUST present the full plan content to the user "
        "in markdown — title, goal, numbered steps, files involved, and "
        "any open questions. Do not summarize it in one line; show the "
        "actual plan. End that message with an explicit prompt asking the "
        "user to run `/build` once they agree — phrase it in the same "
        "language the user has been writing in. The user cannot approve "
        "a plan they cannot see.\n"
        "4. The user reviews the plan (optionally editing the .md file) "
        "and runs `/build` to approve. Only after `/build` will you be "
        "able to make the changes.\n\n"
        "Do NOT loop forever trying to plan. Do NOT pretend the work is "
        "done before `/build`. Do NOT claim files have been modified in "
        "plan mode — they haven't. Do NOT collapse the plan into a one-line "
        "teaser — the user needs the full content to decide. The honest "
        "path is always: investigate → exit_plan_mode → present full plan "
        "in assistant message → yield to user."
    ),
)

# Mode for exploration sub-agents — read-only, no exit affordance (a
# sub-agent's job is to gather info and report back, not to drive a plan).
EXPLORE_MODE_ALLOWED = frozenset({
    "read_file",
    "list_dir",
    "grep",
    "repo_overview",
    "web_fetch",
    "web_search",
})

EXPLORE_MODE = AgentMode(
    name="explore",
    description="Read-only mode for exploration sub-agents.",
    allowed=EXPLORE_MODE_ALLOWED,
    prompt_suffix=(
        "\n\n## EXPLORE MODE — READ-ONLY (strict)\n"
        "You are an exploration sub-agent in READ-ONLY mode. You CANNOT "
        "edit, write, or execute. Your tool surface is restricted to: "
        "read_file, list_dir, grep, repo_overview, web_fetch, web_search.\n\n"
        "If your task requires modifications (writing files, running "
        "commands, applying edits), you CANNOT complete it. Stop "
        "immediately, do NOT loop trying alternative approaches, and "
        "respond with:\n\n"
        "  > Cannot complete this task: it requires modifications but I am "
        "in read-only mode. The parent agent should exit plan mode first "
        "(via exit_plan_mode → user /build) before delegating modification "
        "work.\n\n"
        "Then stop. Do not retry. Do not try to spawn another sub-agent. "
        "Failing fast with this exact message is the correct behavior — "
        "the parent agent needs this signal to course-correct."
    ),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, AgentMode] = {
    BUILD_MODE.name: BUILD_MODE,
    PLAN_MODE.name: PLAN_MODE,
    EXPLORE_MODE.name: EXPLORE_MODE,
}


def get_mode(name: str | None) -> AgentMode:
    """Return the mode by name, defaulting to BUILD_MODE if unknown/None."""
    if not name:
        return BUILD_MODE
    return _REGISTRY.get(name, BUILD_MODE)


def register_mode(mode: AgentMode) -> None:
    """Register a custom mode (e.g. from a plugin)."""
    _REGISTRY[mode.name] = mode


def list_modes() -> list[AgentMode]:
    """All registered modes, in registration order (built-ins first)."""
    return list(_REGISTRY.values())


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def _session_metadata(session: Any) -> dict[str, Any] | None:
    """Return the session's mutable metadata dict, or None if unavailable."""
    if session is None:
        return None
    meta = getattr(session, "metadata", None)
    if meta is None:
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def get_active_mode(session: Any) -> AgentMode:
    """Return the AgentMode active for *session*, defaulting to BUILD_MODE."""
    meta = _session_metadata(session)
    if meta is None:
        return BUILD_MODE
    return get_mode(meta.get(SESSION_MODE_KEY))


def get_active_mode_name(session: Any) -> str:
    """Return the active mode name, or 'build' as default."""
    return get_active_mode(session).name


def set_mode(session: Any, target: str) -> str:
    """Explicitly set the active mode. Returns the previous mode name.

    Does NOT track ``pre_plan_mode`` — use ``enter_plan_mode`` for that. This
    is the low-level setter for callers that know exactly what they want.
    """
    if target not in _REGISTRY:
        raise ValueError(f"Unknown agent mode: {target!r}. Known: {sorted(_REGISTRY)}")
    meta = _session_metadata(session)
    if meta is None:
        raise RuntimeError("Cannot set mode on a session without metadata.")
    previous = meta.get(SESSION_MODE_KEY, DEFAULT_MODE)
    meta[SESSION_MODE_KEY] = target
    return previous


def enter_plan_mode(session: Any) -> str:
    """Enter plan mode, remembering the previous mode for later restore.

    Returns the previous mode name. If already in plan mode, this is a no-op
    and the existing ``pre_plan_mode`` (if any) is preserved.
    """
    meta = _session_metadata(session)
    if meta is None:
        raise RuntimeError("Cannot enter plan mode on a session without metadata.")
    current = meta.get(SESSION_MODE_KEY, DEFAULT_MODE)
    if current == PLAN_MODE.name:
        return current
    meta[SESSION_PRE_PLAN_KEY] = current
    meta[SESSION_MODE_KEY] = PLAN_MODE.name
    return current


def exit_plan_mode(session: Any) -> str:
    """Restore the mode that was active before ``enter_plan_mode``.

    Defaults to ``build`` if no ``pre_plan_mode`` was saved. Returns the
    restored mode name.
    """
    meta = _session_metadata(session)
    if meta is None:
        raise RuntimeError("Cannot exit plan mode on a session without metadata.")
    restored = meta.pop(SESSION_PRE_PLAN_KEY, DEFAULT_MODE)
    meta[SESSION_MODE_KEY] = restored
    return restored


# ---------------------------------------------------------------------------
# Per-turn runtime reminder
# ---------------------------------------------------------------------------


def plan_mode_runtime_lines(metadata: Any) -> list[str]:
    """Per-turn reminder lines for the runtime context block.

    Returns a strongly-worded reminder that gets injected alongside the
    current user message every turn the session is in plan mode. Mirrors
    OpenClaude's approach: a per-turn attachment-style reminder beats a
    system-prompt suffix because the system prompt gets buried in long
    sessions while the runtime context is always fresh near the current
    message.

    The wording deliberately includes "supersedes any other instructions"
    because frontier models otherwise weight earlier prompt content over
    later reminders.
    """
    if not metadata or not isinstance(metadata, dict):
        return []
    mode_name = metadata.get(SESSION_MODE_KEY)
    if mode_name != PLAN_MODE.name:
        return []
    return [
        "",  # blank separator from the time/channel lines above
        "🧠 PLAN MODE IS ACTIVE — this turn-level reminder supersedes any",
        "earlier instruction you may have received. You MUST NOT edit, write,",
        "execute shell, or invoke any state-changing tool. You also MUST NOT",
        "delegate modifications to a subagent (it would run under the same",
        "restrictions). Read-only investigation is allowed.",
        "",
        "When you have a concrete plan, call `exit_plan_mode(plan=...)` with",
        "the plan as markdown. The user will then run `/build` to approve.",
        "Until they run `/build`, NO changes can be made — yours or the",
        "subagent's. If the task you're being asked requires modifications,",
        "your only valid output this turn is investigation + a call to",
        "`exit_plan_mode`. Do NOT claim work has been done; it hasn't.",
    ]


# ---------------------------------------------------------------------------
# Tool filtering
# ---------------------------------------------------------------------------


def filter_tools(tools: list[Any], mode: AgentMode) -> list[Any]:
    """Return the subset of *tools* allowed under *mode*.

    Fast path when the mode imposes no restriction (default BUILD_MODE):
    return the input list unchanged. Hot path on every turn, so we avoid
    list construction when not necessary.
    """
    if mode.allowed is None and not mode.denied:
        return tools
    return [t for t in tools if mode.is_tool_allowed(getattr(t, "name", ""))]
