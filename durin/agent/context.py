"""Context builder for assembling agent prompts."""

import base64
import logging
import mimetypes
import platform
from contextlib import suppress
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Mapping, Sequence

from durin.agent.memory import MemoryStore
from durin.agent.skill_usage import compute_working_set
from durin.agent.skills import SkillsLoader
from durin.memory.hot_layer import read_hot_layer
from durin.session.goal_state import goal_state_runtime_lines
from durin.session.todo_state import todos_runtime_lines
from durin.utils.helpers import (
    current_time_str,
    detect_image_mime,
    truncate_text,
)
from durin.utils.prompt_templates import render_template

logger = logging.getLogger(__name__)


def summarize_composition(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Roll a ``context.composition`` payload into 2 user-facing buckets.

    Returns a dict with ``conversation_tokens`` / ``infra_tokens`` (the
    rollups the user actually thinks in) plus ``conversation_breakdown``
    / ``infra_breakdown`` (sub-components, for the verbose ``/status``
    view).

    - **Conversation** = the user's growing footprint in this session:
      the prior turns (history_msg), the current user message, plus any
      per-turn volatile blocks (long-term memory active in the prompt,
      recent history snippets, archived session summary).
    - **Infrastructure** = everything fixed by configuration: identity,
      bootstrap files, skills (catalog + active), the memory hot layer,
      the agent mode suffix, and tool definitions. The user changes
      these by configuring durin, not by talking.

    Returns zeros when ``payload`` is None or empty so callers can blindly
    format the result.
    """
    if not payload:
        return {
            "conversation_tokens": 0,
            "infra_tokens": 0,
            "conversation_breakdown": {},
            "infra_breakdown": {},
            "total": 0,
        }

    conv: dict[str, int] = {}
    for key, label in (
        ("memory_long_term", "Memory (active)"),
        ("recent_history", "Recent history"),
        ("session_summary", "Session summary"),
    ):
        n = int(payload.get("volatile_breakdown", {}).get(key, 0) or 0)
        if n:
            conv[label] = n
    history_n = int(payload.get("history_msg_tokens", 0) or 0)
    current_n = int(payload.get("current_msg_tokens", 0) or 0)
    if history_n:
        conv["Prior turns"] = history_n
    if current_n:
        conv["Current message"] = current_n

    infra: dict[str, int] = {}
    stable_labels = (
        ("identity", "Identity"),
        ("bootstrap", "Bootstrap files"),
        ("skills_active", "Skills (active)"),
        ("skills_catalog", "Skills catalog"),
        ("memory_pinned", "Memory pinned"),
        ("memory_hot", "Memory hot layer"),
    )
    for key, label in stable_labels:
        n = int(payload.get("stable_breakdown", {}).get(key, 0) or 0)
        if n:
            infra[label] = n
    ctx_n = int(payload.get("context_tokens", 0) or 0)
    if ctx_n:
        infra["Agent mode"] = ctx_n
    tools_n = int(payload.get("tools_tokens", 0) or 0)
    if tools_n:
        infra["Tool definitions"] = tools_n

    conversation_tokens = sum(conv.values())
    infra_tokens = sum(infra.values())
    return {
        "conversation_tokens": conversation_tokens,
        "infra_tokens": infra_tokens,
        "conversation_breakdown": conv,
        "infra_breakdown": infra,
        "total": conversation_tokens + infra_tokens,
    }


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    # §8e: USER.md dropped — the user profile lives in the principal person
    # entity (pinned context), not a bootstrap file. SOUL.md (personality, user
    # control) stays.
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        try:
            from durin.agent.skill_lifecycle import sweep_unverified_skills
            sweep_unverified_skills(workspace)
        except Exception:  # noqa: BLE001 — sweep is best-effort; never break context init
            logger.debug("unverified-skill sweep failed", exc_info=True)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
        # Per-call breakdown of the rendered system-prompt sections, in
        # raw text. Each system-prompt build clears and re-fills it.
        # Read by ``build_messages`` to emit ``context.composition``
        # telemetry and by tests; safe to ignore elsewhere.
        self._last_layer_breakdown: dict[str, dict[str, str]] = {
            "stable": {},
            "context": {},
            "volatile": {},
        }
        # Last composition payload (most-recent ``context.composition``
        # event data). Exposed so AgentLoop / footer / /status can read
        # the current breakdown without touching the JSONL log.
        self.last_composition: dict[str, Any] | None = None
        # Hot working-set tier: computed once per instance (= per session)
        # so the stable prefix stays byte-identical across turns.
        self._skill_working_set: set[str] | None = None
        self._skill_working_set_done = False

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        agent_mode_name: str | None = None,
    ) -> str:
        """Build the system prompt in 3 cache-friendly tiers (Tier 2 C1,
        Hermes-inspired).

        Providers cache by *prefix*, so the canonical order is:

        - **Stable** (rarely changes, large cache value): identity,
          bootstrap files (CLAUDE.md / AGENTS.md), active-skills content,
          and the skills catalog. None of these depend on the current
          turn's memory or history state.
        - **Context** (session-stable, may differ per session): the
          agent-mode prompt suffix (PLAN / BUILD / EXPLORE). Switches
          rarely within a session but isn't identical across all sessions.
        - **Volatile** (changes per turn — never cached): memory section,
          recent history entries, archived session summary. These are
          the dynamic blocks that move with the conversation.

        Layers are joined by ``\\n\\n---\\n\\n`` for visual separation
        and individually skipped when empty. The agent-mode suffix was
        previously placed near the top so the model gives it heavy
        weight; moving it to the Context tier preserves that ordering
        relative to volatile blocks (which would dilute its visibility
        if placed below) while still keeping the stable prefix intact.
        """
        # Reset the per-call breakdown — each layer fills its slot.
        self._last_layer_breakdown = {"stable": {}, "context": {}, "volatile": {}}
        stable = self._build_stable_layer(channel=channel)
        context = self._build_context_layer(agent_mode_name=agent_mode_name)
        volatile = self._build_volatile_layer(session_summary=session_summary)
        return "\n\n---\n\n".join(p for p in (stable, context, volatile) if p)

    def _build_stable_layer(self, *, channel: str | None) -> str:
        """Identity + bootstrap + skills catalog. Cache-friendly anchor.

        Aside from workspace/runtime info embedded in the identity
        template (path, OS, Python version — all stable per process),
        this layer is byte-identical across turns of the same session.
        """
        breakdown: dict[str, str] = {}

        identity = self._get_identity(channel=channel)
        breakdown["identity"] = identity
        parts = [identity]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            breakdown["bootstrap"] = bootstrap
            parts.append(bootstrap)

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                block = f"# Active Skills\n\n{always_content}"
                breakdown["skills_active"] = block
                parts.append(block)

        include = self._hot_tier_include(always_skills)
        skills_summary = self.skills.build_skills_summary(
            exclude=set(always_skills), include=include,
        )
        if skills_summary:
            block = render_template("agent/skills_section.md", skills_summary=skills_summary)
            breakdown["skills_catalog"] = block
            parts.append(block)

        # Pinned memory (Phase 8b): who the user is + always_on feedback
        # (stance/practice). Always injected, independent of retrieval — this
        # is what re-feeds the agent its authored knowledge (design §2.10-2.12).
        pinned = self._build_pinned_memory(channel=channel)
        if pinned:
            breakdown["memory_pinned"] = pinned
            parts.append(pinned)

        # Memory hot layer (Phase 1.9). Always-loaded snapshot of identity +
        # top headlines + known entities. Lives at the END of the stable
        # tier so the earlier (more stable) parts stay cache-hot when the
        # hot layer rotates daily under dream.
        hot = read_hot_layer(self.workspace).render()
        if hot:
            breakdown["memory_hot"] = hot
            parts.append(hot)

        self._last_layer_breakdown["stable"] = breakdown
        return "\n\n---\n\n".join(parts)

    def _build_pinned_memory(self, *, channel: str | None) -> str:
        """The pinned memory layer: the principal's entity + always_on feedback.

        Always injected, independent of retrieval (design §2.10-2.12). The
        principal is resolved channel → owner (config) → person:anonymous; the
        owner config is optional (defaults to anonymous until set). Never raises
        — a failure degrades to no pinned block so the prompt still builds.
        """
        try:
            from durin.memory.principal import (
                build_pinned_context,
                resolve_principal,
            )
            owner = None
            try:
                from durin.config.loader import load_config
                owner = getattr(load_config().memory, "owner", None)
            except Exception:  # noqa: BLE001 — test workspaces without a config
                owner = None
            principal = resolve_principal(channel, owner=owner)
            return build_pinned_context(self.workspace, principal)
        except Exception:  # noqa: BLE001 — never break the prompt build
            return ""

    def _hot_tier_include(self, always_skills: list[str]) -> set[str] | None:
        """The working-set name filter for the skills_catalog block, or None
        (full catalog) when the hot tier is disabled. Memoized per instance."""
        if self._skill_working_set_done:
            return self._skill_working_set
        self._skill_working_set_done = True
        try:
            from durin.config.loader import load_config
            ht = load_config().agents.defaults.skills_hot_tier
        except Exception:  # noqa: BLE001 — unit/test workspaces without a config file
            from durin.config.schema import SkillsHotTierConfig
            ht = SkillsHotTierConfig()
        if not ht.enabled:
            self._skill_working_set = None
            return None
        always = set(always_skills)
        candidates = [
            e["name"] for e in self.skills.list_skills(filter_unavailable=False)
            if e["name"] not in always
        ]
        names = compute_working_set(
            self.workspace, candidates,
            recent=ht.recent, frequent=ht.frequent,
            frequent_window_hours=ht.frequent_window_hours,
            recent_window_hours=ht.recent_window_hours,
        )
        self._skill_working_set = set(names)
        return self._skill_working_set

    def _build_context_layer(self, *, agent_mode_name: str | None) -> str:
        """Session-stable blocks that may differ between sessions.

        Sprint B / L3 — the active-mode prompt suffix. It used to be
        placed near the top of the prompt so the model would weight it
        heavily; in the 3-tier layout it sits between the stable prefix
        and the volatile suffix — still ABOVE the volatile blocks (so
        model attention isn't diluted by memory/history scrolling past
        it) and still cache-stable within a session for the common case
        (no mid-session mode switch).
        """
        breakdown: dict[str, str] = {}
        parts: list[str] = []
        if agent_mode_name:
            from durin.agent.agent_mode import get_mode

            mode_suffix = get_mode(agent_mode_name).prompt_suffix.strip()
            if mode_suffix:
                breakdown["mode_suffix"] = mode_suffix
                parts.append(mode_suffix)
        self._last_layer_breakdown["context"] = breakdown
        return "\n\n---\n\n".join(parts)

    def _build_volatile_layer(self, *, session_summary: str | None) -> str:
        """Memory + recent history + archived summary. Changes per turn
        — never cached by the provider, deliberately placed last so it
        doesn't poison the prefix cache hit rate for the stable layers.
        """
        breakdown: dict[str, str] = {}
        parts: list[str] = []

        # §8e: the legacy MEMORY.md long-term block + the history.jsonl
        # "recent history" block are removed. In the new model that knowledge
        # lives in the pinned context + entities (stable tier) and the raw turns
        # are the session replay — injecting MEMORY.md/history here double-fed
        # the prompt. The compaction summary below survives.
        if session_summary:
            block = f"[Archived Context Summary]\n\n{session_summary}"
            breakdown["session_summary"] = block
            parts.append(block)

        self._last_layer_breakdown["volatile"] = breakdown
        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block appended after user content."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        with suppress(Exception):
            tpl = pkg_files("durin") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        return False

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        session_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        iteration: int | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        from durin.agent.agent_mode import (
            executing_plan_runtime_lines,
            plan_mode_runtime_lines,
        )

        extra = goal_state_runtime_lines(session_metadata)
        # Echo the agent's todo list so it survives compaction. Without
        # this, the list lives only in session metadata and the model
        # forgets it once the relevant tool result scrolls out.
        extra = list(extra) + todos_runtime_lines(session_metadata)
        # Per-turn plan-mode reminder (OpenClaude pattern). When the session
        # is in plan mode, inject a strongly-worded reminder next to the
        # current user message so the model can't "forget" the mode between
        # turns the way it does when the constraint lives only in the
        # system prompt.
        extra = list(extra) + plan_mode_runtime_lines(session_metadata)
        # Per-turn pointer to the approved plan currently executing. Re-
        # derived from session.metadata every turn (same store + cadence as
        # the todo echo above) so the "executing an approved plan" frame
        # survives compaction. This replaces the carry-over the refuted
        # `autocompact` module used to do by splicing plan content into the
        # summary — here it's a lightweight pointer; progress lives in the
        # todo list, so it does not make the model re-run completed steps.
        extra = list(extra) + executing_plan_runtime_lines(session_metadata)
        # Sprint B / file-based plans — after /build approves a plan,
        # surface the path so the next turn can read it without the user
        # having to copy/paste it. One-shot (consumed below); the persistent
        # counterpart is the executing-plan pointer injected just above.
        if session_metadata is not None:
            approved_path = session_metadata.get("approved_plan_path")
            if approved_path:
                extra = list(extra) + [
                    f"Approved plan ready at: {approved_path}",
                    "Start with updating your todo list using the todo_write "
                    "tool if applicable — include the plan's Verification "
                    "items as final todos. The plan file is accessible via "
                    "read_file at any time during implementation.",
                ]
                # One-shot: consume so we don't re-inject every turn.
                with suppress(Exception):
                    if isinstance(session_metadata, dict):
                        session_metadata.pop("approved_plan_path", None)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        # Runtime context is appended to keep the user-content prefix stable
        # for prompt-cache hits (the context changes every turn due to time).
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        agent_mode_name = None
        if session_metadata is not None:
            from durin.agent.agent_mode import SESSION_MODE_KEY

            agent_mode_name = session_metadata.get(SESSION_MODE_KEY)
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    agent_mode_name=agent_mode_name,
                ),
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            self._emit_composition_event(
                history=history,
                current_user_content=merged,
                tools=tools,
                iteration=iteration,
                session_key=session_key,
            )
            return messages
        messages.append({"role": current_role, "content": merged})
        self._emit_composition_event(
            history=history,
            current_user_content=merged,
            tools=tools,
            iteration=iteration,
            session_key=session_key,
        )
        return messages

    def _emit_composition_event(
        self,
        *,
        history: list[dict[str, Any]],
        current_user_content: Any,
        tools: list[dict[str, Any]] | None,
        iteration: int | None,
        session_key: str | None,
    ) -> None:
        """Emit ``context.composition`` with a per-tier token breakdown.

        Best-effort: any failure is silently swallowed — telemetry must
        never affect the user-facing turn.
        """
        try:
            import json

            from durin.telemetry.logger import current_telemetry
            from durin.utils.helpers import (
                estimate_message_tokens,
                estimate_text_tokens,
            )

            logger_obj = current_telemetry()
            if logger_obj is None:
                return

            stable = self._last_layer_breakdown.get("stable", {})
            volatile = self._last_layer_breakdown.get("volatile", {})
            context_break = self._last_layer_breakdown.get("context", {})

            stable_breakdown = {
                name: estimate_text_tokens(text) for name, text in stable.items()
            }
            volatile_breakdown = {
                name: estimate_text_tokens(text) for name, text in volatile.items()
            }
            stable_tokens = sum(stable_breakdown.values())
            volatile_tokens = sum(volatile_breakdown.values())
            context_tokens = sum(
                estimate_text_tokens(t) for t in context_break.values()
            )

            history_msg_tokens = sum(
                estimate_message_tokens(m) for m in history
            )
            # The current user message is either a str or a list of
            # content blocks; build a synthetic message dict so
            # estimate_message_tokens does the right thing.
            current_msg_tokens = estimate_message_tokens(
                {"role": "user", "content": current_user_content}
            )

            tools_tokens = (
                estimate_text_tokens(json.dumps(tools, ensure_ascii=False))
                if tools else 0
            )

            estimated_total = (
                stable_tokens
                + context_tokens
                + volatile_tokens
                + history_msg_tokens
                + current_msg_tokens
                + tools_tokens
            )

            payload: dict[str, Any] = {
                "stable_tokens": stable_tokens,
                "stable_breakdown": stable_breakdown,
                "context_tokens": context_tokens,
                "volatile_tokens": volatile_tokens,
                "volatile_breakdown": volatile_breakdown,
                "history_msg_tokens": history_msg_tokens,
                "current_msg_tokens": current_msg_tokens,
                "tools_tokens": tools_tokens,
                "estimated_total": estimated_total,
            }
            if iteration is not None:
                payload["iteration"] = iteration
            if session_key is not None:
                payload["session_key"] = session_key

            # Cache the most recent payload so the footer and /status
            # can read it directly (no JSONL round-trip).
            self.last_composition = payload

            from contextlib import suppress
            with suppress(Exception):
                logger_obj.log("context.composition", payload)
        except Exception:  # noqa: BLE001
            # Telemetry failure must never break the turn build.
            return

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

