"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from contextlib import suppress
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Mapping, Sequence

from durin.agent.memory import MemoryStore
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


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _MAX_HISTORY_CHARS = 32_000  # hard cap on recent history section size
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(self, workspace: Path, timezone: str | None = None, disabled_skills: list[str] | None = None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)

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
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # Memory hot layer (Phase 1.9). Always-loaded snapshot of identity +
        # top headlines + known entities. Lives at the END of the stable
        # tier so the earlier (more stable) parts stay cache-hot when the
        # hot layer rotates daily under dream.
        hot = read_hot_layer(self.workspace).render()
        if hot:
            parts.append(hot)

        return "\n\n---\n\n".join(parts)

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
        parts: list[str] = []
        if agent_mode_name:
            from durin.agent.agent_mode import get_mode

            mode_suffix = get_mode(agent_mode_name).prompt_suffix.strip()
            if mode_suffix:
                parts.append(mode_suffix)
        return "\n\n---\n\n".join(parts)

    def _build_volatile_layer(self, *, session_summary: str | None) -> str:
        """Memory + recent history + archived summary. Changes per turn
        — never cached by the provider, deliberately placed last so it
        doesn't poison the prefix cache hit rate for the stable layers.
        """
        parts: list[str] = []

        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            history_text = "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            )
            history_text = truncate_text(history_text, self._MAX_HISTORY_CHARS)
            parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

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
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        from durin.agent.agent_mode import plan_mode_runtime_lines

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
        # Sprint B / file-based plans — after /build approves a plan,
        # surface the path so the next turn can read it without the
        # user having to copy/paste it. The session metadata is cleared
        # after first surfacing to avoid noise on subsequent turns.
        # Persistence across compaction is handled separately by the
        # autocompact path (see autocompact.py — `executing_plan_path`
        # injects the full plan content into the summary).
        if session_metadata is not None:
            approved_path = session_metadata.get("approved_plan_path")
            if approved_path:
                extra = list(extra) + [
                    f"Approved plan ready at: {approved_path}",
                    "Start with updating your todo list using the todo_write "
                    "tool if applicable. The plan file is accessible via "
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
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

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

