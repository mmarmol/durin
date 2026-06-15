"""DurinApp — Textual TUI for durin.

D5.3 wires the AgentLoop bus into the TUI: user submissions publish
inbound messages; a background worker drains outbound messages and
streams them into the ChatView.

Metadata flags consumed off OutboundMessage.metadata, matching the
legacy CLI's ``_consume_outbound`` semantics so behaviour stays
consistent across the two interactive surfaces:

- ``_stream_delta`` → append to the active assistant bubble.
- ``_stream_end``   → finalize the active assistant bubble.
- ``_streamed``     → end-of-turn marker (no UI side-effect).
- ``_switch_chat_id`` → mutate ``cli_chat_id`` + refresh chrome.
- otherwise        → render as a standalone bubble (assistant if no
                     stream is in flight, system otherwise).

The legacy ``durin/cli/commands.py`` interactive path is untouched
and remains the default. The TUI is opt-in via ``durin agent --tui``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from textual import __version__ as TEXTUAL_VERSION
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Input

from durin import __version__ as DURIN_VERSION
from durin.cli.tui.widgets import ChatView, FooterBar, HeaderBar, InputArea, MessageBubble
from durin.cli.tui.widgets.footer_bar import payload_from_loop

__all__ = ["DurinApp", "run_durin_tui"]


class DurinApp(App[None]):
    """Top-level Textual App for durin."""

    TITLE = f"durin {DURIN_VERSION}"
    SUB_TITLE = f"Textual {TEXTUAL_VERSION}"

    CSS_PATH = "durin.tcss"

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
        ("escape", "abort", "Abort"),
        ("ctrl+t", "toggle_dark", "Theme"),
        ("ctrl+l", "open_model_picker", "Model"),
        ("ctrl+y", "copy_last_assistant", "Copy"),
    ]

    def __init__(
        self,
        *,
        agent_loop: Any | None = None,
        cli_channel: str = "cli",
        cli_chat_id: str = "direct",
        markdown: bool = True,
        auto_resume: bool = False,
    ) -> None:
        super().__init__()
        self._agent_loop = agent_loop
        self._cli_channel = cli_channel
        self._cli_chat_id = cli_chat_id
        self._markdown = markdown
        # When True, the session picker pops up on mount so the user
        # explicitly chooses which session to resume.
        self._auto_resume = auto_resume
        self._current_assistant_bubble: MessageBubble | None = None
        # Reasoning chunks accumulate into a single dim bubble (one per
        # turn) instead of stacking one bubble per chunk.
        self._current_reasoning_bubble: MessageBubble | None = None
        # Spinner that appears between user submit and first model
        # delta. None when no turn is in flight.
        self._working_indicator: Any = None
        # Track active tool-call bubbles by call_id so the "end" event
        # updates the same widget the "start" event created.
        self._tool_bubbles: dict[str, Any] = {}
        # ActivityCluster wrapping reasoning + tool bubbles during a turn.
        self._active_cluster: Any = None
        self._bus_task: asyncio.Task | None = None
        self._consume_task: asyncio.Task | None = None
        # Fire-and-forget tasks (submit publishes, tool-bubble notes) parked
        # here so the event loop keeps a strong reference and can't GC them
        # mid-flight (RUF006).
        self._background_tasks: set[asyncio.Task] = set()
        self._palette = "ithildin"
        self._mode = "dark"
        self._apply_durin_theme()

    # ---- theme ------------------------------------------------------------

    def _apply_durin_theme(self) -> None:
        """Register durin's six palettes and apply the configured one.

        Palette + mode come from ``config.appearance``; ``mode = "auto"``
        is resolved against the terminal (``COLORFGBG``).
        """
        from durin.cli.theme import PALETTE_NAMES, detect_mode, textual_theme

        palette, mode = "ithildin", "auto"
        try:
            from durin.config.loader import load_config

            appearance = load_config().appearance
            palette, mode = appearance.palette, appearance.mode
        except Exception:  # noqa: BLE001 - never let config break boot
            pass
        self._palette = palette if palette in PALETTE_NAMES else "ithildin"
        self._mode = detect_mode() if mode == "auto" else (
            mode if mode in ("light", "dark") else detect_mode()
        )
        for name in PALETTE_NAMES:
            for theme_mode in ("light", "dark"):
                self.register_theme(textual_theme(name, theme_mode))
        self.theme = f"durin-{self._palette}-{self._mode}"

    def _persist_appearance(self) -> None:
        """Write the current palette/mode back to config."""
        try:
            from durin.config.loader import load_config, save_config

            cfg = load_config()
            cfg.appearance.palette = self._palette
            cfg.appearance.mode = self._mode
            save_config(cfg)
        except Exception:  # noqa: BLE001 - a theme toggle must not crash
            pass

    # ---- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        from durin.cli.tui.widgets import CompletionsHint

        session_label = f"{self._cli_channel}:{self._cli_chat_id}"
        session_meta = self._compute_session_meta()
        with Vertical(id="main-layout"):
            yield HeaderBar(session_label=session_label, session_meta=session_meta)
            yield ChatView(id="chat")
            yield CompletionsHint()
            yield InputArea(
                placeholder="message durin",
                workspace=Path(self._agent_loop.workspace) if self._agent_loop else None,
            )
            yield FooterBar(
                payload_getter=lambda: payload_from_loop(
                    self._agent_loop, self._cli_channel, self._cli_chat_id
                ),
            )

    def _compute_session_meta(self) -> str:
        """Return a short '47 msgs · 12h ago' string for the header.

        Empty string when there's no session yet or no activity to summarise.
        """
        if self._agent_loop is None:
            return ""
        try:
            from durin.cli.sessions import list_sessions

            sessions = list_sessions(Path(self._agent_loop.workspace))
        except Exception:  # noqa: BLE001
            return ""
        for info in sessions:
            if info.channel == self._cli_channel and info.chat_id == self._cli_chat_id:
                if info.msg_count == 0:
                    return ""
                return f"{info.msg_count} msgs · {info.age_label}"
        return ""

    # ---- lifecycle --------------------------------------------------------

    def on_mount(self) -> None:
        """Boot the bus + outbound consumer once the layout is up."""
        # Order matters in a scroll-from-top chat view: the welcome
        # banner stays at the top (install-level info, stable across
        # the session), the restored turns sit just above the input so
        # the user lands directly into recent context.
        self._render_startup_banner()
        self._restore_recent_turns()

        # A chat surface must be ready to type into the moment it opens.
        # Textual otherwise parks focus on the first focusable widget —
        # the scrollable history — and silently swallows keystrokes
        # until the user tabs or clicks into the input.
        self.query_one(InputArea).focus()

        # Load prompt history for Up/Down recall
        from durin.cli.tui.state import get_prompt_history

        try:
            self.query_one(InputArea).load_history(get_prompt_history())
        except Exception:  # noqa: BLE001
            pass

        if self._agent_loop is None:
            return
        bus = getattr(self._agent_loop, "bus", None)
        if bus is None:
            return
        # AgentLoop.run() is the bus's inbound dispatcher; without it
        # no agent turn fires.
        self._bus_task = asyncio.create_task(self._agent_loop.run())
        self._consume_task = asyncio.create_task(self._consume_outbound())
        # `--resume`: pop the session picker after Textual settles so
        # the user can pick a different session if they want.
        if self._auto_resume:
            self.call_later(self._open_session_picker)

    def _restore_recent_turns(self, *, tail: int = 6) -> None:
        """Replay the last ``tail`` messages of the active session as bubbles.

        Resumed sessions need context — the user came back to keep
        working, not to read a welcome screen. We show enough recent
        history to orient (default last 6 messages ≈ 3 turns) and add
        a dim hint when there's more above.
        """
        if self._agent_loop is None:
            return
        try:
            sessions = self._agent_loop.sessions
            session_key = f"{self._cli_channel}:{self._cli_chat_id}"
            session = sessions.get_or_create(session_key)
            messages = list(getattr(session, "messages", []) or [])
        except Exception:  # noqa: BLE001
            return
        if not messages:
            return
        try:
            chat = self.query_one("#chat", ChatView)
        except Exception:  # noqa: BLE001
            return

        total = len(messages)
        recent = messages[-tail:] if total > tail else messages
        hidden = total - len(recent)

        if hidden > 0:
            note = chat.add_message(
                "banner",
                f"… {hidden} earlier message{'s' if hidden != 1 else ''} hidden "
                f"(use `/history {total}` to see all)",
            )
            # Tighter margin on the hint so it doesn't dominate.
            try:
                note.styles.margin = (0, 2, 0, 2)
            except Exception:  # noqa: BLE001
                pass

        # Build a map of tool_call_id → (name, args) so when we hit a
        # tool result we can render it as a proper ToolCallBubble with
        # clickable URLs / paths and the same truncation as live runs,
        # rather than a plain `tool: ...` text bubble.
        tool_call_index: dict[str, tuple[str, dict[str, Any]]] = {}
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            tcs = msg.get("tool_calls") or []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("id") or "")
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except (TypeError, ValueError):
                    args = {}
                if tc_id and name:
                    tool_call_index[tc_id] = (name, args)

        for msg in recent:
            role = msg.get("role") if isinstance(msg, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not role:
                continue

            # Tool result: render as a ToolCallBubble (rich, clickable)
            # rather than a plain text bubble.
            if role == "tool":
                tc_id = str(msg.get("tool_call_id") or "")
                name, args = tool_call_index.get(tc_id, (str(msg.get("name") or "tool"), {}))
                result_text = _content_to_text(content)
                try:
                    from durin.cli.tui.widgets import ToolCallBubble

                    bubble = ToolCallBubble({
                        "version": 1, "phase": "end", "call_id": tc_id,
                        "name": name, "arguments": args,
                        "result": result_text,
                    })
                    chat.mount(bubble)
                    bubble.update_from_event({
                        "version": 1, "phase": "end", "call_id": tc_id,
                        "name": name, "arguments": args,
                        "result": result_text,
                    })
                except Exception:  # noqa: BLE001
                    # Fall back to the old plain rendering so the
                    # session is at least visible if the bubble fails.
                    chat.add_message("system", f"{name}: {result_text[:200]}")
                continue

            # Assistant message that ONLY carried tool_calls (no text).
            # The matching tool result(s) will render as bubbles, so
            # don't add an empty assistant bubble here.
            if role == "assistant" and msg.get("tool_calls") and content is None:
                continue

            text = _content_to_text(content)
            if role == "assistant" and msg.get("tool_calls") and not text.strip():
                continue
            if not text:
                continue
            if role in ("user", "assistant", "system"):
                chat.add_message(role, text)
            else:
                chat.add_message("system", text)

    def _render_startup_banner(self) -> None:
        """Paint pi-style welcome bubble: version, keybindings, install summary.

        Modelled on pi-agent's startup screen — version + condensed
        keybinding line + a "[Context]" block listing what's loaded.
        Everything here is install-level info, not per-conversation,
        so it doesn't clutter the footer once you're typing.
        """
        from durin import __version__
        from durin.cli.tui.startup import build_durin_logo, build_startup_banner

        try:
            chat = self.query_one("#chat", ChatView)
        except Exception:  # noqa: BLE001
            return

        logo = chat.add_message("logo", "")
        logo.body = build_durin_logo()

        body = build_startup_banner(
            version=__version__,
            agent_loop=self._agent_loop,
        )
        bubble = chat.add_message("banner", "")
        bubble.body = body

    async def on_unmount(self) -> None:
        """Cancel background tasks cleanly when the app exits."""
        for task in (self._consume_task, self._bus_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ---- event handlers ---------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        """Refresh the completions hint on every keystroke."""
        from durin.cli.tui.widgets import CompletionsHint, MultiModeSuggester, SlashCommandSuggester

        try:
            hint = self.query_one(CompletionsHint)
        except Exception:  # noqa: BLE001
            return
        # Only the InputArea fires hints; ignore Input.Changed from other
        # widgets that may live in the layout (modal pickers, etc.).
        if not isinstance(event.input, InputArea):
            return
        value = event.value or ""
        if not value.startswith("/"):
            hint.clear()
            return
        suggester = event.input.suggester
        if isinstance(suggester, (MultiModeSuggester, SlashCommandSuggester)):
            candidates = suggester.candidates(value)
            hint.show_candidates(candidates)
        else:
            hint.clear()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Sanitize surrogate pairs that mis-paste emoji can produce.
        from durin.cli.commands import _sanitize_surrogates

        value = _sanitize_surrogates(event.value).strip()
        if not value:
            event.input.value = ""
            return
        event.input.value = ""

        # D5.5 — intercept the bare /sessions and /model commands and
        # open a modal picker instead of dispatching to the bus. Args
        # forms (e.g. `/sessions alpha`, `/model fast`) still route
        # through the router so the inline text response is preserved.
        if value == "/sessions":
            self._open_session_picker()
            return
        if value == "/model":
            self._open_model_picker()
            return
        if value == "/theme":
            self._open_theme_picker()
            return
        if value.startswith("/theme "):
            self._set_palette(value[len("/theme ") :].strip())
            return

        # D3.2 shell paste: !cmd runs and prepends output to the message;
        # !!cmd runs silently without involving the agent.
        from durin.cli.tui.shell_paste import process_shell_paste

        shell_result = process_shell_paste(value)
        if not shell_result.send:
            # !!cmd path: confirm the command ran but don't publish anything.
            chat = self.query_one("#chat", ChatView)
            chat.add_message(
                "system",
                f"Ran `{shell_result.ran_command}` silently (exit {shell_result.exit_code}).",
            )
            return
        value = shell_result.message

        # D5.6 drag-and-drop: image/audio paths become workspace-local
        # copies in .media/; the cleaned text + media list ride InboundMessage.
        media: list[str] = []
        if self._agent_loop is not None:
            from durin.cli.dragdrop import process_dragged_paths

            try:
                value, media = process_dragged_paths(value, Path(self._agent_loop.workspace))
            except Exception:  # noqa: BLE001
                # Never block the turn on a dragdrop error; pass through raw.
                media = []

        chat = self.query_one("#chat", ChatView)
        chat.add_message("user", value)
        # Persist prompt to history for Up/Down recall
        from durin.cli.tui.state import add_prompt

        add_prompt(value)
        # Open a fresh assistant bubble for streaming. Tokens land via
        # the _stream_delta path in _consume_outbound.
        self._current_assistant_bubble = chat.add_message("assistant", "")
        if self._agent_loop is None:
            # Offline / test mode — keep the D5.2 placeholder behaviour.
            self._current_assistant_bubble.body = (
                "Streaming + agent dispatch land in D5.3 — see "
                "docs/10_textual_migration.md."
            )
            return
        # Spinner: shows "thinking…" between submit and first delta.
        self._show_working_indicator()
        task = asyncio.create_task(self._publish_inbound(value, media))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _show_working_indicator(self) -> None:
        """Mount a 'thinking…' spinner below the assistant bubble.

        The spinner is removed on the first arriving reasoning or
        content delta — see ``_dismiss_working_indicator``.
        """
        if self._working_indicator is not None:
            return
        try:
            from durin.cli.tui.widgets import WorkingIndicator

            chat = self.query_one("#chat", ChatView)
            indicator = WorkingIndicator()
            chat.mount(indicator)
            chat.scroll_end(animate=False)
            self._working_indicator = indicator
        except Exception:  # noqa: BLE001
            self._working_indicator = None

    def _dismiss_working_indicator(self) -> None:
        ind = self._working_indicator
        if ind is None:
            return
        try:
            ind.remove()
        except Exception:  # noqa: BLE001
            pass
        self._working_indicator = None

    def _get_or_create_cluster(self) -> Any:
        """Return the active ActivityCluster, creating one if needed.

        The cluster groups reasoning + tool bubbles into a collapsible
        section. It's finalized (collapsed + summary) when the assistant
        text starts streaming.
        """
        from durin.cli.tui.widgets.activity_cluster import ActivityCluster

        if self._active_cluster is not None:
            try:
                self._active_cluster.query_one("#cluster-header")
                return self._active_cluster
            except Exception:  # noqa: BLE001
                self._active_cluster = None

        try:
            chat = self.query_one("#chat", ChatView)
            cluster = ActivityCluster()
            chat.mount(cluster)
            chat.scroll_end(animate=False)
            self._active_cluster = cluster
            return cluster
        except Exception:  # noqa: BLE001
            return None

    def _finalize_cluster(self) -> None:
        """Collapse the active cluster and switch its header to 'Done'."""
        if self._active_cluster is None:
            return
        try:
            self._active_cluster.finalize()
        except Exception:  # noqa: BLE001
            pass
        self._active_cluster = None

    def _render_tool_event(self, event: dict[str, Any]) -> None:
        """Add or update a ToolCallBubble for one tool-call lifecycle event."""
        from durin.cli.tui.widgets import ToolCallBubble

        call_id = str(event.get("call_id") or "")
        phase = str(event.get("phase") or "")

        if phase == "start" or call_id not in self._tool_bubbles:
            try:
                from durin.cli.tui.widgets import ToolCallBubble

                bubble = ToolCallBubble(event)
                cluster = self._get_or_create_cluster()
                if cluster is not None:
                    cluster.mount(bubble)
                    cluster.add_tool_step()
                else:
                    chat = self.query_one("#chat", ChatView)
                    chat.mount(bubble)
                chat = self.query_one("#chat", ChatView)
                chat.scroll_end(animate=False)
                self._tool_bubbles[call_id] = bubble
            except Exception:  # noqa: BLE001
                return
            if phase == "start":
                return
        # phase == "end" / "error" — update the existing bubble.
        bubble = self._tool_bubbles.get(call_id)
        if bubble is not None:
            try:
                bubble.update_from_event(event)
            except Exception:  # noqa: BLE001
                pass

    async def _publish_inbound(self, value: str, media: list[str]) -> None:
        from durin.bus.events import InboundMessage

        await self._agent_loop.bus.publish_inbound(
            InboundMessage(
                channel=self._cli_channel,
                sender_id="user",
                chat_id=self._cli_chat_id,
                content=value,
                media=media,
                metadata={"_wants_stream": True},
            )
        )

    # ---- key-binding actions (D5.7) --------------------------------------

    async def action_abort(self) -> None:
        """Esc: cancel the in-flight agent turn for this session."""
        if self._agent_loop is None:
            return
        try:
            session_key = f"{self._cli_channel}:{self._cli_chat_id}"
            cancel = getattr(self._agent_loop, "_cancel_active_tasks", None)
            if cancel is not None:
                await cancel(session_key)
        except Exception:  # noqa: BLE001
            pass
        # Close any open assistant bubble so the next reply starts fresh.
        self._current_assistant_bubble = None

    def action_toggle_dark(self) -> None:
        """Ctrl+T: flip light/dark within the current durin palette."""
        self._mode = "light" if self._mode == "dark" else "dark"
        self.theme = f"durin-{self._palette}-{self._mode}"
        self._persist_appearance()

    def action_open_model_picker(self) -> None:
        """Ctrl+L: open the model picker modal (D5.5)."""
        self._open_model_picker()

    def action_copy_last_assistant(self) -> None:
        """Ctrl+Y: copy the last assistant message body to the clipboard.

        The agent's body is rendered as Markdown which Textual doesn't
        let you select with the mouse — this gives a keyboard path that
        always works.
        """
        from durin.utils.clipboard import NoClipboardError, copy_text

        try:
            chat = self.query_one("#chat", ChatView)
        except Exception:  # noqa: BLE001
            return
        last_body = ""
        for bubble in reversed(list(chat.query(MessageBubble))):
            if bubble._role == "assistant" and bubble.body:
                last_body = bubble.body
                break
        if not last_body:
            self.notify("No assistant message to copy yet.", severity="warning")
            return
        try:
            copy_text(last_body)
            self.notify(f"Copied last reply ({len(last_body):,} chars).")
        except NoClipboardError as e:
            self.notify(f"Copy failed: {e}", severity="error")

    # ---- D5.5 modal pickers ----------------------------------------------

    @work
    async def _open_session_picker(self) -> None:
        """Worker so push_screen_wait can be awaited (Textual 8.x API)."""
        from durin.cli.tui.screens import SessionPickerScreen

        entries = self._collect_sessions()
        if not entries:
            chat = self.query_one("#chat", ChatView)
            chat.add_message("system", "No sessions yet in this workspace.")
            return
        current_key = f"{self._cli_channel}:{self._cli_chat_id}"
        selected = await self.push_screen_wait(
            SessionPickerScreen(entries, current_key=current_key)
        )
        if selected and selected != current_key:
            await self._publish_inbound(f"/resume {selected}", [])

    @work
    async def _open_model_picker(self) -> None:
        from durin.cli.tui.model_catalog import build_entries, infer_provider
        from durin.cli.tui.screens import ModelPickerScreen
        from durin.cli.tui.state import add_recent_model, get_recent_models
        from durin.config.loader import load_config
        from durin.config.schema import ModelPresetConfig

        if self._agent_loop is None:
            return

        config = load_config()
        presets = self._agent_loop.model_presets
        active = self._model_label()[1]
        recent = get_recent_models()

        entries = build_entries(
            config=config,
            presets=presets,
            recent=recent,
            active=active,
        )
        if not entries:
            chat = self.query_one("#chat", ChatView)
            chat.add_message("system", "No models available.")
            return

        selected = await self.push_screen_wait(
            ModelPickerScreen(entries, active=active)
        )
        if not selected or selected == active:
            return

        if selected not in presets:
            provider = infer_provider(selected)
            temp = ModelPresetConfig(model=selected, provider=provider)
            presets[selected] = temp

        add_recent_model(selected)
        await self._publish_inbound(f"/model {selected}", [])

    @work
    async def _open_theme_picker(self) -> None:
        """`/theme` — pick the colour palette; Ctrl+T still toggles mode."""
        from durin.cli.tui.screens import ThemePickerScreen

        selected = await self.push_screen_wait(
            ThemePickerScreen(active=self._palette)
        )
        if selected and selected != self._palette:
            self._set_palette(selected)

    def _set_palette(self, name: str) -> None:
        """Switch the colour palette (the `/theme <name>` form)."""
        from durin.cli.theme import PALETTE_NAMES

        chat = self.query_one("#chat", ChatView)
        if name not in PALETTE_NAMES:
            chat.add_message(
                "system",
                f"Unknown palette '{name}'. Try: {', '.join(PALETTE_NAMES)}.",
            )
            return
        if name != self._palette:
            self._palette = name
            self.theme = f"durin-{self._palette}-{self._mode}"
            self._persist_appearance()
        chat.add_message("system", f"Palette → {name}.")

    def _collect_sessions(self) -> list:
        """Walk the sessions directory and return a list of SessionEntry."""
        import json

        from durin.cli.tui.screens.session_picker import SessionEntry

        if self._agent_loop is None:
            return []
        try:
            sessions_dir = self._agent_loop.sessions.sessions_dir
        except AttributeError:
            return []
        out: list = []
        for path in sessions_dir.glob("*.jsonl"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            first = text.split("\n", 1)[0] if text else ""
            try:
                meta = json.loads(first)
            except json.JSONDecodeError:
                continue
            if meta.get("_type") != "metadata":
                continue
            out.append(
                SessionEntry(
                    key=meta.get("key", path.stem),
                    display_name=(meta.get("metadata") or {}).get("display_name") or "",
                    msg_count=max(0, text.count("\n") - 1),
                    updated_at=meta.get("updated_at", ""),
                )
            )
        out.sort(key=lambda e: e.updated_at, reverse=True)
        return out

    def _collect_model_presets(self) -> list[str]:
        """Return the configured model preset names, plus 'default'."""
        if self._agent_loop is None:
            return []
        names = set(getattr(self._agent_loop, "model_presets", None) or {})
        names.add("default")
        return sorted(names)

    # ---- outbound consumer (mirrors legacy _consume_outbound) ------------

    async def _consume_outbound(self) -> None:
        """Drain outbound bus messages into the chat view.

        Wrapped in a global try/except so a single message-handling failure
        can never silently kill the task — the symptom that historically
        looked like "the agent stopped responding".
        """
        from loguru import logger

        bus = self._agent_loop.bus
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.bind(channel="tui").warning(
                    f"bus.consume_outbound raised: {exc!r}; continuing"
                )
                continue

            try:
                self._handle_outbound(msg)
            except Exception as exc:  # noqa: BLE001
                # Render the failure as a system bubble so the user
                # sees SOMETHING instead of an empty assistant bubble.
                logger.bind(channel="tui").exception(
                    f"_handle_outbound raised on {msg.content[:60]!r}: {exc!r}"
                )
                try:
                    chat = self.query_one("#chat", ChatView)
                    chat.add_message("system", f"render error: {exc!r}")
                except Exception:  # noqa: BLE001
                    pass

    def _handle_outbound(self, msg: Any) -> None:
        """Route one outbound message to the chat view.

        Extracted from ``_consume_outbound`` so we can wrap it with a
        narrow try/except without losing the typed control flow.
        """
        meta = msg.metadata or {}

        # /resume routes here: the next inbound publish uses the new chat_id.
        switch_to = meta.get("_switch_chat_id")
        if switch_to and switch_to != self._cli_chat_id:
            self._cli_chat_id = switch_to
            self._refresh_chrome()

        # ---- tool calls --------------------------------------------------
        # The agent emits one outbound per tool-call lifecycle phase
        # (start, end, error) with a list of structured events under
        # `_tool_events`. Render each as a `ToolCallBubble` keyed by
        # call_id so the "end" event updates the same bubble created
        # at "start".
        tool_events = meta.get("_tool_events")
        if tool_events:
            # The hint content (msg.content) is redundant with the
            # structured events — the bubble derives its summary line
            # from the event's arguments. Don't double-render it.
            self._dismiss_working_indicator()
            for event in tool_events:
                self._render_tool_event(event)
            return

        # ---- reasoning stream (model's internal monologue) --------------
        # The agent emits one outbound per reasoning chunk with
        # `_reasoning_delta=True`. Without special handling each chunk
        # would create its own bubble — readable as gibberish (one word
        # per line). Collapse them into a single dim "thinking" bubble
        # that grows in place.
        if meta.get("_reasoning_delta"):
            # First reasoning chunk means the model is now responding —
            # drop the spinner.
            self._dismiss_working_indicator()
            if self._current_reasoning_bubble is None:
                from durin.cli.tui.widgets.chat_view import MessageBubble

                cluster = self._get_or_create_cluster()
                bubble = MessageBubble(role="reasoning", body="")
                if cluster is not None:
                    cluster.mount(bubble)
                    cluster.add_reasoning_step()
                else:
                    chat = self.query_one("#chat", ChatView)
                    chat.mount(bubble)
                chat = self.query_one("#chat", ChatView)
                chat.scroll_end(animate=False)
                self._current_reasoning_bubble = bubble
            self._current_reasoning_bubble.append(msg.content or "")
            return
        if meta.get("_reasoning_end"):
            # Close the reasoning bubble; the next stream goes to a
            # fresh assistant bubble.
            self._current_reasoning_bubble = None
            return

        # ---- retry-wait notifications ----------------------------------
        # Transient retries (which are common because the first request
        # after Textual boot often loses a TLS race) just add visual
        # noise. The user perceives the ~1s delay; they don't need a
        # bubble for it. We DO show retry messages from the FINAL
        # attempt (the "failed after N retries, giving up" line) — those
        # come through without `_retry_wait` so they fall through to the
        # normal text path below.
        if meta.get("_retry_wait"):
            return

        if meta.get("_stream_delta"):
            # Content is now flowing — drop the spinner.
            self._dismiss_working_indicator()
            # Finalize the activity cluster (collapse reasoning + tools).
            self._finalize_cluster()
            if self._current_assistant_bubble is not None:
                self._current_assistant_bubble.append(msg.content or "")
            return

        if meta.get("_stream_end"):
            # Check if the finalized assistant bubble looks like an error
            if self._current_assistant_bubble is not None:
                from durin.cli.tui.widgets.chat_view import looks_like_error

                if looks_like_error(self._current_assistant_bubble.body or ""):
                    self._current_assistant_bubble.mark_error()
            self._current_assistant_bubble = None
            self._finalize_cluster()
            # Belt-and-suspenders: if a turn ends without any content
            # (rare error path), make sure the spinner doesn't linger.
            self._dismiss_working_indicator()
            return

        if meta.get("_streamed"):
            # End-of-turn signal; UI already streamed via deltas.
            self._finalize_cluster()
            self._dismiss_working_indicator()
            return

        content = msg.content or ""
        if not content:
            return

        # ANY arriving content means the agent / router has produced
        # output — the spinner has served its purpose. (Slash command
        # responses arrive here, no streaming flags, so without an
        # explicit dismiss the indicator would spin forever — that's
        # what the user reported on `/memory list`.)
        self._dismiss_working_indicator()

        chat = self.query_one("#chat", ChatView)
        if self._current_assistant_bubble is not None:
            # Final non-stream content lands in the open assistant bubble.
            if self._current_assistant_bubble.body:
                self._current_assistant_bubble.body = (
                    f"{self._current_assistant_bubble.body}\n\n{content}"
                )
            else:
                self._current_assistant_bubble.body = content
            self._current_assistant_bubble = None
        else:
            # Out-of-turn payload (slash command response, system note).
            role = "system" if meta.get("render_as") == "text" else "assistant"
            chat.add_message(role, content)

    # ---- helpers ----------------------------------------------------------

    def _refresh_chrome(self) -> None:
        """Update Header + Footer reactive surfaces after a session switch."""
        try:
            footer = self.query_one(FooterBar)
            footer.refresh_now()
        except Exception:  # noqa: BLE001
            pass
        try:
            header = self.query_one(HeaderBar)
            header.session_label = f"{self._cli_channel}:{self._cli_chat_id}"
            header.session_meta = self._compute_session_meta()
        except Exception:  # noqa: BLE001
            pass

    def _workspace_path(self) -> str:
        if self._agent_loop is None:
            return ""
        try:
            return str(Path(self._agent_loop.workspace))
        except Exception:  # noqa: BLE001
            return ""

    def _model_label(self) -> tuple[str, str]:
        if self._agent_loop is None:
            return "?", "default"
        return (
            getattr(self._agent_loop, "model", "?") or "?",
            getattr(self._agent_loop, "model_preset", None) or "default",
        )


def _content_to_text(content: Any) -> str:
    """Coerce a session-message ``content`` into a plain text string.

    Session messages can carry plain strings or a list of multimodal
    content blocks (``{"type": "text", "text": "..."}`` etc). The
    history-replay path needs a single string per message.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                out.append(str(block.get("text", "")))
            else:
                out.append(str(block))
        return "\n".join(out).strip()
    return str(content).strip()


def run_durin_tui(
    *,
    agent_loop: Any | None,
    cli_channel: str = "cli",
    cli_chat_id: str = "direct",
    markdown: bool = True,
    auto_resume: bool = False,
) -> None:
    """Launch the Textual app. Blocks until the user quits."""
    app = DurinApp(
        agent_loop=agent_loop,
        cli_channel=cli_channel,
        cli_chat_id=cli_chat_id,
        markdown=markdown,
        auto_resume=auto_resume,
    )
    app.run()
