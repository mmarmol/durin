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
    ]

    def __init__(
        self,
        *,
        agent_loop: Any | None = None,
        cli_channel: str = "cli",
        cli_chat_id: str = "direct",
        markdown: bool = True,
    ) -> None:
        super().__init__()
        self._agent_loop = agent_loop
        self._cli_channel = cli_channel
        self._cli_chat_id = cli_chat_id
        self._markdown = markdown
        self._current_assistant_bubble: MessageBubble | None = None
        self._bus_task: asyncio.Task | None = None
        self._consume_task: asyncio.Task | None = None

    # ---- composition ------------------------------------------------------

    def compose(self) -> ComposeResult:
        workspace = self._workspace_path()
        model, preset = self._model_label()
        with Vertical(id="main-layout"):
            yield HeaderBar(
                workspace_path=workspace,
                model=model,
                preset=preset,
            )
            yield ChatView(id="chat")
            yield InputArea(
                placeholder="Type a message · Ctrl+Q to quit",
                workspace=Path(self._agent_loop.workspace) if self._agent_loop else None,
            )
            yield FooterBar(
                payload_getter=lambda: payload_from_loop(
                    self._agent_loop, self._cli_channel, self._cli_chat_id
                ),
            )

    # ---- lifecycle --------------------------------------------------------

    def on_mount(self) -> None:
        """Boot the bus + outbound consumer once the layout is up."""
        if self._agent_loop is None:
            return
        bus = getattr(self._agent_loop, "bus", None)
        if bus is None:
            return
        # AgentLoop.run() is the bus's inbound dispatcher; without it
        # no agent turn fires.
        self._bus_task = asyncio.create_task(self._agent_loop.run())
        self._consume_task = asyncio.create_task(self._consume_outbound())

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
        asyncio.create_task(self._publish_inbound(value, media))

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
        """Ctrl+T: flip between light and dark themes."""
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"

    def action_open_model_picker(self) -> None:
        """Ctrl+L: open the model picker modal (D5.5)."""
        self._open_model_picker()

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
        from durin.cli.tui.screens import ModelPickerScreen

        presets = self._collect_model_presets()
        active = self._model_label()[1]
        if not presets:
            chat = self.query_one("#chat", ChatView)
            chat.add_message("system", "No model presets configured.")
            return
        selected = await self.push_screen_wait(
            ModelPickerScreen(presets, active=active)
        )
        if selected and selected != active:
            await self._publish_inbound(f"/model {selected}", [])

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
        bus = self._agent_loop.bus
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                # Bus error — keep the loop alive; lost message is acceptable.
                continue

            meta = msg.metadata or {}

            # /resume routes here: the next inbound publish uses the new chat_id.
            switch_to = meta.get("_switch_chat_id")
            if switch_to and switch_to != self._cli_chat_id:
                self._cli_chat_id = switch_to
                self._refresh_chrome()

            if meta.get("_stream_delta"):
                if self._current_assistant_bubble is not None:
                    self._current_assistant_bubble.append(msg.content or "")
                continue

            if meta.get("_stream_end"):
                self._current_assistant_bubble = None
                continue

            if meta.get("_streamed"):
                # End-of-turn signal; UI already streamed via deltas.
                continue

            content = msg.content or ""
            if not content:
                continue

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


def run_durin_tui(
    *,
    agent_loop: Any | None,
    cli_channel: str = "cli",
    cli_chat_id: str = "direct",
    markdown: bool = True,
) -> None:
    """Launch the Textual app. Blocks until the user quits."""
    app = DurinApp(
        agent_loop=agent_loop,
        cli_channel=cli_channel,
        cli_chat_id=cli_chat_id,
        markdown=markdown,
    )
    app.run()
