"""ChatView — scrollable history of message bubbles."""

from __future__ import annotations

from typing import Literal

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["ChatView", "MessageBubble"]


Role = Literal["user", "assistant", "tool", "system", "reasoning"]


class MessageBubble(Static):
    """One message in the chat history.

    Role drives both the prefix label and the CSS class for styling.
    Text is reactive so a bubble can grow as streaming deltas arrive
    (D5.3 will exercise that path).
    """

    DEFAULT_CSS = """
    MessageBubble {
        width: 100%;
        margin: 1 0 0 0;
        padding: 0 1;
    }
    MessageBubble.user {
        color: $primary;
    }
    MessageBubble.user .role {
        text-style: bold;
        color: $primary;
    }
    MessageBubble.assistant {
        color: $text;
    }
    MessageBubble.assistant .role {
        text-style: bold;
        color: $accent;
    }
    MessageBubble.tool {
        color: $text-muted;
    }
    MessageBubble.tool .role {
        text-style: italic;
        color: $warning;
    }
    MessageBubble.system {
        color: $text-muted;
    }
    MessageBubble.system .role {
        text-style: italic;
    }
    MessageBubble.reasoning {
        color: $text-muted;
        text-style: italic;
    }
    """

    body: reactive[str] = reactive("", init=False)

    _ROLE_LABEL: dict[Role, str] = {
        "user": "you",
        "assistant": "durin",
        "tool": "tool",
        "system": "system",
        "reasoning": "thinking",
    }

    def __init__(self, role: Role, body: str = "") -> None:
        super().__init__(classes=role)
        self._role: Role = role
        self.body = body

    def render(self):  # type: ignore[override]
        # Escape any `[...]` literals in the body so Rich doesn't try to
        # interpret them as markup tags and silently fail to render.
        # Model output regularly contains brackets (code blocks, lists,
        # `[x]` checkboxes, urls with `[...]`) and an unclosed tag in a
        # streaming delta would leave the bubble blank.
        from rich.markup import escape

        label = self._ROLE_LABEL.get(self._role, self._role)
        body = self.body or ""
        prefix = f"[bold]{label}[/bold]"
        if body:
            return f"{prefix}\n{escape(body)}"
        return prefix

    def watch_body(self, _old: str, _new: str) -> None:
        self.refresh()

    def append(self, delta: str) -> None:
        """Streaming helper — append a delta to the body."""
        if not delta:
            return
        self.body = (self.body or "") + delta


class ChatView(VerticalScroll):
    """Scrollable history. Append :class:`MessageBubble` instances to it."""

    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        padding: 0 1;
        scrollbar-gutter: stable;
    }
    """

    def compose(self) -> ComposeResult:
        # ChatView's content is appended dynamically via add_message().
        yield from ()

    def add_message(self, role: Role, body: str = "") -> MessageBubble:
        bubble = MessageBubble(role=role, body=body)
        self.mount(bubble)
        self.scroll_end(animate=False)
        return bubble

    def replace_last(self, role: Role, body: str) -> None:
        """Replace the last bubble's role + body (used by /stop or errors)."""
        bubbles = list(self.query(MessageBubble))
        if not bubbles:
            self.add_message(role, body)
            return
        last = bubbles[-1]
        last.remove_class(*MessageBubble._ROLE_LABEL.keys())
        last.add_class(role)
        last._role = role
        last.body = body
