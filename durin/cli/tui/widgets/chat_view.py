"""ChatView — scrollable history of message bubbles.

Visual style modelled on [pi-agent](https://github.com/earendil-works/pi):

- User messages render inside a darker-bg box (`pi`'s ``userMsgBg``) with
  no role prefix — the box itself differentiates user from assistant.
- Assistant messages render as plain Markdown (code blocks, links,
  headings, lists) with no box and no prefix.
- System / tool / reasoning bubbles keep a small dim prefix so they're
  recognisable; they're rare and visual clutter on them is fine.

Streaming deltas flow through `MessageBubble.append()` which mutates the
reactive `body`. The watcher then calls `Static.update()` (the canonical
Textual pattern) with the right renderable for the role — `Markdown`
for assistant, plain `Text` for everyone else. This is the path that
exercises Rich's full pipeline and matches what a live terminal sees.
"""

from __future__ import annotations

import re
from typing import Literal

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["ChatView", "MessageBubble"]


Role = Literal["user", "assistant", "tool", "system", "reasoning", "banner", "logo"]


_ERROR_RE = re.compile(r"^\s*Error(\s*\(|:\s| calling )", re.MULTILINE)


def looks_like_error(text: str) -> bool:
    """Detect provider/API error patterns in assistant text."""
    if not text:
        return False
    return bool(_ERROR_RE.search(text))


class MessageBubble(Static):
    """One message in the chat history.

    ``body`` is reactive: streaming deltas accumulate into it and the
    watcher pushes the updated renderable through ``Static.update()``.
    """

    DEFAULT_CSS = """
    MessageBubble {
        width: 100%;
        padding: 0;
    }
    MessageBubble.user {
        background: rgb(52, 53, 65);
        color: #d4d4d4;
        padding: 1 2;
        margin: 1 2 0 2;
    }
    MessageBubble.assistant {
        background: transparent;
        padding: 0 2;
        margin: 0 2 1 2;
    }
    MessageBubble.system {
        color: $text-muted;
        text-style: italic;
        padding: 0 2;
        margin: 1 2;
    }
    MessageBubble.tool {
        color: $text-muted;
        padding: 0 2;
        margin: 1 2;
    }
    MessageBubble.reasoning {
        color: $text-muted;
        text-style: italic;
        padding: 0 2;
        margin: 1 2;
    }
    MessageBubble.banner {
        color: $text-muted;
        border: round $primary;
        padding: 1 2;
        margin: 0 2 1 2;
    }
    MessageBubble.logo {
        padding: 1 2 0 4;
        margin: 0 2;
    }
    MessageBubble.error {
        background: $boost;
        border: round $error 50%;
        color: $error;
        padding: 1 2;
        margin: 1 2;
    }
    MessageBubble > .mb-edit {
        width: auto;
        color: $text-muted;
        text-style: underline;
        padding: 0 0;
        dock: right;
    }
    MessageBubble > .mb-edit:hover {
        background: $accent 20%;
        color: $accent;
    }
    """

    body: reactive[str] = reactive("", init=False)

    # Roles that keep a tiny prefix to stay recognisable. user + assistant
    # are intentionally bare — the box / no-box differentiates them.
    _PREFIXED_ROLES: dict[Role, str] = {
        "system": "system",
        "tool": "tool",
        "reasoning": "thinking",
    }

    def __init__(self, role: Role, body: str = "") -> None:
        super().__init__("", classes=role)
        self._role: Role = role
        self._raw_body: str = body
        self.body = body
        # `init=False` on the reactive means the line above didn't fire
        # `watch_body`. Push the initial body through the renderer so the
        # widget is consistent before mount.
        self._render_body()

    def watch_body(self, _old: str, new: str) -> None:
        self._raw_body = new
        self._render_body()

    def _render_body(self) -> None:
        try:
            # No-op outside an active Textual app (e.g., unit tests that
            # construct a bubble without running the app).
            _ = self.app
        except Exception:  # noqa: BLE001
            return
        body = self.body or ""
        if not body:
            self.update("")
            return
        prefix = self._PREFIXED_ROLES.get(self._role)
        if self._role == "assistant":
            # Markdown handles code blocks, lists, links — and crucially
            # treats `[...]` patterns as literals (not Rich markup tags),
            # so streaming deltas can't accidentally truncate the body.
            #
            # Pre-pass: convert bare URLs / abs paths to explicit
            # `[url](url)` markdown links so the rendered output is
            # Cmd+click-able via OSC 8. Skips already-linked text and
            # the inside of fenced / inline code blocks.
            from durin.cli.tui.linkify import autolinkify_markdown

            self.update(Markdown(autolinkify_markdown(body)))
        elif self._role == "logo":
            # The durin logo — pre-built ASCII art carrying its own
            # Rich colour markup. Fully controlled string, so from_markup
            # is safe here (unlike streamed bodies).
            self.update(Text.from_markup(body))
        elif prefix:
            text = Text(f"{prefix}: ", style="dim")
            text.append(body)
            self.update(text)
        else:
            # user (or any role without a prefix): plain text, no markup
            # interpretation.
            self.update(Text(body))

    def editable_text(self) -> str:
        """The raw text to reload into the input when editing this message."""
        return self._raw_body or ""

    def on_mount(self) -> None:
        """Mount the [✎] edit affordance for user bubbles."""
        if self._role == "user":
            self.mount(Static("[✎]", classes="mb-edit", markup=False))

    def on_click(self, event) -> None:  # noqa: ANN001 — Textual Click event
        """On [✎] click, reload this bubble's text into the input."""
        try:
            target = event.widget
        except Exception:  # noqa: BLE001
            return
        if target is None or "mb-edit" not in target.classes:
            return
        try:
            from durin.cli.tui.widgets import InputArea

            inp = self.app.query_one(InputArea)
        except Exception:  # noqa: BLE001
            return
        inp.value = self.editable_text()
        inp.cursor_position = len(inp.value)
        inp.focus()

    def append(self, delta: str) -> None:
        """Streaming helper — append a delta to the body."""
        if not delta:
            return
        self.body = (self.body or "") + delta

    def mark_error(self) -> None:
        """Apply error CSS styling (red border + error color)."""
        self.add_class("error")


class _QuickActionChips(Static):
    """Row of suggestion chips shown on an empty thread."""

    DEFAULT_CSS = """
    _QuickActionChips {
        width: 100%;
        padding: 1 2;
        color: $text-muted;
    }
    _QuickActionChips .qa-chip {
        background: $boost;
        color: $text;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    _QuickActionChips .qa-chip:hover {
        background: $accent 40%;
        color: $accent;
    }
    """

    def compose(self) -> ComposeResult:
        for label in ChatView.quick_actions():
            yield Static(label, classes="qa-chip", markup=False)

    def on_click(self, event) -> None:  # noqa: ANN001
        target = event.widget
        if target is None or "qa-chip" not in target.classes:
            return
        try:
            from durin.cli.tui.widgets import InputArea

            inp = self.app.query_one(InputArea)
        except Exception:  # noqa: BLE001
            return
        inp.value = target.renderable if isinstance(target.renderable, str) else str(target.renderable)
        inp.cursor_position = len(inp.value)
        inp.focus()


class _ScrollToBottom(Static):
    """Button that jumps to the end of the chat history."""

    DEFAULT_CSS = """
    _ScrollToBottom {
        dock: bottom;
        width: auto;
        padding: 0 2;
        background: $boost;
        color: $text-muted;
        display: none;
    }
    _ScrollToBottom:hover {
        background: $accent 40%;
        color: $accent;
    }
    """

    def on_click(self, _event) -> None:  # noqa: ANN001
        try:
            chat = self.app.query_one(ChatView)
            chat.scroll_end(animate=False)
        except Exception:  # noqa: BLE001
            pass


class ChatView(VerticalScroll):
    """Scrollable history. Append :class:`MessageBubble` instances to it."""

    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        scrollbar-gutter: stable;
    }
    """

    @staticmethod
    def quick_actions() -> list[str]:
        """Labels for the suggestion chips shown on an empty thread."""
        return ["Plan", "Analyze", "Brainstorm", "Code", "Summarize"]

    def compose(self) -> ComposeResult:
        yield _QuickActionChips(id="qa-chips")
        yield _ScrollToBottom("↓ Jump to bottom", id="scroll-to-bottom")

    def add_message(self, role: Role, body: str = "") -> MessageBubble:
        # Hide quick-action chips once the thread has real messages.
        # Don't hide for decorative roles (logo, banner).
        if role in ("user", "assistant", "system", "tool", "reasoning"):
            chips = self.query_one("#qa-chips", _QuickActionChips)
            chips.display = False
        bubble = MessageBubble(role=role, body=body)
        self.mount(bubble)
        self.scroll_end(animate=False)
        return bubble

    def on_scroll_changed(self, _event) -> None:  # noqa: ANN001
        """Show/hide the jump button based on scroll position."""
        try:
            btn = self.query_one("#scroll-to-bottom", _ScrollToBottom)
            at_bottom = self.scroll_y >= self.max_scroll_y - 1
            btn.display = not at_bottom
        except Exception:  # noqa: BLE001
            pass

    def replace_last(self, role: Role, body: str) -> None:
        """Replace the last bubble's role + body (used by /stop or errors)."""
        bubbles = list(self.query(MessageBubble))
        if not bubbles:
            self.add_message(role, body)
            return
        last = bubbles[-1]
        # Swap classes so the styling matches the new role.
        for known in ("user", "assistant", "tool", "system", "reasoning"):
            last.remove_class(known)
        last.add_class(role)
        last._role = role
        last.body = body
